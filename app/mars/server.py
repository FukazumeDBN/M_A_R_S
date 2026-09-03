from __future__ import annotations

import re
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .automation import RESTART_WARNING_SECONDS, restart_warning_command
from .operations import ServerOperationLock
from .settings import AppSettings
from .terminal import TerminalBackend, TerminalError, create_terminal_backend


class ServerCommandError(RuntimeError):
    pass


@dataclass(frozen=True)
class ServerStatus:
    running: bool
    port_open: bool
    terminal_running: bool
    pid: int | None = None
    players: str = "-"
    mods: str = "0"
    tps: str = "-"
    ping: str = "-"


class ServerRuntime:
    """Terminal-centric Minecraft runtime used by the GUI and extensions."""

    SAVE_COMPLETE_MARKERS = ("Saved the game", "Saved the world")
    SCHEDULED_RESTART_SKIPPED = "scheduled restart skipped: managed terminal stopped"

    def __init__(
        self,
        server_dir: Path,
        terminal: TerminalBackend,
        start_command: str = "./run.sh nogui",
        stop_command: str = "stop",
        capture_lines: int = 300,
        host: str = "127.0.0.1",
        port: int = 25565,
    ):
        self.server_dir = Path(server_dir).expanduser().resolve()
        self.terminal = terminal
        self.start_command = self._validate_command(start_command, "Start")
        self.stop_command = self._validate_command(stop_command, "Stop")
        self.capture_lines = capture_lines
        self.host = host
        self.port = port
        self.configured = True
        self._player_lock = threading.Lock()
        self._player_log_identity: tuple[int, int] | None = None
        self._player_log_offset = 0
        self._player_log_remainder = b""
        self._online_players: set[str] = set()

    @staticmethod
    def _validate_command(command: str, label: str) -> str:
        if not isinstance(command, str) or not command.strip():
            raise ServerCommandError(f"{label} commandは空にできません")
        if "\n" in command or "\r" in command or "\0" in command:
            raise ServerCommandError(f"{label} commandは1行で入力してください")
        return command.strip()

    @classmethod
    def from_settings(cls, settings: AppSettings) -> "ServerRuntime":
        terminal = create_terminal_backend(settings.terminal.backend, settings.terminal.session_name)
        return cls(
            Path(settings.server_dir),
            terminal,
            settings.terminal.start_command,
            settings.terminal.stop_command,
            settings.terminal.capture_lines,
        )

    def ensure_terminal(self) -> str:
        try:
            created = self.terminal.ensure(self.server_dir)
        except TerminalError as exc:
            raise ServerCommandError(str(exc)) from exc
        return "virtual terminal created" if created else "virtual terminal already running"

    def terminal_running(self) -> bool:
        try:
            return self.terminal.exists()
        except TerminalError:
            return False

    def process_active(self) -> bool:
        try:
            return bool(self.terminal.child_processes())
        except TerminalError:
            return False

    def foreground_command(self) -> str | None:
        try:
            return self.terminal.foreground_command()
        except TerminalError:
            return None

    def minecraft_process_active(self) -> bool:
        try:
            commands = self.terminal.child_processes()
        except TerminalError:
            return False
        return any("java" in Path(command.split()[0]).name.lower() or "run.sh" in command for command in commands if command.split())

    def active(self) -> bool:
        """Return whether Minecraft is online or still starting/stopping."""
        return self._port_open() or self.minecraft_process_active()

    def _port_open(self) -> bool:
        try:
            with socket.create_connection((self.host, self.port), timeout=0.25):
                return True
        except OSError:
            return False

    def save_log_cursor(self) -> int:
        """Return the byte offset used to detect completion of save-all flush."""
        try:
            return (self.server_dir / "logs" / "latest.log").stat().st_size
        except OSError:
            return 0

    def wait_for_save_complete(self, cursor: int, timeout: float = 45) -> None:
        """Wait until Minecraft reports that save-all flush has completed."""
        latest_log = self.server_dir / "logs" / "latest.log"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                size = latest_log.stat().st_size
                offset = cursor if size >= cursor else 0  # latest.log may rotate.
                with latest_log.open("rb") as handle:
                    handle.seek(offset)
                    output = handle.read().decode("utf-8", errors="replace")
                if any(marker in output for marker in self.SAVE_COMPLETE_MARKERS):
                    return
            except OSError:
                pass
            time.sleep(0.1)
        raise ServerCommandError("save-all flushの完了を確認できないためバックアップを中断しました")

    def status(self, console: str | None = None) -> ServerStatus:
        terminal_running = self.terminal_running()
        port_open = self._port_open()
        if port_open:
            players = self._players_from_log(console) if console is not None else self._players_from_latest_log()
        else:
            self._clear_player_tracking()
            players = "0"
        mods_dir = self.server_dir / "mods"
        mods = str(sum(1 for path in mods_dir.glob("*.jar") if path.is_file())) if mods_dir.is_dir() else "0"
        return ServerStatus(running=port_open, port_open=port_open, terminal_running=terminal_running, players=players, mods=mods)

    def start(self) -> str:
        if self._port_open():
            return "server already online"
        self.ensure_terminal()
        if self.process_active():
            return "server process is already running or starting"
        self.send_command(self.start_command)
        return f"sent: {self.start_command}"

    def stop(self) -> str:
        if not self.terminal_running():
            return "virtual terminal is not running"
        if not self._port_open() and not self.minecraft_process_active():
            return "server already offline"
        self.send_command(self.stop_command)
        return f"sent: {self.stop_command}"

    def restart(self, timeout: float = 45) -> str:
        with ServerOperationLock(self.server_dir):
            return self._restart_locked(timeout)

    def restart_with_backup(self, backup_service, keep_count: int = 7, keep_days: int = 30, timeout: float = 45):
        """Stop, back up the stopped server, then start it again as one operation."""
        with ServerOperationLock(self.server_dir):
            return self._restart_locked(timeout, backup_service, keep_count, keep_days)

    def restart_with_warnings(
        self,
        backup_service=None,
        keep_count: int = 7,
        keep_days: int = 30,
        timeout: float = 45,
        sleep_fn=time.sleep,
        clock_fn=None,
    ):
        """Warn in-game at fixed offsets, then perform the safe restart flow.

        This is used by the scheduled worker, whose systemd timer fires 30
        minutes before the configured restart time.  Warnings are sent only
        while Minecraft is online, so an offline server never receives a
        command intended for its tmux shell.
        """
        clock_fn = clock_fn or self._suspend_aware_clock
        target = clock_fn() + RESTART_WARNING_SECONDS[0]
        for seconds in RESTART_WARNING_SECONDS:
            delay = target - seconds - clock_fn()
            if delay > 0:
                sleep_fn(delay)
            if not self.terminal_running():
                return self.SCHEDULED_RESTART_SKIPPED
            if self._port_open():
                try:
                    self.send_command(restart_warning_command(seconds))
                except ServerCommandError:
                    if not self.terminal_running():
                        return self.SCHEDULED_RESTART_SKIPPED
                    raise
        if not self.terminal_running():
            return self.SCHEDULED_RESTART_SKIPPED
        with ServerOperationLock(self.server_dir):
            if not self.terminal_running():
                return self.SCHEDULED_RESTART_SKIPPED
            return self._restart_locked(timeout, backup_service, keep_count, keep_days)

    @staticmethod
    def _suspend_aware_clock() -> float:
        clock_id = getattr(time, "CLOCK_BOOTTIME", None)
        return time.clock_gettime(clock_id) if clock_id is not None else time.monotonic()

    def _restart_locked(self, timeout: float = 45, backup_service=None, keep_count: int = 7, keep_days: int = 30):
        if self._port_open() or self.process_active():
            self.stop()
            self._wait_until_stopped(timeout)
        if backup_service is None:
            return self.start()
        try:
            result = backup_service.create_locked(keep_count, keep_days)
        except Exception:
            # A failed backup must not leave a normally managed server down.
            self.start()
            raise
        start_result = self.start()
        return result, start_result

    def _wait_until_stopped(self, timeout: float, stable_for: float = 0.5) -> None:
        deadline = time.monotonic() + timeout
        idle_since: float | None = None
        while time.monotonic() < deadline:
            if not self._port_open() and not self.process_active():
                idle_since = idle_since or time.monotonic()
                if time.monotonic() - idle_since >= stable_for:
                    return
            else:
                idle_since = None
            time.sleep(0.1)
        raise ServerCommandError("停止完了とターミナル待機状態を確認できないため再起動を中断しました")

    def terminal_attach_argv(self) -> list[str]:
        return self.terminal.attach_argv()

    def shutdown(self, timeout: float = 45) -> str:
        """Gracefully stop Minecraft, then remove the managed terminal."""
        if not self.terminal_running():
            return "virtual terminal already stopped"
        with ServerOperationLock(self.server_dir):
            if self._port_open() or self.minecraft_process_active():
                self.send_command(self.stop_command)
                try:
                    self._wait_until_stopped(timeout)
                except ServerCommandError as exc:
                    raise ServerCommandError("Minecraftの正常停止を確認できないため、M.A.R.S.の終了を中断しました") from exc
            try:
                closed = self.terminal.close()
            except TerminalError as exc:
                raise ServerCommandError(str(exc)) from exc
            return "server stopped; virtual terminal closed" if closed else "virtual terminal already stopped"

    def send_command(self, command: str) -> bool:
        try:
            self.terminal.send_line(command)
            return True
        except TerminalError as exc:
            raise ServerCommandError(str(exc)) from exc

    def console_text(self) -> str:
        try:
            return self.terminal.capture(self.capture_lines)
        except TerminalError as exc:
            return f"Terminal error: {exc}\n"

    def _clear_player_tracking(self) -> None:
        with self._player_lock:
            self._player_log_identity = None
            self._player_log_offset = 0
            self._player_log_remainder = b""
            self._online_players.clear()

    def _players_from_latest_log(self) -> str:
        """Incrementally track joins/leaves without capturing the tmux pane."""
        latest_log = self.server_dir / "logs" / "latest.log"
        with self._player_lock:
            try:
                stat = latest_log.stat()
                identity = (stat.st_dev, stat.st_ino)
                if identity != self._player_log_identity or stat.st_size < self._player_log_offset:
                    self._player_log_identity = identity
                    self._player_log_offset = 0
                    self._player_log_remainder = b""
                    self._online_players.clear()
                if stat.st_size == self._player_log_offset:
                    return str(len(self._online_players))
                with latest_log.open("rb") as handle:
                    handle.seek(self._player_log_offset)
                    chunk = self._player_log_remainder + handle.read()
                    self._player_log_offset = handle.tell()
            except OSError:
                return "-"
            lines = chunk.split(b"\n")
            self._player_log_remainder = lines.pop() if lines else b""
            text = "\n".join(line.decode("utf-8", errors="replace") for line in lines)
            self._update_players(text, self._online_players)
            return str(len(self._online_players))

    @staticmethod
    def _update_players(log: str, online: set[str]) -> None:
        for line in log.splitlines():
            joined = re.search(r": ([A-Za-z0-9_]{1,16}) joined the game", line)
            left = re.search(r": ([A-Za-z0-9_]{1,16}) left the game", line)
            if joined:
                online.add(joined.group(1))
            if left:
                online.discard(left.group(1))

    @staticmethod
    def _players_from_log(log: str) -> str:
        online: set[str] = set()
        ServerRuntime._update_players(log, online)
        return str(len(online))


# Compatibility name for callers outside the package while the API migrates.
TmuxServerAdapter = ServerRuntime


class UnconfiguredServerRuntime:
    """Safe runtime used until a server directory has been registered."""

    configured = False
    server_dir = Path("")
    port = 25565

    @staticmethod
    def _error() -> ServerCommandError:
        return ServerCommandError("Server画面でMinecraftサーバーディレクトリを登録してください")

    def ensure_terminal(self) -> str:
        raise self._error()

    def terminal_running(self) -> bool:
        return False

    def process_active(self) -> bool:
        return False

    def minecraft_process_active(self) -> bool:
        return False

    def active(self) -> bool:
        return False

    def status(self, console: str | None = None) -> ServerStatus:
        return ServerStatus(running=False, port_open=False, terminal_running=False, players="0", mods="0")

    def start(self) -> str:
        raise self._error()

    def stop(self) -> str:
        raise self._error()

    def restart(self, timeout: float = 45) -> str:
        raise self._error()

    def send_command(self, command: str) -> bool:
        raise self._error()

    def console_text(self) -> str:
        return "Server directory is not registered.\n"

    def terminal_attach_argv(self) -> list[str]:
        return []

    def shutdown(self, timeout: float = 45) -> str:
        return "no managed server"

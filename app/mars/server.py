from __future__ import annotations

import re
import socket
import time
from dataclasses import dataclass
from pathlib import Path

from .settings import AppSettings
from .terminal import TerminalBackend, TerminalError, create_terminal_backend
from .operations import ServerOperationLock


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

    @staticmethod
    def _validate_command(command: str, label: str) -> str:
        if not command.strip():
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
            return self.terminal.exists() and not self.terminal.is_idle()
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

    def _port_open(self) -> bool:
        try:
            with socket.create_connection((self.host, self.port), timeout=0.25):
                return True
        except OSError:
            return False

    def status(self, console: str | None = None) -> ServerStatus:
        terminal_running = self.terminal_running()
        port_open = self._port_open()
        if console is None:
            console = self.console_text() if terminal_running else ""
        players = self._players_from_log(console) if port_open else "0"
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
            if self._port_open() or self.process_active():
                self.stop()
                self._wait_until_stopped(timeout)
            return self.start()

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

    @staticmethod
    def _players_from_log(log: str) -> str:
        online: set[str] = set()
        for line in log.splitlines():
            joined = re.search(r": ([A-Za-z0-9_]{1,16}) joined the game", line)
            left = re.search(r": ([A-Za-z0-9_]{1,16}) left the game", line)
            if joined:
                online.add(joined.group(1))
            if left:
                online.discard(left.group(1))
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

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Protocol


class TerminalError(RuntimeError):
    pass


class TerminalBackend(Protocol):
    def exists(self) -> bool: ...

    def ensure(self, working_directory: Path) -> bool: ...

    def send_line(self, line: str) -> None: ...

    def capture(self, lines: int = 300) -> str: ...

    def foreground_command(self) -> str | None: ...

    def child_processes(self) -> tuple[str, ...]: ...

    def is_idle(self) -> bool: ...

    def attach_argv(self) -> list[str]: ...

    def close(self) -> bool: ...


class TmuxTerminalBackend:
    SESSION_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")

    def __init__(self, session_name: str, runner=None):
        if not self.SESSION_PATTERN.fullmatch(session_name):
            raise TerminalError("tmuxセッション名には英数字、点、ハイフン、アンダースコアだけを使用できます")
        self.session_name = session_name
        self.runner = runner or subprocess.run

    def _run(self, *args: str, timeout: float = 10) -> subprocess.CompletedProcess[str]:
        try:
            return self.runner(["tmux", *args], text=True, capture_output=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TerminalError(f"tmuxを実行できません: {exc}") from exc

    @staticmethod
    def _detail(result: subprocess.CompletedProcess[str]) -> str:
        return (result.stderr or result.stdout or "").strip()

    def exists(self) -> bool:
        return self._run("has-session", "-t", self.session_name).returncode == 0

    def ensure(self, working_directory: Path) -> bool:
        directory = Path(working_directory).expanduser().resolve()
        if not directory.is_dir():
            raise TerminalError(f"作業ディレクトリがありません: {directory}")
        created = not self.exists()
        if created:
            result = self._run("new-session", "-d", "-s", self.session_name, "-c", str(directory))
            if result.returncode:
                raise TerminalError(self._detail(result) or "tmuxセッションを作成できません")
        mouse = self._run("set-option", "-t", self.session_name, "mouse", "on")
        if mouse.returncode:
            raise TerminalError(self._detail(mouse) or "tmuxのマウス操作を有効にできません")
        return created

    def send_line(self, line: str) -> None:
        if "\n" in line or "\r" in line:
            raise TerminalError("複数行のコマンドは送信できません")
        if not self.exists():
            raise TerminalError("仮想ターミナルが起動していません")
        literal = self._run("send-keys", "-t", self.session_name, "-l", line)
        if literal.returncode:
            raise TerminalError(self._detail(literal) or "コマンドを送信できません")
        enter = self._run("send-keys", "-t", self.session_name, "Enter")
        if enter.returncode:
            raise TerminalError(self._detail(enter) or "Enterキーを送信できません")

    def capture(self, lines: int = 300) -> str:
        if not self.exists():
            return "Virtual terminal is not running.\n"
        result = self._run("capture-pane", "-p", "-J", "-S", f"-{max(1, lines)}", "-t", self.session_name)
        if result.returncode:
            raise TerminalError(self._detail(result) or "ターミナル画面を取得できません")
        return result.stdout.rstrip("\n") + "\n"

    def foreground_command(self) -> str | None:
        if not self.exists():
            return None
        result = self._run("display-message", "-p", "-t", self.session_name, "#{pane_current_command}")
        if result.returncode:
            raise TerminalError(self._detail(result) or "ターミナルの実行状態を取得できません")
        return result.stdout.strip() or None

    def _pane_pid(self) -> int | None:
        if not self.exists():
            return None
        result = self._run("display-message", "-p", "-t", self.session_name, "#{pane_pid}")
        if result.returncode:
            raise TerminalError(self._detail(result) or "ターミナルのプロセスIDを取得できません")
        try:
            return int(result.stdout.strip())
        except ValueError as exc:
            raise TerminalError("ターミナルのプロセスIDが不正です") from exc

    @staticmethod
    def _children_of(pid: int) -> tuple[int, ...]:
        children_file = Path(f"/proc/{pid}/task/{pid}/children")
        try:
            text = children_file.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            return ()
        try:
            return tuple(int(value) for value in text.split()) if text else ()
        except ValueError:
            return ()

    @staticmethod
    def _command_line(pid: int) -> str:
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").strip()
            if raw:
                return raw.decode("utf-8", errors="replace")
            return Path(f"/proc/{pid}/comm").read_text(encoding="utf-8", errors="replace").strip()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            return ""

    def child_processes(self) -> tuple[str, ...]:
        pane_pid = self._pane_pid()
        if pane_pid is None:
            return ()
        pending = list(self._children_of(pane_pid))
        seen: set[int] = set()
        commands: list[str] = []
        while pending:
            pid = pending.pop()
            if pid in seen:
                continue
            seen.add(pid)
            command = self._command_line(pid)
            if command:
                commands.append(command)
            pending.extend(self._children_of(pid))
        return tuple(commands)

    def is_idle(self) -> bool:
        return self.exists() and not self.child_processes()

    def attach_argv(self) -> list[str]:
        return ["tmux", "attach-session", "-t", self.session_name]

    def close(self) -> bool:
        if not self.exists():
            return False
        result = self._run("kill-session", "-t", self.session_name)
        if result.returncode:
            raise TerminalError(self._detail(result) or "tmuxセッションを終了できません")
        return True


def create_terminal_backend(name: str, session_name: str) -> TerminalBackend:
    if name == "tmux":
        return TmuxTerminalBackend(session_name)
    raise TerminalError(f"未対応のターミナルバックエンドです: {name}")

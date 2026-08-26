from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from pathlib import Path


class ScheduleValidationError(ValueError):
    pass


def validate_time(value: str) -> str:
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ScheduleValidationError("時刻はHH:MM形式で入力してください")
    try:
        hour, minute = (int(part) for part in parts)
    except ValueError as exc:
        raise ScheduleValidationError("時刻はHH:MM形式で入力してください") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ScheduleValidationError("時刻の範囲が不正です")
    return f"{hour:02d}:{minute:02d}"


def calendar_expression(mode: str, day: str, time: str) -> str:
    normalized_time = validate_time(time)
    if mode == "daily":
        return f"*-*-* {normalized_time}:00"
    if mode == "weekly" and day in {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}:
        return f"{day} *-*-* {normalized_time}:00"
    raise ScheduleValidationError("日次または曜日指定を選択してください")


def interval_expression(value: int, unit: str) -> str:
    if not 1 <= value <= 999:
        raise ScheduleValidationError("実行間隔は1〜999で設定してください")
    suffix = {"hours": "h", "days": "d"}.get(unit)
    if suffix is None:
        raise ScheduleValidationError("実行間隔の単位は時間または日を選択してください")
    return f"{value}{suffix}"


class SystemdScheduler:
    """Writes only M.A.R.S.-owned user units and activates them on request."""

    def __init__(self, app_root: Path, unit_dir: Path | None = None):
        self.app_root = Path(app_root).resolve()
        self.unit_dir = unit_dir or (Path.home() / ".config" / "systemd" / "user")
        self.worker = self.app_root / "mars_worker.py"

    def _run_systemctl(self, *args: str) -> None:
        try:
            result = subprocess.run(["systemctl", "--user", *args], text=True, capture_output=True, timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"systemctlを実行できません: {exc}") from exc
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(detail or f"systemctl --user {' '.join(args)} に失敗しました")

    def _write(self, name: str, content: str) -> None:
        self.unit_dir.mkdir(parents=True, exist_ok=True)
        target = self.unit_dir / name
        fd, temporary = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=self.unit_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _apply(self, kind: str, enabled: bool, timer_rules: str, args: list[str]) -> None:
        service = f"mars-{kind}.service"
        timer = f"mars-{kind}.timer"
        if any("\n" in value or "\r" in value or "\0" in value for value in args):
            raise ScheduleValidationError("自動化設定へ改行またはNUL文字を含めることはできません")
        # systemd expands percent specifiers even inside quoted arguments.
        command = shlex.join(["/usr/bin/python3", str(self.worker), kind, *args]).replace("%", "%%")
        self._write(service, "[Unit]\nDescription=M.A.R.S. " + kind + " worker\n\n[Service]\nType=oneshot\nExecStart=" + command + "\n")
        self._write(timer, "[Unit]\nDescription=M.A.R.S. scheduled " + kind + "\n\n[Timer]\n" + timer_rules + "\nPersistent=true\nUnit=" + service + "\n\n[Install]\nWantedBy=timers.target\n")
        self._run_systemctl("daemon-reload")
        if enabled:
            self._run_systemctl("enable", "--now", timer)
        else:
            self._run_systemctl("disable", "--now", timer)

    @staticmethod
    def _terminal_args(terminal) -> list[str]:
        if terminal is None:
            return []
        return [
            "--terminal-backend", terminal.backend,
            "--session", terminal.session_name,
            "--start-command", terminal.start_command,
            "--stop-command", terminal.stop_command,
        ]

    def apply_restart(self, settings, server_dir: Path, terminal=None, backup=None) -> str:
        expression = interval_expression(settings.interval_value, settings.interval_unit)
        args = ["--server-dir", str(Path(server_dir).resolve()), *self._terminal_args(terminal)]
        if backup is not None and backup.enabled and backup.linked_to_restart:
            args.extend([
                "--backup-destination", str(Path(backup.destination).expanduser().resolve()),
                "--keep-count", str(backup.keep_count),
                "--keep-days", str(backup.keep_days),
            ])
        self._apply("restart", settings.enabled, f"OnActiveSec={expression}\nOnUnitActiveSec={expression}", args)
        return expression if settings.enabled else "disabled"

    def apply_backup(self, settings, server_dir: Path, terminal=None) -> str:
        expression = interval_expression(settings.interval_value, settings.interval_unit)
        enabled = settings.enabled and not settings.linked_to_restart
        args = ["--server-dir", str(Path(server_dir).resolve()), *self._terminal_args(terminal), "--destination", str(Path(settings.destination).expanduser().resolve()), "--keep-count", str(settings.keep_count), "--keep-days", str(settings.keep_days)]
        self._apply("backup", enabled, f"OnActiveSec={expression}\nOnUnitActiveSec={expression}", args)
        if not settings.enabled:
            return "disabled"
        if settings.linked_to_restart:
            return "linked-to-restart"
        return expression

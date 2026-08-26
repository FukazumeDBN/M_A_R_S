from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path


DEFAULT_BACKUP_DIR = Path.home() / "empire" / "data" / "minecraft-backups"


@dataclass
class RestartSettings:
    enabled: bool = False
    interval_value: int = 24
    interval_unit: str = "hours"
    time: str = "04:00"
    mode: str = "daily"
    day: str = "Mon"
    warnings: list[int] = field(default_factory=lambda: [10, 5, 1])


@dataclass
class BackupSettings:
    enabled: bool = False
    linked_to_restart: bool = True
    interval_value: int = 24
    interval_unit: str = "hours"
    time: str = "05:00"
    mode: str = "daily"
    day: str = "Mon"
    destination: str = str(DEFAULT_BACKUP_DIR)
    keep_count: int = 7
    keep_days: int = 30


@dataclass
class TerminalSettings:
    backend: str = "tmux"
    session_name: str = "minecraft-server"
    start_command: str = "./run.sh nogui"
    stop_command: str = "stop"
    capture_lines: int = 300


@dataclass
class AppSettings:
    server_dir: str = ""
    terminal: TerminalSettings = field(default_factory=TerminalSettings)
    restart: RestartSettings = field(default_factory=RestartSettings)
    backup: BackupSettings = field(default_factory=BackupSettings)

    @classmethod
    def path(cls) -> Path:
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        return config_home / "mars" / "settings.json"

    @classmethod
    def load(cls, path: Path | None = None) -> "AppSettings":
        target = path or cls.path()
        if not target.exists():
            return cls()
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
            terminal_raw = raw.get("terminal", {})
            # The first backend is intentionally opinionated: users choose a
            # session name, while the implementation owns tmux and commands.
            terminal = TerminalSettings(
                session_name=terminal_raw.get("session_name", TerminalSettings.session_name),
                capture_lines=terminal_raw.get("capture_lines", TerminalSettings.capture_lines),
            )
            restart = RestartSettings(**raw.get("restart", {}))
            backup = BackupSettings(**raw.get("backup", {}))
            return cls(server_dir=raw.get("server_dir", ""), terminal=terminal, restart=restart, backup=backup)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            # A corrupt settings file must not prevent the manager from opening.
            return cls()

    def save(self, path: Path | None = None) -> Path:
        target = path or self.path()
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(self), ensure_ascii=False, indent=2) + "\n"
        fd, temporary = tempfile.mkstemp(prefix="settings.", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        return target

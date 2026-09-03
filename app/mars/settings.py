from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


DEFAULT_BACKUP_DIR = Path.home() / "empire" / "data" / "minecraft-backups"
VALID_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
SESSION_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


def _normalized_days(value, fallback: str) -> list[str]:
    if not isinstance(value, list):
        return [fallback]
    normalized = [day for day in value if isinstance(day, str) and day in VALID_WEEKDAYS]
    return list(dict.fromkeys(normalized)) or [fallback]


def _load_section(section_type, value):
    """Load known dataclass fields while ignoring obsolete/unknown JSON keys."""
    if not isinstance(value, dict):
        return section_type()
    allowed = {item.name for item in fields(section_type)}
    return section_type(**{key: item for key, item in value.items() if key in allowed})


@dataclass
class RestartSettings:
    enabled: bool = False
    time: str = "04:00"
    mode: str = "weekly"
    day: str = "Mon"
    days: list[str] = field(default_factory=lambda: ["Mon"])

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            self.enabled = False
        if not isinstance(self.time, str):
            self.time = "04:00"
        if not isinstance(self.mode, str) or self.mode not in {"daily", "weekly"}:
            self.mode = "weekly"
        if self.day not in VALID_WEEKDAYS:
            self.day = "Mon"
        # Migrate the single-day schedule used by the first calendar prototype.
        if self.days == ["Mon"] and self.day != "Mon":
            self.days = [self.day]
        self.days = _normalized_days(self.days, self.day)


@dataclass
class BackupSettings:
    enabled: bool = False
    linked_to_restart: bool = True
    time: str = "05:00"
    mode: str = "weekly"
    day: str = "Mon"
    days: list[str] = field(default_factory=lambda: ["Mon"])
    destination: str = str(DEFAULT_BACKUP_DIR)
    keep_count: int = 7
    keep_days: int = 30

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            self.enabled = False
        if not isinstance(self.linked_to_restart, bool):
            self.linked_to_restart = True
        if not isinstance(self.time, str):
            self.time = "05:00"
        if not isinstance(self.mode, str) or self.mode not in {"daily", "weekly"}:
            self.mode = "weekly"
        if self.day not in VALID_WEEKDAYS:
            self.day = "Mon"
        # Migrate the single-day schedule used by the first calendar prototype.
        if self.days == ["Mon"] and self.day != "Mon":
            self.days = [self.day]
        self.days = _normalized_days(self.days, self.day)
        if not isinstance(self.destination, str) or "\0" in self.destination:
            self.destination = str(DEFAULT_BACKUP_DIR)
        if type(self.keep_count) is not int or self.keep_count < 1:
            self.keep_count = 7
        if type(self.keep_days) is not int or self.keep_days < 0:
            self.keep_days = 30


@dataclass
class TerminalSettings:
    backend: str = "tmux"
    session_name: str = "minecraft-server"
    start_command: str = "./run.sh nogui"
    stop_command: str = "stop"
    capture_lines: int = 300

    def __post_init__(self) -> None:
        if not isinstance(self.session_name, str) or not SESSION_PATTERN.fullmatch(self.session_name):
            self.session_name = "minecraft-server"
        if type(self.capture_lines) is not int or not 1 <= self.capture_lines <= 10_000:
            self.capture_lines = 300


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
            if not isinstance(raw, dict):
                return cls()
            terminal_raw = raw.get("terminal", {})
            # The first backend is intentionally opinionated: users choose a
            # session name, while the implementation owns tmux and commands.
            if isinstance(terminal_raw, dict):
                session_name = terminal_raw.get("session_name", TerminalSettings.session_name)
                capture_lines = terminal_raw.get("capture_lines", TerminalSettings.capture_lines)
            else:
                session_name = TerminalSettings.session_name
                capture_lines = TerminalSettings.capture_lines
            terminal = TerminalSettings(
                session_name=session_name,
                capture_lines=capture_lines,
            )
            restart = _load_section(RestartSettings, raw.get("restart"))
            backup = _load_section(BackupSettings, raw.get("backup"))
            server_dir = raw.get("server_dir", "")
            if not isinstance(server_dir, str) or "\0" in server_dir:
                server_dir = ""
            return cls(server_dir=server_dir, terminal=terminal, restart=restart, backup=backup)
        except (OSError, TypeError, ValueError):
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

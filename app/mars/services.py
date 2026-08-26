from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .extensions import ExtensionRegistry, builtin_extensions
from .scheduler import SystemdScheduler
from .server import ServerRuntime, UnconfiguredServerRuntime
from .settings import AppSettings


@dataclass
class ApplicationServices:
    settings: AppSettings
    runtime: ServerRuntime | UnconfiguredServerRuntime
    scheduler: SystemdScheduler
    extensions: ExtensionRegistry

    @classmethod
    def build(cls, settings: AppSettings, app_root: Path) -> "ApplicationServices":
        runtime = ServerRuntime.from_settings(settings) if settings.server_dir.strip() else UnconfiguredServerRuntime()
        return cls(settings, runtime, SystemdScheduler(app_root), builtin_extensions())

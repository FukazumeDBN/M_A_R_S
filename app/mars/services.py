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

    def apply_automation(self, candidate: AppSettings, settings_path: Path | None = None) -> str:
        """Apply both timers and settings, restoring previous units on failure."""
        if not self.runtime.configured:
            raise RuntimeError("先にMinecraftサーバーディレクトリを登録してください")

        previous = self.settings

        def apply_units(settings: AppSettings) -> tuple[str, str]:
            server_dir = Path(settings.server_dir)
            restart_result = self.scheduler.apply_restart(
                settings.restart,
                server_dir,
                settings.terminal,
                settings.backup,
            )
            backup_result = self.scheduler.apply_backup(
                settings.backup,
                server_dir,
                settings.terminal,
            )
            return restart_result, backup_result

        try:
            restart_result, backup_result = apply_units(candidate)
            candidate.save(settings_path)
        except Exception as primary:
            try:
                apply_units(previous)
            except Exception as rollback:
                raise RuntimeError(
                    f"自動化設定の適用に失敗し、以前のtimer設定の復元にも失敗しました: "
                    f"{primary}; 復元: {rollback}"
                ) from primary
            raise

        self.settings = candidate
        return f"restart={restart_result}; backup={backup_result}"

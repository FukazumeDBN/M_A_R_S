from __future__ import annotations

import json
import os
import subprocess
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

from app.mars.backup import BackupError, BackupService
from app.mars.extensions import ExtensionDescriptor, builtin_extensions
from app.mars.jvm import JvmArgumentFile, JvmConfigError
from app.mars.operations import OperationBusyError, ServerOperationLock
from app.mars.scheduler import ScheduleValidationError, SystemdScheduler, calendar_expression, validate_time
from app.mars.server import ServerCommandError, ServerRuntime, TmuxServerAdapter
from app.mars.services import ApplicationServices
from app.mars.settings import AppSettings
from app.mars.terminal import TerminalError, TmuxTerminalBackend


class FakeStatus:
    running = True


class FakeServer:
    def __init__(self, root: Path):
        self.server_dir = root
        self.commands: list[str] = []
        self.flush_cursors: list[int] = []

    def status(self):
        return FakeStatus()

    def send_command(self, command: str) -> bool:
        self.commands.append(command)
        return True

    def save_log_cursor(self) -> int:
        return 0

    def wait_for_save_complete(self, cursor: int) -> None:
        self.flush_cursors.append(cursor)


class FakeTerminal:
    def __init__(self, exists: bool = False, foreground: str | None = None):
        self.present = exists
        self.foreground = foreground
        self.commands: list[str] = []
        self.working_directory: Path | None = None
        self.screen = ""
        self.closed = False

    def exists(self) -> bool:
        return self.present

    def ensure(self, working_directory: Path) -> bool:
        created = not self.present
        self.present = True
        self.working_directory = working_directory
        return created

    def send_line(self, line: str) -> None:
        if not self.present:
            raise TerminalError("missing")
        self.commands.append(line)

    def capture(self, lines: int = 300) -> str:
        return self.screen

    def foreground_command(self) -> str | None:
        return self.foreground

    def child_processes(self) -> tuple[str, ...]:
        if self.foreground in {None, "bash", "dash", "fish", "sh", "zsh"}:
            return ()
        return (self.foreground,)

    def attach_argv(self) -> list[str]:
        return ["fake-terminal", "attach"]

    def close(self) -> bool:
        was_present = self.present
        self.present = False
        self.closed = was_present
        return was_present


class OfflineRuntime(ServerRuntime):
    def _port_open(self) -> bool:
        return False


class MarsCoreTests(unittest.TestCase):
    def test_schedule_validation(self):
        self.assertEqual(validate_time("4:05"), "04:05")
        self.assertEqual(calendar_expression("daily", "Mon", "04:05"), "*-*-* 04:05:00")
        self.assertEqual(calendar_expression("weekly", "Sun", "23:59"), "Sun *-*-* 23:59:00")
        self.assertEqual(calendar_expression("weekly", "Mon,Wed,Fri", "07:15"), "Mon,Wed,Fri *-*-* 07:15:00")
        self.assertEqual(calendar_expression("weekly", "Mon,Mon,Sun", "07:15"), "Mon,Sun *-*-* 07:15:00")
        with self.assertRaises(ScheduleValidationError):
            validate_time("25:00")
        with self.assertRaises(ScheduleValidationError):
            validate_time(None)
        with self.assertRaises(ScheduleValidationError):
            calendar_expression("weekly", "NoDay", "04:00")
    def test_settings_save_is_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            settings = AppSettings()
            settings.server_dir = "/tmp/example-server"
            settings.restart.enabled = True
            settings.terminal.session_name = "custom-session"
            settings.save(path)
            loaded = AppSettings.load(path)
            self.assertEqual(loaded.server_dir, "/tmp/example-server")
            self.assertTrue(loaded.restart.enabled)
            self.assertEqual(loaded.terminal.session_name, "custom-session")
            self.assertEqual(json.loads(path.read_text())['restart']['time'], "04:00")
            self.assertEqual(loaded.restart.days, ["Mon"])
            self.assertNotIn("interval_value", json.loads(path.read_text())["restart"])

    def test_settings_migrate_legacy_single_day_to_days(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps({"restart": {"mode": "weekly", "day": "Fri", "time": "04:00"}}), encoding="utf-8")
            loaded = AppSettings.load(path)
            self.assertEqual(loaded.restart.days, ["Fri"])

    def test_settings_tolerate_obsolete_and_malformed_json_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(
                json.dumps(
                    {
                        "server_dir": "/tmp/example-server",
                        "terminal": {"session_name": "", "capture_lines": "many"},
                        "restart": {"interval_value": 12, "mode": [], "days": "Mon", "unknown": True},
                        "backup": {"destination": 42, "keep_count": "seven"},
                    }
                ),
                encoding="utf-8",
            )
            loaded = AppSettings.load(path)
            self.assertEqual(loaded.server_dir, "/tmp/example-server")
            self.assertEqual(loaded.terminal.session_name, "minecraft-server")
            self.assertEqual(loaded.restart.days, ["Mon"])
            self.assertIsInstance(loaded.backup.destination, str)
            self.assertEqual(loaded.backup.keep_count, 7)

            path.write_text("[]", encoding="utf-8")
            self.assertEqual(AppSettings.load(path), AppSettings())

    def test_unconfigured_settings_do_not_infer_server_from_app_location(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = AppSettings()
            self.assertEqual(settings.server_dir, "")
            services = ApplicationServices.build(settings, Path(directory))
            self.assertFalse(services.runtime.configured)
            self.assertEqual(services.runtime.console_text(), "Server directory is not registered.\n")
            self.assertFalse(services.runtime.process_active())
            self.assertFalse(services.runtime.minecraft_process_active())
            self.assertEqual(services.runtime.shutdown(), "no managed server")

    def test_jvm_arguments_are_validated_and_applied_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "user_jvm_args.txt"
            target.write_text("-Xms1G\n-Xmx2G\n-Dold=true\n", encoding="utf-8")
            manager = JvmArgumentFile(root)
            result = manager.apply("2G", "4G", "-Dnew=true\n-XX:+UseG1GC")
            self.assertEqual(result.minimum_memory, "2G")
            self.assertIn("-Xms2G", target.read_text(encoding="utf-8"))
            self.assertIn("-Dnew=true", target.read_text(encoding="utf-8"))
            self.assertEqual(manager.backup_path.read_text(encoding="utf-8"), "-Xms1G\n-Xmx2G\n-Dold=true\n")
            with self.assertRaises(JvmConfigError):
                manager.apply("8G", "4G", "")
            with self.assertRaises(JvmConfigError):
                manager.apply("1G", "4G", "-Xmx3G")

    def test_backup_creates_valid_archive_and_restores_save_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "server"
            world = root / "world"
            world.mkdir(parents=True)
            (world / "level.dat").write_bytes(b"fake-level")
            (world / "session.lock").write_bytes(b"do-not-restore")
            destination = Path(directory) / "backups"
            fake = FakeServer(root)
            result = BackupService(fake, destination).create(keep_count=1, keep_days=30)
            self.assertTrue(result.archive.is_file())
            self.assertEqual(fake.commands, ["save-off", "save-all flush", "save-on"])
            self.assertEqual(fake.flush_cursors, [0])
            with tarfile.open(result.archive, "r:gz") as bundle:
                names = bundle.getnames()
            self.assertIn("world/level.dat", names)
            self.assertNotIn("world/session.lock", names)
            self.assertEqual(len(list(destination.glob("*.tar.gz"))), 1)

    def test_backup_restores_save_mode_when_flush_confirmation_fails(self):
        class FailingFlushServer(FakeServer):
            def wait_for_save_complete(self, cursor: int) -> None:
                raise RuntimeError("flush timeout")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "server"
            (root / "world").mkdir(parents=True)
            server = FailingFlushServer(root)
            with self.assertRaisesRegex(BackupError, "flush timeout"):
                BackupService(server, Path(directory) / "backups").create()
            self.assertEqual(server.commands, ["save-off", "save-all flush", "save-on"])
            self.assertEqual(list((Path(directory) / "backups").glob("*.tar.gz")), [])

    def test_backup_does_not_publish_archive_when_save_on_fails(self):
        class RestoreFailServer(FakeServer):
            def send_command(self, command: str) -> bool:
                super().send_command(command)
                return command != "save-on"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "server"
            (root / "world").mkdir(parents=True)
            destination = Path(directory) / "backups"
            with self.assertRaisesRegex(BackupError, "save-on"):
                BackupService(RestoreFailServer(root), destination).create()
            self.assertEqual(list(destination.glob("minecraft-backup-*.tar.gz")), [])

    def test_backup_retention_caps_recent_archives(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "server"
            world = root / "world"
            world.mkdir(parents=True)
            (world / "level.dat").write_bytes(b"fake-level")
            destination = Path(directory) / "backups"
            destination.mkdir()
            for index in range(2):
                old = destination / f"minecraft-backup-old-{index}.tar.gz"
                old.write_bytes(b"old")
                old.with_suffix(old.suffix + ".json").write_text(json.dumps({"archive": old.name}))
                old_time = time.time() - (index + 1) * 60
                os.utime(old, (old_time, old_time))
            unrelated = destination / "minecraft-backup-user-file.tar.gz"
            unrelated.write_bytes(b"not-managed-by-mars")
            result = BackupService(FakeServer(root), destination).create(keep_count=1, keep_days=30)
            self.assertTrue(result.archive.exists())
            self.assertTrue(unrelated.exists())
            self.assertFalse(any((destination / f"minecraft-backup-old-{index}.tar.gz").exists() for index in range(2)))

    def test_backup_rejects_world_outside_server_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "server"
            root.mkdir()
            with self.assertRaisesRegex(BackupError, "サーバーディレクトリ外"):
                BackupService(FakeServer(root), Path(directory) / "backups", "../outside").create()

    def test_systemd_scheduler_writes_owned_units(self):
        class FakeScheduler(SystemdScheduler):
            def __init__(self, root, unit_dir):
                super().__init__(root, unit_dir)
                self.calls = []

            def _run_systemctl(self, *args):
                self.calls.append(args)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            units = root / "units"
            scheduler = FakeScheduler(root, units)
            settings = AppSettings().restart
            settings.enabled = True
            settings.mode = "weekly"
            settings.day = "Sat"
            settings.days = ["Sat"]
            settings.time = "04:05"
            app_settings = AppSettings()
            app_settings.terminal.start_command = "./run.sh --label=100%"
            expression = scheduler.apply_restart(settings, root / "server", app_settings.terminal)
            self.assertEqual(expression, "Sat *-*-* 04:05:00")
            self.assertIn("--now", scheduler.calls[-1])
            service = (units / "mars-restart.service").read_text()
            timer = (units / "mars-restart.timer").read_text()
            self.assertIn("mars_worker.py restart", service)
            self.assertIn("100%%", service)
            self.assertIn("OnCalendar=Sat *-*-* 04:05:00", timer)
            self.assertNotIn("OnActiveSec=", timer)

    def test_scheduler_links_backup_to_restart_and_disables_independent_timer(self):
        class FakeScheduler(SystemdScheduler):
            def __init__(self, root, unit_dir):
                super().__init__(root, unit_dir)
                self.calls = []

            def _run_systemctl(self, *args):
                self.calls.append(args)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scheduler = FakeScheduler(root, root / "units")
            settings = AppSettings()
            settings.restart.enabled = True
            settings.backup.enabled = True
            settings.backup.linked_to_restart = True
            settings.restart.mode = "weekly"
            settings.restart.day = "Sun"
            settings.restart.time = "03:00"
            scheduler.apply_restart(settings.restart, root / "server", settings.terminal, settings.backup)
            self.assertIn("--backup-destination", (root / "units" / "mars-restart.service").read_text())
            self.assertEqual(scheduler.apply_backup(settings.backup, root / "server", settings.terminal), "linked-to-restart")
            self.assertEqual(scheduler.calls[-1], ("disable", "--now", "mars-backup.timer"))

    def test_scheduler_writes_independent_weekly_backup_calendar(self):
        class FakeScheduler(SystemdScheduler):
            def __init__(self, root, unit_dir):
                super().__init__(root, unit_dir)
                self.calls = []

            def _run_systemctl(self, *args):
                self.calls.append(args)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scheduler = FakeScheduler(root, root / "units")
            settings = AppSettings().backup
            settings.enabled = True
            settings.linked_to_restart = False
            settings.mode = "weekly"
            settings.day = "Wed"
            settings.time = "22:30"
            result = scheduler.apply_backup(settings, root / "server", AppSettings().terminal)
            self.assertEqual(result, "Wed *-*-* 22:30:00")
            timer = (root / "units" / "mars-backup.timer").read_text()
            self.assertIn("OnCalendar=Wed *-*-* 22:30:00", timer)
            self.assertIn(("enable", "--now", "mars-backup.timer"), scheduler.calls)

    def test_tmux_backend_uses_argument_lists_and_literal_send_keys(self):
        calls: list[list[str]] = []

        def runner(args, **_kwargs):
            calls.append(args)
            if args[1] == "has-session":
                return subprocess.CompletedProcess(args, 0, "", "")
            if args[1] == "display-message":
                return subprocess.CompletedProcess(args, 0, "bash\n", "")
            return subprocess.CompletedProcess(args, 0, "", "")

        backend = TmuxTerminalBackend("safe-session", runner=runner)
        with tempfile.TemporaryDirectory() as directory:
            self.assertFalse(backend.ensure(Path(directory)))
        self.assertIn(["tmux", "set-option", "-t", "safe-session", "mouse", "on"], calls)
        backend.send_line("say hello; $(not-a-shell)")
        self.assertIn(["tmux", "send-keys", "-t", "safe-session", "-l", "say hello; $(not-a-shell)"], calls)
        with self.assertRaises(TerminalError):
            backend.send_line("say unsafe\0value")
        self.assertEqual(backend.foreground_command(), "bash")
        self.assertEqual(backend.attach_argv(), ["tmux", "attach-session", "-t", "safe-session"])
        self.assertTrue(backend.close())
        self.assertIn(["tmux", "kill-session", "-t", "safe-session"], calls)
        with self.assertRaises(TerminalError):
            TmuxTerminalBackend("unsafe session")

    def test_runtime_prepares_terminal_and_avoids_commands_at_offline_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            terminal = FakeTerminal()
            runtime = OfflineRuntime(root, terminal, start_command="./run.sh nogui")
            self.assertEqual(runtime.start(), "sent: ./run.sh nogui")
            self.assertEqual(terminal.commands, ["./run.sh nogui"])
            terminal.foreground = "bash"
            self.assertEqual(runtime.stop(), "server already offline")
            self.assertEqual(terminal.commands, ["./run.sh nogui"])
            self.assertEqual(runtime.terminal_attach_argv(), ["fake-terminal", "attach"])
            self.assertEqual(runtime.shutdown(), "server stopped; virtual terminal closed")
            self.assertTrue(terminal.closed)
            with self.assertRaises(RuntimeError):
                OfflineRuntime(root, terminal, start_command="./run.sh\nunsafe")
            with self.assertRaises(RuntimeError):
                OfflineRuntime(root, terminal, start_command=None)

    def test_process_active_checks_child_tree_once(self):
        class CountingTerminal(FakeTerminal):
            def __init__(self):
                super().__init__(exists=True, foreground="java")
                self.child_checks = 0

            def child_processes(self) -> tuple[str, ...]:
                self.child_checks += 1
                return super().child_processes()

        with tempfile.TemporaryDirectory() as directory:
            terminal = CountingTerminal()
            runtime = OfflineRuntime(Path(directory), terminal)
            self.assertTrue(runtime.process_active())
            self.assertEqual(terminal.child_checks, 1)

    def test_save_flush_wait_uses_only_new_latest_log_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logs = root / "logs"
            logs.mkdir()
            latest = logs / "latest.log"
            latest.write_text("[INFO] Saved the game\n", encoding="utf-8")
            runtime = OfflineRuntime(root, FakeTerminal())
            cursor = runtime.save_log_cursor()
            with self.assertRaises(ServerCommandError):
                runtime.wait_for_save_complete(cursor, timeout=0)
            with latest.open("a", encoding="utf-8") as handle:
                handle.write("[INFO] Saved the game\n")
            runtime.wait_for_save_complete(cursor, timeout=0.1)

    def test_restart_with_backup_stops_then_backs_up_then_starts(self):
        events: list[str] = []

        class OrderedTerminal(FakeTerminal):
            def send_line(self, line: str) -> None:
                super().send_line(line)
                events.append(line)
                if line == "stop":
                    self.foreground = "bash"

        class OrderedBackup(BackupService):
            def create_locked(self, keep_count=7, keep_days=30):
                events.append("backup")
                return super().create_locked(keep_count, keep_days)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "world").mkdir()
            terminal = OrderedTerminal(exists=True, foreground="java")
            server = OfflineRuntime(root, terminal)
            result, start_result = server.restart_with_backup(OrderedBackup(server, root / "backups"))
            self.assertTrue(result.archive.exists())
            self.assertEqual(start_result, "sent: ./run.sh nogui")
            self.assertEqual(events, ["stop", "backup", "./run.sh nogui"])

    def test_server_operation_lock_rejects_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_runtime = os.environ.get("XDG_RUNTIME_DIR")
            os.environ["XDG_RUNTIME_DIR"] = directory
            try:
                with ServerOperationLock(root):
                    with self.assertRaises(OperationBusyError):
                        with ServerOperationLock(root):
                            pass
            finally:
                if old_runtime is None:
                    os.environ.pop("XDG_RUNTIME_DIR", None)
                else:
                    os.environ["XDG_RUNTIME_DIR"] = old_runtime

    def test_shutdown_keeps_tmux_when_minecraft_does_not_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            terminal = FakeTerminal(exists=True, foreground="java")
            runtime = OfflineRuntime(Path(directory), terminal)
            with self.assertRaises(ServerCommandError):
                runtime.shutdown(timeout=0)
            self.assertEqual(terminal.commands, ["stop"])
            self.assertTrue(terminal.present)
            self.assertFalse(terminal.closed)

    def test_restart_waits_for_terminal_shell_before_sending_start(self):
        class DelayedStoppingTerminal(FakeTerminal):
            def __init__(self):
                super().__init__(exists=True, foreground="java")
                self.stop_checks = 0

            def child_processes(self) -> tuple[str, ...]:
                if "stop" not in self.commands:
                    return ("/usr/bin/java forge",)
                self.stop_checks += 1
                return ("/usr/bin/java forge",) if self.stop_checks < 3 else ()

        with tempfile.TemporaryDirectory() as directory:
            terminal = DelayedStoppingTerminal()
            runtime = OfflineRuntime(Path(directory), terminal)
            self.assertEqual(runtime.restart(timeout=2), "sent: ./run.sh nogui")
            self.assertEqual(terminal.commands, ["stop", "./run.sh nogui"])
            self.assertGreaterEqual(terminal.stop_checks, 3)

    def test_builtin_extension_ids_are_unique(self):
        registry = builtin_extensions()
        self.assertEqual([item.extension_id for item in registry.all()], ["scheduled-restart", "backup"])
        with self.assertRaises(ValueError):
            registry.register(ExtensionDescriptor("backup", "duplicate", "duplicate"))

    def test_server_metrics_parse_players_and_mod_count(self):
        log = "\n".join(
            [
                "[Server thread/INFO]: alice joined the game",
                "[Server thread/INFO]: bob joined the game",
                "[Server thread/INFO]: alice left the game",
            ]
        )
        self.assertEqual(TmuxServerAdapter._players_from_log(log), "1")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "mods").mkdir()
            (root / "mods" / "example.jar").write_bytes(b"jar")
            self.assertEqual(sum(1 for path in (root / "mods").glob("*.jar") if path.is_file()), 1)


if __name__ == "__main__":
    unittest.main()

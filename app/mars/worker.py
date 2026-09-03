from __future__ import annotations

import argparse
from pathlib import Path

from .backup import BackupService
from .server import ServerRuntime
from .terminal import create_terminal_backend


def add_runtime_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--server-dir", required=True, type=Path)
    command.add_argument("--terminal-backend", default="tmux")
    command.add_argument("--session", default="minecraft-server")
    command.add_argument("--start-command", default="./run.sh nogui")
    command.add_argument("--stop-command", default="stop")


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="M.A.R.S. background worker")
    subparsers = command.add_subparsers(dest="action", required=True)
    restart = subparsers.add_parser("restart")
    add_runtime_arguments(restart)
    restart.add_argument("--backup-destination", type=Path)
    restart.add_argument("--keep-count", type=int, default=7)
    restart.add_argument("--keep-days", type=int, default=30)
    backup = subparsers.add_parser("backup")
    add_runtime_arguments(backup)
    backup.add_argument("--destination", required=True, type=Path)
    backup.add_argument("--keep-count", type=int, default=7)
    backup.add_argument("--keep-days", type=int, default=30)
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    terminal = create_terminal_backend(args.terminal_backend, args.session)
    server = ServerRuntime(args.server_dir, terminal, args.start_command, args.stop_command)
    if not server.terminal_running():
        print("skipped: M.A.R.S. managed terminal is not running")
        return 0
    if args.action == "restart":
        if args.backup_destination:
            result, restart_result = server.restart_with_warnings(
                BackupService(server, args.backup_destination),
                args.keep_count,
                args.keep_days,
            )
            print(f"backup: {result.archive} ({result.size} bytes)")
            print(restart_result)
            return 0
        print(server.restart_with_warnings())
        return 0
    result = BackupService(server, args.destination).create(args.keep_count, args.keep_days)
    print(f"backup: {result.archive} ({result.size} bytes)")
    return 0

from __future__ import annotations

import hashlib
import json
import os
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .operations import ServerOperationLock


class BackupError(RuntimeError):
    pass


@dataclass(frozen=True)
class BackupResult:
    archive: Path
    manifest: Path
    size: int
    sha256: str
    deleted: tuple[Path, ...]


class BackupService:
    def __init__(self, server, destination: Path, world_name: str = "world"):
        self.server = server
        self.destination = Path(destination).expanduser().resolve()
        self.world_name = world_name

    def _paths(self) -> tuple[Path, Path]:
        server_dir = Path(self.server.server_dir).resolve()
        world = (server_dir / self.world_name).resolve()
        if world == server_dir or not world.is_relative_to(server_dir):
            raise BackupError("バックアップ対象ワールドがサーバーディレクトリ外を指しています")
        if not world.is_dir():
            raise BackupError(f"ワールドディレクトリがありません: {world}")
        if self.destination == world or world.is_relative_to(self.destination) or self.destination.is_relative_to(world):
            raise BackupError("バックアップ保存先をワールドディレクトリの内外に置けません")
        self.destination.mkdir(parents=True, exist_ok=True)
        return server_dir, world

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _tar_filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        if Path(info.name).name == "session.lock":
            return None
        return info

    @staticmethod
    def _write_json_atomic(path: Path, payload: dict) -> None:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def create(self, keep_count: int = 7, keep_days: int = 30) -> BackupResult:
        with ServerOperationLock(self.server.server_dir):
            return self.create_locked(keep_count, keep_days)

    def create_locked(self, keep_count: int = 7, keep_days: int = 30) -> BackupResult:
        """Create a backup while the caller owns the server operation lock."""
        return self._create_locked(keep_count, keep_days)

    def _create_locked(self, keep_count: int, keep_days: int) -> BackupResult:
        if type(keep_count) is not int or type(keep_days) is not int or keep_count < 1 or keep_days < 0:
            raise BackupError("保持設定が不正です")
        try:
            _, world = self._paths()
        except OSError as exc:
            raise BackupError(f"バックアップ先を準備できません: {exc}") from exc
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        archive = self.destination / f"minecraft-backup-{timestamp}.tar.gz"
        temporary = self.destination / f".{archive.name}.tmp"
        locked = False
        failure: Exception | None = None
        restore_failure: Exception | None = None
        try:
            if self.server.status().running:
                save_cursor = self.server.save_log_cursor()
                if not self.server.send_command("save-off"):
                    raise BackupError("save-offをMinecraftへ送信できません")
                locked = True
                if not self.server.send_command("save-all flush"):
                    raise BackupError("save-all flushをMinecraftへ送信できません")
                self.server.wait_for_save_complete(save_cursor)
            with tarfile.open(temporary, "w:gz") as bundle:
                bundle.add(world, arcname=self.world_name, filter=self._tar_filter)
        except Exception as exc:
            failure = exc if isinstance(exc, BackupError) else BackupError(f"バックアップ作成に失敗しました: {exc}")
        finally:
            if locked:
                try:
                    if not self.server.send_command("save-on"):
                        raise BackupError("save-onをMinecraftへ送信できません")
                except Exception as exc:
                    restore_failure = exc

        if failure is not None or restore_failure is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

        if failure is not None:
            if restore_failure is not None:
                raise BackupError(f"{failure}; さらにsave-onの復旧にも失敗しました: {restore_failure}") from failure
            raise failure
        if restore_failure is not None:
            raise BackupError(f"バックアップ後にsave-onを復旧できませんでした: {restore_failure}") from restore_failure

        try:
            os.replace(temporary, archive)
            size = archive.stat().st_size
            sha256 = self._sha256(archive)
        except OSError as exc:
            try:
                archive.unlink()
            except OSError:
                pass
            try:
                temporary.unlink()
            except OSError:
                pass
            raise BackupError(f"バックアップ検証に失敗しました: {exc}") from exc
        manifest = archive.with_suffix(archive.suffix + ".json")
        manifest_payload = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "world": self.world_name,
            "archive": archive.name,
            "size": size,
            "sha256": sha256,
        }
        try:
            self._write_json_atomic(manifest, manifest_payload)
        except OSError as exc:
            try:
                archive.unlink()
            except OSError:
                pass
            raise BackupError(f"バックアップマニフェストを保存できません: {exc}") from exc
        deleted = tuple(self._apply_retention(keep_count, keep_days, newest=archive))
        return BackupResult(archive=archive, manifest=manifest, size=size, sha256=sha256, deleted=deleted)

    def _apply_retention(self, keep_count: int, keep_days: int, newest: Path) -> list[Path]:
        archives = sorted(
            (path for path in self.destination.glob("minecraft-backup-*.tar.gz") if self._is_managed_archive(path)),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        keep = set(archives[:keep_count])
        keep.add(newest)
        cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
        deleted: list[Path] = []
        for path in archives:
            modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            if path != newest and (path not in keep or modified < cutoff):
                path.unlink()
                manifest = path.with_suffix(path.suffix + ".json")
                had_manifest = manifest.exists()
                if had_manifest:
                    manifest.unlink()
                deleted.extend([path, manifest] if had_manifest else [path])
        return deleted

    @staticmethod
    def _is_managed_archive(path: Path) -> bool:
        manifest = path.with_suffix(path.suffix + ".json")
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            return False
        return isinstance(payload, dict) and payload.get("archive") == path.name

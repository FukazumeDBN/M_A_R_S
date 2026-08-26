from __future__ import annotations

import hashlib
import json
import os
import tarfile
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
        world = server_dir / self.world_name
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

    def create(self, keep_count: int = 7, keep_days: int = 30) -> BackupResult:
        with ServerOperationLock(self.server.server_dir):
            return self._create_locked(keep_count, keep_days)

    def _create_locked(self, keep_count: int, keep_days: int) -> BackupResult:
        if keep_count < 1 or keep_days < 0:
            raise BackupError("保持設定が不正です")
        _, world = self._paths()
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        archive = self.destination / f"minecraft-backup-{timestamp}.tar.gz"
        temporary = self.destination / f".{archive.name}.tmp"
        locked = False
        try:
            if self.server.status().running:
                if not self.server.send_command("save-off"):
                    raise BackupError("save-offをMinecraftへ送信できません")
                locked = True
                if not self.server.send_command("save-all flush"):
                    raise BackupError("save-all flushをMinecraftへ送信できません")
            with tarfile.open(temporary, "w:gz") as bundle:
                bundle.add(world, arcname=self.world_name, filter=self._tar_filter)
            os.replace(temporary, archive)
        except (OSError, tarfile.TarError) as exc:
            raise BackupError(f"バックアップ作成に失敗しました: {exc}") from exc
        finally:
            if locked:
                self.server.send_command("save-on")
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

        size = archive.stat().st_size
        sha256 = self._sha256(archive)
        manifest = archive.with_suffix(archive.suffix + ".json")
        manifest_payload = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "world": self.world_name,
            "archive": archive.name,
            "size": size,
            "sha256": sha256,
        }
        manifest.write_text(json.dumps(manifest_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        deleted = tuple(self._apply_retention(keep_count, keep_days, newest=archive))
        return BackupResult(archive=archive, manifest=manifest, size=size, sha256=sha256, deleted=deleted)

    def _apply_retention(self, keep_count: int, keep_days: int, newest: Path) -> list[Path]:
        archives = sorted(self.destination.glob("minecraft-backup-*.tar.gz"), key=lambda path: path.stat().st_mtime, reverse=True)
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

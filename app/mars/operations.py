from __future__ import annotations

import fcntl
import hashlib
import os
from pathlib import Path


class OperationBusyError(RuntimeError):
    pass


class ServerOperationLock:
    """Cross-process lock shared by restart and backup operations."""

    def __init__(self, server_dir: Path):
        identity = str(Path(server_dir).expanduser().resolve()).encode("utf-8")
        digest = hashlib.sha256(identity).hexdigest()[:16]
        runtime_root = os.environ.get("XDG_RUNTIME_DIR")
        cache_root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        self._fallback_root = cache_root / "mars" / "locks"
        lock_root = Path(runtime_root) / "mars" if runtime_root else self._fallback_root
        self.path = lock_root / f"server-{digest}.lock"
        self._filename = self.path.name
        self._handle = None

    def __enter__(self) -> "ServerOperationLock":
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._handle = self.path.open("a+", encoding="utf-8")
        except OSError:
            self.path = self._fallback_root / self._filename
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._handle.close()
            self._handle = None
            raise OperationBusyError("別のバックアップまたは再起動処理が実行中です") from exc
        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(f"pid={os.getpid()}\n")
        self._handle.flush()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        if self._handle is None:
            return
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path


class JvmConfigError(ValueError):
    pass


@dataclass(frozen=True)
class JvmConfig:
    minimum_memory: str
    maximum_memory: str
    custom_arguments: str


class JvmArgumentFile:
    MEMORY_PATTERN = re.compile(r"^(\d+)([MG])$", re.IGNORECASE)

    def __init__(self, server_dir: Path):
        self.server_dir = Path(server_dir).expanduser().resolve()
        self.path = self.server_dir / "user_jvm_args.txt"
        self.backup_path = self.server_dir / "user_jvm_args.txt.mars-backup"

    @classmethod
    def _memory_mib(cls, value: str) -> int:
        match = cls.MEMORY_PATTERN.fullmatch(value.strip())
        if not match:
            raise JvmConfigError("メモリ値は1024Mや2Gの形式で入力してください")
        amount = int(match.group(1))
        if amount <= 0:
            raise JvmConfigError("メモリ値は1以上にしてください")
        return amount * (1024 if match.group(2).upper() == "G" else 1)

    @classmethod
    def validate(cls, minimum: str, maximum: str, custom: str) -> JvmConfig:
        minimum = minimum.strip().upper()
        maximum = maximum.strip().upper()
        if cls._memory_mib(minimum) > cls._memory_mib(maximum):
            raise JvmConfigError("最小メモリは最大メモリ以下にしてください")
        for line in custom.splitlines():
            stripped = line.strip()
            if re.match(r"^-Xm[sx]", stripped, re.IGNORECASE):
                raise JvmConfigError("任意JVM引数欄へ-Xms/-Xmxを重複して記述できません")
        return JvmConfig(minimum, maximum, custom.strip())

    def load(self) -> JvmConfig:
        if not self.path.is_file():
            return JvmConfig("1G", "2G", "")
        minimum, maximum = "1G", "2G"
        custom: list[str] = []
        for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.lower().startswith("-xms"):
                minimum = stripped[4:]
            elif stripped.lower().startswith("-xmx"):
                maximum = stripped[4:]
            else:
                custom.append(line)
        try:
            return self.validate(minimum, maximum, "\n".join(custom))
        except JvmConfigError:
            return JvmConfig("1G", "2G", "\n".join(custom))

    def apply(self, minimum: str, maximum: str, custom: str) -> JvmConfig:
        config = self.validate(minimum, maximum, custom)
        if not self.server_dir.is_dir():
            raise JvmConfigError(f"サーバーディレクトリがありません: {self.server_dir}")
        if self.path.exists():
            shutil.copy2(self.path, self.backup_path)
            mode = self.path.stat().st_mode
        else:
            mode = 0o664
        lines = [
            "# Managed by M.A.R.S. — edit from the Server tab.",
            f"-Xms{config.minimum_memory}",
            f"-Xmx{config.maximum_memory}",
        ]
        if config.custom_arguments:
            lines.extend(["", "# Custom JVM arguments", config.custom_arguments])
        payload = "\n".join(lines) + "\n"
        fd, temporary = tempfile.mkstemp(prefix="user_jvm_args.", suffix=".tmp", dir=self.server_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, mode & 0o777)
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        return config

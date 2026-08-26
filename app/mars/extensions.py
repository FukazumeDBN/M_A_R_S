from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExtensionDescriptor:
    extension_id: str
    title: str
    description: str


class ExtensionRegistry:
    def __init__(self):
        self._extensions: dict[str, ExtensionDescriptor] = {}

    def register(self, extension: ExtensionDescriptor) -> None:
        if extension.extension_id in self._extensions:
            raise ValueError(f"拡張機能IDが重複しています: {extension.extension_id}")
        self._extensions[extension.extension_id] = extension

    def all(self) -> tuple[ExtensionDescriptor, ...]:
        return tuple(self._extensions.values())


def builtin_extensions() -> ExtensionRegistry:
    registry = ExtensionRegistry()
    registry.register(ExtensionDescriptor("scheduled-restart", "Scheduled restart", "仮想ターミナル経由でサーバーを定期再起動します"))
    registry.register(ExtensionDescriptor("backup", "Backup", "安全保存後にワールドをバックアップします"))
    return registry

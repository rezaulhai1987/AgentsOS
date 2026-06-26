"""In-memory registry of known agent manifests and tools."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .manifest import LoadedManifest, iter_templates


class ManifestRegistry:
    def __init__(self) -> None:
        self._items: dict[str, LoadedManifest] = {}

    def register(self, lm: LoadedManifest) -> None:
        self._items[lm.id] = lm

    def get(self, manifest_id: str) -> LoadedManifest:
        if manifest_id not in self._items:
            raise KeyError(f"Unknown manifest: {manifest_id}. Known: {sorted(self._items)}")
        return self._items[manifest_id]

    def all(self) -> list[LoadedManifest]:
        return list(self._items.values())

    @classmethod
    def from_template_dirs(cls, dirs: list[Path]) -> ManifestRegistry:
        reg = cls()
        for lm in iter_templates(dirs):
            reg.register(lm)
        return reg


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Callable] = {}

    def register(self, name: str, fn: Callable) -> None:
        self._tools[name] = fn

    def get(self, name: str) -> Callable:
        if name not in self._tools:
            raise KeyError(f"Unknown tool: {name}")
        return self._tools[name]

    def names(self) -> list[str]:
        return sorted(self._tools)

"""Backend selection. The only MVP backend is Local Disk, but the factory
keeps the choice centralised so new providers slot in without touching callers.
"""
from __future__ import annotations

from .backend import StorageBackend
from .local_disk import LocalDiskBackend
from ..config import ConfigManager
from ..state import StateManager


def create_backend(
    name: str, config: ConfigManager, state: StateManager
) -> StorageBackend:
    if name == "local_disk":
        return LocalDiskBackend(config.settings.localdisk_root)
    raise ValueError(f"Unknown storage backend: {name}")

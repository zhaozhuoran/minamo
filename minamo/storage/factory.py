"""Backend selection. The only MVP backend is Local Disk, but the factory
keeps the choice centralised so new providers slot in without touching callers.
"""
from __future__ import annotations

from pathlib import Path

from .backend import StorageBackend
from .local_disk import LocalDiskBackend


def create_backend(name: str, data_root: Path) -> StorageBackend:
    if name == "local_disk":
        return LocalDiskBackend(data_root)
    raise ValueError(f"Unknown storage backend: {name}")

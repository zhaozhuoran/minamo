"""Backend selection. Swapping storage providers or loading dynamically.
"""
from __future__ import annotations

from .backend import StorageBackend
from .local_disk import LocalDiskBackend
from .r2 import R2Backend
from ..config import ConfigManager
from ..state import StateManager


def create_backend(
    name: str, config: ConfigManager, state: StateManager
) -> StorageBackend:
    if name == "local_disk":
        return LocalDiskBackend(config.settings.localdisk_root)
    elif name == "r2":
        cfg = config.backend_config("r2")
        # Fallbacks for missing values
        return R2Backend(
            endpoint_url=cfg.get("endpoint_url", ""),
            access_key_id=cfg.get("access_key_id", ""),
            secret_access_key=cfg.get("secret_access_key", ""),
            bucket=cfg.get("bucket", ""),
            region_name=cfg.get("region_name", "auto"),
        )
    raise ValueError(f"Unknown storage backend: {name}")

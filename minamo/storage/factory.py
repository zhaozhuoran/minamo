"""Backend selection. Swapping storage providers or loading dynamically.
"""
from __future__ import annotations

from pathlib import Path
from .backend import StorageBackend
from .local_disk import LocalDiskBackend
from .r2 import R2Backend
from .hackclub_cdn import HackClubCDNBackend
from .onedrive import OneDriveBackend
from ..config import ConfigManager
from ..state import StateManager


def create_backend(
    name: str, config: ConfigManager, state: StateManager
) -> StorageBackend:
    cfg = config.backend_config(name)
    backend_type = cfg.get("type", name)

    if backend_type == "local_disk":
        root_val = cfg.get("root")
        if root_val:
            p = Path(root_val)
            root_path = p if p.is_absolute() else config.settings.data_root / p
        else:
            root_path = config.settings.localdisk_root
        return LocalDiskBackend(root_path)
    elif backend_type == "r2":
        return R2Backend(
            endpoint_url=cfg.get("endpoint_url", ""),
            access_key_id=cfg.get("access_key_id", ""),
            secret_access_key=cfg.get("secret_access_key", ""),
            bucket=cfg.get("bucket", ""),
            region_name=cfg.get("region_name", "auto"),
        )
    elif backend_type == "hackclub_cdn":
        return HackClubCDNBackend(
            api_key=cfg.get("api_key", ""),
            base_url=cfg.get("base_url", "https://cdn.hackclub.com"),
            backend_id=name,
            data_root=config.settings.data_root,
        )
    elif backend_type == "onedrive":
        return OneDriveBackend(
            client_id=cfg.get("client_id", ""),
            client_secret=cfg.get("client_secret", ""),
            state_file=cfg.get("state_file", "data/state/onedrive.json"),
            root_path=cfg.get("root_path", "/Developing/minamo"),
            backend_id=name,
            data_root=config.settings.data_root,
        )
    raise ValueError(f"Unknown storage backend type '{backend_type}' for backend '{name}'")

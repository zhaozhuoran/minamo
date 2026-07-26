"""Application configuration for Minamo.

Settings are loaded from environment variables (MINAMO_* prefix) with sensible
defaults so the service can run with zero configuration during development.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MINAMO_", extra="ignore")

    # Directory used by the Local Disk backend to store object *data*.
    data_root: Path = Path("./data")

    # Directory used by the Metadata layer to store *metadata* (SQLite).
    # Kept separate from data_root on purpose: metadata is provider-independent.
    metadata_root: Path = Path("./metadata")

    # The active storage backend. Only "local_disk" exists in the MVP.
    backend: str = "local_disk"

    # The endpoint host clients connect to. Used to distinguish virtual-hosted
    # style bucket names (bucket.<host>) from path-style requests.
    endpoint_host: str = "localhost"

    # SigV4 credentials. Real S3 SDKs always sign requests; Minamo verifies the
    # signature against these configured credentials.
    access_key: str = "minamo"
    secret_key: str = "minamo-secret"
    region: str = "us-east-1"

    # Whether to enforce signature verification. Disabled only for quick local
    # debugging; production should keep this True.
    enforce_signature: bool = True

    # Presigned URL default expiry in seconds.
    presign_ttl: int = 3600

    def ensure_dirs(self) -> None:
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.metadata_root.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    return settings

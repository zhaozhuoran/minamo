"""Minamo configuration: schema, loading and the ConfigManager.

Configuration is split into operator-authored TOML files under ``config/``
(gitignored, scaffolded from ``config.example/``), program-maintained state
under ``data/state/`` (see :mod:`minamo.state`), and user data under
``data/``.

Load precedence is **CLI > ENV > CONFIG (file)**. Environment variables use the
``MINAMO_*`` prefix and override file values so containers / compose keep working.
"""
from __future__ import annotations

from pathlib import Path
from typing import List
from pydantic import BaseModel


class DataPaths(BaseModel):
    """Runtime data layout. All paths are relative to the working directory
    unless absolute."""

    root: str = "data"
    metadata_dir: str = "metadata"
    cache_dir: str = "cache"
    state_dir: str = "state"


class AppConfig(BaseModel):
    """Content of ``config/app.toml`` (service-level settings)."""

    backend: str = "local_disk"
    endpoint_host: str = "localhost"
    region: str = "us-east-1"
    enforce_signature: bool = True
    presign_ttl: int = 3600
    data: DataPaths = DataPaths()
    max_temp_usage: int = 5368709120  # Default 5GB


class SecretsConfig(BaseModel):
    """Content of ``config/secrets.toml`` (credentials)."""

    access_key: str = "minamo"
    secret_key: str = "minamo-secret"


class HsmTier(BaseModel):
    id: str
    backend: str
    priority: int
    target_capacity: int = 0
    high_watermark: int = 0
    limit: int = 0
    minimum_residency: int = 0


class HsmConfig(BaseModel):
    enabled: bool = False
    overflow_policy: str = "reject"  # reject or fallback
    tiers: List[HsmTier] = []
    max_concurrent_migrations: int = 5
    decay_rate_per_hour: float = 0.01
    base_score: float = 100.0
    read_weight: float = 10.0
    write_weight: float = 5.0


class Settings(BaseModel):
    """Fully resolved, validated runtime configuration.

    This is the object stored on ``app.state.settings`` and consumed by the API
    and service layers. Paths are already resolved to absolute ``Path`` objects.
    """

    backend: str
    endpoint_host: str
    region: str
    access_key: str
    secret_key: str
    enforce_signature: bool
    presign_ttl: int

    max_temp_usage: int

    data_root: Path
    localdisk_root: Path
    metadata_dir: Path
    cache_dir: Path
    state_dir: Path
    config_dir: Path

    hsm: HsmConfig = HsmConfig()

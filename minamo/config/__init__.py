"""Minamo configuration package.

Public surface: :class:`ConfigManager` (loading / resolution) and
:class:`Settings` (resolved runtime config, stored on ``app.state.settings``).
"""
from __future__ import annotations

from .manager import DEFAULT_CONFIG_DIR, ConfigManager
from .schema import AppConfig, DataPaths, SecretsConfig, Settings

__all__ = [
    "ConfigManager",
    "Settings",
    "AppConfig",
    "SecretsConfig",
    "DataPaths",
    "DEFAULT_CONFIG_DIR",
]

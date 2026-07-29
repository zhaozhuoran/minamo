"""The ConfigManager: single entry point for loading and resolving Minamo
configuration.

It merges operator-authored TOML files under ``config/``, applies environment
variable overrides (``MINAMO_*``), then CLI overrides, and validates the result
into a :class:`~minamo.config.schema.Settings` object. On first run, when
``config/`` is missing, it scaffolds the directory from ``config.example/`` and
exits (both dev and production) so the operator sets real values.
"""
from __future__ import annotations

import os
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib  # type: ignore

from .scaffold import scaffold_config
from .schema import AppConfig, SecretsConfig, Settings

DEFAULT_CONFIG_DIR = Path("config")

# Maps MINAMO_* environment variables to a path inside the raw config dict.
# Applied after file load, before validation, so ENV > CONFIG.
ENV_MAP: dict[str, list[str]] = {
    "MINAMO_BACKEND": ["app", "backend"],
    "MINAMO_ENDPOINT_HOST": ["app", "endpoint_host"],
    "MINAMO_REGION": ["app", "region"],
    "MINAMO_ENFORCE_SIGNATURE": ["app", "enforce_signature"],
    "MINAMO_PRESIGN_TTL": ["app", "presign_ttl"],
    "MINAMO_ACCESS_KEY": ["secrets", "access_key"],
    "MINAMO_SECRET_KEY": ["secrets", "secret_key"],
    "MINAMO_DATA_ROOT": ["app", "data", "root"],
}


def _read_toml(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _namespace(filename_stem: str) -> str:
    return filename_stem.replace("-", "_")


def _get_path(d: dict, path: list[str]) -> Any:
    cur: Any = d
    for key in path:
        cur = cur[key]
    return cur


def _set_path(d: dict, path: list[str], value: Any) -> None:
    cur = d
    for key in path[:-1]:
        cur = cur.setdefault(key, {})
    cur[path[-1]] = value


def _coerce(value: str) -> Any:
    low = value.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("1", "0") and value.strip() in ("1", "0"):
        return value.strip() == "1"
    try:
        return int(value)
    except ValueError:
        return value


class ConfigManager:
    def __init__(
        self,
        config_dir: str | Path = DEFAULT_CONFIG_DIR,
        overrides: dict | None = None,
        exit_on_missing: bool = False,
    ) -> None:
        self.config_dir = Path(config_dir)
        self._raw: dict = {}

        if not self.config_dir.is_dir():
            if exit_on_missing:
                self._scaffold_and_exit()
            # else: proceed with built-in defaults (import / programmatic use)
            raw: dict = {}
        else:
            raw = self._load_raw()

        self._apply_env(raw)
        if overrides:
            self._apply_overrides(raw, overrides)

        self._raw = raw
        self.settings = self._build_settings(raw)

    # -- construction helpers ------------------------------------------------
    @classmethod
    def from_dict(
        cls, raw: dict, config_dir: str | Path = Path(".")
    ) -> "ConfigManager":
        """Build a manager from an in-memory dict (no filesystem access). Used by
        tests and programmatic callers."""
        self = cls.__new__(cls)
        self.config_dir = Path(config_dir)
        raw = deepcopy(raw)
        self._apply_env(raw)
        self._raw = raw
        self.settings = self._build_settings(raw)
        return self

    def _load_raw(self) -> dict:
        raw: dict = {}
        for path in sorted(self.config_dir.glob("*.toml")):
            raw[_namespace(path.stem)] = _read_toml(path)
        return raw

    def _apply_env(self, raw: dict) -> None:
        for env_key, path in ENV_MAP.items():
            if env_key in os.environ:
                _set_path(raw, path, _coerce(os.environ[env_key]))

    def _apply_overrides(self, raw: dict, overrides: dict) -> None:
        for dotted, value in overrides.items():
            _set_path(raw, dotted.split("."), value)

    def _build_settings(self, raw: dict) -> Settings:
        app_cfg = AppConfig.model_validate(raw.get("app", {}))
        secrets_cfg = SecretsConfig.model_validate(raw.get("secrets", {}))
        data_root = Path(app_cfg.data.root).resolve()
        backend = app_cfg.backend
        backend_raw = raw.get(f"storage_{backend}", {})
        localdisk_root = self._resolve_localdisk(backend, backend_raw, data_root)
        return Settings(
            backend=backend,
            endpoint_host=app_cfg.endpoint_host,
            region=app_cfg.region,
            access_key=secrets_cfg.access_key,
            secret_key=secrets_cfg.secret_key,
            enforce_signature=app_cfg.enforce_signature,
            presign_ttl=app_cfg.presign_ttl,
            data_root=data_root,
            localdisk_root=localdisk_root,
            metadata_dir=data_root / app_cfg.data.metadata_dir,
            cache_dir=data_root / app_cfg.data.cache_dir,
            state_dir=data_root / app_cfg.data.state_dir,
            config_dir=self.config_dir,
        )

    @staticmethod
    def _resolve_localdisk(backend: str, backend_raw: dict, data_root: Path) -> Path:
        if backend == "local_disk":
            root = backend_raw.get("root", "data/localdisk")
            p = Path(root)
            return p if p.is_absolute() else data_root / root
        # Unknown backends resolve later in the factory; return a safe default.
        return data_root

    # -- runtime helpers -----------------------------------------------------
    def backend_config(self, name: str) -> dict:
        return dict(self._raw.get(f"storage_{name}", {}))

    def ensure_dirs(self) -> None:
        for d in (
            self.settings.localdisk_root,
            self.settings.metadata_dir,
            self.settings.cache_dir,
            self.settings.state_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

    def _scaffold_and_exit(self) -> None:
        try:
            scaffold_config(self.config_dir)
        except Exception as exc:  # pragma: no cover - defensive
            print(
                f"[minamo] failed to create config at {self.config_dir}: {exc}",
                file=sys.stderr,
            )
        else:
            print(
                f"[minamo] config directory created at: {self.config_dir}\n"
                f"[minamo] it was populated from the default templates.\n"
                f"[minamo] please review and edit the files (especially "
                f"secrets.toml),\n"
                f"[minamo] then run Minamo again.",
                file=sys.stderr,
            )
        sys.exit(1)

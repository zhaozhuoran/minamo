"""Default configuration templates and the first-run scaffolder.

The canonical, human-readable templates also live in ``config.example/`` at the
repository root (committed). :func:`scaffold_config` copies them when present,
otherwise falls back to the embedded defaults below so a freshly installed Minamo
can always bootstrap itself.
"""
from __future__ import annotations

import shutil
from pathlib import Path

EXAMPLE_DIR = Path(__file__).resolve().parent.parent.parent / "config.example"

APP_TOML = """\
# Minamo service configuration.
# See docs/configuration.md for the full reference.

# Active storage backend. The bundled backend is "local_disk".
backend = "local_disk"

# Host clients connect to; used to detect virtual-hosted bucket names.
endpoint_host = "localhost"

# SigV4 region.
region = "us-east-1"

# Verify request signatures. Keep true in production.
enforce_signature = true

# Default presigned URL expiry, in seconds.
presign_ttl = 3600

# Runtime data layout (object data, metadata, cache, program state).
# All paths are relative to the working directory unless absolute.
[data]
root = "data"
metadata_dir = "metadata"
cache_dir = "cache"
state_dir = "state"
"""

SECRETS_TOML = """\
# Credentials. Treat this file as a secret; it is gitignored.
access_key = "minamo"
secret_key = "minamo-secret"
"""

STORAGE_LOCALDISK_TOML = """\
# Local Disk backend configuration.
# Root directory for object data. Relative paths resolve under data.root.
root = "data/localdisk"
"""

STORAGE_ONEDRIVE_TOML = """\
# OneDrive backend configuration (future).
# Static, operator-authored values live here. Auto-refreshed tokens
# (access_token / refresh_token) are written by Minamo to data/state/onedrive.json.
client_id = ""
client_secret = ""
state_file = "data/state/onedrive.json"
"""

DEFAULT_FILES: dict[str, str] = {
    "app.toml": APP_TOML,
    "secrets.toml": SECRETS_TOML,
    "storage-localdisk.toml": STORAGE_LOCALDISK_TOML,
    "storage-onedrive.toml": STORAGE_ONEDRIVE_TOML,
}


def scaffold_config(config_dir: Path) -> None:
    """Create ``config_dir`` populated from ``config.example/`` (or embedded
    defaults if the example directory is unavailable, e.g. an installed wheel)."""
    config_dir = Path(config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    if EXAMPLE_DIR.is_dir():
        for src in sorted(EXAMPLE_DIR.glob("*.toml")):
            shutil.copyfile(src, config_dir / src.name)
    else:
        for name, content in DEFAULT_FILES.items():
            (config_dir / name).write_text(content, encoding="utf-8")

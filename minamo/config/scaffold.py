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
# Minamo Service Configuration
# -----------------------------
# This file governs global service settings such as the default backend,
# network endpoint, and global signature verification rules.
# For full reference, please see docs/configuration.md.

# Active storage backend.
# The core bundled backend is "local_disk".
# If HSM is enabled, this typically refers to the entrypoint/default tier backend.
# Default: "local_disk"
backend = "local_disk"

# Hostname that S3 clients connect to.
# This is crucial for virtual-hosted-style bucket routing detection
# (e.g., <bucket>.localhost vs. localhost/<bucket>).
# Default: "localhost"
endpoint_host = "localhost"

# AWS Signature Version 4 (SigV4) region name.
# Default: "us-east-1"
region = "us-east-1"

# Signature Verification Policy.
# If true, all incoming HTTP requests must contain valid AWS SigV4 signatures.
# Disable (false) ONLY for local development or trusted private networks.
# Default: true
enforce_signature = true

# Time-To-Live (TTL) for presigned S3 URLs, specified in seconds.
# Default: 3600 (1 hour)
presign_ttl = 3600

# Maximum size limit for the Temporary Workspace (in bytes) used during uploads
# and metadata assembly. Set this based on host disk space limits.
# Default: 5368709120 (5 GB)
max_temp_usage = 5368709120

# Runtime Data Layout
# All subdirectory paths are relative to the 'root' directory unless absolute paths are specified.
[data]
# Root directory where all persistent files, cache, and state will be stored.
# Default: "data"
root = "data"

# Subdirectory to store Minamo's dual-SQLite metadata databases (metadata.db & access_logs.db).
# Default: "metadata"
metadata_dir = "metadata"

# Subdirectory for temporary objects and the LRU/FIFO storage cache.
# Default: "cache"
cache_dir = "cache"

# Subdirectory for internal state files (such as OneDrive token files).
# Default: "state"
state_dir = "state"
"""

SECRETS_TOML = """\
# Credentials Configuration
# -------------------------
# S3 Authentication Credentials used to sign and authenticate S3 gateway operations.
# WARNING: Treat this file as sensitive. Ensure it is gitignored and secure in production.

# S3 Access Key ID.
# Default: "minamo"
access_key = "minamo"

# S3 Secret Access Key.
# Default: "minamo-secret"
secret_key = "minamo-secret"
"""

STORAGE_LOCALDISK_TOML = """\
# Local Disk Storage Backend Configuration
# ----------------------------------------
# This backend stores object payloads directly on the local host filesystem.
# The filename format must be storage-<id>.toml, where <id> matches the `id` field.

# Pluggable backend type. Must be "local_disk".
type = "local_disk"

# A unique backend instance identifier within Minamo.
id = "local_disk"

# Root directory for object data payloads.
# Relative paths are automatically resolved under the global data.root directory.
# Default: "data/localdisk"
root = "data/localdisk"
"""

STORAGE_ONEDRIVE_TOML = """\
# Microsoft OneDrive Storage Backend Configuration
# ------------------------------------------------
# Connects Minamo to Microsoft OneDrive as a cloud storage tier (experimental).
# Static, operator-authored developer credentials live here.
# The filename format must be storage-<id>.toml, where <id> matches the `id` field.

# Pluggable backend type. Must be "onedrive".
type = "onedrive"

# A unique backend instance identifier within Minamo.
id = "onedrive"

# Azure Active Directory (AAD) Application Client ID.
client_id = ""

# Azure Active Directory (AAD) Application Client Secret.
client_secret = ""

# Filepath where Minamo stores OAuth refresh and access tokens.
# Auto-refreshed credentials are autonomously updated here.
# Default: "data/state/onedrive.json"
state_file = "data/state/onedrive.json"
"""

STORAGE_R2_TOML = """\
# Cloudflare R2 / S3-Compatible Storage Backend Configuration
# -----------------------------------------------------------
# Connects Minamo to Cloudflare R2 or any generic, standard S3-compatible backend.
# The filename format must be storage-<id>.toml, where <id> matches the `id` field.

# Pluggable backend type. Must be "r2".
type = "r2"

# A unique backend instance identifier within Minamo.
id = "r2"

# Fully qualified target S3 / R2 API Endpoint URL.
# Example: "https://<account_id>.r2.cloudflarestorage.com"
endpoint_url = "http://localhost:8010"

# Target access credentials.
access_key_id = "test-access-key"
secret_access_key = "test-secret-key"

# Destination bucket name in the remote backend.
bucket = "minamo-r2"

# S3 region name. Cloudflare R2 generally defaults to "auto".
region_name = "auto"
"""

HSM_TOML = """\
# Hierarchical Storage Management (HSM) Tiered Configuration
# ---------------------------------------------------------
# HSM enables automatic, transparent migration of data across multiple tiers of storage,
# prioritizing high-performance or low-latency media for frequently used files ("hot" data)
# and transitioning less active files ("cold" data) to cheaper, cloud-based backends.

# Globally enable or disable Hierarchical Storage Management.
# If false, Minamo directly puts/gets all objects to the single backend configured in app.toml.
# Default: false
enabled = true

# Overflow policy determines what happens when the hot tier (priority 0) capacity limits are hit.
# Options:
#  - "fallback": Automatically route new uploads to the next available tier.
#  - "reject": Immediately reject new uploads with an HTTP 507 (Insufficient Storage) error if limits are breached.
# Default: "reject"
overflow_policy = "fallback"

# Definitive storage tiers configured in order of descending hierarchy (highest priority is 0).
# Data moves from lower priority indexes (hotter) to higher priority indexes (colder) over time.
[[tiers]]
# Unique string identifier for this tier.
id = "tier0"

# Pluggable storage backend name linked to this tier (must match the name of a storage-*.toml file, minus the storage- prefix).
backend = "local_disk"

# Priority index. MUST be sequentially ordered (0, 1, 2...). 0 represents the hottest tier.
priority = 0

# Target capacity (in bytes). HSM background workers will aim to keep utilization below this watermark.
# 1000000000 = 1 GB
target_capacity = 1000000000

# High watermark (in bytes). Crossing this threshold triggers immediate automated background migrations to colder tiers.
# 1500000000 = 1.5 GB
high_watermark = 1500000000

# Hard limit (in bytes). If the tier's storage exceeds this, write operations are rejected or fallback based on the overflow policy.
# 2000000000 = 2 GB
limit = 2000000000

# Minimum residency duration (in seconds). Objects must reside on this tier for at least this period before becoming eligible for cold migration.
# Default: 0 (immediate eligibility)
minimum_residency = 60

[[tiers]]
# Next colder storage tier.
id = "tier1"

# Typically uses a cloud-based storage backend like Cloudflare R2 or Amazon S3.
backend = "r2"

priority = 1

# Colder tiers generally do not enforce hard storage watermarks.
# They act as the terminal point (final stop) for cold migration.
minimum_residency = 0

# Example of configuring an additional super-cold tier (e.g., HackClub CDN)
# [[tiers]]
# id = "tier2"
# backend = "cdn1"
# priority = 2
# minimum_residency = 0
"""

DEFAULT_FILES: dict[str, str] = {
    "app.toml": APP_TOML,
    "secrets.toml": SECRETS_TOML,
    "storage-localdisk.toml": STORAGE_LOCALDISK_TOML,
    "storage-onedrive.toml": STORAGE_ONEDRIVE_TOML,
    "storage-r2.toml": STORAGE_R2_TOML,
    "hsm.toml": HSM_TOML,
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

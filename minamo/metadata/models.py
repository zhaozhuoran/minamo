"""Metadata models.

Metadata is intentionally decoupled from object *data*. The MVP persists it in
a SQLite database; a future version could back this with a separate service
without touching the storage backends or the S3 service layer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional


@dataclass
class BucketInfo:
    name: str
    created_at: datetime
    backend: str = "local_disk"


@dataclass
class ObjectInfo:
    key: str
    size: int
    etag: str
    content_type: str = "application/octet-stream"
    last_modified: datetime = field(default_factory=datetime.now)
    storage_class: str = "STANDARD"
    content_encoding: Optional[str] = None
    expires: Optional[str] = None
    metadata: Dict[str, str] = field(default_factory=dict)
    current_tier: str = "tier0"
    migration_state: str = "idle"
    heat_score: float = 100.0


@dataclass
class PartInfo:
    part_number: int
    etag: str
    size: int


@dataclass
class UploadInfo:
    upload_id: str
    bucket: str
    key: str
    created_at: datetime
    content_type: str = "application/octet-stream"
    metadata: Dict[str, str] = field(default_factory=dict)


@dataclass
class ListObjectsResult:
    objects: List[ObjectInfo]
    common_prefixes: List[str]
    is_truncated: bool
    next_continuation_token: Optional[str] = None

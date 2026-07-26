"""Time helpers for S3 date handling."""
from __future__ import annotations

from datetime import datetime, timezone


def now() -> datetime:
    return datetime.now(timezone.utc)


def http_date(dt: datetime | None = None) -> str:
    """RFC 1123 format used by the HTTP Date / Last-Modified headers."""
    if dt is None:
        dt = now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%a, %d %b %Y %H:%M:%S GMT")


def amz_date(dt: datetime | None = None) -> str:
    """AWS x-amz-date format: YYYYMMDDTHHMMSSZ."""
    if dt is None:
        dt = now()
    return dt.strftime("%Y%m%dT%H%M%SZ")


def amz_date_short(dt: datetime | None = None) -> str:
    """AWS date stamp: YYYYMMDD."""
    if dt is None:
        dt = now()
    return dt.strftime("%Y%m%d")

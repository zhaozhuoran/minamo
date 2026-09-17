"""Validation utilities for S3 request parameters."""
from __future__ import annotations

import re


def validate_bucket_name(bucket: str) -> bool:
    """Validate that a bucket name complies with AWS S3 naming rules.

    Rules enforced:
    - Bucket names must be between 3 and 63 characters long.
    - Bucket names can consist only of lowercase letters, numbers, dots (.), and hyphens (-).
    - Bucket names must begin and end with a letter or number.
    - Bucket names must not contain two adjacent periods (e.g. '..').
    - Bucket names must not be formatted as an IP address (e.g., 192.168.5.4).
    """
    if not bucket or not (3 <= len(bucket) <= 63):
        return False

    # Check for lowercase alphanumeric, dots, and hyphens.
    # Must start and end with an alphanumeric character.
    if not re.match(r"^[a-z0-9][a-z0-9.-]*[a-z0-9]$", bucket):
        return False

    # No adjacent periods (avoids path traversal via '..')
    if ".." in bucket:
        return False

    # Not formatted as an IP address
    if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", bucket):
        return False

    return True

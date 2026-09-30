"""Request target parsing.

S3 supports two addressing styles:

* path-style:  ``PUT /<bucket>/<key>``
* virtual-hosted: ``PUT /<key>`` with ``Host: <bucket>.<endpoint>``

This module resolves the effective bucket/key from the request so the rest of
the stack can stay addressing-style agnostic.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import unquote

from starlette.requests import Request

from ..config import Settings


logger = logging.getLogger("minamo.request")


@dataclass
class Target:
    bucket: str | None
    key: str | None
    # True when the request targets a bucket itself (create/delete/head/list),
    # as opposed to an object inside a bucket.
    is_bucket_op: bool


def resolve_target(request: Request, settings: Settings) -> Target:
    # S3 uses x-id=ListBuckets to list all buckets via the root path.
    # This overrides host-based and path-based bucket detection.
    q = request.query_params
    logger.debug("resolve_target: query_params=%s", dict(q.multi_items()))
    for key, val in q.multi_items():
        if key.lower() == "x-id" and val == "ListBuckets":
            logger.debug("resolve_target: x-id=ListBuckets detected, returning bucket=None")
            return Target(bucket=None, key=None, is_bucket_op=True)

    host = request.headers.get("host", "")
    if host.startswith("["):
        if "]" in host:
            host = host[1:host.index("]")]
    elif ":" in host and host.count(":") == 1:
        host = host.rsplit(":", 1)[0]

    endpoint = settings.endpoint_host
    if endpoint.startswith("["):
        if "]" in endpoint:
            endpoint = endpoint[1:endpoint.index("]")]
    elif ":" in endpoint and endpoint.count(":") == 1:
        endpoint = endpoint.rsplit(":", 1)[0]

    bucket_from_host: str | None = None
    if host and host != endpoint and host.endswith("." + endpoint):
        bucket_from_host = host[: -(len(endpoint) + 1)]

    raw_path = unquote(request.url.path)

    if bucket_from_host:
        # virtual-hosted style: host carries the bucket, path is the key
        key = raw_path.lstrip("/")
        if not key:
            return Target(bucket=bucket_from_host, key=None, is_bucket_op=True)
        return Target(bucket=bucket_from_host, key=key, is_bucket_op=False)

    # path-style
    path_clean = raw_path.rstrip("/")
    segments = [s for s in path_clean.split("/") if s != ""]
    if not segments:
        return Target(bucket=None, key=None, is_bucket_op=False)
    bucket = segments[0]
    if len(segments) == 1:
        return Target(bucket=bucket, key=None, is_bucket_op=True)

    # Preserve trailing slashes in object keys
    prefix_len = len("/" + bucket + "/")
    key = raw_path[prefix_len:] if len(raw_path) >= prefix_len else "/".join(segments[1:])
    return Target(bucket=bucket, key=key, is_bucket_op=False)

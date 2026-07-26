"""Request target parsing.

S3 supports two addressing styles:

* path-style:  ``PUT /<bucket>/<key>``
* virtual-hosted: ``PUT /<key>`` with ``Host: <bucket>.<endpoint>``

This module resolves the effective bucket/key from the request so the rest of
the stack can stay addressing-style agnostic.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import unquote

from starlette.requests import Request

from ..config import Settings


@dataclass
class Target:
    bucket: str | None
    key: str | None
    # True when the request targets a bucket itself (create/delete/head/list),
    # as opposed to an object inside a bucket.
    is_bucket_op: bool


def resolve_target(request: Request, settings: Settings) -> Target:
    host = request.headers.get("host", "")
    if ":" in host:
        host = host.split(":", 1)[0]
    endpoint = settings.endpoint_host

    bucket_from_host: str | None = None
    if host and host != endpoint and host.endswith("." + endpoint):
        bucket_from_host = host[: -(len(endpoint) + 1)]

    path = unquote(request.url.path)
    # Normalise: strip trailing slash so "/bucket/" == "/bucket".
    path = path.rstrip("/")

    if bucket_from_host:
        # virtual-hosted style: host carries the bucket, path is the key
        key = path.lstrip("/") if path != "" else ""
        key = key if key != "" else None
        if key is None:
            return Target(bucket=bucket_from_host, key=None, is_bucket_op=True)
        return Target(bucket=bucket_from_host, key=key, is_bucket_op=False)

    # path-style
    segments = [s for s in path.split("/") if s != ""]
    if not segments:
        return Target(bucket=None, key=None, is_bucket_op=False)
    bucket = segments[0]
    if len(segments) == 1:
        return Target(bucket=bucket, key=None, is_bucket_op=True)
    key = "/".join(segments[1:])
    return Target(bucket=bucket, key=key, is_bucket_op=False)

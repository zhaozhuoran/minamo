"""Presigned URL generation.

Serving presigned URLs is handled by the query-string branch of the SigV4
verifier in :mod:`minamo.api.auth`. This module lets Minamo *generate* them,
e.g. for a management client or internal use.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ..config import Settings
from ..utils.signing import presign_query
from ..utils.time import amz_date

presign_router = APIRouter()


class PresignRequest(BaseModel):
    bucket: str
    key: str
    expires: int = 3600
    method: str = "GET"


def build_presigned_url(
    base_url: str, settings: Settings, bucket: str, key: str, expires: int, method: str
) -> str:
    from urllib.parse import urlparse

    date = amz_date()
    host = urlparse(base_url).netloc
    query = presign_query(
        secret=settings.secret_key,
        access_key=settings.access_key,
        region=settings.region,
        service="s3",
        method=method,
        bucket=bucket,
        key=key,
        expires=expires,
        amz_date=date,
        host=host,
        signed_headers=["host"],
    )
    base = base_url.rstrip("/")
    return f"{base}/{bucket}/{key}?{query}"


@presign_router.post("/presign")
async def presign(payload: PresignRequest, request: Request) -> dict:
    settings: Settings = request.app.state.settings
    url = build_presigned_url(
        str(request.base_url),
        settings,
        payload.bucket,
        payload.key,
        payload.expires,
        payload.method,
    )
    return {"url": url}

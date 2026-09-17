"""Authentication dependency.

Verifies AWS Signature Version 4 (header or query/presigned) and decodes the
request body. The decoded raw payload is attached to ``request.state.body`` so
downstream handlers never re-read or re-parse the stream.

This is the *only* place that understands request signing; the service and
storage layers remain completely unaware of it.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from starlette.requests import Request

from ..config import Settings
from ..service.errors import S3Error, access_denied, signature_does_not_match
from ..utils.signing import (
    CHUNK_ALGORITHM,
    parse_chunked_body,
    verify,
    verify_chunk_signatures,
)
from .request import resolve_target


@dataclass
class AuthResult:
    body: bytes
    request_id: str


def _collect_headers(request: Request) -> dict[str, str]:
    headers: dict[str, str] = {}
    for name, value in request.headers.raw:
        lname = name.decode("latin-1").lower()
        decoded = value.decode("latin-1")
        if lname in headers:
            headers[lname] += ", " + decoded
        else:
            headers[lname] = decoded
    return headers


async def s3_auth(request: Request) -> AuthResult:
    settings: Settings = request.app.state.settings
    request_id = uuid.uuid4().hex
    request.state.request_id = request_id
    headers = _collect_headers(request)
    body = await request.body()
    content_sha256 = headers.get("x-amz-content-sha256", "")
    is_streaming = content_sha256 == CHUNK_ALGORITHM

    target = resolve_target(request, settings)
    request.state.target = target
    request.state.settings = settings

    if settings.enforce_signature:
        try:
            ctx = verify(
                secret=settings.secret_key,
                method=request.method,
                path=request.url.path,
                query=request.query_params,
                headers=headers,
                body=body,
                region=settings.region,
                service="s3",
            )
        except ValueError:
            raise signature_does_not_match()

        if is_streaming:
            try:
                body = verify_chunk_signatures(
                    secret=settings.secret_key,
                    body=body,
                    header_signature=ctx.signature,
                    amz_date=ctx.amz_date,
                    scope="",
                    region=ctx.region,
                    service=ctx.service,
                )
            except ValueError:
                raise signature_does_not_match()
        elif content_sha256 and content_sha256 not in (
            "UNSIGNED-PAYLOAD",
            "STREAMING-AWS4-HMAC-SHA256-PAYLOAD",
        ):
            from ..utils.signing import _sha256_hex

            if _sha256_hex(body) != content_sha256:
                raise S3Error(
                    "XAmzContentChecksumMismatch",
                    "The provided 'x-amz-content-sha256' header does not match the "
                    "computed payload hash.",
                    400,
                )
        # future: enforce ctx.access_key against an IAM store -> access_denied()
        pass
    else:
        if is_streaming:
            body = parse_chunked_body(body)

    request.state.body = body
    return AuthResult(body=body, request_id=request_id)

"""Authentication dependency.

Verifies AWS Signature Version 4 (header or query/presigned) and decodes the
request body. The decoded raw payload is attached to ``request.state.body`` so
downstream handlers never re-read or re-parse the stream.

This is the *only* place that understands request signing; the service and
storage layers remain completely unaware of it.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from starlette.requests import Request

from ..config import Settings
from ..service.errors import S3Error, access_denied, signature_does_not_match
from ..utils.signing import (
    CHUNK_ALGORITHM,
    _parse_authorization,
    _parse_query_auth,
    parse_chunked_body,
    verify,
    verify_chunk_signatures,
)
from .request import resolve_target

logger = logging.getLogger("minamo.auth")


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


def _extract_request_region(headers: dict[str, str], query_params: dict) -> str | None:
    try:
        if "authorization" in headers:
            ctx, _ = _parse_authorization(headers["authorization"])
            return ctx.region
        for key in ("X-Amz-Credential", "x-amz-credential"):
            if key in query_params:
                ctx, _ = _parse_query_auth({k: v for k, v in query_params.items()})
                return ctx.region
    except Exception:
        pass
    return None


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
    logger.debug("auth: method=%s url=%s target.bucket=%s target.key=%s enforce_signature=%s region=%s",
                  request.method, str(request.url), target.bucket, target.key,
                  settings.enforce_signature, settings.region)

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
            logger.debug("auth: signature verified OK, access_key=%s region=%s", ctx.access_key, ctx.region)
        except ValueError as exc:
            msg = str(exc)
            if "expired" in msg.lower():
                logger.warning("auth: request expired, access denied")
                raise S3Error("AccessDenied", "Request has expired", 403)
            if "Region mismatch" in msg:
                req_region = _extract_request_region(headers, request.query_params)
                logger.warning("auth: region mismatch - configured=%s request_credential_region=%s",
                               settings.region, req_region)
            else:
                logger.warning("auth: signature verification failed: %s", msg)
            raise signature_does_not_match()

        if ctx.access_key != settings.access_key:
            logger.warning("auth: access key mismatch, expected=%s got=%s",
                           settings.access_key, ctx.access_key)
            raise S3Error(
                "InvalidAccessKeyId",
                "The AWS Access Key Id you provided does not exist in our records.",
                403,
            )

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
                logger.warning("auth: chunk signature verification failed")
                raise signature_does_not_match()
        elif content_sha256 and content_sha256 not in (
            "UNSIGNED-PAYLOAD",
            "STREAMING-AWS4-HMAC-SHA256-PAYLOAD",
        ):
            from ..utils.signing import _sha256_hex

            if _sha256_hex(body) != content_sha256:
                logger.warning("auth: content sha256 mismatch")
                raise S3Error(
                    "XAmzContentChecksumMismatch",
                    "The provided 'x-amz-content-sha256' header does not match the "
                    "computed payload hash.",
                    400,
                )
    else:
        if is_streaming:
            body = parse_chunked_body(body)
        logger.debug("auth: signature enforcement disabled")

    request.state.body = body
    return AuthResult(body=body, request_id=request_id)

"""AWS Signature Version 4 (SigV4) helpers.

This module implements the SigV4 algorithm used by the S3 REST API for both
header authentication and query-string (presigned URL) authentication,
including the AWS chunked (`STREAMING-AWS4-HMAC-SHA256-PAYLOAD`) payload
encoding that real SDKs such as boto3 emit for streamed uploads.

The implementation mirrors botocore's canonicalisation rules so that standard
S3 SDKs can talk to Minamo without modification.
"""
from __future__ import annotations

import binascii
import hashlib
import hmac
from dataclasses import dataclass
from typing import Dict
from urllib.parse import quote

ALGORITHM = "AWS4-HMAC-SHA256"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
CHUNK_ALGORITHM = "AWS4-HMAC-SHA256-PAYLOAD"


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_bytes(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def signature_key(secret: str, datestamp: str, region: str, service: str) -> bytes:
    k_date = _hmac_sha256(("AWS4" + secret).encode("utf-8"), datestamp)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, service)
    k_signing = _hmac_sha256(k_service, "aws4_request")
    return k_signing


def aws_uri_encode(value: str, safe: str = "-_.~") -> str:
    """Percent-encode a string using the AWS SigV4 UriEncode rules."""
    return quote(value, safe=safe)


def _canonical_headers(headers: Dict[str, str]) -> tuple[str, str]:
    """Return (canonical_headers_string, signed_headers_string).

    `headers` must already be lower-cased and contain exactly the headers that
    are to be signed (incl. host, x-amz-* etc.). Values are stripped of leading
    and trailing whitespace and runs of internal whitespace are collapsed.
    """
    items = []
    for name in sorted(headers):
        value = " ".join(headers[name].split())
        items.append((name, value))
    canonical = "".join(f"{n}:{v}\n" for n, v in items)
    signed = ";".join(n for n, _ in items)
    return canonical, signed


def _canonical_request(
    method: str,
    canonical_uri: str,
    canonical_query: str,
    canonical_headers: str,
    signed_headers: str,
    payload_hash: str,
) -> str:
    return "\n".join(
        [
            method,
            canonical_uri,
            canonical_query,
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )


def _string_to_sign(amz_date: str, scope: str, canonical_request: str) -> str:
    return "\n".join(
        [
            ALGORITHM,
            amz_date,
            scope,
            _sha256_hex(canonical_request.encode("utf-8")),
        ]
    )


@dataclass
class AuthContext:
    access_key: str
    datestamp: str
    region: str
    service: str
    signed_headers: str
    signature: str
    amz_date: str


def _parse_authorization(header: str) -> tuple[AuthContext, str]:
    """Parse an ``Authorization: AWS4-HMAC-SHA256 ...`` header.

    Returns (context, credential_scope).
    """
    prefix = ALGORITHM + " "
    if not header.startswith(prefix):
        raise ValueError("Unsupported authorization algorithm")
    parts = header[len(prefix):].split(",")
    cred = None
    signed_headers = None
    signature = None
    for part in parts:
        part = part.strip()
        if part.startswith("Credential="):
            cred = part[len("Credential="):]
        elif part.startswith("SignedHeaders="):
            signed_headers = part[len("SignedHeaders="):]
        elif part.startswith("Signature="):
            signature = part[len("Signature="):]
    if not (cred and signed_headers and signature):
        raise ValueError("Malformed authorization header")
    ak, datestamp, region, service, _ = cred.split("/")
    scope = "/".join([datestamp, region, service, "aws4_request"])
    ctx = AuthContext(ak, datestamp, region, service, signed_headers, signature, "")
    return ctx, scope


def _parse_query_auth(query: Dict[str, str]) -> tuple[AuthContext, str]:
    algorithm = query.get("X-Amz-Algorithm", ALGORITHM)
    if algorithm != ALGORITHM:
        raise ValueError("Unsupported authorization algorithm")
    credential = query["X-Amz-Credential"]
    ak, datestamp, region, service, _ = credential.split("/")
    scope = "/".join([datestamp, region, service, "aws4_request"])
    ctx = AuthContext(
        ak,
        datestamp,
        region,
        service,
        query.get("X-Amz-SignedHeaders", ""),
        query["X-Amz-Signature"],
        query.get("X-Amz-Date", ""),
    )
    return ctx, scope


def _canonical_query_string(query: Dict[str, str], exclude: set[str]) -> str:
    exclude_lower = {e.lower() for e in exclude}
    items = []
    for key in query:
        if key.lower() in exclude_lower:
            continue
        encoded_key = aws_uri_encode(key)
        values = query.getlist(key) if hasattr(query, "getlist") else [query[key]]
        for value in values:
            encoded_value = aws_uri_encode(value)
            items.append(f"{encoded_key}={encoded_value}")
    items.sort()
    return "&".join(items)


def _build_payload_hash(
    body: bytes, content_sha256: str, is_streaming: bool
) -> str:
    if is_streaming:
        return CHUNK_ALGORITHM
    if content_sha256 == "UNSIGNED-PAYLOAD":
        return "UNSIGNED-PAYLOAD"
    if content_sha256:
        return content_sha256
    return _sha256_hex(body)


def verify(
    *,
    secret: str,
    method: str,
    path: str,
    query: Dict[str, str],
    headers: Dict[str, str],
    body: bytes,
    region: str | None = None,
    service: str = "s3",
    verify_expires: bool = True,
) -> AuthContext:
    """Verify a SigV4 request (header or query) and return the auth context.

    Raises ``ValueError`` when the signature does not match.
    """
    try:
        content_sha256 = headers.get("x-amz-content-sha256", "")
        is_streaming = content_sha256 == CHUNK_ALGORITHM
        is_query = "authorization" not in headers

        if is_query:
            ctx, scope = _parse_query_auth(query)
            signed_headers_list = ctx.signed_headers.split(";") if ctx.signed_headers else []
            exclude = {"X-Amz-Signature", "x-id"}
            exp_val = None
            if hasattr(query, "get"):
                exp_val = query.get("X-Amz-Expires") or query.get("x-amz-expires")
            if verify_expires and exp_val and ctx.amz_date:
                try:
                    expires_sec = int(exp_val)
                    from datetime import datetime, timezone
                    req_time = datetime.strptime(ctx.amz_date, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                    now = datetime.now(timezone.utc)
                    if (now - req_time).total_seconds() > expires_sec:
                        raise ValueError("Request has expired")
                except ValueError as ve:
                    if "expired" in str(ve).lower():
                        raise
        else:
            ctx, scope = _parse_authorization(headers["authorization"])
            ctx.amz_date = headers.get("x-amz-date", "")
            if not ctx.amz_date and "date" in headers:
                from email.utils import parsedate_to_datetime
                try:
                    dt = parsedate_to_datetime(headers["date"])
                    ctx.amz_date = dt.strftime("%Y%m%dT%H%M%SZ")
                except Exception:
                    pass
            signed_headers_list = ctx.signed_headers.split(";")
            exclude = set()
    except KeyError as e:
        raise ValueError(f"Missing required authentication header or query parameter: {e}")

    effective_region = ctx.region
    if region is not None and ctx.region != "auto" and ctx.region != region:
        # Region is taken from the credential scope; mismatch means forged scope.
        raise ValueError("Region mismatch")

    canonical_uri = aws_uri_encode(path, safe="/-_.~")
    canonical_query = _canonical_query_string(query, exclude)

    sign_headers: Dict[str, str] = {}
    for name in signed_headers_list:
        if name in headers:
            sign_headers[name] = headers[name]
    canonical_headers, signed_headers = _canonical_headers(sign_headers)

    if is_streaming:
        payload_hash = CHUNK_ALGORITHM
    elif is_query:
        # Presigned URLs always use UNSIGNED-PAYLOAD as the canonical payload hash.
        payload_hash = "UNSIGNED-PAYLOAD"
    elif content_sha256:
        payload_hash = content_sha256
    else:
        payload_hash = _sha256_hex(body)
    cr = _canonical_request(
        method.upper(),
        canonical_uri,
        canonical_query,
        canonical_headers,
        signed_headers,
        payload_hash,
    )
    sts = _string_to_sign(ctx.amz_date, scope, cr)
    signing_key = signature_key(secret, ctx.datestamp, effective_region, ctx.service)
    expected = hmac.new(signing_key, sts.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, ctx.signature):
        raise ValueError("Signature mismatch")
    return ctx


def parse_chunked_body(body: bytes) -> bytes:
    """Decode an AWS-chunked ``STREAMING-AWS4-HMAC-SHA256-PAYLOAD`` body.

    Returns the reconstructed raw payload bytes. Per-chunk signatures are
    verified by the caller via :func:`verify_chunk_signatures`.
    """
    data = bytearray()
    i = 0
    n = len(body)
    while i < n:
        line_end = body.find(b"\r\n", i)
        if line_end == -1:
            break
        size_line = body[i:line_end]
        # Format: "<hex-size>;chunk-signature=<sig>"
        chunk_meta = size_line.decode("ascii")
        size_hex = chunk_meta.split(";")[0]
        try:
            chunk_size = int(size_hex, 16)
        except ValueError:
            raise ValueError("Malformed chunk size")
        i = line_end + 2
        if chunk_size == 0:
            # final chunk: signature line then trailing CRLF
            i = body.find(b"\r\n", i)
            if i != -1:
                i += 2
            break
        data += body[i : i + chunk_size]
        i += chunk_size
        # consume trailing CRLF after chunk data
        if i + 1 < n and body[i : i + 2] == b"\r\n":
            i += 2
    return bytes(data)


def verify_chunk_signatures(
    *,
    secret: str,
    body: bytes,
    header_signature: str,
    amz_date: str,
    scope: str,
    region: str,
    service: str,
) -> bytes:
    """Verify AWS-chunked signatures and return the decoded payload.

    The first chunk is signed with the header ``Signature``; each subsequent
    chunk is signed with the previous chunk signature.
    """
    signing_key = signature_key(secret, amz_date[:8], region, service)
    prev_signature = header_signature
    data = bytearray()
    i = 0
    n = len(body)
    datestamp = amz_date[:8]
    full_scope = "/".join([datestamp, region, service, "aws4_request"])

    while i < n:
        line_end = body.find(b"\r\n", i)
        if line_end == -1:
            break
        chunk_meta = body[i:line_end].decode("ascii")
        parts = chunk_meta.split(";")
        chunk_size = int(parts[0], 16)
        chunk_sig = ""
        for p in parts[1:]:
            if p.startswith("chunk-signature="):
                chunk_sig = p[len("chunk-signature="):]
        i = line_end + 2
        if chunk_size == 0:
            # verify final chunk signature over empty payload
            _verify_one_chunk(
                signing_key, prev_signature, amz_date, full_scope, b"", chunk_sig
            )
            break
        chunk_data = body[i : i + chunk_size]
        _verify_one_chunk(
            signing_key, prev_signature, amz_date, full_scope, chunk_data, chunk_sig
        )
        prev_signature = chunk_sig
        data += chunk_data
        i += chunk_size
        if i + 1 < n and body[i : i + 2] == b"\r\n":
            i += 2
    return bytes(data)


def _verify_one_chunk(
    signing_key: bytes,
    previous_signature: str,
    amz_date: str,
    scope: str,
    chunk_data: bytes,
    chunk_sig: str,
) -> None:
    string_to_sign = "\n".join(
        [
            CHUNK_ALGORITHM,
            amz_date,
            scope,
            previous_signature,
            EMPTY_SHA256,
            _sha256_hex(chunk_data),
        ]
    )
    key = binascii.unhexlify(previous_signature)
    expected = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, chunk_sig):
        raise ValueError("Chunk signature mismatch")


def presign_query(
    *,
    secret: str,
    access_key: str,
    region: str,
    service: str,
    method: str,
    bucket: str,
    key: str,
    expires: int,
    amz_date: str,
    host: str,
    signed_headers: list[str] | None = None,
) -> str:
    """Build a presigned-URL query string for a given object operation.

    The resulting query string can be appended to the object URL. Minamo
    verifies it via :func:`verify` using the query branch.
    """
    datestamp = amz_date[:8]
    scope = "/".join([datestamp, region, service, "aws4_request"])
    credential = f"{access_key}/{scope}"
    if signed_headers is None:
        signed_headers = ["host"]
    signed_headers_str = ";".join(signed_headers)

    params: dict[str, str] = {}
    params["X-Amz-Algorithm"] = ALGORITHM
    params["X-Amz-Credential"] = credential
    params["X-Amz-Date"] = amz_date
    params["X-Amz-Expires"] = str(expires)
    params["X-Amz-SignedHeaders"] = signed_headers_str

    canonical_uri = aws_uri_encode(f"/{bucket}/{key}".rstrip("/") or "/", safe="/-_.~")
    canonical_query = _canonical_query_string(params, exclude={"X-Amz-Signature"})
    header_dict = {h: (host if h == "host" else "") for h in signed_headers}
    canonical_headers, _ = _canonical_headers(header_dict)
    payload_hash = "UNSIGNED-PAYLOAD"
    cr = _canonical_request(
        method.upper(),
        canonical_uri,
        canonical_query,
        canonical_headers,
        signed_headers_str,
        payload_hash,
    )
    sts = _string_to_sign(amz_date, scope, cr)
    signing_key = signature_key(secret, datestamp, region, service)
    signature = hmac.new(signing_key, sts.encode("utf-8"), hashlib.sha256).hexdigest()
    params["X-Amz-Signature"] = signature
    return _canonical_query_string(params, exclude=set())


def compute_etag(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()

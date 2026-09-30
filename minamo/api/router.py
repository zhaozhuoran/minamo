"""S3 HTTP API layer.

A single catch-all route dispatches every request to the right S3 operation.
Responsibilities of this layer are strictly:

* parse the S3 request (target, headers, query, body) via the auth dependency
* call the S3 service layer
* render S3-compatible XML / headers / status codes

It never touches the filesystem or the database directly. All storage and
metadata concerns live behind :class:`~minamo.service.s3_service.S3Service`.
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from typing import List, Tuple

from fastapi import APIRouter, Depends, Request, Response

from ..config import Settings
from ..service.errors import S3Error
from ..service.s3_service import S3Service
from ..utils.time import http_date
from .auth import AuthResult, s3_auth
from .responses import (
    complete_multipart_xml,
    create_bucket_xml,
    error_xml,
    get_bucket_location_xml,
    initiate_multipart_xml,
    list_buckets_xml,
    list_objects_v2_xml,
    list_parts_xml,
)

logger = logging.getLogger("minamo.router")

router = APIRouter()


def _service(request: Request) -> S3Service:
    return request.app.state.service


def _object_headers(info) -> dict[str, str]:
    headers = {
        "Content-Length": str(info.size),
        "ETag": '"' + info.etag + '"',
        "Last-Modified": http_date(info.last_modified),
        "Content-Type": info.content_type,
    }
    if info.content_encoding:
        headers["Content-Encoding"] = info.content_encoding
    if info.expires:
        headers["Expires"] = info.expires
    for k, v in info.metadata.items():
        headers["x-amz-meta-" + k] = v
    return headers


def _user_metadata(headers: dict[str, str]) -> dict[str, str]:
    meta: dict[str, str] = {}
    for name, value in headers.items():
        if name.startswith("x-amz-meta-"):
            meta[name[len("x-amz-meta-"):]] = value
    return meta


def _parse_range(header: str) -> tuple[int, int | None] | None:
    """Parse a single-range HTTP Range header.

    Supports ``bytes=START-END``, ``bytes=START-`` and ``bytes=-N`` (suffix).
    Multi-range requests fall back to ``None`` (caller serves the full object).
    """
    import re

    if "," in header:
        return None
    m = re.match(r"\s*bytes=(\d*)-(\d*)\s*$", header)
    if not m:
        return None
    start_str, end_str = m.group(1), m.group(2)
    if start_str == "" and end_str == "":
        return None
    if start_str == "":
        return (-int(end_str), None)  # suffix range; resolve against size later
    start = int(start_str)
    end = int(end_str) if end_str != "" else None
    return (start, end)


def _parse_complete_body(body: bytes) -> List[Tuple[int, str]]:
    from ..utils.xml import parse as parse_xml
    try:
        root = parse_xml(body)
    except Exception as e:
        raise ValueError(f"Invalid XML: {e}")

    def local(tag: str) -> str:
        return tag.split("}", 1)[1] if "}" in tag else tag

    parts: List[Tuple[int, str]] = []
    for part in root:
        if local(part.tag) != "Part":
            continue
        num = None
        etag = None
        for child in part:
            name = local(child.tag)
            if name == "PartNumber":
                num = int(child.text)
            elif name == "ETag":
                etag = child.text
        if num is not None:
            parts.append((num, etag))
    return parts


async def _dispatch(request: Request, auth: AuthResult = Depends(s3_auth)) -> Response:
    service = _service(request)
    settings: Settings = request.app.state.settings
    target = request.state.target
    q = request.query_params
    method = request.method

    logger.debug("dispatch: method=%s url=%s bucket=%s key=%s",
                  method, str(request.url), target.bucket, target.key)

    # --- service-level: list buckets ---------------------------------------
    if target.bucket is None:
        if method == "GET":
            logger.debug("dispatch: listing buckets, target.bucket=%s", target.bucket)
            try:
                buckets = await service.list_buckets()
                logger.debug("dispatch: listed %d buckets", len(buckets))
                body = list_buckets_xml(buckets)
                logger.debug("dispatch: list_buckets_xml body length=%d", len(body))
                logger.debug("dispatch: list_buckets_xml body=%s", body[:200])
                return Response(
                    body,
                    media_type="text/xml",
                )
            except Exception as exc:
                logger.exception("dispatch: failed to list buckets")
                raise
        return _error(S3Error("MethodNotAllowed", "Method not allowed.", 405), request)

    bucket = target.bucket

    # --- bucket-level operations -------------------------------------------
    if target.key is None:
        if method == "PUT":
            logger.debug("dispatch: create bucket=%s", bucket)
            await service.create_bucket(bucket)
            return Response(
                create_bucket_xml(),
                status_code=200,
                headers={"Location": "/" + bucket},
                media_type="application/xml",
            )
        if method == "DELETE":
            logger.debug("dispatch: delete bucket=%s", bucket)
            await service.delete_bucket(bucket)
            return Response(status_code=204)
        if method == "HEAD":
            logger.debug("dispatch: head bucket=%s", bucket)
            await service.head_bucket(bucket)
            return Response(
                status_code=200,
                headers={"x-amz-bucket-region": settings.region},
            )
        if method == "GET":
            if "location" in q or q.get("location") is not None:
                logger.debug("dispatch: get bucket location bucket=%s", bucket)
                await service.head_bucket(bucket)
                body = get_bucket_location_xml(settings.region)
                return Response(body, media_type="text/xml")

            prefix = q.get("prefix", "")
            delimiter = q.get("delimiter", "")
            max_keys = int(q.get("max-keys", "1000"))
            continuation = q.get("continuation-token")
            start_after = q.get("start-after", "")
            logger.debug("dispatch: list objects bucket=%s prefix=%s", bucket, prefix)
            result = await service.list_objects_v2(
                bucket,
                prefix=prefix,
                delimiter=delimiter,
                max_keys=max_keys,
                continuation_token=continuation,
                start_after=start_after,
            )
            body = list_objects_v2_xml(
                bucket, result, prefix, delimiter, max_keys, continuation
            )
            return Response(body, media_type="application/xml")
        return _error(S3Error("MethodNotAllowed", "Method not allowed.", 405), request)

    # --- object-level operations -------------------------------------------
    key = target.key
    if method == "PUT":
        if q.get("uploadId"):
            part_number = int(q.get("partNumber"))
            logger.debug("dispatch: upload part bucket=%s key=%s part=%d", bucket, key, part_number)
            etag = await service.upload_part(
                bucket, key, q.get("uploadId"), part_number, auth.body
            )
            return Response(status_code=200, headers={"ETag": '"' + etag + '"'})
        content_type = request.headers.get("content-type", "application/octet-stream")
        content_encoding = request.headers.get("content-encoding")
        if content_encoding == "aws-chunked":
            content_encoding = None
        storage_class = request.headers.get("x-amz-storage-class", "STANDARD")
        logger.debug("dispatch: put object bucket=%s key=%s", bucket, key)
        info = await service.put_object(
            bucket,
            key,
            auth.body,
            content_type=content_type,
            metadata=_user_metadata(request.headers),
            content_encoding=content_encoding,
            storage_class=storage_class,
        )
        return Response(status_code=200, headers={"ETag": '"' + info.etag + '"'})

    if method == "GET":
        if q.get("uploadId"):
            max_parts = int(q.get("max-parts", "1000"))
            marker = int(q.get("part-number-marker", "0"))
            logger.debug("dispatch: list parts bucket=%s key=%s upload_id=%s", bucket, key, q.get("uploadId"))
            parts = await service.list_parts(
                bucket, key, q.get("uploadId"), max_parts, marker
            )
            body = list_parts_xml(
                bucket, key, q.get("uploadId"), parts, max_parts, marker
            )
            return Response(body, media_type="application/xml")
        range_header = request.headers.get("range")
        if range_header:
            parsed = _parse_range(range_header)
            if parsed is not None:
                start, end = parsed
                logger.debug("dispatch: get object range bucket=%s key=%s start=%d end=%d", bucket, key, start, end)
                info, data, res_start, res_end = await service.get_object_range(
                    bucket, key, start, end
                )
                headers = _object_headers(info)
                headers["Content-Range"] = f"bytes {res_start}-{res_end}/{info.size}"
                headers["Content-Length"] = str(len(data))
                headers["Accept-Ranges"] = "bytes"
                return Response(content=data, status_code=206, headers=headers)
        logger.debug("dispatch: get object bucket=%s key=%s", bucket, key)
        info, stream = await service.get_object(bucket, key)
        data = b"".join([chunk async for chunk in stream])
        headers = _object_headers(info)
        headers["Accept-Ranges"] = "bytes"
        return Response(content=data, headers=headers)

    if method == "HEAD":
        logger.debug("dispatch: head object bucket=%s key=%s", bucket, key)
        info = await service.head_object(bucket, key)
        headers = _object_headers(info)
        headers["Accept-Ranges"] = "bytes"
        return Response(status_code=200, headers=headers)

    if method == "DELETE":
        if q.get("uploadId"):
            logger.debug("dispatch: abort multipart bucket=%s key=%s upload_id=%s", bucket, key, q.get("uploadId"))
            await service.abort_multipart_upload(bucket, key, q.get("uploadId"))
            return Response(status_code=204)
        logger.debug("dispatch: delete object bucket=%s key=%s", bucket, key)
        await service.delete_object(bucket, key)
        return Response(status_code=204)

    if method == "POST":
        if q.get("uploads") is not None:
            logger.debug("dispatch: create multipart upload bucket=%s key=%s", bucket, key)
            content_type = request.headers.get(
                "content-type", "application/octet-stream"
            )
            upload = await service.create_multipart_upload(
                bucket, key, content_type=content_type,
                metadata=_user_metadata(request.headers),
            )
            return Response(
                initiate_multipart_xml(upload),
                media_type="application/xml",
            )
        if q.get("uploadId"):
            parts = _parse_complete_body(auth.body)
            logger.debug("dispatch: complete multipart upload bucket=%s key=%s upload_id=%s", bucket, key, q.get("uploadId"))
            info = await service.complete_multipart_upload(
                bucket, key, q.get("uploadId"), parts
            )
            location = (
                f"http://{bucket}.{settings.endpoint_host}/{key}"
            )
            body = complete_multipart_xml(bucket, key, info.etag, location)
            return Response(
                body,
                media_type="application/xml",
                headers={"ETag": '"' + info.etag + '"'},
            )

    logger.warning("dispatch: method not allowed: %s %s", method, request.url.path)
    return _error(S3Error("MethodNotAllowed", "Method not allowed.", 405), request)


def _error(err: S3Error, request: Request) -> Response:
    request_id = getattr(request.state, "request_id", "unknown")
    resource = request.url.path
    body = error_xml(err.code, err.message, resource, request_id)
    return Response(body, status_code=err.http_status, media_type="application/xml")


router.add_api_route(
    "/{full_path:path}", _dispatch, methods=["GET", "PUT", "POST", "DELETE", "HEAD"]
)
router.add_api_route(
    "/", _dispatch, methods=["GET", "PUT", "POST", "DELETE", "HEAD"]
)

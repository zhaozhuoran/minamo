"""S3 XML response builders.

Each function returns the XML body expected by standard S3 clients. Keeping
these in one place guarantees the wire format matches AWS references.
"""
from __future__ import annotations

from xml.sax.saxutils import escape

from ..metadata.models import ListObjectsResult, ObjectInfo, PartInfo, UploadInfo
from ..utils.time import http_date
from ..utils.xml import element


def _quote_etag(etag: str) -> str:
    if etag.startswith('"') and etag.endswith('"'):
        return etag
    return '"' + etag + '"'


def error_xml(code: str, message: str, resource: str, request_id: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Error>'
        + element("Code", code)
        + element("Message", message)
        + element("Resource", resource)
        + element("RequestId", request_id)
        + "</Error>"
    )


def create_bucket_xml() -> str:
    return '<?xml version="1.0" encoding="UTF-8"?><CreateBucketResult/>'


def list_buckets_xml(buckets) -> str:
    items = []
    for b in buckets:
        items.append(
            "<Bucket>"
            + element("Name", b.name)
            + element("CreationDate", http_date(b.created_at))
            + "</Bucket>"
        )
    inner = "".join(items)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<ListAllMyBucketsResult '
        'xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        "<Owner><ID>minamo</ID><DisplayName>minamo</DisplayName></Owner>"
        "<Buckets>" + inner + "</Buckets>"
        "</ListAllMyBucketsResult>"
    )


def list_objects_v2_xml(
    bucket: str,
    result: ListObjectsResult,
    prefix: str,
    delimiter: str,
    max_keys: int,
    continuation_token: str | None,
) -> str:
    parts = [
        element("Name", bucket),
        element("Prefix", prefix),
        element("KeyCount", str(len(result.objects) + len(result.common_prefixes))),
        element("MaxKeys", str(max_keys)),
    ]
    if delimiter:
        parts.append(element("Delimiter", delimiter))
    parts.append(element("IsTruncated", "true" if result.is_truncated else "false"))
    if continuation_token:
        parts.append(element("ContinuationToken", continuation_token))
    if result.is_truncated and result.next_continuation_token:
        parts.append(element("NextContinuationToken", result.next_continuation_token))

    contents = []
    for obj in result.objects:
        contents.append(
            "<Contents>"
            + element("Key", obj.key)
            + element("LastModified", http_date(obj.last_modified))
            + element("ETag", _quote_etag(obj.etag))
            + element("Size", str(obj.size))
            + element("StorageClass", obj.storage_class)
            + "</Contents>"
        )
    common = []
    for cp in result.common_prefixes:
        common.append("<CommonPrefixes>" + element("Prefix", cp) + "</CommonPrefixes>")
    body = "".join(parts) + "".join(contents) + "".join(common)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        + body
        + "</ListBucketResult>"
    )


def initiate_multipart_xml(upload: UploadInfo) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<InitiateMultipartUploadResult "
        'xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        + element("Bucket", upload.bucket)
        + element("Key", upload.key)
        + element("UploadId", upload.upload_id)
        + "</InitiateMultipartUploadResult>"
    )


def list_parts_xml(
    bucket: str,
    key: str,
    upload_id: str,
    parts: list[PartInfo],
    max_parts: int,
    part_number_marker: int,
) -> str:
    truncated = len(parts) >= max_parts
    next_marker = parts[-1].part_number if truncated and parts else 0
    head = (
        element("Bucket", bucket)
        + element("Key", key)
        + element("UploadId", upload_id)
        + element("PartNumberMarker", str(part_number_marker))
        + element("NextPartNumberMarker", str(next_marker))
        + element("MaxParts", str(max_parts))
        + element("IsTruncated", "true" if truncated else "false")
    )
    body = ""
    for p in parts:
        body += (
            "<Part>"
            + element("PartNumber", str(p.part_number))
            + element("LastModified", http_date(_now()))
            + element("ETag", _quote_etag(p.etag))
            + element("Size", str(p.size))
            + "</Part>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<ListPartsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        + head
        + body
        + "</ListPartsResult>"
    )


def complete_multipart_xml(bucket: str, key: str, etag: str, location: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<CompleteMultipartUploadResult "
        'xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        + element("Location", location)
        + element("Bucket", bucket)
        + element("Key", key)
        + element("ETag", _quote_etag(etag))
        + "</CompleteMultipartUploadResult>"
    )


def _now():
    from ..utils.time import now

    return now()

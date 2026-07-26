"""Domain errors that map 1:1 to S3 error codes.

The HTTP layer catches :class:`S3Error` and renders the canonical S3 XML error
document, so error behaviour stays consistent with AWS.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class S3Error(Exception):
    code: str
    message: str
    http_status: int = 400
    bucket: str | None = None
    key: str | None = None


def no_such_bucket(bucket: str) -> S3Error:
    return S3Error(
        "NoSuchBucket",
        "The specified bucket does not exist.",
        404,
        bucket=bucket,
    )


def no_such_key(key: str, bucket: str) -> S3Error:
    return S3Error(
        "NoSuchKey",
        "The specified key does not exist.",
        404,
        bucket=bucket,
        key=key,
    )


def no_such_upload(upload_id: str) -> S3Error:
    return S3Error(
        "NoSuchUpload",
        "The specified multipart upload does not exist.",
        404,
    )


def bucket_already_exists(bucket: str) -> S3Error:
    return S3Error(
        "BucketAlreadyExists",
        "The requested bucket name is not available.",
        409,
        bucket=bucket,
    )


def bucket_not_empty(bucket: str) -> S3Error:
    return S3Error(
        "BucketNotEmpty",
        "The bucket you tried to delete is not empty.",
        409,
        bucket=bucket,
    )


def invalid_part() -> S3Error:
    return S3Error(
        "InvalidPart",
        "One or more of the specified parts could not be found.",
        400,
    )


def invalid_part_order() -> S3Error:
    return S3Error(
        "InvalidPartOrder",
        "The list of parts was not in ascending order.",
        400,
    )


def access_denied() -> S3Error:
    return S3Error("AccessDenied", "Access Denied.", 403)


def signature_does_not_match() -> S3Error:
    return S3Error(
        "SignatureDoesNotMatch",
        "The request signature we calculated does not match the signature you provided.",
        403,
    )


def invalid_argument(message: str) -> S3Error:
    return S3Error("InvalidArgument", message, 400)

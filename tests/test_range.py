"""Range GET compatibility tests (boto3).

Range reads are required for standard clients such as rclone and the AWS CLI
(`aws s3 cp` of large files), which fetch objects in chunks via Range requests.
"""
from __future__ import annotations

import pytest
from botocore.exceptions import ClientError


def test_range_middle(s3, unique_bucket):
    payload = bytes(range(256)) * 10  # 2560 bytes, deterministic
    s3.put_object(Bucket=unique_bucket, Key="data.bin", Body=payload)
    resp = s3.get_object(Bucket=unique_bucket, Key="data.bin", Range="bytes=10-19")
    assert resp["Body"].read() == payload[10:20]
    assert resp["ContentRange"] == f"bytes 10-19/{len(payload)}"
    assert resp["ContentLength"] == 10
    assert resp["ResponseMetadata"]["HTTPStatusCode"] == 206


def test_range_open_end(s3, unique_bucket):
    payload = b"abcdefghij" * 10  # 100 bytes
    s3.put_object(Bucket=unique_bucket, Key="k", Body=payload)
    resp = s3.get_object(Bucket=unique_bucket, Key="k", Range="bytes=95-")
    assert resp["Body"].read() == payload[95:]


def test_range_suffix(s3, unique_bucket):
    payload = b"x" * 100
    s3.put_object(Bucket=unique_bucket, Key="k", Body=payload)
    resp = s3.get_object(Bucket=unique_bucket, Key="k", Range="bytes=-10")
    assert resp["Body"].read() == payload[-10:]


def test_get_returns_accept_ranges(s3, unique_bucket):
    s3.put_object(Bucket=unique_bucket, Key="k", Body=b"hello")
    resp = s3.get_object(Bucket=unique_bucket, Key="k")
    assert resp["ResponseMetadata"]["HTTPHeaders"].get("accept-ranges") == "bytes"


def test_range_unsatisfiable(s3, unique_bucket):
    s3.put_object(Bucket=unique_bucket, Key="k", Body=b"abc")
    with pytest.raises(ClientError) as exc:
        s3.get_object(Bucket=unique_bucket, Key="k", Range="bytes=100-200")
    assert exc.value.response["Error"]["Code"] == "InvalidRange"
    assert exc.value.response["ResponseMetadata"]["HTTPStatusCode"] == 416

"""Object operation compatibility tests (boto3)."""
from __future__ import annotations

import pytest
from botocore.exceptions import ClientError


def test_put_and_get_object(s3, unique_bucket):
    s3.put_object(Bucket=unique_bucket, Key="hello.txt", Body=b"hello world", ContentType="text/plain")
    resp = s3.get_object(Bucket=unique_bucket, Key="hello.txt")
    assert resp["Body"].read() == b"hello world"
    assert resp["ContentLength"] == 11
    assert resp["ContentType"] == "text/plain"
    assert resp["ETag"].startswith('"')


def test_head_object(s3, unique_bucket):
    s3.put_object(Bucket=unique_bucket, Key="k", Body=b"abcd")
    resp = s3.head_object(Bucket=unique_bucket, Key="k")
    assert resp["ContentLength"] == 4


def test_key_with_trailing_slash(s3, unique_bucket):
    folder_key = "folder/sub/"
    s3.put_object(Bucket=unique_bucket, Key=folder_key, Body=b"")
    head_resp = s3.head_object(Bucket=unique_bucket, Key=folder_key)
    assert head_resp["ContentLength"] == 0

    get_resp = s3.get_object(Bucket=unique_bucket, Key=folder_key)
    assert get_resp["Body"].read() == b""

    # Test fallback head without trailing slash
    head_fallback = s3.head_object(Bucket=unique_bucket, Key="folder/sub")
    assert head_fallback["ContentLength"] == 0


def test_virtual_directory_prefix_head_and_get(s3, unique_bucket):
    # Create an object inside a prefix directory without creating explicit folder object
    s3.put_object(Bucket=unique_bucket, Key="openrouter-traces/2026-09-27/trace1.json", Body=b"{}")

    # HEAD on prefix "openrouter-traces/" should succeed (200 OK) via virtual directory fallback
    head_resp = s3.head_object(Bucket=unique_bucket, Key="openrouter-traces/")
    assert head_resp["ContentLength"] == 0

    get_resp = s3.get_object(Bucket=unique_bucket, Key="openrouter-traces/")
    assert get_resp["Body"].read() == b""


def test_head_missing_object(s3, unique_bucket):
    # AWS returns 404 (not NoSuchKey) for HEAD on a missing key.
    with pytest.raises(ClientError) as exc:
        s3.head_object(Bucket=unique_bucket, Key="nope")
    assert exc.value.response["Error"]["Code"] == "404"


def test_delete_object(s3, unique_bucket):
    s3.put_object(Bucket=unique_bucket, Key="k", Body=b"x")
    s3.delete_object(Bucket=unique_bucket, Key="k")
    with pytest.raises(ClientError) as exc:
        s3.head_object(Bucket=unique_bucket, Key="k")
    assert exc.value.response["Error"]["Code"] == "404"


def test_user_metadata_roundtrip(s3, unique_bucket):
    s3.put_object(Bucket=unique_bucket, Key="m", Body=b"x", Metadata={"foo": "bar"})
    resp = s3.head_object(Bucket=unique_bucket, Key="m")
    assert resp["Metadata"] == {"foo": "bar"}


def test_streaming_put_object(s3, unique_bucket, tmp_path):
    payload = b"x" * (1024 * 256)
    path = tmp_path / "big.bin"
    path.write_bytes(payload)
    # File-like body triggers AWS-chunked (STREAMING-AWS4-HMAC-SHA256-PAYLOAD).
    with open(path, "rb") as fh:
        s3.put_object(Bucket=unique_bucket, Key="big", Body=fh)
    resp = s3.get_object(Bucket=unique_bucket, Key="big")
    assert resp["Body"].read() == payload

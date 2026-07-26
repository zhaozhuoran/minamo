"""Bucket operation compatibility tests (boto3)."""
from __future__ import annotations

import pytest
from botocore.exceptions import ClientError


def test_create_and_head_bucket(s3):
    name = "test-bucket-1"
    s3.create_bucket(Bucket=name)
    resp = s3.head_bucket(Bucket=name)
    assert resp["ResponseMetadata"]["HTTPStatusCode"] == 200


def test_create_existing_bucket_conflicts(s3):
    name = "test-bucket-dup"
    s3.create_bucket(Bucket=name)
    with pytest.raises(s3.exceptions.BucketAlreadyExists):
        s3.create_bucket(Bucket=name)


def test_head_missing_bucket(s3):
    # AWS returns 404 for HEAD on a missing bucket (not NoSuchBucket).
    with pytest.raises(ClientError) as exc:
        s3.head_bucket(Bucket="does-not-exist-bucket")
    assert exc.value.response["Error"]["Code"] == "404"


def test_list_buckets(s3):
    name = "test-bucket-list"
    s3.create_bucket(Bucket=name)
    names = [b["Name"] for b in s3.list_buckets()["Buckets"]]
    assert name in names


def test_delete_bucket(s3):
    name = "test-bucket-del"
    s3.create_bucket(Bucket=name)
    s3.delete_bucket(Bucket=name)
    with pytest.raises(ClientError) as exc:
        s3.head_bucket(Bucket=name)
    assert exc.value.response["Error"]["Code"] == "404"


def test_delete_non_empty_bucket(s3):
    name = "test-bucket-notempty"
    s3.create_bucket(Bucket=name)
    s3.put_object(Bucket=name, Key="obj", Body=b"data")
    with pytest.raises(ClientError) as exc:
        s3.delete_bucket(Bucket=name)
    assert exc.value.response["Error"]["Code"] == "BucketNotEmpty"

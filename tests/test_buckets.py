"""Bucket operation compatibility tests (boto3)."""
from __future__ import annotations

import pytest
from botocore.exceptions import ClientError


def test_create_and_head_bucket(s3):
    name = "test-bucket-1"
    s3.create_bucket(Bucket=name)
    resp = s3.head_bucket(Bucket=name)
    assert resp["ResponseMetadata"]["HTTPStatusCode"] == 200


def test_get_bucket_location(s3):
    name = "test-bucket-location"
    s3.create_bucket(Bucket=name)
    resp = s3.get_bucket_location(Bucket=name)
    assert resp["ResponseMetadata"]["HTTPStatusCode"] == 200
    assert resp.get("LocationConstraint") == "us-east-1"


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


def test_create_invalid_bucket_name(s3):
    # Bucket names that are allowed by botocore client-side validation but are invalid per S3 specs
    for invalid_name in ["UPPER", "bu", "a" * 64, "192.168.1.1", "foo..bar", "foo_bar"]:
        with pytest.raises(ClientError) as exc:
            s3.create_bucket(Bucket=invalid_name)
        assert exc.value.response["Error"]["Code"] == "InvalidBucketName"
        assert exc.value.response["ResponseMetadata"]["HTTPStatusCode"] == 400


def test_operations_on_invalid_bucket_name(s3):
    # HEAD bucket operation on an invalid bucket name should return HTTP 400 (parsed as "400" by boto3 for HEAD)
    for invalid_name in ["UPPER", "bu", "a" * 64, "192.168.1.1", "foo..bar", "foo_bar"]:
        with pytest.raises(ClientError) as exc:
            s3.head_bucket(Bucket=invalid_name)
        assert exc.value.response["Error"]["Code"] == "400"
        assert exc.value.response["ResponseMetadata"]["HTTPStatusCode"] == 400


def test_create_valid_bucket_name_variants(s3):
    # AWS S3 allows dots and hyphens in bucket names, starts/ends with alphanumeric
    for valid_name in ["my-bucket.name-123", "123-bucket", "bucket.name.dot"]:
        s3.create_bucket(Bucket=valid_name)
        resp = s3.head_bucket(Bucket=valid_name)
        assert resp["ResponseMetadata"]["HTTPStatusCode"] == 200

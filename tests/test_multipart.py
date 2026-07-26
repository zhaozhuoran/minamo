"""Multipart upload compatibility tests (boto3)."""
from __future__ import annotations


def test_multipart_upload_flow(s3):
    b = "mpu-bucket"
    s3.create_bucket(Bucket=b)
    key = "big-object"
    created = s3.create_multipart_upload(Bucket=b, Key=key)
    upload_id = created["UploadId"]

    part1 = s3.upload_part(Bucket=b, Key=key, UploadId=upload_id, PartNumber=1, Body=b"a" * 100)
    part2 = s3.upload_part(Bucket=b, Key=key, UploadId=upload_id, PartNumber=2, Body=b"b" * 50)

    parts = [
        {"ETag": part1["ETag"], "PartNumber": 1},
        {"ETag": part2["ETag"], "PartNumber": 2},
    ]
    completed = s3.complete_multipart_upload(
        Bucket=b, Key=key, UploadId=upload_id, MultipartUpload={"Parts": parts}
    )
    assert completed["ETag"].endswith("-2\"")

    resp = s3.get_object(Bucket=b, Key=key)
    assert resp["Body"].read() == b"a" * 100 + b"b" * 50


def test_list_parts(s3):
    b = "mpu-list-bucket"
    s3.create_bucket(Bucket=b)
    key = "k"
    uid = s3.create_multipart_upload(Bucket=b, Key=key)["UploadId"]
    s3.upload_part(Bucket=b, Key=key, UploadId=uid, PartNumber=1, Body=b"x")
    s3.upload_part(Bucket=b, Key=key, UploadId=uid, PartNumber=2, Body=b"y")
    resp = s3.list_parts(Bucket=b, Key=key, UploadId=uid)
    assert [p["PartNumber"] for p in resp["Parts"]] == [1, 2]
    s3.abort_multipart_upload(Bucket=b, Key=key, UploadId=uid)


def test_abort_multipart(s3):
    b = "mpu-abort-bucket"
    s3.create_bucket(Bucket=b)
    key = "k"
    uid = s3.create_multipart_upload(Bucket=b, Key=key)["UploadId"]
    s3.abort_multipart_upload(Bucket=b, Key=key, UploadId=uid)
    try:
        s3.list_parts(Bucket=b, Key=key, UploadId=uid)
        assert False
    except s3.exceptions.NoSuchUpload:
        pass


def test_high_level_upload_file(s3, tmp_path):
    b = "mpu-high-bucket"
    s3.create_bucket(Bucket=b)
    path = tmp_path / "data.bin"
    payload = b"z" * (1024 * 1024 * 10)
    path.write_bytes(payload)
    s3.upload_file(str(path), b, "uploaded.bin")
    resp = s3.get_object(Bucket=b, Key="uploaded.bin")
    assert resp["Body"].read() == payload

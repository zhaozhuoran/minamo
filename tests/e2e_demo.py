"""End-to-end simulation: drive a live Minamo server with a real boto3 client
through every MVP operation and print the result of each step."""
from __future__ import annotations

import socket
import tempfile
import threading
import time
from pathlib import Path

import boto3
import uvicorn
from botocore.config import Config
from botocore.exceptions import ClientError

from minamo.app import create_app
from minamo.config import ConfigManager

PASS = []
FAIL = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="minamo-e2e-"))
    config = ConfigManager.from_dict(
        {
            "app": {
                "backend": "local_disk",
                "endpoint_host": "localhost",
                "region": "us-east-1",
                "data": {"root": str(tmp / "data")},
            },
            "secrets": {"access_key": "minamo", "secret_key": "minamo-secret"},
            "storage_localdisk": {"root": str(tmp / "data" / "localdisk")},
        }
    )
    app = create_app(config)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"

    s3 = boto3.client(
        "s3",
        endpoint_url=base,
        aws_access_key_id="minamo",
        aws_secret_access_key="minamo-secret",
        region_name="us-east-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}, retries={"max_attempts": 0}),
    )

    print("== Bucket operations ==")
    s3.create_bucket(Bucket="demo-bucket")
    check("CreateBucket", True)
    check("HeadBucket", s3.head_bucket(Bucket="demo-bucket")["ResponseMetadata"]["HTTPStatusCode"] == 200)
    check("ListBuckets contains demo-bucket", any(b["Name"] == "demo-bucket" for b in s3.list_buckets()["Buckets"]))

    try:
        s3.create_bucket(Bucket="demo-bucket")
        check("CreateBucket conflict -> 409", False)
    except ClientError as e:
        check("CreateBucket conflict -> 409", e.response["Error"]["Code"] == "BucketAlreadyExists",
              e.response["Error"]["Code"])

    print("== Object operations ==")
    s3.put_object(Bucket="demo-bucket", Key="hello.txt", Body=b"hello world",
                  ContentType="text/plain", Metadata={"author": "minamo"})
    obj = s3.get_object(Bucket="demo-bucket", Key="hello.txt")
    check("PutObject + GetObject body", obj["Body"].read() == b"hello world")
    check("GetObject ContentType", obj["ContentType"] == "text/plain")
    check("GetObject ETag quoted", obj["ETag"].startswith('"') and obj["ETag"].endswith('"'))
    head = s3.head_object(Bucket="demo-bucket", Key="hello.txt")
    check("HeadObject size", head["ContentLength"] == 11)
    check("HeadObject metadata roundtrip", head["Metadata"] == {"author": "minamo"})

    try:
        s3.head_object(Bucket="demo-bucket", Key="missing")
        check("HeadObject missing -> 404", False)
    except ClientError as e:
        check("HeadObject missing -> 404", e.response["Error"]["Code"] == "404")

    print("== Streaming (AWS-chunked) upload ==")
    payload = b"z" * (1024 * 512)
    path = tmp / "big.bin"
    path.write_bytes(payload)
    with open(path, "rb") as fh:
        s3.put_object(Bucket="demo-bucket", Key="streamed.bin", Body=fh)
    got = s3.get_object(Bucket="demo-bucket", Key="streamed.bin")["Body"].read()
    check("Streaming PutObject + GetObject", got == payload)

    print("== ListObjectsV2 ==")
    for k in ["a.txt", "dir/b.txt", "dir/c.txt", "dir/sub/d.txt"]:
        s3.put_object(Bucket="demo-bucket", Key=k, Body=k.encode())
    all_keys = sorted(o["Key"] for o in s3.list_objects_v2(Bucket="demo-bucket")["Contents"])
    check("List all", all_keys == sorted(["hello.txt", "streamed.bin", "a.txt", "dir/b.txt", "dir/c.txt", "dir/sub/d.txt"]))
    pfx = sorted(o["Key"] for o in s3.list_objects_v2(Bucket="demo-bucket", Prefix="dir/")["Contents"])
    check("List prefix=dir/", pfx == ["dir/b.txt", "dir/c.txt", "dir/sub/d.txt"])
    resp = s3.list_objects_v2(Bucket="demo-bucket", Delimiter="/")
    common = sorted(c["Prefix"] for c in resp.get("CommonPrefixes", []))
    check("List delimiter=/ common prefixes", common == ["dir/"])
    # pagination
    for i in range(5):
        s3.put_object(Bucket="demo-bucket", Key=f"page/{i}", Body=b"x")
    r1 = s3.list_objects_v2(Bucket="demo-bucket", Prefix="page/", MaxKeys=2)
    check("Pagination IsTruncated", r1["IsTruncated"])
    r2 = s3.list_objects_v2(Bucket="demo-bucket", Prefix="page/", MaxKeys=2, ContinuationToken=r1["NextContinuationToken"])
    check("Pagination continuation", sorted([o["Key"] for o in r1["Contents"]] + [o["Key"] for o in r2["Contents"]]) == ["page/0", "page/1", "page/2", "page/3"])

    print("== Multipart upload ==")
    created = s3.create_multipart_upload(Bucket="demo-bucket", Key="mpu.bin")
    uid = created["UploadId"]
    check("CreateMultipartUpload has UploadId", bool(uid))
    p1 = s3.upload_part(Bucket="demo-bucket", Key="mpu.bin", UploadId=uid, PartNumber=1, Body=b"part-one-")
    p2 = s3.upload_part(Bucket="demo-bucket", Key="mpu.bin", UploadId=uid, PartNumber=2, Body=b"part-two")
    parts = [{"ETag": p1["ETag"], "PartNumber": 1}, {"ETag": p2["ETag"], "PartNumber": 2}]
    completed = s3.complete_multipart_upload(Bucket="demo-bucket", Key="mpu.bin", UploadId=uid, MultipartUpload={"Parts": parts})
    check("CompleteMultipartUpload ETag -N", completed["ETag"].endswith('-2"'))
    mpu_data = s3.get_object(Bucket="demo-bucket", Key="mpu.bin")["Body"].read()
    check("Multipart assembled body", mpu_data == b"part-one-part-two")

    try:
        s3.list_parts(Bucket="demo-bucket", Key="mpu.bin", UploadId=uid)
        check("ListParts after complete -> NoSuchUpload", False)
    except ClientError as e:
        check("ListParts after complete -> NoSuchUpload", e.response["Error"]["Code"] == "NoSuchUpload")

    # abort flow
    uid2 = s3.create_multipart_upload(Bucket="demo-bucket", Key="abort.bin")["UploadId"]
    s3.upload_part(Bucket="demo-bucket", Key="abort.bin", UploadId=uid2, PartNumber=1, Body=b"x")
    s3.abort_multipart_upload(Bucket="demo-bucket", Key="abort.bin", UploadId=uid2)
    try:
        s3.list_parts(Bucket="demo-bucket", Key="abort.bin", UploadId=uid2)
        check("AbortMultipartUpload -> NoSuchUpload", False)
    except ClientError as e:
        check("AbortMultipartUpload -> NoSuchUpload", e.response["Error"]["Code"] == "NoSuchUpload")

    print("== Presigned URL ==")
    url = s3.generate_presigned_url("get_object", Params={"Bucket": "demo-bucket", "Key": "hello.txt"}, ExpiresIn=300)
    import httpx

    raw = httpx.get(url)
    check("Presigned GET (no auth) works", raw.status_code == 200 and raw.content == b"hello world")

    print("== Delete + bucket-not-empty ==")
    s3.put_object(Bucket="demo-bucket", Key="keep.txt", Body=b"x")
    try:
        s3.delete_bucket(Bucket="demo-bucket")
        check("DeleteBucket on non-empty -> 409", False)
    except ClientError as e:
        check("DeleteBucket on non-empty -> 409", e.response["Error"]["Code"] == "BucketNotEmpty")

    # Clean up every object, then delete the bucket.
    cont = None
    while True:
        kwargs = {"Bucket": "demo-bucket"}
        if cont:
            kwargs["ContinuationToken"] = cont
        resp = s3.list_objects_v2(**kwargs)
        for o in resp.get("Contents", []):
            s3.delete_object(Bucket="demo-bucket", Key=o["Key"])
        if resp.get("IsTruncated"):
            cont = resp["NextContinuationToken"]
        else:
            break
    s3.delete_bucket(Bucket="demo-bucket")
    try:
        s3.head_bucket(Bucket="demo-bucket")
        check("HeadBucket after delete -> 404", False)
    except ClientError as e:
        check("HeadBucket after delete -> 404", e.response["Error"]["Code"] == "404")

    server.should_exit = True
    print(f"\n== RESULT: {len(PASS)} passed, {len(FAIL)} failed ==")
    if FAIL:
        print("FAILED:", FAIL)
        raise SystemExit(1)


if __name__ == "__main__":
    main()

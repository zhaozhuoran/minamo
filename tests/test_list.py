"""ListObjectsV2 compatibility tests (boto3)."""
from __future__ import annotations


def _seed(bucket, s3):
    s3.create_bucket(Bucket=bucket)
    for k in ["a.txt", "b.txt", "dir/c.txt", "dir/d.txt", "dir/sub/e.txt"]:
        s3.put_object(Bucket=bucket, Key=k, Body=k.encode())


def test_list_all(s3):
    b = "list-bucket"
    _seed(b, s3)
    resp = s3.list_objects_v2(Bucket=b)
    keys = sorted(o["Key"] for o in resp["Contents"])
    assert keys == ["a.txt", "b.txt", "dir/c.txt", "dir/d.txt", "dir/sub/e.txt"]


def test_list_prefix(s3):
    b = "list-prefix-bucket"
    _seed(b, s3)
    resp = s3.list_objects_v2(Bucket=b, Prefix="dir/")
    keys = sorted(o["Key"] for o in resp["Contents"])
    assert keys == ["dir/c.txt", "dir/d.txt", "dir/sub/e.txt"]


def test_list_delimiter(s3):
    b = "list-delim-bucket"
    _seed(b, s3)
    resp = s3.list_objects_v2(Bucket=b, Delimiter="/")
    keys = sorted(o["Key"] for o in resp.get("Contents", []))
    prefixes = sorted(cp["Prefix"] for cp in resp.get("CommonPrefixes", []))
    assert keys == ["a.txt", "b.txt"]
    assert prefixes == ["dir/"]


def test_list_pagination(s3):
    b = "list-page-bucket"
    s3.create_bucket(Bucket=b)
    for i in range(5):
        s3.put_object(Bucket=b, Key=f"k{i}", Body=b"x")
    resp = s3.list_objects_v2(Bucket=b, MaxKeys=2)
    assert resp["IsTruncated"]
    assert resp["KeyCount"] == 2
    token = resp["NextContinuationToken"]
    resp2 = s3.list_objects_v2(Bucket=b, MaxKeys=2, ContinuationToken=token)
    all_keys = [o["Key"] for o in resp["Contents"]] + [o["Key"] for o in resp2["Contents"]]
    assert sorted(all_keys) == ["k0", "k1", "k2", "k3"]

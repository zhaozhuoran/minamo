import pytest
from pathlib import Path
import botocore.auth
import botocore.awsrequest
from botocore.credentials import Credentials
from fastapi.testclient import TestClient

from minamo.app import create_app
from minamo.config import ConfigManager


def _create_test_app(tmp_path: Path) -> TestClient:
    raw_cfg = {
        "app": {
            "backend": "local_disk",
            "endpoint_host": "localhost",
            "region": "us-east-1",
            "enforce_signature": True,
            "presign_ttl": 3600,
            "data": {"root": str(tmp_path / "data")},
        },
        "secrets": {"access_key": "minamo", "secret_key": "minamosecret"},
        "hsm": {"enabled": False, "tiers": []},
    }
    cm = ConfigManager.from_dict(raw_cfg, config_dir=tmp_path)
    app = create_app(cm)
    return TestClient(app)


def test_list_buckets_with_x_id_param(tmp_path: Path):
    client = _create_test_app(tmp_path)
    with client:
        req = botocore.awsrequest.AWSRequest(
            method="GET",
            url="http://localhost/?x-id=ListBuckets",
            headers={"Host": "localhost"},
        )
        credentials = Credentials("minamo", "minamosecret")
        auth = botocore.auth.SigV4Auth(credentials, "s3", "us-east-1")
        auth.add_auth(req)

        headers = {k: v for k, v in req.headers.items()}
        res = client.get("/?x-id=ListBuckets", headers=headers)
        assert res.status_code == 200
        assert "<ListAllMyBucketsResult" in res.text


def test_list_buckets_xml_structure(tmp_path: Path):
    client = _create_test_app(tmp_path)
    with client:
        # Create a bucket first
        req_create = botocore.awsrequest.AWSRequest(
            method="PUT",
            url="http://localhost/test-bucket",
            headers={"Host": "localhost"},
        )
        credentials = Credentials("minamo", "minamosecret")
        auth = botocore.auth.SigV4Auth(credentials, "s3", "us-east-1")
        auth.add_auth(req_create)
        client.put("/test-bucket", headers={k: v for k, v in req_create.headers.items()})

        # List buckets and check XML format
        req = botocore.awsrequest.AWSRequest(
            method="GET",
            url="http://localhost/?x-id=ListBuckets",
            headers={"Host": "localhost"},
        )
        auth.add_auth(req)
        headers = {k: v for k, v in req.headers.items()}
        res = client.get("/?x-id=ListBuckets", headers=headers)
        assert res.status_code == 200
        xml = res.text
        assert "<BucketRegion>us-east-1</BucketRegion>" in xml
        assert "<Region>us-east-1</Region>" in xml
        assert "<Name>test-bucket</Name>" in xml


def test_list_buckets_ipv6_host_header(tmp_path: Path):
    client = _create_test_app(tmp_path)
    ipv6_host = "[2408:8206:482e:8701::b261]:8000"
    with client:
        req = botocore.awsrequest.AWSRequest(
            method="GET",
            url=f"http://{ipv6_host}/?x-id=ListBuckets",
            headers={"Host": ipv6_host},
        )
        credentials = Credentials("minamo", "minamosecret")
        auth = botocore.auth.SigV4Auth(credentials, "s3", "us-east-1")
        auth.add_auth(req)

        headers = {k: v for k, v in req.headers.items()}
        res = client.get("/?x-id=ListBuckets", headers=headers)
        assert res.status_code == 200
        assert "<ListAllMyBucketsResult" in res.text


def test_date_header_fallback(tmp_path: Path):
    client = _create_test_app(tmp_path)
    with client:
        req = botocore.awsrequest.AWSRequest(
            method="GET",
            url="http://localhost/?x-id=ListBuckets",
            headers={
                "Host": "localhost",
                "Date": "Sun, 27 Sep 2026 06:37:57 GMT",
            },
        )
        credentials = Credentials("minamo", "minamosecret")
        auth = botocore.auth.SigV4Auth(credentials, "s3", "us-east-1")
        # Remove x-amz-date if added automatically by auth or manual header construction
        auth.add_auth(req)
        if "X-Amz-Date" in req.headers:
            del req.headers["X-Amz-Date"]
        if "x-amz-date" in req.headers:
            del req.headers["x-amz-date"]

        headers = {k: v for k, v in req.headers.items()}
        res = client.get("/?x-id=ListBuckets", headers=headers)
        assert res.status_code == 200
        assert "<ListAllMyBucketsResult" in res.text


def test_invalid_access_key_returns_403(tmp_path: Path):
    client = _create_test_app(tmp_path)
    with client:
        req = botocore.awsrequest.AWSRequest(
            method="GET",
            url="http://localhost/?x-id=ListBuckets",
            headers={"Host": "localhost"},
        )
        credentials = Credentials("wrongkey", "minamosecret")
        auth = botocore.auth.SigV4Auth(credentials, "s3", "us-east-1")
        auth.add_auth(req)

        headers = {k: v for k, v in req.headers.items()}
        res = client.get("/?x-id=ListBuckets", headers=headers)
        assert res.status_code == 403
        assert "InvalidAccessKeyId" in res.text


def test_expired_presigned_url_returns_403(tmp_path: Path):
    from minamo.utils.signing import presign_query

    client = _create_test_app(tmp_path)
    with client:
        expired_date = "20200101T000000Z"
        query_str = presign_query(
            secret="minamosecret",
            access_key="minamo",
            region="us-east-1",
            service="s3",
            method="GET",
            bucket="",
            key="",
            expires=3600,
            amz_date=expired_date,
            host="localhost",
        )
        res = client.get(f"/?x-id=ListBuckets&{query_str}")
        assert res.status_code == 403
        assert "AccessDenied" in res.text or "SignatureDoesNotMatch" in res.text

"""Regression tests for Content-Range resolution, virtual-hosted addressing
and SigV4 helper round-trips.

Covers:
* suffix ranges (bytes=-N) and open-ended ranges (bytes=N-) must produce
  fully-resolved ``Content-Range`` headers (no negative / None values).
* S3Service.get_object_range returns the resolved (start, end) pair.
* virtual-hosted style bucket addressing (<bucket>.<endpoint>).
* presign_query + verify round-trip for query-auth SigV4.
"""
from __future__ import annotations

import pytest
from pathlib import Path
from fastapi.testclient import TestClient

from minamo.app import create_app
from minamo.config import ConfigManager
from minamo.metadata.store import MetadataStore
from minamo.service.s3_service import S3Service
from minamo.service.errors import S3Error
from minamo.storage.local_disk import LocalDiskBackend
from minamo.utils.signing import presign_query, verify


def _make_app(tmp_path: Path) -> TestClient:
    raw_cfg = {
        "app": {
            "backend": "local_disk",
            "endpoint_host": "localhost",
            "region": "us-east-1",
            "enforce_signature": False,
            "presign_ttl": 3600,
            "data": {"root": str(tmp_path / "data")},
        },
        "secrets": {"access_key": "k", "secret_key": "s"},
        "hsm": {"enabled": False, "tiers": []},
    }
    cm = ConfigManager.from_dict(raw_cfg, config_dir=tmp_path)
    app = create_app(cm)
    return TestClient(app)


@pytest.mark.asyncio
async def test_get_object_range_returns_resolved_bounds(tmp_path: Path):
    meta = MetadataStore(tmp_path / "meta.db")
    meta.init()
    storage = LocalDiskBackend(tmp_path / "disk")
    service = S3Service(meta, storage)

    await service.create_bucket("rangebkt")
    data = b"0123456789" * 10  # 100 bytes
    await service.put_object("rangebkt", "k", data)

    # Suffix range: last 10 bytes
    info, body, start, end = await service.get_object_range("rangebkt", "k", start=-10, end=None)
    assert (start, end) == (90, 99)
    assert body == data[90:]

    # Open-ended range: bytes 95-
    info, body, start, end = await service.get_object_range("rangebkt", "k", start=95, end=None)
    assert (start, end) == (95, 99)
    assert body == data[95:]

    # Explicit range stays unchanged
    info, body, start, end = await service.get_object_range("rangebkt", "k", start=10, end=19)
    assert (start, end) == (10, 19)
    assert body == data[10:20]

    # End beyond size is clamped to size-1
    info, body, start, end = await service.get_object_range("rangebkt", "k", start=0, end=999)
    assert (start, end) == (0, 99)

    # Unsatisfiable suffix
    with pytest.raises(S3Error) as exc:
        await service.get_object_range("rangebkt", "k", start=-101, end=None)
    assert exc.value.code == "InvalidRange"

    meta.close()


def test_range_open_end_content_range_header(tmp_path: Path):
    client = _make_app(tmp_path)
    with client:
        assert client.put("/rb1").status_code == 200
        client.put("/rb1/k", content=b"x" * 100)

        resp = client.get("/rb1/k", headers={"Range": "bytes=95-"})
        assert resp.status_code == 206
        assert resp.headers["Content-Range"] == "bytes 95-99/100"
        assert resp.content == b"x" * 5


def test_range_suffix_content_range_header(tmp_path: Path):
    client = _make_app(tmp_path)
    with client:
        assert client.put("/rb2").status_code == 200
        payload = b"abcdefghij" * 10  # 100 bytes
        client.put("/rb2/k", content=payload)

        resp = client.get("/rb2/k", headers={"Range": "bytes=-10"})
        assert resp.status_code == 206
        assert resp.headers["Content-Range"] == "bytes 90-99/100"
        assert resp.content == payload[-10:]


def test_range_exact_content_range_header(tmp_path: Path):
    client = _make_app(tmp_path)
    with client:
        assert client.put("/rb3").status_code == 200
        client.put("/rb3/k", content=b"0123456789")

        resp = client.get("/rb3/k", headers={"Range": "bytes=2-4"})
        assert resp.status_code == 206
        assert resp.headers["Content-Range"] == "bytes 2-4/10"
        assert resp.content == b"234"


def test_range_unsatisfiable_returns_416(tmp_path: Path):
    client = _make_app(tmp_path)
    with client:
        assert client.put("/rb4").status_code == 200
        client.put("/rb4/k", content=b"abc")

        resp = client.get("/rb4/k", headers={"Range": "bytes=100-200"})
        assert resp.status_code == 416
        assert "InvalidRange" in resp.text


def test_virtual_hosted_style_bucket_addressing(tmp_path: Path):
    client = _make_app(tmp_path)
    with client:
        host = {"Host": "vhbucket.localhost"}

        # create bucket via virtual-hosted PUT /
        assert client.put("/", headers=host).status_code == 200

        # list objects via virtual-hosted GET /
        assert client.get("/", headers=host).status_code == 200
        assert "vhbucket" in client.get("/", headers=host).text

        # put an object via virtual-hosted PUT /<key>
        assert client.put("/file.txt", content=b"vh", headers=host).status_code == 200

        # get it back via virtual-hosted GET /<key>
        resp = client.get("/file.txt", headers=host)
        assert resp.status_code == 200
        assert resp.content == b"vh"

        # head bucket
        assert client.head("/", headers=host).status_code == 200


def test_presign_query_verify_roundtrip():
    """A URL signed by presign_query must pass verify() on the same request."""
    from starlette.datastructures import ImmutableMultiDict
    from minamo.utils.time import amz_date as get_amz_date

    amz_date = get_amz_date()
    query = presign_query(
        secret="minamo-secret",
        access_key="minamo",
        region="us-east-1",
        service="s3",
        method="GET",
        bucket="bk",
        key="some/key.txt",
        expires=3600,
        amz_date=amz_date,
        host="localhost",
        signed_headers=["host"],
    )
    # parse query string into multi-dict
    from urllib.parse import parse_qsl

    qdict = ImmutableMultiDict(parse_qsl(query, keep_blank_values=True))

    headers = {"host": "localhost", "x-amz-date": amz_date}
    ctx = verify(
        secret="minamo-secret",
        method="GET",
        path="/bk/some/key.txt",
        query=qdict,
        headers=headers,
        body=b"",
        region="us-east-1",
        service="s3",
    )
    assert ctx.access_key == "minamo"
    assert ctx.signature in query

    # tampered body/scope must fail
    with pytest.raises(ValueError):
        verify(
            secret="minamo-secret",
            method="PUT",
            path="/bk/some/key.txt",
            query=qdict,
            headers=headers,
            body=b"",
            region="us-east-1",
            service="s3",
        )

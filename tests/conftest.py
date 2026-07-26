"""Compatibility test fixtures.

We boot the real ASGI app in a background uvicorn server and point an actual
boto3 client at it. These tests verify *protocol* compatibility (SigV4, XML,
headers, status codes) rather than internal behaviour.
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
from pathlib import Path

import boto3
import pytest
import uvicorn
from botocore.config import Config

from minamo.app import create_app
from minamo.config import Settings

ACCESS_KEY = "minamo"
SECRET_KEY = "minamo-secret"
REGION = "us-east-1"


def _free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="session")
def server():
    tmp = Path(tempfile.mkdtemp(prefix="minamo-test-"))
    settings = Settings(
        data_root=tmp / "data",
        metadata_root=tmp / "metadata",
        access_key=ACCESS_KEY,
        secret_key=SECRET_KEY,
        region=REGION,
        endpoint_host="localhost",
        enforce_signature=True,
        backend="local_disk",
    )
    app = create_app(settings)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    while not srv.started:
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    yield base
    srv.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def s3(server):
    return boto3.client(
        "s3",
        endpoint_url=server,
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
        region_name=REGION,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 0},
            connect_timeout=5,
        ),
    )


@pytest.fixture
def unique_bucket(s3):
    """Create a freshly-named bucket for the test (avoids cross-test collisions
    on the shared session database)."""
    import uuid

    name = "b-" + uuid.uuid4().hex[:16]
    s3.create_bucket(Bucket=name)
    return name


@pytest.fixture
def raw(server):
    import httpx

    return httpx.Client(base_url=server, timeout=10)

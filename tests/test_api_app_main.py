import pytest
import asyncio
from pathlib import Path
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock

from minamo.app import create_app
from minamo.config.manager import ConfigManager
from minamo.service.errors import S3Error
from minamo.service.s3_service import S3Service
from minamo.__main__ import main, _build_parser, _collect_overrides

def test_app_exception_handlers_and_lifespan(tmp_path: Path):
    raw_cfg = {
        "app": {
            "backend": "local_disk",
            "endpoint_host": "0.0.0.0:8000",
            "region": "us-east-1",
            "enforce_signature": False,
            "presign_ttl": 3600,
            "data": {"root": str(tmp_path / "data")}
        },
        "secrets": {"access_key": "k", "secret_key": "s"},
        "hsm": {"enabled": False, "tiers": []}
    }
    cm = ConfigManager.from_dict(raw_cfg, config_dir=tmp_path)
    app = create_app(cm)

    with TestClient(app, raise_server_exceptions=False) as client:
        # Create bucket first
        res_create = client.put("/mybucket1")
        assert res_create.status_code == 200

        # Initiate upload
        res_up = client.post("/mybucket1/file.txt?uploads")
        assert res_up.status_code == 200

        # Send invalid XXE XML payload to complete_multipart_upload to trigger ValueError -> 400 MalformedXML / InvalidArgument
        res = client.post("/mybucket1/file.txt?uploadId=dummy123", content=b"<!DOCTYPE foo [<!ENTITY xxe SYSTEM 'file:///etc/passwd'>]><CompleteMultipartUpload></CompleteMultipartUpload>")
        assert res.status_code == 400
        assert "MalformedXML" in res.text or "InvalidArgument" in res.text

        # Test generic exception handler by mocking a service call to raise Exception
        with patch.object(app.state.service, "list_buckets", side_effect=RuntimeError("Database failure")):
            res_500 = client.get("/")
            assert res_500.status_code == 500
            assert "InternalError" in res_500.text

def test_presign_endpoint(tmp_path: Path):
    raw_cfg = {
        "app": {
            "backend": "local_disk",
            "endpoint_host": "0.0.0.0:8000",
            "region": "us-east-1",
            "enforce_signature": False,
            "presign_ttl": 3600,
            "data": {"root": str(tmp_path / "data")}
        },
        "secrets": {"access_key": "k", "secret_key": "s"},
        "hsm": {"enabled": False, "tiers": []}
    }
    cm = ConfigManager.from_dict(raw_cfg, config_dir=tmp_path)
    app = create_app(cm)

    with TestClient(app) as client:
        res = client.post("/presign", json={
            "bucket": "mybucket1",
            "key": "file.txt",
            "expires": 3600,
            "method": "GET"
        })
        assert res.status_code == 200
        data = res.json()
        assert "url" in data
        assert "/mybucket1/file.txt?" in data["url"]
        assert "X-Amz-Signature=" in data["url"]

def test_main_cli_commands(tmp_path: Path, capsys):
    # Test init command
    config_dir = tmp_path / "config"
    main(["init", "--config-dir", str(config_dir)])
    assert config_dir.exists()
    assert (config_dir / "app.toml").exists()

    # Re-run init when exists
    main(["init", "--config-dir", str(config_dir)])
    captured = capsys.readouterr()
    assert "already exists" in captured.out

    # Test parser flags
    parser = _build_parser()
    args = parser.parse_args(["run", "--backend", "r2", "--access-key", "mykey"])
    overrides = _collect_overrides(args)
    assert overrides["app.backend"] == "r2"
    assert overrides["secrets.access_key"] == "mykey"

@pytest.mark.asyncio
async def test_s3_service_error_branches(tmp_path: Path):
    from minamo.metadata.store import MetadataStore
    from minamo.storage.local_disk import LocalDiskBackend

    meta = MetadataStore(tmp_path / "meta.db")
    meta.init()
    storage = LocalDiskBackend(tmp_path / "disk")
    service = S3Service(meta, storage)

    # Invalid bucket name
    with pytest.raises(S3Error) as exc:
        await service.create_bucket("AB") # uppercase & short
    assert exc.value.code == "InvalidBucketName"

    # Bucket not found
    with pytest.raises(S3Error) as exc:
        await service.get_object("nonexistent", "k")
    assert exc.value.code == "NoSuchBucket"

    # Create bucket & object
    await service.create_bucket("mybucket")
    await service.put_object("mybucket", "k1", b"Hello World")

    # Invalid range request
    with pytest.raises(S3Error) as exc:
        await service.get_object_range("mybucket", "k1", start=100, end=200)
    assert exc.value.code == "InvalidRange"

    # Negative start range beyond size
    with pytest.raises(S3Error) as exc:
        await service.get_object_range("mybucket", "k1", start=-100, end=None)
    assert exc.value.code == "InvalidRange"

    # Bucket not empty on delete
    with pytest.raises(S3Error) as exc:
        await service.delete_bucket("mybucket")
    assert exc.value.code == "BucketNotEmpty"

    # Complete multipart upload with empty parts list
    upload = await service.create_multipart_upload("mybucket", "mp.txt")
    with pytest.raises(S3Error) as exc:
        await service.complete_multipart_upload("mybucket", "mp.txt", upload.upload_id, parts=[])
    assert exc.value.code == "InvalidArgument"

    meta.close()

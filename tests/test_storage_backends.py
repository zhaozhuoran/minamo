import pytest
import asyncio
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from botocore.exceptions import ClientError

from minamo.storage.r2 import R2Backend
from minamo.storage.cache import CacheManager
from minamo.storage.local_disk import LocalDiskBackend
from minamo.storage.manager import StorageManager
from minamo.storage.scheduler import HsmScheduler
from minamo.config.manager import ConfigManager
from minamo.state.manager import StateManager
from minamo.metadata.store import MetadataStore
from minamo.metadata.models import ObjectInfo
from minamo.service.errors import S3Error

@pytest.mark.asyncio
async def test_r2_backend_capabilities_and_crud():
    r2 = R2Backend("https://r2.cloud.com", "key", "secret", "mybucket")
    caps = r2.capabilities
    assert caps.supports_random_read is True
    assert caps.supports_multipart_upload is True

    # Mock boto3 client
    mock_client = MagicMock()
    with patch.object(r2, "_get_client", return_value=mock_client):
        # head_bucket throws 404 -> create bucket
        mock_client.head_bucket.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadBucket"
        )
        await r2.create_bucket("testb")
        mock_client.create_bucket.assert_called_with(Bucket="mybucket")

        # bucket_exists
        mock_client.head_bucket.side_effect = None
        assert await r2.bucket_exists("testb") is True

        mock_client.head_bucket.side_effect = ClientError(
            {"Error": {"Code": "404"}}, "HeadBucket"
        )
        assert await r2.bucket_exists("testb") is False

        # put_object bytes & Path
        await r2.put_object("testb", "key1", b"hello r2")
        mock_client.put_object.assert_called()

        # get_object
        mock_body = MagicMock()
        mock_body.read.return_value = b"hello r2"
        mock_client.get_object.return_value = {"Body": mock_body}
        chunks = [c async for c in r2.get_object("testb", "key1")]
        assert b"".join(chunks) == b"hello r2"

        # read_range
        assert await r2.read_range("testb", "key1", 0, 4) == b"hello r2"

        # object_size
        mock_client.head_object.return_value = {"ContentLength": 8}
        assert await r2.object_size("testb", "key1") == 8

        # delete_object
        await r2.delete_object("testb", "key1")
        mock_client.delete_object.assert_called_with(Bucket="mybucket", Key="testb/key1")

        # list_keys
        mock_client.list_objects_v2.return_value = {
            "Contents": [{"Key": "testb/k1"}, {"Key": "testb/k2"}],
            "IsTruncated": False,
        }
        res = await r2.list_keys("testb")
        assert res.keys == ["k1", "k2"]

        # multipart
        await r2.put_part("testb", "up1", 1, b"part1")
        mock_body.read.return_value = b"part1"
        assert await r2.get_part("testb", "up1", 1) == b"part1"

        # abort_upload
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [
            {"Contents": [{"Key": ".minamo-mpu/testb/up1/00000001"}]}
        ]
        mock_client.get_paginator.return_value = mock_paginator
        await r2.abort_upload("testb", "up1")
        mock_client.delete_objects.assert_called()

        # delete_bucket
        await r2.delete_bucket("testb")

def test_cache_manager(tmp_path: Path):
    cm = CacheManager(cache_dir=tmp_path / "cache", max_size_bytes=50, policy="LRU")
    assert cm.get("b", "k") is None

    p1 = cm.put("b", "k1", b"A" * 30)
    assert p1.exists()

    # Re-put k1 -> size updated
    p1_new = cm.put("b", "k1", b"A" * 20)
    assert cm.current_size == 20

    # Put k2 -> 20 + 40 = 60 > 50 -> k1 evicted
    cm.put("b", "k2", b"B" * 40)
    assert cm.get("b", "k1") is None
    assert cm.get("b", "k2") is not None

    # Path traversal validation
    with pytest.raises(ValueError, match="path traversal"):
        cm.get("../invalid", "k")

    with pytest.raises(ValueError, match="path traversal"):
        cm.put("b", "../invalid_key", b"123")

    # Delete
    cm.delete("b", "k2")
    assert cm.get("b", "k2") is None

    # Rebuild index
    (tmp_path / "cache" / "b" / "existing.txt").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "cache" / "b" / "existing.txt").write_bytes(b"hello")
    cm_rebuilt = CacheManager(cache_dir=tmp_path / "cache")
    assert cm_rebuilt.get("b", "existing.txt") is not None

    # Clear
    cm_rebuilt.clear()
    assert cm_rebuilt.current_size == 0

@pytest.mark.asyncio
async def test_local_disk_path_traversal(tmp_path: Path):
    ld = LocalDiskBackend(root=tmp_path / "localdisk")

    with pytest.raises(ValueError, match="invalid key"):
        await ld.put_object("b", "/abs_path", b"data")

    with pytest.raises(ValueError, match="path traversal"):
        await ld.put_object("b", "../outside.txt", b"data")

@pytest.mark.asyncio
async def test_storage_manager_and_scheduler_overflow_and_tick(tmp_path: Path):
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
        "hsm": {
            "enabled": True,
            "overflow_policy": "reject",
            "tiers": [
                {
                    "id": "tier0",
                    "backend": "t0_backend",
                    "priority": 0,
                    "limit": 50,
                    "target_capacity": 30,
                    "high_watermark": 40,
                    "minimum_residency": 0,
                },
                {
                    "id": "tier1",
                    "backend": "t1_backend",
                    "priority": 1,
                    "limit": 50,
                    "target_capacity": 30,
                    "high_watermark": 40,
                    "minimum_residency": 0,
                }
            ]
        },
        "storage_t0_backend": {"type": "local_disk", "root": "t0"},
        "storage_t1_backend": {"type": "local_disk", "root": "t1"},
    }

    config = ConfigManager.from_dict(raw_cfg, config_dir=tmp_path)
    state = StateManager(tmp_path / "state")
    meta = MetadataStore(tmp_path / "meta.db")
    meta.init()

    sm = StorageManager(config, state, meta)
    scheduler = HsmScheduler(config, meta, sm, interval_seconds=0.1)

    await sm.create_bucket("b")

    now = datetime.now(timezone.utc)
    # Put k1 in tier0 (40 bytes), k_t1 in tier1 (40 bytes)
    sz1 = await sm.put_object("b", "k1", b"X" * 40)
    meta.put_object("b", ObjectInfo(key="k1", size=40, etag="e1", content_type="text/plain", last_modified=now, storage_class="STANDARD", content_encoding=None, expires=None, metadata={}, current_tier="tier0", migration_state="idle", heat_score=100.0))
    meta.put_object("b", ObjectInfo(key="k_t1", size=40, etag="e2", content_type="text/plain", last_modified=now, storage_class="STANDARD", content_encoding=None, expires=None, metadata={}, current_tier="tier1", migration_state="idle", heat_score=100.0))

    # Now put 30 bytes in tier0 -> tier0 current 40 + 30 = 70 > 50. Eviction to tier1 fails (tier1 40 + 40 = 80 > 50).
    # Overflow policy reject -> raises S3Error 507 InsufficientStorageSpace
    with pytest.raises(S3Error) as exc_info:
        await sm.put_object("b", "k2", b"Y" * 30)
    assert exc_info.value.http_status == 507

    # Start scheduler loop briefly and stop
    scheduler.start()
    await asyncio.sleep(0.2)
    await scheduler.stop()

    status = scheduler.get_migration_status()
    assert "active_tasks_count" in status

    # Cleanup
    meta.close()

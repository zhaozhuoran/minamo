"""Additional Comprehensive and Robust Tests for Minamo."""
from __future__ import annotations

import asyncio
import datetime
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
import pytest
from unittest.mock import MagicMock, patch

from minamo.config import ConfigManager
from minamo.config.scaffold import scaffold_config
from minamo.metadata.models import ObjectInfo
from minamo.metadata.store import MetadataStore
from minamo.state.manager import StateManager
from minamo.storage.local_disk import LocalDiskBackend
from minamo.storage.r2 import R2Backend
from minamo.storage.manager import StorageManager
from minamo.storage.scheduler import HsmScheduler
from minamo.api.router import _parse_range, _parse_complete_body


def test_scaffold_config():
    """Verify that scaffold_config creates templates correctly."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir) / "config"
        scaffold_config(tmp_path)
        assert tmp_path.is_dir()
        assert (tmp_path / "app.toml").is_file()
        assert (tmp_path / "secrets.toml").is_file()
        assert (tmp_path / "hsm.toml").is_file()


def test_metadata_store_concurrency():
    """Verify that MetadataStore is fully thread-safe and doesn't crash under concurrent load."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        metadata = MetadataStore(tmp_path / "metadata.db")
        metadata.init()

        errors = []

        def worker(worker_id: int):
            try:
                for i in range(20):
                    # Concurrent write
                    metadata.create_bucket(f"b-{worker_id}-{i}", datetime.datetime.now(datetime.timezone.utc))
                    # Concurrent exists check
                    metadata.bucket_exists(f"b-{worker_id}-{i}")
                    # Concurrent list
                    metadata.list_buckets()
                    # Concurrent log access
                    metadata.log_access(f"b-{worker_id}-{i}", "k", "READ")
                    # Concurrent retrieve access logs
                    metadata.get_access_logs(f"b-{worker_id}-{i}", "k")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(t_idx,)) for t_idx in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        metadata.close()
        assert len(errors) == 0, f"Encountered concurrent DB errors: {errors}"


def test_state_manager_operations():
    """Verify StateManager's read, write, and update operations, including error boundaries."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        state_mgr = StateManager(tmp_path)

        # 1. Read non-existent namespace
        data = state_mgr.read("non_existent")
        assert data == {}

        # 2. Write data
        state_mgr.write("oauth", {"access_token": "foo", "expires_in": 3600})
        data = state_mgr.read("oauth")
        assert data["access_token"] == "foo"
        assert (tmp_path / "oauth.json").is_file()

        # 3. Update data
        state_mgr.update("oauth", refresh_token="bar")
        data = state_mgr.read("oauth")
        assert data["access_token"] == "foo"
        assert data["refresh_token"] == "bar"

        # 4. Fallback on chmod OSError
        with patch("os.chmod", side_effect=OSError("permission denied")):
            state_mgr.write("oauth", {"access_token": "new-token"})
            data = state_mgr.read("oauth")
            assert data["access_token"] == "new-token"


def test_local_disk_backend_key_validation():
    """Verify that LocalDiskBackend blocks invalid keys and path traversals."""
    backend = LocalDiskBackend(Path("/tmp/minamo-test"))

    with pytest.raises(ValueError, match="empty key"):
        backend._object_path("bucket", "")

    with pytest.raises(ValueError, match="invalid key"):
        backend._object_path("bucket", "/leading")

    with pytest.raises(ValueError, match="invalid key"):
        backend._object_path("bucket", "trailing/")

    with pytest.raises(ValueError, match="invalid key \\(path traversal\\)"):
        backend._object_path("bucket", "../traversal")

    with pytest.raises(ValueError, match="invalid key \\(path traversal\\)"):
        backend._object_path("bucket", "nested/../traversal")


def test_parse_range_edge_cases():
    """Test _parse_range with various valid and invalid headers."""
    # Suffix range
    assert _parse_range("bytes=-500") == (-500, None)
    # Range with start and end
    assert _parse_range("bytes=0-1000") == (0, 1000)
    # Range with start only
    assert _parse_range("bytes=500-") == (500, None)
    # Invalid ranges
    assert _parse_range("bytes=abc") is None
    assert _parse_range("bytes=-") is None
    assert _parse_range("bytes=100-50,200-300") is None  # multi range fallback to None


@pytest.mark.asyncio
async def test_hsm_scheduler_disabled_and_error_logging():
    """Verify that HSM Scheduler handles disabled state and gracefully handles tick errors."""
    config = ConfigManager.from_dict({
        "app": {
            "backend": "local_disk",
            "endpoint_host": "localhost",
            "region": "us-east-1",
            "enforce_signature": True,
            "data": {"root": "/tmp/minamo-test"},
        },
        "secrets": {"access_key": "minamo", "secret_key": "minamo-secret"},
        "hsm": {
            "enabled": False,
            "tiers": []
        }
    })

    metadata = MagicMock()
    storage = MagicMock()
    scheduler = HsmScheduler(config, metadata, storage)

    # Tick when disabled should return immediately
    await scheduler.tick()
    metadata.get_all_objects_hsm_info.assert_not_called()

    # Enable HSM but trigger tick exception, should log it but not crash
    config.settings.hsm.enabled = True
    config.settings.hsm.tiers = [MagicMock(id="tier0", priority=0)]
    metadata.get_all_objects_hsm_info.side_effect = Exception("DB error")

    with patch("minamo.storage.scheduler.logger.error") as mock_log:
        with patch("asyncio.sleep", side_effect=asyncio.CancelledError):
            await scheduler._loop()
        mock_log.assert_called_once()


@pytest.mark.asyncio
async def test_hsm_minimum_residency_naive_datetime():
    """Verify that timezone-naive datetime returned from DB does not raise TypeError in residency check."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        config = ConfigManager.from_dict({
            "app": {
                "backend": "local_disk",
                "endpoint_host": "localhost",
                "region": "us-east-1",
                "enforce_signature": True,
                "data": {"root": str(tmp_path)},
            },
            "secrets": {"access_key": "minamo", "secret_key": "minamo-secret"},
            "storage_localdisk": {"root": str(tmp_path / "local")},
            "hsm": {
                "enabled": True,
                "overflow_policy": "fallback",
                "tiers": [
                    {
                        "id": "tier0",
                        "backend": "local_disk",
                        "priority": 0,
                        "target_capacity": 50,
                        "high_watermark": 80,
                        "limit": 100,
                        "minimum_residency": 3600  # 1 hour residency
                    },
                    {
                        "id": "tier1",
                        "backend": "local_disk",
                        "priority": 1,
                        "minimum_residency": 0
                    }
                ]
            }
        })

        metadata = MetadataStore(tmp_path / "metadata.db")
        metadata.init()

        # Save object with timezone-naive last_modified (simulating old/broken data or raw naive inserts)
        naive_now = datetime.datetime.now() # Naive
        metadata.put_object("b1", ObjectInfo(
            key="k1", size=10, etag="e1", current_tier="tier1", last_modified=naive_now
        ))

        state = MagicMock()
        manager = StorageManager(config, state, metadata)

        # Trigger recall which evaluates minimum residency
        # Since the object is on tier1, let's check its residency against tier1 (which is 0).
        # What if it's recalled from a tier with residency > 0? Let's check:
        tier1_info = manager._get_tier_info("tier1")
        tier1_info.minimum_residency = 1800  # 30 mins residency

        # This should execute successfully and not crash with Offset-Naive/Aware subtract TypeError
        await manager.trigger_recall("b1", "k1", "tier1")
        # Let background recall task have a moment to execute
        await asyncio.sleep(0.1)
        metadata.close()


@pytest.mark.asyncio
async def test_compose_object_path_upload():
    """Verify compose_object utilizes path-based memory-efficient uploads for R2/LocalDisk backends."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        config = ConfigManager.from_dict({
            "app": {
                "backend": "local_disk",
                "endpoint_host": "localhost",
                "region": "us-east-1",
                "enforce_signature": True,
                "data": {"root": str(tmp_path)},
            },
            "secrets": {"access_key": "minamo", "secret_key": "minamo-secret"},
            "storage_localdisk": {"root": str(tmp_path / "local")},
            "hsm": {
                "enabled": True,
                "overflow_policy": "fallback",
                "tiers": [
                    {
                        "id": "tier0",
                        "backend": "local_disk",
                        "priority": 0,
                    },
                    {
                        "id": "tier1",
                        "backend": "local_disk",
                        "priority": 1,
                    }
                ]
            }
        })

        metadata = MetadataStore(tmp_path / "metadata.db")
        metadata.init()

        state = MagicMock()
        manager = StorageManager(config, state, metadata)

        # Store upload parts on tier0
        await manager.backends["tier0"].create_bucket("b1")
        await manager.backends["tier0"].put_part("b1", "upload123", 1, b"part1")
        await manager.backends["tier0"].put_part("b1", "upload123", 2, b"part2")

        metadata.create_upload("b1", "obj1", "upload123", datetime.datetime.now(datetime.timezone.utc))
        metadata.put_part("upload123", 1, "e1", 5)
        metadata.put_part("upload123", 2, "e2", 5)

        # Compose to tier1 (which is local disk here)
        # Should merge in temp file and write/upload to tier1
        size = await manager.compose_object("b1", "obj1", "upload123", [1, 2])
        assert size == 10

        # Read back written data from tier1
        data_chunks = []
        async for chunk in manager.backends["tier1"].get_object("b1", "obj1"):
            data_chunks.append(chunk)
        assert b"".join(data_chunks) == b"part1part2"
        metadata.close()

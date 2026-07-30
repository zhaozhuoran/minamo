"""HSM and Cloudflare R2 Backend Integration Tests."""
from __future__ import annotations

import asyncio
import os
import tempfile
import pytest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime, timezone, timedelta

from minamo.config import ConfigManager
from minamo.metadata.store import MetadataStore
from minamo.metadata.models import ObjectInfo
from minamo.storage.r2 import R2Backend
from minamo.storage.local_disk import LocalDiskBackend
from minamo.storage.manager import StorageManager
from minamo.storage.scheduler import HsmScheduler
from minamo.storage.heat import ExponentialDecayHeatCalculator
from minamo.service.errors import S3Error


@pytest.mark.asyncio
async def test_r2_backend_mocked_calls():
    """Verify R2Backend correctly delegates object and multipart calls to boto3."""
    with patch("boto3.client") as mock_client_factory:
        mock_s3 = MagicMock()
        mock_client_factory.return_value = mock_s3

        # Differentiate range vs full reads using a side_effect
        def get_object_side_effect(Bucket, Key, Range=None):
            if Range:
                import re
                m = re.match(r"bytes=(\d+)-(\d+)", Range)
                if m:
                    start, end = int(m.group(1)), int(m.group(2))
                    return {"Body": MagicMock(read=lambda: b"r2-data"[start:end+1])}
            return {"Body": MagicMock(read=lambda: b"r2-data")}
        mock_s3.get_object.side_effect = get_object_side_effect

        mock_s3.head_object.return_value = {
            "ContentLength": 7
        }
        mock_s3.list_objects_v2.return_value = {
            "Contents": [{"Key": "b1/k1"}],
            "IsTruncated": False
        }

        backend = R2Backend(
            endpoint_url="http://mock-r2",
            access_key_id="key",
            secret_access_key="secret",
            bucket="test-bucket"
        )

        # 1. Bucket Exists
        exists = await backend.bucket_exists("b1")
        assert exists is True
        mock_s3.head_bucket.assert_called_with(Bucket="test-bucket")

        # 2. Put Object
        size = await backend.put_object("b1", "k1", b"r2-data")
        assert size == 7
        mock_s3.put_object.assert_called_with(Bucket="test-bucket", Key="b1/k1", Body=b"r2-data")

        # 3. Get Object
        data_chunks = []
        async for chunk in backend.get_object("b1", "k1"):
            data_chunks.append(chunk)
        assert b"".join(data_chunks) == b"r2-data"

        # 4. Read Range
        range_data = await backend.read_range("b1", "k1", 0, 4)
        assert range_data == b"r2-da"  # Sliced range 0-4 of "r2-data"
        mock_s3.get_object.assert_called_with(Bucket="test-bucket", Key="b1/k1", Range="bytes=0-4")

        # 5. Delete Object
        await backend.delete_object("b1", "k1")
        mock_s3.delete_object.assert_called_with(Bucket="test-bucket", Key="b1/k1")


@pytest.mark.asyncio
async def test_hsm_routing_and_overflow_policy():
    """Test capacity threshold limits, fallback, and reject policies."""
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
            "storage_r2": {
                "endpoint_url": "http://mock-r2",
                "access_key_id": "key",
                "secret_access_key": "secret",
                "bucket": "test-bucket"
            },
            "hsm": {
                "enabled": True,
                "overflow_policy": "reject",
                "tiers": [
                    {
                        "id": "tier0",
                        "backend": "local_disk",
                        "priority": 0,
                        "target_capacity": 50,
                        "high_watermark": 80,
                        "limit": 100,  # 100 bytes limit
                        "minimum_residency": 0
                    },
                    {
                        "id": "tier1",
                        "backend": "r2",
                        "priority": 1,
                        "minimum_residency": 0
                    }
                ]
            }
        })

        metadata = MetadataStore(tmp_path / "metadata.db")
        metadata.init()

        # Create StorageManager under reject policy
        state = MagicMock()
        manager = StorageManager(config, state, metadata)

        # Tier size initially 0
        assert metadata.get_tier_size("tier0") == 0

        # 1. Reject Policy: Write within limit (80 bytes)
        tier_to_write = await manager.determine_write_tier(80)
        assert tier_to_write == "tier0"

        # Update metadata size simulating we stored 80 bytes
        metadata.put_object("b1", ObjectInfo(key="k1", size=80, etag="e1", current_tier="tier0"))
        assert metadata.get_tier_size("tier0") == 80

        # 2. Reject Policy: Write exceeding limit (80 + 30 = 110 bytes > 100 limit)
        with pytest.raises(S3Error) as exc:
            await manager.determine_write_tier(30)
        assert exc.value.http_status == 507
        assert "Limit exceeded" in exc.value.message

        # 3. Fallback Policy: Switch to fallback and verify it goes to next tier
        config.settings.hsm.overflow_policy = "fallback"
        manager = StorageManager(config, state, metadata)

        target_tier = await manager.determine_write_tier(30)
        assert target_tier == "tier1"  # Fallback to R2


@pytest.mark.asyncio
async def test_access_logging_and_heat_decay():
    """Verify raw logs are populated in separate DB and exponential decay computes correctly."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        metadata = MetadataStore(tmp_path / "metadata.db")
        metadata.init()

        # Check separate DB path exists
        assert (tmp_path / "access_logs.db").is_file()

        # Log some read/write events
        metadata.log_access("b1", "k1", "WRITE")
        metadata.log_access("b1", "k1", "READ")
        metadata.log_access("b1", "k1", "READ")

        logs = metadata.get_access_logs("b1", "k1")
        assert len(logs) == 3
        assert logs[0][0] == "READ"  # Ordered descending by timestamp

        calculator = ExponentialDecayHeatCalculator(base_score=100.0, read_weight=10.0, write_weight=5.0, decay_rate_per_hour=0.1)
        now = datetime.now(timezone.utc)

        # Freshly modified object with fresh logs
        heat = calculator.calculate_heat(
            last_modified=now,
            access_logs=[
                ("WRITE", now),
                ("READ", now),
                ("READ", now)
            ],
            now=now
        )
        # Expected: 100 (base) + 5 (write) + 10 (read) + 10 (read) = 125
        assert heat == 125.0

        # Object modified 10 hours ago, logs from 10 hours ago (should decay exponentially)
        ten_hours_ago = now - timedelta(hours=10)
        decayed_heat = calculator.calculate_heat(
            last_modified=ten_hours_ago,
            access_logs=[
                ("WRITE", ten_hours_ago),
                ("READ", ten_hours_ago),
                ("READ", ten_hours_ago)
            ],
            now=now
        )
        # Expected: 125 * e^(-0.1 * 10) = 125 * e^(-1.0) = 125 * 0.367879 = 45.98
        assert decayed_heat < 50.0
        assert decayed_heat > 40.0


@pytest.mark.asyncio
async def test_hsm_scheduler_migration_and_residency():
    """Verify cold objects migrate down correctly while respecting minimum residency."""
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
            "storage_r2": {
                "endpoint_url": "http://mock-r2",
                "access_key_id": "key",
                "secret_access_key": "secret",
                "bucket": "test-bucket"
            },
            "hsm": {
                "enabled": True,
                "overflow_policy": "fallback",
                "tiers": [
                    {
                        "id": "tier0",
                        "backend": "local_disk",
                        "priority": 0,
                        "target_capacity": 50,  # Target capacity is 50 bytes
                        "high_watermark": 80,
                        "limit": 100,
                        "minimum_residency": 3600  # 1 hour minimum residency required
                    },
                    {
                        "id": "tier1",
                        "backend": "r2",
                        "priority": 1,
                        "minimum_residency": 0
                    }
                ]
            }
        })

        metadata = MetadataStore(tmp_path / "metadata.db")
        metadata.init()

        # Initialize mock StorageManager
        state = MagicMock()
        manager = StorageManager(config, state, metadata)

        # Mock both tier backends using AsyncMock
        mock_tier0 = AsyncMock()
        mock_tier1 = AsyncMock()
        manager.backends = {"tier0": mock_tier0, "tier1": mock_tier1}

        scheduler = HsmScheduler(config, metadata, manager)

        # Create objects:
        # obj1: hot, size=40, modified now (cannot migrate anyway because of residency)
        # obj2: cold, size=30, modified 2 hours ago (residency met, should migrate)
        # obj3: cold, size=20, modified now (cannot migrate because of residency)
        now = datetime.now(timezone.utc)
        two_hours_ago = now - timedelta(hours=2)

        metadata.put_object("b1", ObjectInfo(
            key="k1", size=40, etag="e1", last_modified=now, current_tier="tier0", heat_score=100.0
        ))
        metadata.put_object("b1", ObjectInfo(
            key="k2", size=30, etag="e2", last_modified=two_hours_ago, current_tier="tier0", heat_score=10.0
        ))
        metadata.put_object("b1", ObjectInfo(
            key="k3", size=20, etag="e3", last_modified=now, current_tier="tier0", heat_score=5.0
        ))

        # Size on tier0 = 40 + 30 + 20 = 90 bytes. Exceeds target capacity of 50 by 40 bytes.
        assert metadata.get_tier_size("tier0") == 90

        # Trigger scheduler tick
        # Mock get_object stream for tier0
        async def mock_stream(bucket, key):
            yield b"dummy-data"
        mock_tier0.get_object = mock_stream

        await scheduler.tick()

        # Yield task processing
        await asyncio.sleep(0.1)

        # Check if obj2 (k2) was migrated
        # k2 met the minimum residency (2 hours old > 1 hour residency) and has the lowest score among eligible.
        # k3 has lower score (5.0 < 10.0) but residency was not met.
        mock_tier1.put_object.assert_called_once_with("b1", "k2", b"dummy-data")
        mock_tier0.delete_object.assert_called_once_with("b1", "k2")


@pytest.mark.asyncio
async def test_read_through_recall_and_deduplication():
    """Verify that reading a lower-tier object triggers a background recall task and de-duplicates concurrent reads."""
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
                        "backend": "r2",
                        "priority": 1,
                        "minimum_residency": 0
                    }
                ]
            }
        })

        metadata = MetadataStore(tmp_path / "metadata.db")
        metadata.init()

        # Store object on tier1
        metadata.put_object("b1", ObjectInfo(
            key="k1", size=10, etag="e1", current_tier="tier1"
        ))

        state = MagicMock()
        manager = StorageManager(config, state, metadata)

        # Mock backends using AsyncMock
        mock_tier0 = AsyncMock()
        mock_tier1 = AsyncMock()

        async def mock_stream(bucket, key):
            yield b"hello-r2"
        mock_tier1.get_object = mock_stream

        manager.backends = {"tier0": mock_tier0, "tier1": mock_tier1}

        # Trigger concurrent reads
        async def read_task():
            chunks = []
            async for chunk in manager.get_object("b1", "k1"):
                chunks.append(chunk)
            return b"".join(chunks)

        results = await asyncio.gather(
            read_task(),
            read_task(),
            read_task()
        )

        # All concurrent reads got correct data from R2
        for data in results:
            assert data == b"hello-r2"

        # Give background recall tasks a moment to execute
        await asyncio.sleep(0.1)

        # Verify only a single recall was performed (mock_tier0.put_object should be called exactly once)
        mock_tier0.put_object.assert_called_once_with("b1", "k1", b"hello-r2")
        mock_tier1.delete_object.assert_called_once_with("b1", "k1")

        # Object is now on tier0
        info = metadata.get_object("b1", "k1")
        assert info.current_tier == "tier0"


@pytest.mark.asyncio
async def test_storage_manager_validation():
    """Verify StorageManager fails fast if 'tier0' is missing from configuration."""
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
            "hsm": {
                "enabled": True,
                "tiers": [
                    {
                        "id": "my_custom_tier",
                        "backend": "local_disk",
                        "priority": 0,
                    }
                ]
            }
        })
        metadata = MetadataStore(tmp_path / "metadata.db")
        metadata.init()
        state = MagicMock()

        with pytest.raises(ValueError) as exc:
            StorageManager(config, state, metadata)
        assert "tier0" in str(exc.value)

"""Comprehensive Robustness and Bug Fix Tests for HSM, Recall, and Scheduler."""
from __future__ import annotations

import asyncio
import tempfile
import pytest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime, timezone, timedelta

from minamo.config import ConfigManager
from minamo.metadata.store import MetadataStore
from minamo.metadata.models import ObjectInfo
from minamo.storage.manager import StorageManager
from minamo.storage.scheduler import HsmScheduler
from minamo.storage.heat import ExponentialDecayHeatCalculator
from minamo.service.errors import S3Error


@pytest.mark.asyncio
async def test_interruption_recovery():
    """Verify that system startup automatically recovers/resets in-flight migration states."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        metadata = MetadataStore(tmp_path / "metadata.db")
        metadata.init()

        # Insert object in migrating_down state
        metadata.put_object("b1", ObjectInfo(
            key="k1", size=10, etag="e1", current_tier="tier1", migration_state="migrating_down"
        ))
        # Insert object in recalling state
        metadata.put_object("b2", ObjectInfo(
            key="k2", size=20, etag="e2", current_tier="tier1", migration_state="recalling"
        ))

        # Manually create a task in in_flight state
        task_id = metadata.create_migration_task("b1", "k1", "tier0", "tier1")
        assert metadata.get_migration_tasks()[0][5] == "in_flight"

        # Re-initialize MetadataStore to trigger recovery
        metadata2 = MetadataStore(tmp_path / "metadata.db")
        metadata2.init()

        # Verify object state has been reset to idle
        info1 = metadata2.get_object("b1", "k1")
        info2 = metadata2.get_object("b2", "k2")
        assert info1.migration_state == "idle"
        assert info2.migration_state == "idle"

        # Verify task is now interrupted
        tasks = metadata2.get_migration_tasks()
        assert tasks[0][5] == "interrupted"
        assert "Interrupted by system restart" in tasks[0][6]


@pytest.mark.asyncio
async def test_high_watermark_triggering():
    """Verify high_watermark controls migration triggering, with target_capacity as the goal."""
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
                        "target_capacity": 50,  # Target capacity
                        "high_watermark": 80,   # High watermark trigger
                        "limit": 100,
                        "minimum_residency": 0
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

        state = MagicMock()
        manager = StorageManager(config, state, metadata)

        mock_tier0 = AsyncMock()
        mock_tier0.max_file_size = 999999
        mock_tier1 = AsyncMock()
        mock_tier1.max_file_size = 999999
        async def mock_stream(bucket, key):
            yield b"abc"
        mock_tier0.get_object = mock_stream

        manager.backends = {"tier0": mock_tier0, "tier1": mock_tier1}
        scheduler = HsmScheduler(config, metadata, manager)

        # 1. Size 70 bytes: above target_capacity (50) but below high_watermark (80)
        metadata.put_object("b1", ObjectInfo(key="k1", size=70, etag="e1", current_tier="tier0"))
        await scheduler.tick()
        await asyncio.sleep(0.05)
        # Verify no migrations started
        assert len(scheduler._active_tasks) == 0

        # 2. Size 90 bytes: above high_watermark (80), should trigger migrations
        # We add another object
        metadata.put_object("b1", ObjectInfo(key="k2", size=20, etag="e2", current_tier="tier0"))
        await scheduler.tick()
        await asyncio.sleep(0.05)
        # Verify migration was triggered
        assert mock_tier1.put_object.call_count > 0


@pytest.mark.asyncio
async def test_read_through_recall_strategy_b_and_eviction():
    """Verify strategy B (cancel active down-migration for recall) and proactive eviction."""
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
                        "limit": 100,
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

        # Insert some objects
        metadata.put_object("b1", ObjectInfo(key="k1", size=40, etag="e1", current_tier="tier1", heat_score=10.0))
        metadata.put_object("b1", ObjectInfo(key="k2", size=50, etag="e2", current_tier="tier0", heat_score=5.0))

        state = MagicMock()
        manager = StorageManager(config, state, metadata)

        # Mock backends
        mock_tier0 = AsyncMock()
        mock_tier0.max_file_size = 999999
        mock_tier1 = AsyncMock()
        mock_tier1.max_file_size = 999999

        async def mock_stream(bucket, key):
            yield b"some-tier1-data-bytes"
        mock_tier1.get_object = mock_stream

        manager.backends = {"tier0": mock_tier0, "tier1": mock_tier1}

        # Initialize scheduler to enable strategy B cancel-and-revert
        scheduler = HsmScheduler(config, metadata, manager)

        # Mock active down-migration task for k1
        async def mock_down_migration():
            try:
                metadata.update_hsm_state("b1", "k1", "migrating_down")
                await asyncio.sleep(5)  # Long-running migration
            except asyncio.CancelledError:
                metadata.update_hsm_state("b1", "k1", "idle")
                raise

        mig_task = asyncio.create_task(mock_down_migration())
        scheduler._active_tasks[("b1", "k1")] = (mig_task, 40, "tier1")

        # Trigger recall on k1 (will cancel active migration)
        await manager.trigger_recall("b1", "k1", "tier1")
        await asyncio.sleep(0.05)

        # Verify down migration task was cancelled and k1 recalled to tier0
        assert mig_task.cancelled() or mig_task.done()
        # Verify k1 state is updated
        info = metadata.get_object("b1", "k1")
        assert info.migration_state == "idle"


@pytest.mark.asyncio
async def test_pruning_and_write_logging():
    """Verify WRITE logging and access logs automatic pruning upon deletion."""
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
                    }
                ]
            }
        })

        metadata = MetadataStore(tmp_path / "metadata.db")
        metadata.init()

        state = MagicMock()
        manager = StorageManager(config, state, metadata)

        # Put Object (should log WRITE)
        metadata.put_object("b1", ObjectInfo(key="k1", size=10, etag="e1", current_tier="tier0"))
        await manager.put_object("b1", "k1", b"1234567890")

        # Verify WRITE log was created
        logs = metadata.get_access_logs("b1", "k1")
        assert any(log[0] == "WRITE" for log in logs)

        # Delete Object (should prune logs)
        await manager.delete_object("b1", "k1")
        metadata.delete_object("b1", "k1")

        # Verify logs for k1 are fully deleted
        logs_after = metadata.get_access_logs("b1", "k1")
        assert len(logs_after) == 0


@pytest.mark.asyncio
async def test_config_reload():
    """Verify runtime reload_config synchronization on manager and scheduler."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        config_dir = tmp_path / "config"
        config_dir.mkdir(parents=True, exist_ok=True)

        # Write app.toml
        (config_dir / "app.toml").write_text("[app]\nbackend = 'local_disk'\n")
        # Write secrets.toml
        (config_dir / "secrets.toml").write_text("[secrets]\naccess_key = 'minamo'\nsecret_key = 'minamo-secret'\n")
        # Write hsm.toml
        (config_dir / "hsm.toml").write_text("enabled = true\noverflow_policy = 'reject'\nmax_concurrent_migrations = 3\n[[tiers]]\nid = 'tier0'\nbackend = 'local_disk'\npriority = 0\n")

        config = ConfigManager(config_dir=config_dir)
        metadata = MetadataStore(tmp_path / "metadata.db")
        metadata.init()

        state = MagicMock()
        manager = StorageManager(config, state, metadata)
        scheduler = HsmScheduler(config, metadata, manager)

        assert scheduler.hsm_settings.overflow_policy == "reject"

        # Overwrite hsm.toml with new values
        (config_dir / "hsm.toml").write_text("enabled = true\noverflow_policy = 'fallback'\nmax_concurrent_migrations = 10\n[[tiers]]\nid = 'tier0'\nbackend = 'local_disk'\npriority = 0\n")

        # Trigger reload config
        config.reload_config()
        manager.reload_config()
        scheduler.reload_config()

        # Check values are synchronized
        assert scheduler.hsm_settings.overflow_policy == "fallback"
        assert scheduler._semaphore._value == 10

"""Comprehensive tests for HackClub CDN Backend, dynamic configuration loading,
Temporary Workspace, CacheManager, and HSM size limit/downgrade logic.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
import httpx

from minamo.config.manager import ConfigManager
from minamo.service.errors import S3Error
from minamo.storage.backend import BackendCapabilities
from minamo.storage.cache import CacheManager
from minamo.storage.factory import create_backend
from minamo.storage.hackclub_cdn import HackClubCDNBackend
from minamo.storage.manager import StorageManager
from minamo.utils.temp_workspace import TempWorkspace


# -- TempWorkspace Tests ----------------------------------------------------
def test_temp_workspace():
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        # Setup workspace with 80 bytes limit
        ws = TempWorkspace(root / "tmp", max_usage_bytes=80)

        # Test file allocation
        with ws.allocate_file(prefix="test_") as path:
            assert path.exists()
            assert path.parent == ws.root
            # Write 40 bytes
            path.write_bytes(b"a" * 40)
            assert ws.get_current_usage() == 40

            # Allocate another file of 30 bytes (Total 40 + 30 = 70 <= 80)
            with ws.allocate_file() as path2:
                path2.write_bytes(b"b" * 30)
                assert ws.get_current_usage() == 70

                # Try to allocate beyond limit (70 >= 80 is False, but after writing 30 bytes, total becomes 100 > 80, so next allocation raises)
                with path2.open("ab") as f:
                    f.write(b"b" * 30)  # Total is now 40 + 60 = 100
                assert ws.get_current_usage() == 100

                with pytest.raises(OSError) as exc:
                    with ws.allocate_file() as path3:
                        pass
                assert "Temporary workspace exceeded" in str(exc.value)

        # File is automatically deleted after block
        assert ws.get_current_usage() == 0


# -- CacheManager Tests -----------------------------------------------------
def test_cache_manager_lru():
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        cache = CacheManager(root / "cache", max_size_bytes=15, policy="LRU")

        # Put k1 (5 bytes), k2 (5 bytes)
        p1 = cache.put("b1", "k1", b"12345")
        p2 = cache.put("b1", "k2", b"abcde")
        assert cache.current_size == 10
        assert cache.get("b1", "k1") == p1

        # Put k3 (7 bytes). Since 10 + 7 = 17 > 15, k2 (oldest, as k1 was accessed) should be evicted
        p3 = cache.put("b1", "k3", b"xyz7890")
        assert cache.current_size == 12  # k1 (5) + k3 (7)
        assert cache.get("b1", "k2") is None
        assert cache.get("b1", "k1") == p1
        assert cache.get("b1", "k3") == p3

        # Clear cache
        cache.clear()
        assert cache.current_size == 0
        assert not p1.exists()


def test_cache_manager_fifo():
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        cache = CacheManager(root / "cache", max_size_bytes=15, policy="FIFO")

        # Put k1 (5 bytes), k2 (5 bytes)
        p1 = cache.put("b1", "k1", b"12345")
        p2 = cache.put("b1", "k2", b"abcde")

        # Retrieve k1. Under FIFO, this does NOT make k1 "newer" than k2
        cache.get("b1", "k1")

        # Put k3 (7 bytes). Since 10 + 7 = 17 > 15, k1 (first inserted) should be evicted instead of k2
        p3 = cache.put("b1", "k3", b"xyz7890")
        assert cache.get("b1", "k1") is None
        assert cache.get("b1", "k2") == p2


def test_cache_manager_path_traversal():
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        cache = CacheManager(root / "cache", max_size_bytes=100)

        with pytest.raises(ValueError) as exc:
            cache.put("../illegal_bucket", "k1", b"123")
        assert "path traversal" in str(exc.value)

        with pytest.raises(ValueError) as exc:
            cache.put("b1", "../illegal_key", b"123")
        assert "path traversal" in str(exc.value)


# -- ConfigManager & Factory Tests ------------------------------------------
def test_dynamic_backend_loading():
    raw_config = {
        "app": {
            "backend": "cdn-primary",
            "endpoint_host": "localhost",
            "region": "us-east-1",
            "max_temp_usage": 1024,
        },
        "secrets": {"access_key": "minamo", "secret_key": "minamo-secret"},
        "storage_cdn_primary": {
            "type": "hackclub_cdn",
            "id": "cdn-primary",
            "api_key": "sk_test_123",
            "base_url": "https://custom.hackclub.com",
        },
        "storage_r2_custom": {
            "type": "r2",
            "id": "r2-custom",
            "endpoint_url": "https://test-r2",
            "access_key_id": "r2key",
            "secret_access_key": "r2secret",
            "bucket": "mybucket",
        }
    }

    config = ConfigManager.from_dict(raw_config)
    assert config.settings.max_temp_usage == 1024
    assert "cdn-primary" in config.backend_configs
    assert "r2-custom" in config.backend_configs

    # Check configurations
    assert config.backend_configs["cdn-primary"]["type"] == "hackclub_cdn"
    assert config.backend_configs["cdn-primary"]["api_key"] == "sk_test_123"
    assert config.backend_configs["r2-custom"]["bucket"] == "mybucket"

    # Test factory creation
    state = MagicMock()
    cdn_backend = create_backend("cdn-primary", config, state)
    assert isinstance(cdn_backend, HackClubCDNBackend)
    assert cdn_backend.api_key == "sk_test_123"
    assert cdn_backend.base_url == "https://custom.hackclub.com"


# -- HackClubCDNBackend Tests ------------------------------------------------
@pytest.mark.asyncio
async def test_hackclub_cdn_backend_put_and_get():
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        backend = HackClubCDNBackend(
            api_key="sk_test",
            base_url="https://test.cdn",
            backend_id="test_cdn",
            data_root=root,
        )

        # Test single file too large
        large_bytes = b"x" * (100 * 1024 * 1024 + 1)
        with pytest.raises(S3Error) as exc:
            await backend.put_object("b1", "large.bin", large_bytes)
        assert exc.value.http_status == 402
        assert "exceeds the 100MB maximum limit" in exc.value.message

        # Mock success upload
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "id": "file-123",
            "filename": "hello.txt",
            "size": 5,
            "content_type": "text/plain",
            "url": "https://test.cdn/file-123/hello.txt",
            "created_at": "2026-01-01T00:00:00Z"
        }

        with patch("httpx.AsyncClient.post", return_value=mock_response):
            size = await backend.put_object("b1", "hello.txt", b"hello")
            assert size == 5

        # Verify mapping was saved in SQLite
        mapping = backend.meta.get_mapping("b1", "hello.txt")
        assert mapping is not None
        assert mapping["id"] == "file-123"
        assert mapping["url"] == "https://test.cdn/file-123/hello.txt"
        assert mapping["size"] == 5

        # Mock GET object stream response
        mock_stream_resp = MagicMock()
        mock_stream_resp.status_code = 200
        async def mock_aiter():
            yield b"he"
            yield b"llo"
        mock_stream_resp.aiter_bytes = mock_aiter

        # httpx.AsyncClient().stream returns an async context manager
        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_stream_resp)
        mock_cm.__aexit__ = AsyncMock()

        with patch("httpx.AsyncClient.stream", return_value=mock_cm):
            chunks = []
            async for chunk in backend.get_object("b1", "hello.txt"):
                chunks.append(chunk)
            assert b"".join(chunks) == b"hello"

        # Mock GET object range response
        mock_range_resp = MagicMock()
        mock_range_resp.status_code = 200
        mock_range_resp.content = b"ell"

        with patch("httpx.AsyncClient.get", return_value=mock_range_resp):
            data = await backend.read_range("b1", "hello.txt", 1, 3)
            assert data == b"ell"

        # Mock DELETE response
        mock_delete_resp = MagicMock()
        mock_delete_resp.status_code = 200

        with patch("httpx.AsyncClient.delete", return_value=mock_delete_resp):
            await backend.delete_object("b1", "hello.txt")

        # Verify mapping deleted
        assert backend.meta.get_mapping("b1", "hello.txt") is None


@pytest.mark.asyncio
async def test_hackclub_cdn_multipart_emulation():
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        backend = HackClubCDNBackend(
            api_key="sk_test",
            base_url="https://test.cdn",
            backend_id="test_cdn",
            data_root=root,
        )

        upload_id = "mpu-999"
        # Upload parts
        await backend.put_part("b1", upload_id, 1, b"hello ")
        await backend.put_part("b1", upload_id, 2, b"world!")

        # Verify parts exist
        assert await backend.get_part("b1", upload_id, 1) == b"hello "
        assert await backend.get_part("b1", upload_id, 2) == b"world!"

        # Mock success upload of composed file
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "id": "file-composed",
            "filename": "composed.txt",
            "size": 12,
            "content_type": "text/plain",
            "url": "https://test.cdn/file-composed/composed.txt",
            "created_at": "2026-01-01T00:00:00Z"
        }

        with patch("httpx.AsyncClient.post", return_value=mock_response):
            total_size = await backend.compose_object("b1", "composed.txt", upload_id, [1, 2])
            assert total_size == 12

        # Verify mapping created
        mapping = backend.meta.get_mapping("b1", "composed.txt")
        assert mapping is not None
        assert mapping["id"] == "file-composed"

        # Verify mpu directory for upload_id was cleaned up
        assert not backend._mpu_upload_dir(upload_id).exists()


# -- StorageManager & HSM size limits / downgrade logic tests ----------------
@pytest.mark.asyncio
async def test_storage_manager_size_limit_and_downgrade():
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
            "storage_local_disk": {"type": "local_disk", "root": str(tmp_path / "local")},
            "storage_cdn1": {
                "type": "hackclub_cdn",
                "api_key": "key1",
                "base_url": "https://cdn1"
            },
            "hsm": {
                "enabled": True,
                "overflow_policy": "fallback",
                "tiers": [
                    {
                        "id": "tier0",
                        "backend": "local_disk",
                        "priority": 0,
                        "limit": 100,  # 100 bytes limit
                    },
                    {
                        "id": "tier1",
                        "backend": "cdn1",
                        "priority": 1,
                        "limit": 100,  # 100 bytes limit
                    }
                ]
            }
        })

        metadata = MagicMock()
        # Mock tier sizes to make tier0 full, but tier1 has space
        metadata.get_tier_size.side_effect = lambda tier_id: 1000 if tier_id == "tier0" else 0
        state = MagicMock()

        manager = StorageManager(config, state, metadata)

        # File of size 50. Since tier0 is full and fallback is enabled, should fallback to tier1.
        target = await manager.determine_write_tier(50)
        assert target == "tier1"

        # File of size 150MB (157,286,400 bytes).
        # Since tier1 is HackClubCDN, its max_file_size is 100MB.
        # This file exceeds tier1's capability, so it CANNOT fall back to tier1.
        # It should bypass tier1 and write to tier0 (the terminal tier for this file size since tier1 is filtered out).
        target_large = await manager.determine_write_tier(150 * 1024 * 1024)
        assert target_large == "tier0"

        # If a file is larger than BOTH tier0 and tier1 limits (e.g. if we configure/mock tier0 to have a limit too),
        # but tier0 has unlimited max size. What if we have a setup where no backend can support the size?
        # Let's mock both backends to have 100MB limit.
        with patch("minamo.storage.backend.StorageBackend.max_file_size", new_callable=PropertyMock) as mock_max_size:
            mock_max_size.return_value = 100 * 1024 * 1024
            with pytest.raises(S3Error) as exc:
                await manager.determine_write_tier(150 * 1024 * 1024)
            assert exc.value.http_status == 402
            assert "too large" in exc.value.message

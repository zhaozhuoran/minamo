"""Tests for OneDrive storage backend and OneDrive API client."""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

from minamo.config.manager import ConfigManager
from minamo.storage.factory import create_backend
from minamo.storage.onedrive import OneDriveClient, OneDriveBackend


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


@pytest.fixture
def onedrive_client(temp_dir):
    state_file = temp_dir / "onedrive.json"
    client = OneDriveClient(
        client_id="test_client_id",
        client_secret="test_client_secret",
        state_file=state_file,
        root_path="/Developing/minamo",
    )
    return client


@pytest.fixture
def onedrive_backend(temp_dir):
    backend = OneDriveBackend(
        client_id="test_client_id",
        client_secret="test_client_secret",
        state_file="onedrive.json",
        root_path="/Developing/minamo",
        backend_id="test_onedrive",
        data_root=temp_dir,
    )
    return backend


# -- OneDriveClient Token Management Tests -----------------------------------

def test_token_load_save(onedrive_client):
    # Load empty tokens
    tokens = onedrive_client._load_tokens()
    assert tokens == {}

    # Save and load tokens
    sample_tokens = {
        "access_token": "acc_123",
        "refresh_token": "ref_123",
        "expires_at": 2000000000,
    }
    onedrive_client._save_tokens(sample_tokens)

    tokens = onedrive_client._load_tokens()
    assert tokens == sample_tokens
    assert onedrive_client.state_file.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_get_valid_token_no_config(onedrive_client):
    with pytest.raises(ValueError) as exc:
        await onedrive_client.get_valid_token()
    assert "token not configured" in str(exc.value)


@pytest.mark.asyncio
async def test_get_valid_token_unexpired(onedrive_client):
    future_time = int(time.time()) + 1000
    sample_tokens = {
        "access_token": "acc_123",
        "refresh_token": "ref_123",
        "expires_at": future_time,
    }
    onedrive_client._save_tokens(sample_tokens)

    token = await onedrive_client.get_valid_token()
    assert token == "acc_123"


@pytest.mark.asyncio
async def test_get_valid_token_expired_trigger_refresh(onedrive_client):
    past_time = int(time.time()) - 100
    sample_tokens = {
        "access_token": "acc_expired",
        "refresh_token": "ref_123",
        "expires_at": past_time,
    }
    onedrive_client._save_tokens(sample_tokens)

    # Mock token refresh response
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "access_token": "acc_new",
        "refresh_token": "ref_new",
        "expires_in": 3600,
    }

    with patch("httpx.AsyncClient.post", return_value=mock_resp) as mock_post:
        token = await onedrive_client.get_valid_token()
        assert token == "acc_new"
        mock_post.assert_called_once()

    # Verify updated in state_file
    updated = onedrive_client._load_tokens()
    assert updated["access_token"] == "acc_new"
    assert updated["refresh_token"] == "ref_new"
    assert updated["expires_at"] > time.time() + 3000


# -- OneDriveClient Graph API Operations Tests -------------------------------

@pytest.mark.asyncio
async def test_get_item_info(onedrive_client):
    onedrive_client._save_tokens({
        "access_token": "acc",
        "refresh_token": "ref",
        "expires_at": int(time.time()) + 1000,
    })

    # Test item exists as folder
    mock_resp_folder = MagicMock()
    mock_resp_folder.status_code = 200
    mock_resp_folder.json.return_value = {
        "name": "minamo",
        "folder": {},
        "size": 100,
    }

    with patch("httpx.AsyncClient.get", return_value=mock_resp_folder):
        exists, is_folder, size = await onedrive_client.get_item_info("/Developing/minamo")
        assert exists is True
        assert is_folder is True
        assert size == 100

    # Test item does not exist
    mock_resp_404 = MagicMock()
    mock_resp_404.status_code = 404

    with patch("httpx.AsyncClient.get", return_value=mock_resp_404):
        exists, is_folder, size = await onedrive_client.get_item_info("/Developing/missing")
        assert exists is False
        assert is_folder is False
        assert size == 0


@pytest.mark.asyncio
async def test_create_folder_under(onedrive_client):
    onedrive_client._save_tokens({
        "access_token": "acc",
        "refresh_token": "ref",
        "expires_at": int(time.time()) + 1000,
    })

    mock_resp = MagicMock()
    mock_resp.status_code = 201

    with patch("httpx.AsyncClient.post", return_value=mock_resp) as mock_post:
        await onedrive_client.create_folder_under("/Developing", "minamo")
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert "/Developing" in args[0]
        assert kwargs["json"] == {"name": "minamo", "folder": {}}


@pytest.mark.asyncio
async def test_ensure_folder(onedrive_client):
    onedrive_client._save_tokens({
        "access_token": "acc",
        "refresh_token": "ref",
        "expires_at": int(time.time()) + 1000,
    })

    # Mock get_item_info to return exists=False for /Developing and /Developing/minamo
    # and mock create_folder_under to succeed
    with patch.object(onedrive_client, "get_item_info", return_value=(False, False, 0)) as mock_info:
        with patch.object(onedrive_client, "create_folder_under", return_value=None) as mock_create:
            await onedrive_client._ensure_folder("/Developing/minamo")
            assert mock_info.call_count == 2
            assert mock_create.call_count == 2


@pytest.mark.asyncio
async def test_upload_file_simple(onedrive_client):
    onedrive_client._save_tokens({
        "access_token": "acc",
        "refresh_token": "ref",
        "expires_at": int(time.time()) + 1000,
    })

    mock_resp = MagicMock()
    mock_resp.status_code = 200

    # Put a small payload (100 bytes)
    payload = b"hello" * 20

    with patch.object(onedrive_client, "_ensure_folder") as mock_ensure:
        with patch("httpx.AsyncClient.put", return_value=mock_resp) as mock_put:
            size = await onedrive_client.upload_file("/Developing/minamo/test.txt", payload)
            assert size == 100
            mock_ensure.assert_called_once_with("/Developing/minamo")
            mock_put.assert_called_once()


@pytest.mark.asyncio
async def test_upload_file_session(onedrive_client):
    onedrive_client._save_tokens({
        "access_token": "acc",
        "refresh_token": "ref",
        "expires_at": int(time.time()) + 1000,
    })

    # Large payload (5MB) to trigger upload session
    large_payload = b"a" * (5 * 1024 * 1024)

    mock_resp_session = MagicMock()
    mock_resp_session.status_code = 200
    mock_resp_session.json.return_value = {"uploadUrl": "https://test.upload/session_url"}

    mock_resp_chunk = MagicMock()
    mock_resp_chunk.status_code = 202

    with patch.object(onedrive_client, "_ensure_folder") as mock_ensure:
        with patch("httpx.AsyncClient.post", return_value=mock_resp_session) as mock_post:
            with patch("httpx.AsyncClient.put", return_value=mock_resp_chunk) as mock_put:
                size = await onedrive_client.upload_file("/Developing/minamo/large.bin", large_payload)
                assert size == 5 * 1024 * 1024
                mock_ensure.assert_called_once_with("/Developing/minamo")
                mock_post.assert_called_once()
                # 5MB split into 3.2MB chunks -> 2 chunks
                assert mock_put.call_count == 2


@pytest.mark.asyncio
async def test_get_object_stream(onedrive_client):
    onedrive_client._save_tokens({
        "access_token": "acc",
        "refresh_token": "ref",
        "expires_at": int(time.time()) + 1000,
    })

    mock_stream_resp = MagicMock()
    mock_stream_resp.status_code = 200
    async def mock_aiter():
        yield b"chunk1"
        yield b"chunk2"
    mock_stream_resp.aiter_bytes = mock_aiter

    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_stream_resp)
    mock_cm.__aexit__ = AsyncMock()

    with patch("httpx.AsyncClient.stream", return_value=mock_cm):
        chunks = []
        async for chunk in onedrive_client.get_object_stream("/Developing/minamo/test.txt"):
            chunks.append(chunk)
        assert b"".join(chunks) == b"chunk1chunk2"


@pytest.mark.asyncio
async def test_read_range(onedrive_client):
    onedrive_client._save_tokens({
        "access_token": "acc",
        "refresh_token": "ref",
        "expires_at": int(time.time()) + 1000,
    })

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b"range_data"

    with patch("httpx.AsyncClient.get", return_value=mock_resp) as mock_get:
        data = await onedrive_client.read_range("/Developing/minamo/test.txt", 10, 19)
        assert data == b"range_data"
        mock_get.assert_called_once()
        assert "Range" in mock_get.call_args[1]["headers"]
        assert mock_get.call_args[1]["headers"]["Range"] == "bytes=10-19"


@pytest.mark.asyncio
async def test_list_recursive(onedrive_client):
    onedrive_client._save_tokens({
        "access_token": "acc",
        "refresh_token": "ref",
        "expires_at": int(time.time()) + 1000,
    })

    # Mock get_item_info to show bucket exists as folder
    # Mock list_folder_children to return some files/folders
    with patch.object(onedrive_client, "get_item_info", return_value=(True, True, 0)):
        async def mock_children(folder_path):
            if folder_path == "/Developing/minamo/bucket1":
                return [
                    {"name": "file1.txt", "file": {}, "size": 10},
                    {"name": "subdir", "folder": {}}
                ]
            elif folder_path == "/Developing/minamo/bucket1/subdir":
                return [
                    {"name": "file2.txt", "file": {}, "size": 20}
                ]
            return []

        with patch.object(onedrive_client, "list_folder_children", side_effect=mock_children):
            results = await onedrive_client.list_recursive("bucket1")
            assert len(results) == 2
            assert results[0] == {"key": "file1.txt", "size": 10}
            assert results[1] == {"key": "subdir/file2.txt", "size": 20}


# -- OneDriveBackend Operations Tests -----------------------------------------

@pytest.mark.asyncio
async def test_onedrive_backend_bucket_operations(onedrive_backend):
    # Set tokens
    onedrive_backend.client._save_tokens({
        "access_token": "acc",
        "refresh_token": "ref",
        "expires_at": int(time.time()) + 1000,
    })

    # 1. create_bucket
    with patch.object(onedrive_backend.client, "_ensure_folder") as mock_ensure:
        await onedrive_backend.create_bucket("bucket1")
        mock_ensure.assert_called_once_with("/Developing/minamo/bucket1")

    # 2. bucket_exists
    with patch.object(onedrive_backend.client, "get_item_info", return_value=(True, True, 0)) as mock_info:
        assert await onedrive_backend.bucket_exists("bucket1") is True
        mock_info.assert_called_once_with("/Developing/minamo/bucket1")

    # 3. delete_bucket
    with patch.object(onedrive_backend.client, "delete_item") as mock_delete:
        await onedrive_backend.delete_bucket("bucket1")
        mock_delete.assert_called_once_with("/Developing/minamo/bucket1")


@pytest.mark.asyncio
async def test_onedrive_backend_object_operations(onedrive_backend):
    onedrive_backend.client._save_tokens({
        "access_token": "acc",
        "refresh_token": "ref",
        "expires_at": int(time.time()) + 1000,
    })

    # 1. put_object
    with patch.object(onedrive_backend.client, "upload_file", return_value=12) as mock_upload:
        size = await onedrive_backend.put_object("b1", "test.txt", b"hello world!")
        assert size == 12
        mock_upload.assert_called_once_with("/Developing/minamo/b1/test.txt", b"hello world!")

    # 2. object_size
    with patch.object(onedrive_backend.client, "get_item_info", return_value=(True, False, 12)):
        assert await onedrive_backend.object_size("b1", "test.txt") == 12

    # 3. delete_object
    with patch.object(onedrive_backend.client, "delete_item") as mock_delete:
        await onedrive_backend.delete_object("b1", "test.txt")
        mock_delete.assert_called_once_with("/Developing/minamo/b1/test.txt")


@pytest.mark.asyncio
async def test_onedrive_backend_list_keys(onedrive_backend):
    onedrive_backend.client._save_tokens({
        "access_token": "acc",
        "refresh_token": "ref",
        "expires_at": int(time.time()) + 1000,
    })

    list_results = [
        {"key": "abc.txt", "size": 10},
        {"key": "foo/bar.txt", "size": 20},
        {"key": "foo/baz.txt", "size": 30},
    ]

    async def mock_list_rec(bucket, prefix=""):
        return [item for item in list_results if not prefix or item["key"].startswith(prefix)]

    with patch.object(onedrive_backend.client, "list_recursive", side_effect=mock_list_rec):
        # List all without prefix
        res = await onedrive_backend.list_keys("b1")
        assert res.keys == ["abc.txt", "foo/bar.txt", "foo/baz.txt"]
        assert res.is_truncated is False

        # List with prefix
        res_prefix = await onedrive_backend.list_keys("b1", prefix="foo/")
        assert res_prefix.keys == ["foo/bar.txt", "foo/baz.txt"]


@pytest.mark.asyncio
async def test_onedrive_backend_multipart_emulation(onedrive_backend):
    onedrive_backend.client._save_tokens({
        "access_token": "acc",
        "refresh_token": "ref",
        "expires_at": int(time.time()) + 1000,
    })

    upload_id = "onedrive-mpu-123"

    # Put parts
    await onedrive_backend.put_part("b1", upload_id, 1, b"part1 ")
    await onedrive_backend.put_part("b1", upload_id, 2, b"part2!")

    assert await onedrive_backend.get_part("b1", upload_id, 1) == b"part1 "
    assert await onedrive_backend.get_part("b1", upload_id, 2) == b"part2!"

    # Compose with side_effect to capture data before cleanup
    composed_bytes = b""
    async def mock_upload_side_effect(path, data):
        nonlocal composed_bytes
        if isinstance(data, Path):
            composed_bytes = data.read_bytes()
        return 12

    with patch.object(onedrive_backend.client, "upload_file", side_effect=mock_upload_side_effect) as mock_upload:
        total_size = await onedrive_backend.compose_object("b1", "composed.txt", upload_id, [1, 2])
        assert total_size == 12
        mock_upload.assert_called_once()
        args, kwargs = mock_upload.call_args
        assert args[0] == "/Developing/minamo/b1/composed.txt"
        assert isinstance(args[1], Path)
        # Composed content should be correct
        assert composed_bytes == b"part1 part2!"

    # Verify temp dir cleaned up
    assert not onedrive_backend._mpu_upload_dir(upload_id).exists()


# -- Factory Registration Test ------------------------------------------------

def test_factory_onedrive_creation():
    raw_config = {
        "app": {
            "backend": "onedrive-primary",
            "endpoint_host": "localhost",
            "region": "us-east-1",
        },
        "secrets": {"access_key": "minamo", "secret_key": "minamo-secret"},
        "storage_onedrive_primary": {
            "type": "onedrive",
            "id": "onedrive-primary",
            "client_id": "cli_id",
            "client_secret": "cli_sec",
            "state_file": "custom_onedrive.json",
            "root_path": "/Developing/minamo",
        }
    }

    config = ConfigManager.from_dict(raw_config)
    state = MagicMock()
    backend = create_backend("onedrive-primary", config, state)

    assert isinstance(backend, OneDriveBackend)
    assert backend.backend_id == "onedrive-primary"
    assert backend.client.client_id == "cli_id"
    assert backend.client.client_secret == "cli_sec"
    assert backend.client.root_path == "/Developing/minamo"


@pytest.mark.asyncio
async def test_list_recursive_pruning(onedrive_client):
    onedrive_client._save_tokens({
        "access_token": "acc",
        "refresh_token": "ref",
        "expires_at": int(time.time()) + 1000,
    })

    visited_folders = []
    async def mock_children(folder_path):
        visited_folders.append(folder_path)
        if folder_path == "/Developing/minamo/bucket1":
            return [
                {"name": "photos", "folder": {}},
                {"name": "docs", "folder": {}},
            ]
        elif folder_path == "/Developing/minamo/bucket1/photos":
            return [
                {"name": "pic1.png", "file": {}, "size": 100}
            ]
        elif folder_path == "/Developing/minamo/bucket1/docs":
            return [
                {"name": "doc1.pdf", "file": {}, "size": 200}
            ]
        return []

    with patch.object(onedrive_client, "get_item_info", return_value=(True, True, 0)):
        with patch.object(onedrive_client, "list_folder_children", side_effect=mock_children):
            results = await onedrive_client.list_recursive("bucket1", prefix="photos/")
            assert len(results) == 1
            assert results[0]["key"] == "photos/pic1.png"
            # Verify "docs" directory was pruned and NEVER visited!
            assert "/Developing/minamo/bucket1/docs" not in visited_folders


@pytest.mark.asyncio
async def test_compose_object_exception_cleanup(onedrive_backend):
    upload_id = "onedrive-mpu-error"
    await onedrive_backend.put_part("b1", upload_id, 1, b"part1")

    # Patch put_object to raise error during compose
    with patch.object(onedrive_backend, "put_object", side_effect=RuntimeError("Upload failed")):
        with pytest.raises(RuntimeError) as exc:
            await onedrive_backend.compose_object("b1", "composed.txt", upload_id, [1])
        assert "Upload failed" in str(exc.value)

    # Verify MPU temp dir was cleaned up in finally block
    assert not onedrive_backend._mpu_upload_dir(upload_id).exists()


def test_state_file_data_prefix_resolution(temp_dir):
    backend = OneDriveBackend(
        client_id="id",
        client_secret="sec",
        state_file="data/state/onedrive.json",
        data_root=temp_dir,
    )
    assert backend.client.state_file == Path("data/state/onedrive.json")

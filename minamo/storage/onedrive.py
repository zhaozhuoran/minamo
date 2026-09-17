"""Microsoft OneDrive storage backend.

Integrates with Microsoft Graph API to store and retrieve files in OneDrive.
Supports automatic token refreshing, large file upload sessions, chunked
streaming download, and range downloads.

Multipart upload is emulated locally, caching parts in a local workspace and
streaming the consolidated file to OneDrive upon completion.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.parse
from pathlib import Path, PurePosixPath
from typing import AsyncIterator, List, Optional, Dict
import httpx

from .backend import StorageBackend, ListResult, BackendCapabilities


class OneDriveClient:
    """A modular OneDrive API client handling Graph API requests and Token Management."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        state_file: Path,
        root_path: str = "/Developing/minamo",
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.state_file = Path(state_file)
        self.root_path = "/" + root_path.strip("/")
        self._lock = asyncio.Lock()

    def _load_tokens(self) -> dict:
        if not self.state_file.exists():
            return {}
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_tokens(self, tokens: dict) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(tokens, indent=2), encoding="utf-8")
            tmp.replace(self.state_file)
            try:
                os.chmod(self.state_file, 0o600)
            except OSError:
                pass
        except Exception:
            pass

    async def get_valid_token(self) -> str:
        async with self._lock:
            tokens = self._load_tokens()
            if not tokens:
                raise ValueError("OneDrive token not configured. Please run configuration tool first.")

            refresh_token = tokens.get("refresh_token")
            access_token = tokens.get("access_token")
            expires_at = tokens.get("expires_at", 0)

            if not access_token or time.time() > (expires_at - 300):
                if not refresh_token:
                    raise ValueError("OneDrive access token expired and no refresh token is available.")
                tokens = await self._refresh_token(refresh_token)
                access_token = tokens["access_token"]

            return access_token

    async def _refresh_token(self, refresh_token: str) -> dict:
        url = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, data=data)
            if resp.status_code != 200:
                raise RuntimeError(f"Failed to refresh OneDrive token: {resp.text}")

            res_json = resp.json()
            access_token = res_json["access_token"]
            new_refresh_token = res_json.get("refresh_token", refresh_token)
            expires_in = res_json.get("expires_in", 3600)

            expires_at = int(time.time()) + expires_in

            tokens = {
                "access_token": access_token,
                "refresh_token": new_refresh_token,
                "expires_at": expires_at,
            }
            self._save_tokens(tokens)
            return tokens

    async def get_item_info(self, path: str) -> tuple[bool, bool, int]:
        """Returns (exists, is_folder, size) for a path relative to OneDrive root."""
        token = await self.get_valid_token()
        headers = {"Authorization": f"Bearer {token}"}

        cleaned_path = "/" + path.strip("/") if path and path != "/" else ""
        if not cleaned_path:
            url = "https://graph.microsoft.com/v1.0/me/drive/root"
        else:
            url = f"https://graph.microsoft.com/v1.0/me/drive/root:{urllib.parse.quote(cleaned_path)}"

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 404:
                return False, False, 0
            if resp.status_code >= 400:
                raise RuntimeError(f"Failed to get info for OneDrive path '{path}': {resp.text}")

            data = resp.json()
            is_folder = "folder" in data
            size = data.get("size", 0)
            return True, is_folder, size

    async def create_folder_under(self, parent_path: str, folder_name: str) -> None:
        """Creates a folder under the given parent folder path."""
        token = await self.get_valid_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        cleaned_parent = "/" + parent_path.strip("/") if parent_path and parent_path != "/" else ""
        if not cleaned_parent:
            url = "https://graph.microsoft.com/v1.0/me/drive/root/children"
        else:
            url = f"https://graph.microsoft.com/v1.0/me/drive/root:{urllib.parse.quote(cleaned_parent)}:/children"

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json={"name": folder_name, "folder": {}}, headers=headers)
            if resp.status_code not in (200, 201) and resp.status_code != 409:
                raise RuntimeError(f"Failed to create OneDrive folder '{folder_name}' under '{parent_path}': {resp.text}")

    async def _ensure_folder(self, path: str) -> None:
        """Ensures that the entire folder hierarchy exists on OneDrive."""
        parts = [p for p in path.split("/") if p]
        current_path = ""
        for part in parts:
            parent_path = current_path
            current_path = f"{current_path}/{part}"
            exists, is_folder, _ = await self.get_item_info(current_path)
            if not exists:
                await self.create_folder_under(parent_path, part)

    async def upload_file(self, path: str, data: bytes | Path) -> int:
        """Uploads a file (automatically handles simple PUT or UploadSession based on file size)."""
        parent_dir = str(Path(path).parent).replace("\\", "/")
        await self._ensure_folder(parent_dir)

        if isinstance(data, Path):
            size = data.stat().st_size
        else:
            size = len(data)

        # Use simple upload if file is <= 4MB
        if size <= 4 * 1024 * 1024:
            return await self._upload_simple(path, data, size)
        else:
            return await self._upload_session(path, data, size)

    async def _upload_simple(self, path: str, data: bytes | Path, size: int) -> int:
        token = await self.get_valid_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/octet-stream",
        }
        url = f"https://graph.microsoft.com/v1.0/me/drive/root:{urllib.parse.quote(path)}:/content"

        async with httpx.AsyncClient(timeout=60.0) as client:
            if isinstance(data, Path):
                with data.open("rb") as f:
                    resp = await client.put(url, content=f, headers=headers)
            else:
                resp = await client.put(url, content=data, headers=headers)

            if resp.status_code not in (200, 201):
                raise RuntimeError(f"Failed simple upload to '{path}': {resp.text}")
            return size

    async def _upload_session(self, path: str, data: bytes | Path, size: int) -> int:
        token = await self.get_valid_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        url = f"https://graph.microsoft.com/v1.0/me/drive/root:{urllib.parse.quote(path)}:/createUploadSession"

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                url,
                json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
                headers=headers,
            )
            if resp.status_code not in (200, 201):
                raise RuntimeError(f"Failed to create upload session for '{path}': {resp.text}")

            upload_url = resp.json()["uploadUrl"]

        # Upload chunks (must be multiple of 320 KiB = 327,680 bytes)
        chunk_size = 320 * 1024 * 10  # 3.2MB
        async with httpx.AsyncClient(timeout=60.0) as client:
            if isinstance(data, Path):
                with data.open("rb") as f:
                    start = 0
                    while start < size:
                        chunk_data = f.read(chunk_size)
                        if not chunk_data:
                            break
                        end = start + len(chunk_data) - 1
                        headers = {
                            "Content-Range": f"bytes {start}-{end}/{size}",
                            "Content-Length": str(len(chunk_data)),
                        }
                        resp = await client.put(upload_url, content=chunk_data, headers=headers)
                        if resp.status_code not in (200, 201, 202):
                            raise RuntimeError(f"Failed to upload chunk {start}-{end} of '{path}': {resp.text}")
                        start = end + 1
            else:
                start = 0
                while start < size:
                    chunk_data = data[start : start + chunk_size]
                    end = start + len(chunk_data) - 1
                    headers = {
                        "Content-Range": f"bytes {start}-{end}/{size}",
                        "Content-Length": str(len(chunk_data)),
                    }
                    resp = await client.put(upload_url, content=chunk_data, headers=headers)
                    if resp.status_code not in (200, 201, 202):
                        raise RuntimeError(f"Failed to upload chunk {start}-{end} of '{path}': {resp.text}")
                    start = end + 1
        return size

    async def get_object_stream(self, path: str) -> AsyncIterator[bytes]:
        token = await self.get_valid_token()
        headers = {"Authorization": f"Bearer {token}"}
        url = f"https://graph.microsoft.com/v1.0/me/drive/root:{urllib.parse.quote(path)}:/content"

        client = httpx.AsyncClient(timeout=120.0, follow_redirects=True)
        try:
            async with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code == 404:
                    raise FileNotFoundError(f"OneDrive file not found at '{path}'")
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes():
                    yield chunk
        finally:
            await client.aclose()

    async def read_range(self, path: str, start: int, end: int) -> bytes:
        token = await self.get_valid_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Range": f"bytes={start}-{end}",
        }
        url = f"https://graph.microsoft.com/v1.0/me/drive/root:{urllib.parse.quote(path)}:/content"
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 404:
                raise FileNotFoundError(f"OneDrive file not found at '{path}'")
            resp.raise_for_status()
            return resp.content

    async def delete_item(self, path: str) -> None:
        token = await self.get_valid_token()
        headers = {"Authorization": f"Bearer {token}"}
        url = f"https://graph.microsoft.com/v1.0/me/drive/root:{urllib.parse.quote(path)}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.delete(url, headers=headers)
            if resp.status_code not in (200, 204, 404):
                raise RuntimeError(f"Failed to delete '{path}': {resp.text}")

    async def list_folder_children(self, folder_path: str) -> list[dict]:
        token = await self.get_valid_token()
        headers = {"Authorization": f"Bearer {token}"}

        cleaned_folder = "/" + folder_path.strip("/") if folder_path and folder_path != "/" else ""
        if not cleaned_folder:
            url = "https://graph.microsoft.com/v1.0/me/drive/root/children"
        else:
            url = f"https://graph.microsoft.com/v1.0/me/drive/root:{urllib.parse.quote(cleaned_folder)}:/children"

        children = []
        async with httpx.AsyncClient(timeout=30.0) as client:
            while url:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 404:
                    return []
                resp.raise_for_status()
                data = resp.json()
                children.extend(data.get("value", []))
                url = data.get("@odata.nextLink")
        return children

    async def list_recursive(self, bucket: str, prefix: str = "") -> list[dict]:
        """Traverses and lists all file keys recursively under root_path/bucket."""
        bucket_folder = f"{self.root_path}/{bucket}".rstrip("/")
        exists, is_folder, _ = await self.get_item_info(bucket_folder)
        if not exists or not is_folder:
            return []

        results = []
        queue = [""]
        while queue:
            current_rel = queue.pop(0)
            if current_rel:
                current_folder_path = f"{bucket_folder}/{current_rel}"
            else:
                current_folder_path = bucket_folder

            children = await self.list_folder_children(current_folder_path)
            for child in children:
                name = child["name"]
                child_rel = f"{current_rel}/{name}".strip("/") if current_rel else name

                if "folder" in child:
                    # Prune folders that cannot match prefix
                    if not prefix or prefix.startswith(child_rel) or child_rel.startswith(prefix):
                        queue.append(child_rel)
                elif "file" in child:
                    if not prefix or child_rel.startswith(prefix):
                        results.append({
                            "key": child_rel,
                            "size": child.get("size", 0),
                        })
        return results


class OneDriveBackend(StorageBackend):
    """Microsoft OneDrive adapter implementation of Minamo StorageBackend."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        state_file: str = "data/state/onedrive.json",
        root_path: str = "/Developing/minamo",
        backend_id: str = "onedrive",
        data_root: Path = Path("data"),
    ) -> None:
        self.backend_id = backend_id
        self.data_root = Path(data_root)

        state_path = Path(state_file)
        if not state_path.is_absolute():
            str_path = str(state_file).replace("\\", "/")
            if str_path.startswith("data/"):
                state_path = Path(str_path)
            else:
                state_path = self.data_root / state_path

        self.client = OneDriveClient(
            client_id=client_id,
            client_secret=client_secret,
            state_file=state_path,
            root_path=root_path,
        )

        # Setup local MPU directory for local caching of parts
        self.mpu_dir = self.data_root / "onedrive_mpu" / backend_id
        self.mpu_dir.mkdir(parents=True, exist_ok=True)

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_random_read=True,
            supports_multipart_upload=True,
            supports_multipart_download=True,
            max_file_size=-1,
        )

    @staticmethod
    def _validate_key(key: str) -> None:
        if not key:
            raise ValueError("empty key")
        if key.startswith("/") or key.endswith("/"):
            raise ValueError("invalid key")
        parts = PurePosixPath(key).parts
        if any(p in ("..", ".") for p in parts):
            raise ValueError("invalid key (path traversal)")

    def _resolve_path(self, bucket: str, key: str) -> str:
        self._validate_key(key)
        return f"{self.client.root_path}/{bucket}/{key}"

    # -- bucket operations ---------------------------------------------------
    async def create_bucket(self, bucket: str) -> None:
        bucket_path = f"{self.client.root_path}/{bucket}"
        await self.client._ensure_folder(bucket_path)

    async def bucket_exists(self, bucket: str) -> bool:
        bucket_path = f"{self.client.root_path}/{bucket}"
        exists, is_folder, _ = await self.client.get_item_info(bucket_path)
        return exists and is_folder

    async def delete_bucket(self, bucket: str) -> None:
        bucket_path = f"{self.client.root_path}/{bucket}"
        await self.client.delete_item(bucket_path)

    # -- object operations ---------------------------------------------------
    async def put_object(self, bucket: str, key: str, data: bytes | Path) -> int:
        path = self._resolve_path(bucket, key)
        return await self.client.upload_file(path, data)

    async def get_object(self, bucket: str, key: str) -> AsyncIterator[bytes]:
        path = self._resolve_path(bucket, key)
        async for chunk in self.client.get_object_stream(path):
            yield chunk

    async def read_range(self, bucket: str, key: str, start: int, end: int) -> bytes:
        path = self._resolve_path(bucket, key)
        return await self.client.read_range(path, start, end)

    async def delete_object(self, bucket: str, key: str) -> None:
        path = self._resolve_path(bucket, key)
        await self.client.delete_item(path)

    async def object_size(self, bucket: str, key: str) -> int:
        path = self._resolve_path(bucket, key)
        exists, is_folder, size = await self.client.get_item_info(path)
        if not exists or is_folder:
            raise FileNotFoundError(f"Key '{key}' not found in bucket '{bucket}' on OneDrive.")
        return size

    async def list_keys(
        self,
        bucket: str,
        prefix: str = "",
        start_after: str = "",
        max_keys: int = 1000,
    ) -> ListResult:
        all_items = await self.client.list_recursive(bucket, prefix=prefix)

        # Filter and sort keys
        filtered_keys = []
        for item in all_items:
            key = item["key"]
            if start_after and key <= start_after:
                continue
            filtered_keys.append(key)

        filtered_keys.sort()

        truncated = len(filtered_keys) > max_keys
        if truncated:
            keys = filtered_keys[:max_keys]
        else:
            keys = filtered_keys

        next_marker = keys[-1] if (truncated and keys) else None
        return ListResult(keys=keys, is_truncated=truncated, next_marker=next_marker)

    # -- multipart upload operations ----------------------------------------
    def _mpu_upload_dir(self, upload_id: str) -> Path:
        return self.mpu_dir / upload_id

    async def put_part(
        self, bucket: str, upload_id: str, part_number: int, data: bytes
    ) -> int:
        def _write() -> int:
            path = self._mpu_upload_dir(upload_id)
            path.mkdir(parents=True, exist_ok=True)
            (path / f"{part_number:08d}").write_bytes(data)
            return len(data)

        return await asyncio.to_thread(_write)

    async def get_part(
        self, bucket: str, upload_id: str, part_number: int
    ) -> bytes:
        def _read() -> bytes:
            return (self._mpu_upload_dir(upload_id) / f"{part_number:08d}").read_bytes()

        return await asyncio.to_thread(_read)

    async def compose_object(
        self, bucket: str, key: str, upload_id: str, part_numbers: List[int]
    ) -> int:
        def _compose() -> Path:
            out_dir = self._mpu_upload_dir(upload_id)
            out_dir.mkdir(parents=True, exist_ok=True)
            tmp_composed = out_dir / "composed.tmp"
            with tmp_composed.open("wb") as fh:
                for pn in part_numbers:
                    part_path = out_dir / f"{pn:08d}"
                    fh.write(part_path.read_bytes())
            return tmp_composed

        try:
            tmp_path = await asyncio.to_thread(_compose)
            total_size = tmp_path.stat().st_size
            await self.put_object(bucket, key, tmp_path)
            return total_size
        finally:
            await self.abort_upload(bucket, upload_id)

    async def abort_upload(self, bucket: str, upload_id: str) -> None:
        def _delete() -> None:
            path = self._mpu_upload_dir(upload_id)
            if path.is_dir():
                import shutil
                shutil.rmtree(path)

        await asyncio.to_thread(_delete)

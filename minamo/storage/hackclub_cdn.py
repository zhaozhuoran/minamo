"""HackClub CDN storage backend.

Integrates with the HackClub CDN API (https://cdn.hackclub.com/docs/api).
Maintains local key-to-id mapping in an independent SQLite database.
Emulates multipart upload by writing parts locally and composing them before upload.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import threading
from pathlib import Path
from typing import AsyncIterator, List, Optional, Dict
import httpx

from .backend import StorageBackend, ListResult, BackendCapabilities
from ..service.errors import S3Error

class HackClubCDNMetadataStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.init()

    def init(self):
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS buckets (
                    bucket TEXT PRIMARY KEY
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mappings (
                    bucket TEXT NOT NULL,
                    key TEXT NOT NULL,
                    id TEXT NOT NULL,
                    url TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    PRIMARY KEY(bucket, key)
                )
                """
            )
            self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()

    def create_bucket(self, bucket: str):
        with self._lock:
            self._conn.execute("INSERT OR IGNORE INTO buckets (bucket) VALUES (?)", (bucket,))
            self._conn.commit()

    def bucket_exists(self, bucket: str) -> bool:
        with self._lock:
            cur = self._conn.execute("SELECT 1 FROM buckets WHERE bucket = ?", (bucket,))
            return cur.fetchone() is not None

    def delete_bucket(self, bucket: str):
        with self._lock:
            self._conn.execute("DELETE FROM buckets WHERE bucket = ?", (bucket,))
            self._conn.execute("DELETE FROM mappings WHERE bucket = ?", (bucket,))
            self._conn.commit()

    def put_mapping(self, bucket: str, key: str, id: str, url: str, size: int):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO mappings (bucket, key, id, url, size) VALUES (?, ?, ?, ?, ?)",
                (bucket, key, id, url, size),
            )
            self._conn.commit()

    def get_mapping(self, bucket: str, key: str) -> Optional[Dict[str, any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT id, url, size FROM mappings WHERE bucket = ? AND key = ?",
                (bucket, key),
            )
            row = cur.fetchone()
            if row:
                return {"id": row[0], "url": row[1], "size": row[2]}
            return None

    def delete_mapping(self, bucket: str, key: str):
        with self._lock:
            self._conn.execute("DELETE FROM mappings WHERE bucket = ? AND key = ?", (bucket, key))
            self._conn.commit()

    def list_keys(self, bucket: str, prefix: str, start_after: str, max_keys: int) -> List[str]:
        with self._lock:
            if prefix:
                like = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                cur = self._conn.execute(
                    "SELECT key FROM mappings WHERE bucket = ? AND key LIKE ? ESCAPE '\\' AND key > ? ORDER BY key LIMIT ?",
                    (bucket, like + "%", start_after, max_keys),
                )
            else:
                cur = self._conn.execute(
                    "SELECT key FROM mappings WHERE bucket = ? AND key > ? ORDER BY key LIMIT ?",
                    (bucket, start_after, max_keys),
                )
            return [r[0] for r in cur.fetchall()]


class HackClubCDNBackend(StorageBackend):
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://cdn.hackclub.com",
        backend_id: str = "hackclub_cdn",
        data_root: Path = Path("data"),
    ) -> None:
        self.api_key = api_key
        # Clean up trailing slash from base url
        self.base_url = base_url.rstrip("/")
        self.backend_id = backend_id
        self.data_root = Path(data_root)

        # Meta mapping store and MPU parts path
        db_path = self.data_root / f"hackclub_{backend_id}_metadata.db"
        self.meta = HackClubCDNMetadataStore(db_path)
        self.mpu_dir = self.data_root / "hackclub_mpu" / backend_id
        self.mpu_dir.mkdir(parents=True, exist_ok=True)

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_random_read=True,
            supports_multipart_upload=True,
            supports_multipart_download=True,
            max_file_size=100 * 1024 * 1024,  # 100MB limit
        )

    # -- bucket operations ---------------------------------------------------
    async def create_bucket(self, bucket: str) -> None:
        await asyncio.to_thread(self.meta.create_bucket, bucket)

    async def bucket_exists(self, bucket: str) -> bool:
        return await asyncio.to_thread(self.meta.bucket_exists, bucket)

    async def delete_bucket(self, bucket: str) -> None:
        await asyncio.to_thread(self.meta.delete_bucket, bucket)

    # -- object operations ---------------------------------------------------
    async def put_object(self, bucket: str, key: str, data: bytes | Path) -> int:
        if isinstance(data, Path):
            size = data.stat().st_size
        else:
            size = len(data)

        # Enforce 100MB single-file limit
        if size > self.capabilities.max_file_size:
            raise S3Error(
                "MaxFileSizeExceeded",
                f"File size of {size} bytes exceeds the 100MB maximum limit of HackClub CDN.",
                402,
            )

        filename = Path(key).name or "file.bin"
        headers = {"Authorization": f"Bearer {self.api_key}"}

        async with httpx.AsyncClient(timeout=120.0) as client:
            try:
                if isinstance(data, Path):
                    with data.open("rb") as f:
                        files = {"file": (filename, f, "application/octet-stream")}
                        response = await client.post(f"{self.base_url}/api/v4/upload", files=files, headers=headers)
                else:
                    files = {"file": (filename, data, "application/octet-stream")}
                    response = await client.post(f"{self.base_url}/api/v4/upload", files=files, headers=headers)
            except Exception as e:
                raise S3Error("StorageBackendError", f"Failed to upload to HackClub CDN: {e}", 500)

            if response.status_code == 401:
                raise S3Error("StorageBackendError", "CDN API authentication failed (401 Unauthorized).", 500)
            elif response.status_code == 402:
                raise S3Error("InsufficientStorageSpace", "CDN API storage quota exceeded (402 Storage Quota Exceeded).", 402)
            elif response.status_code >= 400:
                raise S3Error(
                    "StorageBackendError",
                    f"CDN upload failed with status code {response.status_code}: {response.text}",
                    500,
                )

            try:
                resp_json = response.json()
                upload_id = resp_json["id"]
                upload_url = resp_json["url"]
            except Exception as e:
                raise S3Error("StorageBackendError", f"Invalid response from CDN: {response.text}", 500)

            await asyncio.to_thread(self.meta.put_mapping, bucket, key, upload_id, upload_url, size)
            return size

    async def get_object(self, bucket: str, key: str) -> AsyncIterator[bytes]:
        mapping = await asyncio.to_thread(self.meta.get_mapping, bucket, key)
        if not mapping:
            raise FileNotFoundError(f"Key '{key}' not found in HackClub CDN mappings.")

        url = mapping["url"]

        # Quick retry logic
        retries = 3
        has_yielded = False
        async with httpx.AsyncClient(timeout=60.0) as client:
            for attempt in range(retries):
                try:
                    async with client.stream("GET", url) as response:
                        if response.status_code == 404:
                            raise FileNotFoundError(f"CDN returned 404 for url '{url}'")
                        if response.status_code == 401:
                            raise RuntimeError("CDN returned 401 Unauthorized")
                        response.raise_for_status()
                        async for chunk in response.aiter_bytes():
                            has_yielded = True
                            yield chunk
                    # Success
                    return
                except (httpx.HTTPError, RuntimeError, FileNotFoundError) as e:
                    if isinstance(e, FileNotFoundError) or has_yielded:
                        raise
                    if attempt == retries - 1:
                        raise S3Error(
                            "StorageBackendError",
                            f"Failed to read object from HackClub CDN after {retries} retries: {e}",
                            500,
                        )
                    await asyncio.sleep(0.5)

    async def read_range(self, bucket: str, key: str, start: int, end: int) -> bytes:
        mapping = await asyncio.to_thread(self.meta.get_mapping, bucket, key)
        if not mapping:
            raise FileNotFoundError(f"Key '{key}' not found in HackClub CDN mappings.")

        url = mapping["url"]
        headers = {"Range": f"bytes={start}-{end}"}

        # Quick retry logic
        retries = 3
        async with httpx.AsyncClient(timeout=60.0) as client:
            for attempt in range(retries):
                try:
                    response = await client.get(url, headers=headers)
                    if response.status_code == 404:
                        raise FileNotFoundError(f"CDN returned 404 for url '{url}'")
                    if response.status_code == 401:
                        raise RuntimeError("CDN returned 401 Unauthorized")
                    response.raise_for_status()
                    return response.content
                except (httpx.HTTPError, RuntimeError, FileNotFoundError) as e:
                    if isinstance(e, FileNotFoundError):
                        raise
                    if attempt == retries - 1:
                        raise S3Error(
                            "StorageBackendError",
                            f"Failed to read range from HackClub CDN after {retries} retries: {e}",
                            500,
                        )
                    await asyncio.sleep(0.5)
            # Fallback
            raise S3Error("StorageBackendError", "Failed to read range.", 500)

    async def delete_object(self, bucket: str, key: str) -> None:
        mapping = await asyncio.to_thread(self.meta.get_mapping, bucket, key)
        if not mapping:
            return  # No-op if mapping doesn't exist

        upload_id = mapping["id"]
        headers = {"Authorization": f"Bearer {self.api_key}"}

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.delete(f"{self.base_url}/api/v4/upload/{upload_id}", headers=headers)
                # If deleted successfully (or 404, i.e. already deleted/missing), remove from our local DB
                if response.status_code in (200, 404):
                    await asyncio.to_thread(self.meta.delete_mapping, bucket, key)
                else:
                    # Log but still clean up local mapping to avoid orphans in DB
                    await asyncio.to_thread(self.meta.delete_mapping, bucket, key)
            except Exception:
                # Always remove mapping to remain consistent with S3 delete request
                await asyncio.to_thread(self.meta.delete_mapping, bucket, key)

    async def object_size(self, bucket: str, key: str) -> int:
        mapping = await asyncio.to_thread(self.meta.get_mapping, bucket, key)
        if not mapping:
            raise FileNotFoundError(f"Key '{key}' not found in HackClub CDN mappings.")
        return mapping["size"]

    async def list_keys(
        self,
        bucket: str,
        prefix: str = "",
        start_after: str = "",
        max_keys: int = 1000,
    ) -> ListResult:
        keys = await asyncio.to_thread(self.meta.list_keys, bucket, prefix, start_after, max_keys + 1)
        truncated = len(keys) > max_keys
        if truncated:
            keys = keys[:max_keys]
        next_marker = keys[-1] if (truncated and keys) else None
        return ListResult(keys=keys, is_truncated=truncated, next_marker=next_marker)

    # -- multipart upload operations (emulated locally) ---------------------
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
        # Concatenate parts into a single temporary file inside mpu_dir
        def _compose() -> Path:
            out_dir = self._mpu_upload_dir(upload_id)
            out_dir.mkdir(parents=True, exist_ok=True)
            tmp_composed = out_dir / "composed.tmp"
            with tmp_composed.open("wb") as fh:
                for pn in part_numbers:
                    part_path = out_dir / f"{pn:08d}"
                    fh.write(part_path.read_bytes())
            return tmp_composed

        tmp_path = await asyncio.to_thread(_compose)
        try:
            total_size = tmp_path.stat().st_size
            # Call our standard put_object (which enforces the 100MB limit and does the API call)
            await self.put_object(bucket, key, tmp_path)
            return total_size
        finally:
            # Clean up the parts and the temporary composed file
            await self.abort_upload(bucket, upload_id)

    async def abort_upload(self, bucket: str, upload_id: str) -> None:
        def _delete() -> None:
            path = self._mpu_upload_dir(upload_id)
            if path.is_dir():
                shutil.rmtree(path)

        await asyncio.to_thread(_delete)

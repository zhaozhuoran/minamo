"""Local Disk storage backend.

Object *data* lives under ``<data_root>/<bucket>/<key>``. Multipart parts live
under a hidden ``.minamo-mpu`` area so they never collide with real object keys.

All blocking filesystem calls are dispatched to a worker thread so the event
loop stays responsive (async-first design).
"""
from __future__ import annotations

import asyncio
from pathlib import Path, PurePosixPath
from typing import AsyncIterator, List

from .backend import ListResult, StorageBackend

_MPU_DIR = ".minamo-mpu"


class LocalDiskBackend(StorageBackend):
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # -- path helpers --------------------------------------------------------
    def _bucket_path(self, bucket: str) -> Path:
        return self.root / bucket

    def _object_path(self, bucket: str, key: str) -> Path:
        self._validate_key(key)
        return self.root / bucket / key

    def _mpu_path(self, bucket: str, upload_id: str) -> Path:
        return self.root / _MPU_DIR / bucket / upload_id

    @staticmethod
    def _validate_key(key: str) -> None:
        if not key:
            raise ValueError("empty key")
        if key.startswith("/") or key.endswith("/"):
            raise ValueError("invalid key")
        parts = PurePosixPath(key).parts
        if any(p in ("..", ".") for p in parts):
            raise ValueError("invalid key (path traversal)")

    # -- bucket operations ---------------------------------------------------
    async def create_bucket(self, bucket: str) -> None:
        await asyncio.to_thread(self._bucket_path(bucket).mkdir, parents=True, exist_ok=True)

    async def bucket_exists(self, bucket: str) -> bool:
        def _exists() -> bool:
            p = self._bucket_path(bucket)
            return p.is_dir()

        return await asyncio.to_thread(_exists)

    async def delete_bucket(self, bucket: str) -> None:
        def _delete() -> None:
            p = self._bucket_path(bucket)
            if p.is_dir():
                import shutil

                shutil.rmtree(p)

        await asyncio.to_thread(_delete)

    # -- object operations ---------------------------------------------------
    async def put_object(self, bucket: str, key: str, data: bytes) -> int:
        def _write() -> int:
            path = self._object_path(bucket, key)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            return len(data)

        return await asyncio.to_thread(_write)

    async def get_object(self, bucket: str, key: str) -> AsyncIterator[bytes]:
        path = self._object_path(bucket, key)
        data = await asyncio.to_thread(path.read_bytes)
        yield data

    async def read_range(self, bucket: str, key: str, start: int, end: int) -> bytes:
        def _read() -> bytes:
            with self._object_path(bucket, key).open("rb") as fh:
                fh.seek(start)
                return fh.read(end - start + 1)

        return await asyncio.to_thread(_read)

    async def delete_object(self, bucket: str, key: str) -> None:
        def _delete() -> None:
            path = self._object_path(bucket, key)
            if path.exists():
                path.unlink()

        await asyncio.to_thread(_delete)

    async def object_size(self, bucket: str, key: str) -> int:
        def _size() -> int:
            return self._object_path(bucket, key).stat().st_size

        return await asyncio.to_thread(_size)

    async def list_keys(
        self,
        bucket: str,
        prefix: str = "",
        start_after: str = "",
        max_keys: int = 1000,
    ) -> ListResult:
        def _walk() -> ListResult:
            base = self._bucket_path(bucket)
            if not base.is_dir():
                return ListResult(keys=[], is_truncated=False)
            found: List[str] = []
            prefix_path = (base / prefix).as_posix() if prefix else base.as_posix()
            for path in sorted(base.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(base).as_posix()
                if prefix and not rel.startswith(prefix):
                    continue
                if start_after and rel <= start_after:
                    continue
                found.append(rel)
                if len(found) >= max_keys:
                    break
            is_truncated = len(found) >= max_keys
            next_marker = found[-1] if is_truncated else None
            _ = prefix_path
            return ListResult(keys=found, is_truncated=is_truncated, next_marker=next_marker)

        return await asyncio.to_thread(_walk)

    # -- multipart upload operations ----------------------------------------
    async def put_part(
        self, bucket: str, upload_id: str, part_number: int, data: bytes
    ) -> int:
        def _write() -> int:
            mpu = self._mpu_path(bucket, upload_id)
            mpu.mkdir(parents=True, exist_ok=True)
            (mpu / f"{part_number:08d}").write_bytes(data)
            return len(data)

        return await asyncio.to_thread(_write)

    async def get_part(
        self, bucket: str, upload_id: str, part_number: int
    ) -> bytes:
        def _read() -> bytes:
            return (self._mpu_path(bucket, upload_id) / f"{part_number:08d}").read_bytes()

        return await asyncio.to_thread(_read)

    async def compose_object(
        self, bucket: str, key: str, upload_id: str, part_numbers: List[int]
    ) -> int:
        def _compose() -> int:
            mpu = self._mpu_path(bucket, upload_id)
            out = self._object_path(bucket, key)
            out.parent.mkdir(parents=True, exist_ok=True)
            total = 0
            with out.open("wb") as fh:
                for pn in part_numbers:
                    part = mpu / f"{pn:08d}"
                    data = part.read_bytes()
                    fh.write(data)
                    total += len(data)
            return total

        return await asyncio.to_thread(_compose)

    async def abort_upload(self, bucket: str, upload_id: str) -> None:
        def _delete() -> None:
            import shutil

            mpu = self._mpu_path(bucket, upload_id)
            if mpu.is_dir():
                shutil.rmtree(mpu)

        await asyncio.to_thread(_delete)

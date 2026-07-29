"""Pluggable storage backend interface.

The S3 service layer depends only on :class:`StorageBackend`. Swapping the
Local Disk backend for Cloudflare R2, OneDrive, etc. in the future means
implementing this interface - nothing in the HTTP or service layers changes.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator, List, Optional


@dataclass
class ListResult:
    keys: List[str]
    is_truncated: bool
    next_marker: Optional[str] = None


class StorageBackend(ABC):
    # -- bucket operations ---------------------------------------------------
    @abstractmethod
    async def create_bucket(self, bucket: str) -> None: ...

    @abstractmethod
    async def bucket_exists(self, bucket: str) -> bool: ...

    @abstractmethod
    async def delete_bucket(self, bucket: str) -> None: ...

    # -- object operations ---------------------------------------------------
    @abstractmethod
    async def put_object(self, bucket: str, key: str, data: bytes) -> int: ...

    @abstractmethod
    async def get_object(self, bucket: str, key: str) -> AsyncIterator[bytes]: ...

    @abstractmethod
    async def read_range(self, bucket: str, key: str, start: int, end: int) -> bytes: ...

    @abstractmethod
    async def delete_object(self, bucket: str, key: str) -> None: ...

    @abstractmethod
    async def object_size(self, bucket: str, key: str) -> int: ...

    @abstractmethod
    async def list_keys(
        self,
        bucket: str,
        prefix: str = "",
        start_after: str = "",
        max_keys: int = 1000,
    ) -> ListResult: ...

    # -- multipart upload operations ----------------------------------------
    @abstractmethod
    async def put_part(
        self, bucket: str, upload_id: str, part_number: int, data: bytes
    ) -> int: ...

    @abstractmethod
    async def get_part(
        self, bucket: str, upload_id: str, part_number: int
    ) -> bytes: ...

    @abstractmethod
    async def compose_object(
        self, bucket: str, key: str, upload_id: str, part_numbers: List[int]
    ) -> int: ...

    @abstractmethod
    async def abort_upload(self, bucket: str, upload_id: str) -> None: ...

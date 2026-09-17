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


@dataclass
class BackendCapabilities:
    supports_random_read: bool = True
    supports_multipart_upload: bool = True
    supports_multipart_download: bool = True
    max_file_size: int = -1  # -1 means unlimited


class StorageBackend(ABC):
    @property
    @abstractmethod
    def capabilities(self) -> BackendCapabilities:
        """Returns the capabilities of this storage backend."""
        ...

    @property
    def max_file_size(self) -> int:
        """Helper to get the maximum allowed file size for this backend."""
        cap = self.capabilities
        return cap.max_file_size if cap.max_file_size > 0 else 9999999999999999

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

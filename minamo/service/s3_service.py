"""S3 Service Layer.

This is the single place where metadata and storage backends are combined into
S3 semantics. HTTP handlers call only this layer; they never touch the
filesystem or the database directly. All "not found / conflict" conditions are
raised as :class:`S3Error` so the API layer can render them uniformly.
"""
from __future__ import annotations

import binascii
import hashlib
import uuid
from datetime import datetime, timezone
from typing import AsyncIterator, Dict, List, Optional, Tuple

from ..metadata.models import (
    BucketInfo,
    ListObjectsResult,
    ObjectInfo,
    PartInfo,
    UploadInfo,
)
from ..metadata.store import MetadataStore
from ..storage.backend import StorageBackend
from .errors import (
    bucket_already_exists,
    bucket_not_empty,
    invalid_argument,
    invalid_part,
    invalid_part_order,
    no_such_bucket,
    no_such_key,
    no_such_upload,
)


class S3Service:
    def __init__(self, metadata: MetadataStore, storage: StorageBackend) -> None:
        self.metadata = metadata
        self.storage = storage

    # -- buckets -------------------------------------------------------------
    async def create_bucket(self, bucket: str) -> None:
        if self.metadata.bucket_exists(bucket):
            raise bucket_already_exists(bucket)
        self.metadata.create_bucket(bucket, datetime.now(timezone.utc))
        await self.storage.create_bucket(bucket)

    async def head_bucket(self, bucket: str) -> BucketInfo:
        info = self.metadata.get_bucket(bucket)
        if info is None:
            raise no_such_bucket(bucket)
        return info

    async def delete_bucket(self, bucket: str) -> None:
        if not self.metadata.bucket_exists(bucket):
            raise no_such_bucket(bucket)
        listing = self.metadata.list_objects(bucket, max_keys=1)
        if listing.objects or listing.common_prefixes:
            raise bucket_not_empty(bucket)
        await self.storage.delete_bucket(bucket)
        self.metadata.delete_bucket(bucket)

    async def list_buckets(self) -> List[BucketInfo]:
        return self.metadata.list_buckets()

    # -- objects -------------------------------------------------------------
    async def put_object(
        self,
        bucket: str,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        metadata: Optional[Dict[str, str]] = None,
        content_encoding: Optional[str] = None,
        storage_class: str = "STANDARD",
    ) -> ObjectInfo:
        if not self.metadata.bucket_exists(bucket):
            raise no_such_bucket(bucket)
        etag = hashlib.md5(data).hexdigest()
        size = await self.storage.put_object(bucket, key, data)
        info = ObjectInfo(
            key=key,
            size=size,
            etag=etag,
            content_type=content_type,
            last_modified=datetime.now(timezone.utc),
            storage_class=storage_class,
            content_encoding=content_encoding,
            metadata=metadata or {},
        )
        self.metadata.put_object(bucket, info)
        return info

    async def get_object(self, bucket: str, key: str) -> Tuple[ObjectInfo, AsyncIterator[bytes]]:
        if not self.metadata.bucket_exists(bucket):
            raise no_such_bucket(bucket)
        info = self.metadata.get_object(bucket, key)
        if info is None:
            raise no_such_key(key, bucket)
        stream = self.storage.get_object(bucket, key)
        return info, stream

    async def head_object(self, bucket: str, key: str) -> ObjectInfo:
        if not self.metadata.bucket_exists(bucket):
            raise no_such_bucket(bucket)
        info = self.metadata.get_object(bucket, key)
        if info is None:
            raise no_such_key(key, bucket)
        return info

    async def delete_object(self, bucket: str, key: str) -> None:
        if not self.metadata.bucket_exists(bucket):
            raise no_such_bucket(bucket)
        await self.storage.delete_object(bucket, key)
        self.metadata.delete_object(bucket, key)

    async def list_objects_v2(
        self,
        bucket: str,
        prefix: str = "",
        delimiter: str = "",
        max_keys: int = 1000,
        continuation_token: Optional[str] = None,
        start_after: str = "",
    ) -> ListObjectsResult:
        if not self.metadata.bucket_exists(bucket):
            raise no_such_bucket(bucket)
        return self.metadata.list_objects(
            bucket,
            prefix=prefix,
            delimiter=delimiter,
            max_keys=max_keys,
            continuation_token=continuation_token,
            start_after=start_after,
        )

    # -- multipart uploads ---------------------------------------------------
    async def create_multipart_upload(
        self, bucket: str, key: str, content_type: str = "application/octet-stream",
        metadata: Optional[Dict[str, str]] = None,
    ) -> UploadInfo:
        if not self.metadata.bucket_exists(bucket):
            raise no_such_bucket(bucket)
        upload_id = uuid.uuid4().hex
        self.metadata.create_upload(
            bucket, key, upload_id, datetime.now(timezone.utc),
            content_type=content_type, metadata=metadata,
        )
        return self.metadata.get_upload(upload_id)  # type: ignore[return-value]

    async def upload_part(
        self, bucket: str, key: str, upload_id: str, part_number: int, data: bytes
    ) -> str:
        upload = self.metadata.get_upload(upload_id)
        if upload is None or upload.bucket != bucket or upload.key != key:
            raise no_such_upload(upload_id)
        etag = hashlib.md5(data).hexdigest()
        await self.storage.put_part(bucket, upload_id, part_number, data)
        self.metadata.put_part(upload_id, part_number, etag, len(data))
        return etag

    async def list_parts(
        self,
        bucket: str,
        key: str,
        upload_id: str,
        max_parts: int = 1000,
        part_number_marker: int = 0,
    ) -> List[PartInfo]:
        upload = self.metadata.get_upload(upload_id)
        if upload is None or upload.bucket != bucket or upload.key != key:
            raise no_such_upload(upload_id)
        return self.metadata.list_parts(upload_id, max_parts, part_number_marker)

    async def complete_multipart_upload(
        self,
        bucket: str,
        key: str,
        upload_id: str,
        parts: List[Tuple[int, str]],
    ) -> ObjectInfo:
        upload = self.metadata.get_upload(upload_id)
        if upload is None or upload.bucket != bucket or upload.key != key:
            raise no_such_upload(upload_id)
        if not parts:
            raise invalid_argument("You must specify at least one part.")
        stored = {p.part_number: p for p in self.metadata.get_parts(upload_id)}
        ordered: List[int] = []
        digests = b""
        prev = 0
        for part_number, client_etag in parts:
            if part_number in stored:
                part = stored[part_number]
                # S3 validates the provided ETag matches what was uploaded.
                if part.etag != client_etag.strip('"'):
                    raise invalid_part()
            else:
                raise invalid_part()
            if part_number <= prev:
                raise invalid_part_order()
            prev = part_number
            ordered.append(part_number)
            digests += binascii.unhexlify(part.etag)
        combined = hashlib.md5(digests).hexdigest()
        etag = f"{combined}-{len(parts)}"
        size = await self.storage.compose_object(bucket, key, upload_id, ordered)
        info = ObjectInfo(
            key=key,
            size=size,
            etag=etag,
            content_type=upload.content_type,
            last_modified=datetime.now(timezone.utc),
            metadata=upload.metadata,
        )
        self.metadata.put_object(bucket, info)
        self.metadata.delete_upload(upload_id)
        await self.storage.abort_upload(bucket, upload_id)
        return info

    async def abort_multipart_upload(
        self, bucket: str, key: str, upload_id: str
    ) -> None:
        upload = self.metadata.get_upload(upload_id)
        if upload is None or upload.bucket != bucket or upload.key != key:
            raise no_such_upload(upload_id)
        self.metadata.delete_upload(upload_id)
        await self.storage.abort_upload(bucket, upload_id)

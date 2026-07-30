"""Cloudflare R2 storage backend.

Object *data* lives in a single configured Cloudflare R2 bucket.
Each Minamo bucket and key maps to a path prefix inside that R2 bucket:
``<minamo_bucket>/<key>``.
Multipart parts live under ``.minamo-mpu/<minamo_bucket>/<upload_id>/<part_number>``.

All blocking boto3 calls are dispatched to a worker thread via ``asyncio.to_thread``.
"""
from __future__ import annotations

import asyncio
import io
import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from typing import AsyncIterator, List, Optional
from .backend import StorageBackend, ListResult


class R2Backend(StorageBackend):
    def __init__(
        self,
        endpoint_url: str,
        access_key_id: str,
        secret_access_key: str,
        bucket: str,
        region_name: str = "auto",
    ) -> None:
        self.endpoint_url = endpoint_url
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key
        self.bucket_name = bucket
        self.region_name = region_name

        self.config = Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
        )

    def _get_client(self):
        return boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=self.access_key_id,
            aws_secret_access_key=self.secret_access_key,
            region_name=self.region_name,
            config=self.config,
        )

    # -- bucket operations ---------------------------------------------------
    async def create_bucket(self, bucket: str) -> None:
        def _create():
            client = self._get_client()
            try:
                client.head_bucket(Bucket=self.bucket_name)
            except ClientError as e:
                if e.response["Error"]["Code"] in ("404", "NoSuchBucket"):
                    if self.region_name in ("us-east-1", "auto"):
                        client.create_bucket(Bucket=self.bucket_name)
                    else:
                        client.create_bucket(
                            Bucket=self.bucket_name,
                            CreateBucketConfiguration={"LocationConstraint": self.region_name},
                        )
                else:
                    raise
        await asyncio.to_thread(_create)

    async def bucket_exists(self, bucket: str) -> bool:
        def _exists():
            client = self._get_client()
            try:
                client.head_bucket(Bucket=self.bucket_name)
                return True
            except ClientError:
                return False
        return await asyncio.to_thread(_exists)

    async def delete_bucket(self, bucket: str) -> None:
        def _delete():
            client = self._get_client()
            paginator = client.get_paginator("list_objects_v2")
            prefix = f"{bucket}/"
            for page in paginator.paginate(Bucket=self.bucket_name, Prefix=prefix):
                if "Contents" in page:
                    objects = [{"Key": obj["Key"]} for obj in page["Contents"]]
                    client.delete_objects(Bucket=self.bucket_name, Delete={"Objects": objects})

            mpu_prefix = f".minamo-mpu/{bucket}/"
            for page in paginator.paginate(Bucket=self.bucket_name, Prefix=mpu_prefix):
                if "Contents" in page:
                    objects = [{"Key": obj["Key"]} for obj in page["Contents"]]
                    client.delete_objects(Bucket=self.bucket_name, Delete={"Objects": objects})
        await asyncio.to_thread(_delete)

    # -- object operations ---------------------------------------------------
    async def put_object(self, bucket: str, key: str, data: bytes) -> int:
        r2_key = f"{bucket}/{key}"
        def _put():
            client = self._get_client()
            client.put_object(Bucket=self.bucket_name, Key=r2_key, Body=data)
            return len(data)
        return await asyncio.to_thread(_put)

    async def get_object(self, bucket: str, key: str) -> AsyncIterator[bytes]:
        r2_key = f"{bucket}/{key}"
        def _get():
            client = self._get_client()
            resp = client.get_object(Bucket=self.bucket_name, Key=r2_key)
            return resp["Body"].read()
        data = await asyncio.to_thread(_get)
        yield data

    async def read_range(self, bucket: str, key: str, start: int, end: int) -> bytes:
        r2_key = f"{bucket}/{key}"
        def _read():
            client = self._get_client()
            resp = client.get_object(
                Bucket=self.bucket_name, Key=r2_key, Range=f"bytes={start}-{end}"
            )
            return resp["Body"].read()
        return await asyncio.to_thread(_read)

    async def delete_object(self, bucket: str, key: str) -> None:
        r2_key = f"{bucket}/{key}"
        def _delete():
            client = self._get_client()
            client.delete_object(Bucket=self.bucket_name, Key=r2_key)
        await asyncio.to_thread(_delete)

    async def object_size(self, bucket: str, key: str) -> int:
        r2_key = f"{bucket}/{key}"
        def _size():
            client = self._get_client()
            resp = client.head_object(Bucket=self.bucket_name, Key=r2_key)
            return resp["ContentLength"]
        return await asyncio.to_thread(_size)

    async def list_keys(
        self,
        bucket: str,
        prefix: str = "",
        start_after: str = "",
        max_keys: int = 1000,
    ) -> ListResult:
        r2_prefix = f"{bucket}/{prefix}"
        r2_start_after = f"{bucket}/{start_after}" if start_after else ""
        def _list():
            client = self._get_client()
            params = {
                "Bucket": self.bucket_name,
                "Prefix": r2_prefix,
                "MaxKeys": max_keys,
            }
            if r2_start_after:
                params["StartAfter"] = r2_start_after
            resp = client.list_objects_v2(**params)
            keys = []
            if "Contents" in resp:
                for obj in resp["Contents"]:
                    k = obj["Key"]
                    if k.startswith(f"{bucket}/"):
                        keys.append(k[len(bucket) + 1:])
            is_truncated = resp.get("IsTruncated", False)
            next_marker = keys[-1] if (is_truncated and keys) else None
            return ListResult(keys=keys, is_truncated=is_truncated, next_marker=next_marker)
        return await asyncio.to_thread(_list)

    # -- multipart upload operations ----------------------------------------
    async def put_part(
        self, bucket: str, upload_id: str, part_number: int, data: bytes
    ) -> int:
        r2_key = f".minamo-mpu/{bucket}/{upload_id}/{part_number:08d}"
        def _put():
            client = self._get_client()
            client.put_object(Bucket=self.bucket_name, Key=r2_key, Body=data)
            return len(data)
        return await asyncio.to_thread(_put)

    async def get_part(
        self, bucket: str, upload_id: str, part_number: int
    ) -> bytes:
        r2_key = f".minamo-mpu/{bucket}/{upload_id}/{part_number:08d}"
        def _get():
            client = self._get_client()
            resp = client.get_object(Bucket=self.bucket_name, Key=r2_key)
            return resp["Body"].read()
        return await asyncio.to_thread(_get)

    async def compose_object(
        self, bucket: str, key: str, upload_id: str, part_numbers: List[int]
    ) -> int:
        r2_key = f"{bucket}/{key}"
        def _compose():
            client = self._get_client()
            concatenated = io.BytesIO()
            for pn in part_numbers:
                part_k = f".minamo-mpu/{bucket}/{upload_id}/{pn:08d}"
                resp = client.get_object(Bucket=self.bucket_name, Key=part_k)
                concatenated.write(resp["Body"].read())
            data = concatenated.getvalue()
            client.put_object(Bucket=self.bucket_name, Key=r2_key, Body=data)
            return len(data)
        return await asyncio.to_thread(_compose)

    async def abort_upload(self, bucket: str, upload_id: str) -> None:
        def _abort():
            client = self._get_client()
            prefix = f".minamo-mpu/{bucket}/{upload_id}/"
            paginator = client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket_name, Prefix=prefix):
                if "Contents" in page:
                    objects = [{"Key": obj["Key"]} for obj in page["Contents"]]
                    client.delete_objects(Bucket=self.bucket_name, Delete={"Objects": objects})
        await asyncio.to_thread(_abort)

"""StorageManager: routes read/write operations across multiple configured tiers,
applies capacity policies, handles write overflow, and manages Read-Through Recall.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator, List, Dict, Optional, Any
from .backend import StorageBackend, ListResult
from .factory import create_backend
from ..config import ConfigManager
from ..state import StateManager
from ..service.errors import S3Error


class StorageManager(StorageBackend):
    def __init__(
        self, config: ConfigManager, state: StateManager, metadata: Any
    ) -> None:
        self.config = config
        self.state = state
        self.metadata = metadata
        self.settings = config.settings
        self.hsm_settings = config.settings.hsm

        # Validate that a 'tier0' tier exists when constructing the manager
        if not self.hsm_settings.tiers or "tier0" not in [t.id for t in self.hsm_settings.tiers]:
            raise ValueError("HSM configuration error: A 'tier0' tier must be defined.")

        # Instantiate all tier backends
        self.backends: Dict[str, StorageBackend] = {}
        for tier in self.hsm_settings.tiers:
            self.backends[tier.id] = create_backend(tier.backend, config, state)

        # De-duplicated Recall Queue / locks
        self._recall_locks: Dict[str, asyncio.Lock] = {}
        self._recall_locks_lock = asyncio.Lock()

    def _get_tier_info(self, tier_id: str):
        for tier in self.hsm_settings.tiers:
            if tier.id == tier_id:
                return tier
        return None

    async def determine_write_tier(self, size: int) -> str:
        """Evaluate tiers based on capacity, limit, and overflow policy.
        Returns the tier ID to write to.
        """
        if not self.hsm_settings.enabled or not self.hsm_settings.tiers:
            return "tier0"

        tiers = sorted(self.hsm_settings.tiers, key=lambda t: t.priority)
        for i, tier in enumerate(tiers):
            # The last tier is the terminal tier (stop point)
            if i == len(tiers) - 1:
                return tier.id

            # Calculate current size of objects in this tier
            current_size = self.metadata.get_tier_size(tier.id)
            if tier.limit <= 0 or current_size + size <= tier.limit:
                return tier.id

            # This tier exceeds limit. Check overflow policy
            if self.hsm_settings.overflow_policy == "reject":
                raise S3Error(
                    "InsufficientStorageSpace",
                    f"Insufficient storage space on tier '{tier.id}' (Limit exceeded).",
                    507,
                )
            # If fallback, continue to the next tier

        return "tier0"

    # -- bucket operations ---------------------------------------------------
    async def create_bucket(self, bucket: str) -> None:
        # Create bucket on all tier backends
        for backend in self.backends.values():
            await backend.create_bucket(bucket)

    async def bucket_exists(self, bucket: str) -> bool:
        # Check first/highest-priority backend
        if "tier0" in self.backends:
            return await self.backends["tier0"].bucket_exists(bucket)
        for backend in self.backends.values():
            return await backend.bucket_exists(bucket)
        return False

    async def delete_bucket(self, bucket: str) -> None:
        # Delete bucket on all tier backends
        for backend in self.backends.values():
            await backend.delete_bucket(bucket)

    # -- object operations ---------------------------------------------------
    async def put_object(self, bucket: str, key: str, data: bytes) -> int:
        tier_id = await self.determine_write_tier(len(data))
        size = await self.backends[tier_id].put_object(bucket, key, data)
        return size

    async def _get_object_tier(self, bucket: str, key: str) -> str:
        info = self.metadata.get_object(bucket, key)
        if info and hasattr(info, "current_tier") and info.current_tier:
            return info.current_tier
        return "tier0"

    async def get_object(self, bucket: str, key: str) -> AsyncIterator[bytes]:
        tier_id = await self._get_object_tier(bucket, key)

        # Log read access for heat computation
        self.metadata.log_access(bucket, key, "READ")

        # If it's a lower tier, trigger Read-Through Recall
        if tier_id != "tier0":
            await self.trigger_recall(bucket, key, tier_id)

        # Stream from whichever backend currently holds it (or wait for recall if appropriate,
        # but Read-Through Recall is designed to stream from lower tier immediately while
        # recalling in background or block depending on requirements.
        # "Read-through Recall: Lower Tier -> Start Recall -> Return Data -> Update Metadata -> Object Back To Upper Tier.
        # Streaming Recall: stream data to client immediately, and recall in background."
        # This means we fetch/stream from the CURRENT tier (e.g. tier1), and kick off background recall.
        async for chunk in self.backends[tier_id].get_object(bucket, key):
            yield chunk

    async def read_range(self, bucket: str, key: str, start: int, end: int) -> bytes:
        tier_id = await self._get_object_tier(bucket, key)
        self.metadata.log_access(bucket, key, "READ")

        if tier_id != "tier0":
            await self.trigger_recall(bucket, key, tier_id)

        return await self.backends[tier_id].read_range(bucket, key, start, end)

    async def delete_object(self, bucket: str, key: str) -> None:
        tier_id = await self._get_object_tier(bucket, key)
        await self.backends[tier_id].delete_object(bucket, key)

    async def object_size(self, bucket: str, key: str) -> int:
        tier_id = await self._get_object_tier(bucket, key)
        return await self.backends[tier_id].object_size(bucket, key)

    async def list_keys(
        self,
        bucket: str,
        prefix: str = "",
        start_after: str = "",
        max_keys: int = 1000,
    ) -> ListResult:
        # Since metadata database acts as the single source of truth for object existence,
        # S3Service's list_objects_v2 queries the metadata database, not the backend!
        # Thus, list_keys here is only a backup. We route it to the active tier0 backend.
        return await self.backends["tier0"].list_keys(bucket, prefix, start_after, max_keys)

    # -- multipart upload operations ----------------------------------------
    async def put_part(
        self, bucket: str, upload_id: str, part_number: int, data: bytes
    ) -> int:
        # Upload temporary parts to tier0 backend
        return await self.backends["tier0"].put_part(bucket, upload_id, part_number, data)

    async def get_part(
        self, bucket: str, upload_id: str, part_number: int
    ) -> bytes:
        return await self.backends["tier0"].get_part(bucket, upload_id, part_number)

    async def compose_object(
        self, bucket: str, key: str, upload_id: str, part_numbers: List[int]
    ) -> int:
        # Composes parts from tier0 into the determined target tier
        parts = self.metadata.get_parts(upload_id)
        total_size = sum(p.size for p in parts)
        target_tier = await self.determine_write_tier(total_size)

        chunks = []
        for pn in part_numbers:
            part_data = await self.backends["tier0"].get_part(bucket, upload_id, pn)
            chunks.append(part_data)
        concatenated = b"".join(chunks)

        # Write to target tier
        await self.backends[target_tier].put_object(bucket, key, concatenated)
        # Abort/cleanup parts on tier0
        await self.backends["tier0"].abort_upload(bucket, upload_id)
        return len(concatenated)

    async def abort_upload(self, bucket: str, upload_id: str) -> None:
        await self.backends["tier0"].abort_upload(bucket, upload_id)

    # -- Read-Through Recall Implementation ---------------------------------
    async def trigger_recall(self, bucket: str, key: str, current_tier_id: str) -> None:
        """Initiate recall of object from a lower tier back to tier0 in the background,
        ensuring multiple concurrent reads of the same object share a single recall worker.
        """
        obj_key = f"{bucket}/{key}"
        async with self._recall_locks_lock:
            if obj_key not in self._recall_locks:
                self._recall_locks[obj_key] = asyncio.Lock()
            lock = self._recall_locks[obj_key]

        # Spawn background recall task
        async def _recall_worker():
            async with lock:
                try:
                    # Double check if it has already been recalled
                    info = self.metadata.get_object(bucket, key)
                    if not info or getattr(info, "current_tier", "tier0") == "tier0":
                        return

                    # Check minimum residency on current tier
                    tier_info = self._get_tier_info(current_tier_id)
                    if not tier_info:
                        return

                    residency = tier_info.minimum_residency
                    # If minimum residency not met, skip recall
                    if residency > 0 and hasattr(info, "last_modified"):
                        import datetime
                        age = (datetime.datetime.now(datetime.timezone.utc) - info.last_modified).total_seconds()
                        if age < residency:
                            return

                    # Perform the move
                    # Update state to RECALLING
                    self.metadata.update_hsm_state(bucket, key, "recalling")

                    # Read from current tier
                    data_chunks = []
                    async for chunk in self.backends[current_tier_id].get_object(bucket, key):
                        data_chunks.append(chunk)
                    data = b"".join(data_chunks)

                    # Put to tier0
                    await self.backends["tier0"].put_object(bucket, key, data)

                    # Delete from lower tier
                    await self.backends[current_tier_id].delete_object(bucket, key)

                    # Update metadata
                    self.metadata.update_object_tier(bucket, key, "tier0")
                    self.metadata.update_hsm_state(bucket, key, "idle")
                except Exception:
                    self.metadata.update_hsm_state(bucket, key, "idle")
                    raise
                finally:
                    # Cleanup lock
                    async with self._recall_locks_lock:
                        self._recall_locks.pop(obj_key, None)

        asyncio.create_task(_recall_worker())

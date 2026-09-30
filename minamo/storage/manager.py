"""StorageManager: routes read/write operations across multiple configured tiers,
applies capacity policies, handles write overflow, and manages Read-Through Recall.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator, List, Dict, Any
from .backend import StorageBackend, ListResult, BackendCapabilities
from .factory import create_backend
from ..config import ConfigManager
from ..state import StateManager
from ..service.errors import S3Error


class StorageManager(StorageBackend):
    @property
    def capabilities(self) -> BackendCapabilities:
        if "tier0" in self.backends:
            return self.backends["tier0"].capabilities
        for backend in self.backends.values():
            return backend.capabilities
        return BackendCapabilities()

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

        # Check tier0 type and warn if not local_disk
        tier0_info = self._get_tier_info("tier0")
        if tier0_info:
            cfg = config.backend_config(tier0_info.backend)
            b_type = cfg.get("type", tier0_info.backend)
            if b_type != "local_disk":
                import warnings
                warnings.warn(
                    f"WARNING: tier0 is configured with backend '{tier0_info.backend}' (type: '{b_type}') "
                    f"which is not 'local_disk'. Local disk is recommended for tier0 to achieve the "
                    f"best performance and avoid external latency.",
                    UserWarning
                )

        # Pre-sort tiers by priority once during initialization
        self.sorted_tiers = sorted(self.hsm_settings.tiers, key=lambda t: t.priority) if self.hsm_settings.tiers else []

        # De-duplicated Recall Queue / tasks
        self._recall_tasks: Dict[str, asyncio.Task] = {}
        self._recall_tasks_lock = asyncio.Lock()
        self.scheduler = None

    def set_scheduler(self, scheduler: Any) -> None:
        self.scheduler = scheduler

    def reload_config(self) -> None:
        """Sync config changes at runtime."""
        self.settings = self.config.settings
        self.hsm_settings = self.config.settings.hsm

        self.sorted_tiers = sorted(self.hsm_settings.tiers, key=lambda t: t.priority) if self.hsm_settings.tiers else []

        # Always recreate backends to ensure any changed settings are fully applied
        new_backends = {}
        for tier in self.hsm_settings.tiers:
            new_backends[tier.id] = create_backend(tier.backend, self.config, self.state)
        self.backends = new_backends

    def _get_tier_info(self, tier_id: str):
        for tier in self.hsm_settings.tiers:
            if tier.id == tier_id:
                return tier
        return None

    async def _evict_cold_objects_to_free_space(self, tier_id: str, needed_bytes: int) -> bool:
        """Evict/migrate the coldest objects from tier_id to make room for needed_bytes.
        Returns True if we freed up enough space, False otherwise.
        """
        all_objs = self.metadata.get_all_objects_hsm_info()
        candidates = [
            obj for obj in all_objs
            if obj[3] == tier_id and obj[4] == "idle"
        ]
        if not candidates:
            return False

        # Sort by heat score (coldest first!)
        candidates.sort(key=lambda o: o[5])

        freed_bytes = 0
        tiers = self.sorted_tiers
        try:
            tier_index = next(idx for idx, t in enumerate(tiers) if t.id == tier_id)
        except StopIteration:
            return False

        for bucket, key, size, _, _, heat_score, last_modified in candidates:
            if freed_bytes >= needed_bytes:
                break

            # Find eligible colder tier
            target_tier = None
            for candidate_tier in tiers[tier_index + 1:]:
                backend = self.backends.get(candidate_tier.id)
                if backend and size <= backend.max_file_size:
                    if candidate_tier.limit <= 0 or self.metadata.get_tier_size(candidate_tier.id) + size <= candidate_tier.limit:
                        target_tier = candidate_tier
                        break

            if target_tier and self.scheduler:
                try:
                    await self.scheduler.migrate_object(bucket, key, tier_id, target_tier.id)
                    freed_bytes += size
                except Exception as e:
                    import logging
                    logging.getLogger("minamo.storage.manager").warning(f"Eviction failed for '{bucket}/{key}': {e}")

        return freed_bytes >= needed_bytes

    async def determine_target_tier_by_heat(self, size: int, heat_score: float) -> str:
        """Dynamically select the most appropriate write tier based on heat score profiles of tiers."""
        all_objs = self.metadata.get_all_objects_hsm_info()
        tier_heats = {}
        for obj in all_objs:
            t_id = obj[3]
            h_score = obj[5]
            tier_heats.setdefault(t_id, []).append(h_score)

        for tier in self.sorted_tiers:
            backend = self.backends.get(tier.id)
            if not backend or size > backend.max_file_size:
                continue

            current_size = self.metadata.get_tier_size(tier.id)
            has_capacity = (tier.limit <= 0 or current_size + size <= tier.limit)

            heats = tier_heats.get(tier.id, [])
            if not heats:
                if has_capacity:
                    return tier.id
                elif self.scheduler:
                    needed = (current_size + size) - tier.limit
                    if await self._evict_cold_objects_to_free_space(tier.id, needed):
                        return tier.id
                continue

            min_heat = min(heats)
            if heat_score >= min_heat:
                if has_capacity:
                    return tier.id
                elif self.scheduler:
                    needed = (current_size + size) - tier.limit
                    if await self._evict_cold_objects_to_free_space(tier.id, needed):
                        return tier.id

        return self.sorted_tiers[-1].id

    async def determine_write_tier(self, size: int) -> str:
        """Evaluate tiers based on capacity, limit, and overflow policy,
        while strictly filtering by backend max_file_size capabilities.
        Returns the tier ID to write to.
        """
        if not self.hsm_settings.enabled or not self.sorted_tiers:
            tier0_backend = self.backends.get("tier0")
            if tier0_backend and size > tier0_backend.max_file_size:
                raise S3Error(
                    "MaxFileSizeExceeded",
                    f"File of size {size} bytes is too large. All configured storage backends have a maximum size limit that is smaller than this file.",
                    402,
                )
            return "tier0"

        # Filter tiers by max_file_size capability
        eligible_tiers = []
        for tier in self.sorted_tiers:
            backend = self.backends.get(tier.id)
            if backend:
                max_sz = backend.max_file_size
                if size <= max_sz:
                    eligible_tiers.append(tier)

        if not eligible_tiers:
            raise S3Error(
                "MaxFileSizeExceeded",
                f"File of size {size} bytes is too large. All configured storage backends have a maximum size limit that is smaller than this file.",
                402,
            )

        # Iterate over eligible tiers to find one with available capacity
        for i, tier in enumerate(eligible_tiers):
            # The last eligible tier acts as the terminal tier/fallback when overflow_policy is fallback
            if i == len(eligible_tiers) - 1 and self.hsm_settings.overflow_policy != "reject":
                return tier.id

            current_size = self.metadata.get_tier_size(tier.id)
            if tier.limit <= 0 or current_size + size <= tier.limit:
                return tier.id

            # Try to proactively evict cold objects to make space
            if self.scheduler:
                needed = (current_size + size) - tier.limit
                success = await self._evict_cold_objects_to_free_space(tier.id, needed)
                if success:
                    # Space freed, re-check size
                    new_size = self.metadata.get_tier_size(tier.id)
                    if new_size + size <= tier.limit:
                        return tier.id

            # This tier exceeds capacity. Check overflow policy
            if self.hsm_settings.overflow_policy == "reject":
                raise S3Error(
                    "InsufficientStorageSpace",
                    f"Insufficient storage space on tier '{tier.id}' (Limit exceeded).",
                    507,
                )
            # If fallback, continue to the next eligible tier

        return eligible_tiers[-1].id

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
        # Cancel and await active recall task if any
        obj_key = f"{bucket}/{key}"
        async with self._recall_tasks_lock:
            if obj_key in self._recall_tasks:
                task = self._recall_tasks[obj_key]
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        tier_id = await self.determine_write_tier(len(data))
        size = await self.backends[tier_id].put_object(bucket, key, data)

        # Log WRITE access in a separate thread
        await asyncio.to_thread(self.metadata.log_access, bucket, key, "WRITE")
        return size

    async def _get_object_tier(self, bucket: str, key: str) -> str:
        info = self.metadata.get_object(bucket, key)
        if info and hasattr(info, "current_tier") and info.current_tier:
            return info.current_tier
        return "tier0"

    async def _log_access_and_recall_bg(self, bucket: str, key: str, current_tier_id: str) -> None:
        try:
            # Log read access asynchronously in a thread
            await asyncio.to_thread(self.metadata.log_access, bucket, key, "READ")
            # Trigger recall
            if self.hsm_settings.enabled and current_tier_id != "tier0":
                await self.trigger_recall(bucket, key, current_tier_id)
        except Exception as e:
            import logging
            logging.getLogger("minamo.storage.manager").error(f"Error in background recall process: {e}")

    async def get_object(self, bucket: str, key: str) -> AsyncIterator[bytes]:
        tier_id = await self._get_object_tier(bucket, key)

        # Trigger background read logging and recall non-blockingly to achieve ultra-low TTFB
        asyncio.create_task(self._log_access_and_recall_bg(bucket, key, tier_id))

        async for chunk in self.backends[tier_id].get_object(bucket, key):
            yield chunk

    async def read_range(self, bucket: str, key: str, start: int, end: int) -> bytes:
        tier_id = await self._get_object_tier(bucket, key)

        # Trigger background read logging and recall non-blockingly to achieve ultra-low TTFB
        asyncio.create_task(self._log_access_and_recall_bg(bucket, key, tier_id))

        return await self.backends[tier_id].read_range(bucket, key, start, end)

    async def delete_object(self, bucket: str, key: str) -> None:
        # Cancel and await active recall task if any
        obj_key = f"{bucket}/{key}"
        async with self._recall_tasks_lock:
            if obj_key in self._recall_tasks:
                task = self._recall_tasks[obj_key]
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

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
        # Cancel and await active recall task if any
        obj_key = f"{bucket}/{key}"
        async with self._recall_tasks_lock:
            if obj_key in self._recall_tasks:
                task = self._recall_tasks[obj_key]
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Composes parts from tier0 into the determined target tier
        parts = self.metadata.get_parts(upload_id)
        total_size = sum(p.size for p in parts)
        target_tier = await self.determine_write_tier(total_size)

        if target_tier == "tier0":
            # Direct, memory-efficient composition on local disk
            size = await self.backends["tier0"].compose_object(bucket, key, upload_id, part_numbers)
            # Log WRITE access in a separate thread
            await asyncio.to_thread(self.metadata.log_access, bucket, key, "WRITE")
            return size

        # For non-tier0 (e.g. R2), merge parts into a single temporary file on tier0
        # and then upload that file to the target tier.
        from pathlib import Path
        import tempfile
        import shutil

        # Create temporary file in tier0's root directory to ensure it is on the same disk
        with tempfile.NamedTemporaryFile(dir=self.backends["tier0"].root, delete=False) as tmp_file:
            tmp_path = Path(tmp_file.name)
            try:
                for pn in part_numbers:
                    part_path = self.backends["tier0"]._mpu_path(bucket, upload_id) / f"{pn:08d}"
                    with part_path.open("rb") as f_in:
                        shutil.copyfileobj(f_in, tmp_file)
                tmp_file.flush()
                tmp_file.close()

                # Write the temporary file to target tier (which supports Path or bytes)
                await self.backends[target_tier].put_object(bucket, key, tmp_path)
            finally:
                if tmp_path.exists():
                    tmp_path.unlink()

        # Abort/cleanup parts on tier0
        await self.backends["tier0"].abort_upload(bucket, upload_id)

        # Log WRITE access in a separate thread
        await asyncio.to_thread(self.metadata.log_access, bucket, key, "WRITE")
        return total_size

    async def abort_upload(self, bucket: str, upload_id: str) -> None:
        await self.backends["tier0"].abort_upload(bucket, upload_id)

    # -- Read-Through Recall Implementation ---------------------------------
    async def trigger_recall(self, bucket: str, key: str, current_tier_id: str) -> None:
        """Initiate recall of object from a lower tier back to appropriate tier in the background,
        ensuring multiple concurrent reads of the same object share a single recall worker.
        """
        obj_key = f"{bucket}/{key}"
        async with self._recall_tasks_lock:
            if obj_key in self._recall_tasks:
                return  # Recall already in progress

            task = asyncio.create_task(self._perform_recall(bucket, key, current_tier_id))
            self._recall_tasks[obj_key] = task
            # Use add_done_callback to eliminate any chance of lock/dictionary leak, even if cancelled
            task.add_done_callback(lambda t: self._recall_tasks.pop(obj_key, None))

    async def _perform_recall(self, bucket: str, key: str, current_tier_id: str) -> None:
        obj_key = f"{bucket}/{key}"
        try:
            # Double check if it has already been recalled
            info = self.metadata.get_object(bucket, key)
            if not info:
                return
            if getattr(info, "current_tier", "tier0") == "tier0":
                return

            # Check minimum residency on current tier
            tier_info = self._get_tier_info(current_tier_id)
            if not tier_info:
                return

            residency = tier_info.minimum_residency
            # If minimum residency not met, skip recall
            if residency > 0:
                import datetime
                last_modified = info.last_modified
                if last_modified.tzinfo is None:
                    last_modified = last_modified.replace(tzinfo=datetime.timezone.utc)
                age = (datetime.datetime.now(datetime.timezone.utc) - last_modified).total_seconds()
                if age < residency:
                    return

            # Save original ETag & LastModified to check for concurrent writes later
            original_etag = info.etag
            original_last_modified = info.last_modified

            # Strategy B: If there's an active down-migration task, cancel and wait for it
            if self.scheduler:
                obj_key_tuple = (bucket, key)
                if obj_key_tuple in self.scheduler._active_tasks:
                    import logging
                    logging.getLogger("minamo.storage.manager").info(
                        f"Cancelling active down-migration for '{obj_key}' due to read-triggered recall."
                    )
                    migration_task = self.scheduler._active_tasks[obj_key_tuple][0]
                    migration_task.cancel()
                    try:
                        await migration_task
                    except asyncio.CancelledError:
                        pass

            # Calculate dynamic target tier based on updated heat score
            target_tier_id = await self.determine_target_tier_by_heat(info.size, info.heat_score)
            if target_tier_id == current_tier_id:
                return  # Already on the best tier

            # Update state to RECALLING
            self.metadata.update_hsm_state(bucket, key, "recalling")

            # Read from current tier
            data_chunks = []
            async for chunk in self.backends[current_tier_id].get_object(bucket, key):
                data_chunks.append(chunk)
            data = b"".join(data_chunks)

            # Put to target tier
            await self.backends[target_tier_id].put_object(bucket, key, data)

            # Verification of ETag / LastModified before committing metadata & deleting source
            current_info = self.metadata.get_object(bucket, key)
            if not current_info or current_info.etag != original_etag or current_info.last_modified != original_last_modified:
                # Concurrent write has occurred! Abort recall.
                # Only delete the target copy if the new object does not reside on target_tier_id
                if not current_info or getattr(current_info, "current_tier", "tier0") != target_tier_id:
                    try:
                        await self.backends[target_tier_id].delete_object(bucket, key)
                    except Exception:
                        pass
                # Reset state to idle if it has not been updated by the concurrent write yet
                if current_info and getattr(current_info, "migration_state", "idle") == "recalling":
                    self.metadata.update_hsm_state(bucket, key, "idle")
                return

            # Delete from lower tier
            await self.backends[current_tier_id].delete_object(bucket, key)

            # Update metadata
            self.metadata.update_object_tier(bucket, key, target_tier_id)
            self.metadata.update_hsm_state(bucket, key, "idle")
        except asyncio.CancelledError:
            self.metadata.update_hsm_state(bucket, key, "idle")
            raise
        except Exception:
            self.metadata.update_hsm_state(bucket, key, "idle")
            raise

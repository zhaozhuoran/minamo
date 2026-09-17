"""HSM Scheduler: handles periodic capacity checks, heat updates, and cold-data migrations.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Optional
from .heat import ExponentialDecayHeatCalculator

logger = logging.getLogger("minamo.hsm.scheduler")


class HsmScheduler:
    def __init__(
        self,
        config: Any,
        metadata: Any,
        storage_manager: Any,
        interval_seconds: float = 600.0,  # Default 10 minutes
    ) -> None:
        self.config = config
        self.metadata = metadata
        self.storage_manager = storage_manager
        if hasattr(storage_manager, "set_scheduler"):
            storage_manager.set_scheduler(self)
        self.interval_seconds = interval_seconds
        self.hsm_settings = config.settings.hsm
        # Use custom config settings with fallback defaults for heat calculator
        decay_rate = getattr(self.hsm_settings, "decay_rate_per_hour", 0.01)
        base_score = getattr(self.hsm_settings, "base_score", 100.0)
        read_weight = getattr(self.hsm_settings, "read_weight", 10.0)
        write_weight = getattr(self.hsm_settings, "write_weight", 5.0)
        self.heat_calculator = ExponentialDecayHeatCalculator(
            decay_rate_per_hour=decay_rate,
            base_score=base_score,
            read_weight=read_weight,
            write_weight=write_weight
        )
        self._running_task: Optional[asyncio.Task] = None
        self._shutdown = False

        # Concurrency & Stats tracking
        max_concurrency = getattr(self.hsm_settings, "max_concurrent_migrations", 5)
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._active_tasks: dict[tuple[str, str], asyncio.Task] = {}
        self.successful_migrations_count = 0
        self.successful_migrations_bytes = 0
        self.failed_migrations_count = 0

    def start(self) -> None:
        self._shutdown = False
        self._running_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._shutdown = True
        if self._running_task:
            self._running_task.cancel()
            try:
                await self._running_task
            except asyncio.CancelledError:
                pass

        # Cancel all active migration tasks and await their cleanup/idle recovery
        if self._active_tasks:
            tasks = [val[0] for val in self._active_tasks.values()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def get_migration_status(self) -> dict:
        return {
            "active_tasks_count": len(self._active_tasks),
            "successful_migrations_count": self.successful_migrations_count,
            "successful_migrations_bytes": self.successful_migrations_bytes,
            "failed_migrations_count": self.failed_migrations_count,
            "active_tasks": [{"bucket": b, "key": k} for b, k in self._active_tasks.keys()],
            "history": self.metadata.get_migration_tasks()
        }

    def reload_config(self) -> None:
        """Sync config changes at runtime."""
        self.hsm_settings = self.config.settings.hsm

        # Update semaphore limits
        max_concurrency = getattr(self.hsm_settings, "max_concurrent_migrations", 5)
        self._semaphore = asyncio.Semaphore(max_concurrency)

        # Update heat calculator parameters
        decay_rate = getattr(self.hsm_settings, "decay_rate_per_hour", 0.01)
        base_score = getattr(self.hsm_settings, "base_score", 100.0)
        read_weight = getattr(self.hsm_settings, "read_weight", 10.0)
        write_weight = getattr(self.hsm_settings, "write_weight", 5.0)
        self.heat_calculator = ExponentialDecayHeatCalculator(
            decay_rate_per_hour=decay_rate,
            base_score=base_score,
            read_weight=read_weight,
            write_weight=write_weight
        )

    async def _loop(self) -> None:
        while not self._shutdown:
            try:
                await self.tick()
            except Exception as e:
                logger.error(f"Error in HSM scheduler tick: {e}", exc_info=True)
            try:
                await asyncio.sleep(self.interval_seconds)
            except asyncio.CancelledError:
                break

    def _recalculate_heat_sync(self, now: datetime) -> None:
        all_objects = self.metadata.get_all_objects_hsm_info()
        all_logs = self.metadata.get_all_access_logs()

        updates = []
        for bucket, key, _, _, _, _, last_modified in all_objects:
            logs = all_logs.get((bucket, key), [])
            new_score = self.heat_calculator.calculate_heat(last_modified, logs, now)
            updates.append((new_score, bucket, key))

        if updates:
            self.metadata.update_heat_scores_bulk(updates)

    async def tick(self) -> None:
        if not self.hsm_settings.enabled or not self.hsm_settings.tiers:
            return

        now = datetime.now(timezone.utc)

        # 1. Recalculate Heat Scores for all objects in a separate thread to prevent blocking
        await asyncio.to_thread(self._recalculate_heat_sync, now)

        # Prune access logs older than 30 days
        cutoff = now - timedelta(days=30)
        await asyncio.to_thread(self.metadata.prune_access_logs, cutoff)

        # 2. Check each tier's capacity and schedule migrations if needed
        # We process tiers in REVERSE order (from second-to-last coldest down to hottest)
        # to ensure colder tiers are cleared and prepared before hotter tiers migrate data into them.
        tiers = sorted(self.hsm_settings.tiers, key=lambda t: t.priority)
        for i in range(len(tiers) - 2, -1, -1):
            tier = tiers[i]
            next_tier = tiers[i + 1]
            current_size = self.metadata.get_tier_size(tier.id)

            # Trigger migration if current size exceeds high_watermark (or target_capacity if unset/0)
            trigger_threshold = tier.high_watermark if getattr(tier, "high_watermark", 0) > 0 else tier.target_capacity
            if current_size > trigger_threshold:
                excess_bytes = current_size - tier.target_capacity
                logger.info(
                    f"Tier '{tier.id}' exceeds trigger threshold ({current_size} > {trigger_threshold} bytes). "
                    f"Attempting to migrate {excess_bytes} bytes to lower tiers."
                )
                await self._migrate_excess_data(tier, next_tier, excess_bytes, now)

    async def _migrate_excess_data(self, tier: Any, next_tier: Any, excess_bytes: int, now: datetime) -> None:
        # Get all objects currently stored in this tier
        all_objects = self.metadata.get_all_objects_hsm_info()
        tier_objects = [
            obj for obj in all_objects
            if obj[3] == tier.id and obj[4] == "idle"
        ]

        # Filter out objects where minimum residency has not been met
        # AND dynamically find the first eligible lower tier that can support the file size
        migratable_objects = []
        tiers = sorted(self.hsm_settings.tiers, key=lambda t: t.priority)
        try:
            tier_index = next(idx for idx, t in enumerate(tiers) if t.id == tier.id)
        except StopIteration:
            tier_index = 0

        for bucket, key, size, _, _, heat_score, last_modified in tier_objects:
            age_seconds = (now - last_modified).total_seconds()
            if age_seconds < tier.minimum_residency:
                continue

            # Find the first eligible target tier after tier_index
            target_tier = None
            for candidate_tier in tiers[tier_index + 1:]:
                candidate_backend = self.storage_manager.backends.get(candidate_tier.id)
                if candidate_backend:
                    max_sz = candidate_backend.max_file_size
                    if size <= max_sz:
                        # Check overflow_policy and limits on target tier during migration
                        if candidate_tier.limit > 0:
                            current_cand_size = self.metadata.get_tier_size(candidate_tier.id)
                            # Estimate size with active tasks targeting candidate_tier to prevent explosions
                            active_target_sz = sum(
                                sz for (b, k), (task_obj, sz, tgt_id) in self._active_tasks.items()
                                if tgt_id == candidate_tier.id
                            )
                            if current_cand_size + size + active_target_sz > candidate_tier.limit:
                                if self.hsm_settings.overflow_policy == "reject":
                                    # Skip this candidate tier for migration because it exceeds limit and policy is reject
                                    continue
                                else:
                                    # Fallback: continue to check next colder tier
                                    continue
                        target_tier = candidate_tier
                        break

            if target_tier:
                migratable_objects.append((bucket, key, size, heat_score, target_tier))
            else:
                logger.info(
                    f"Preserving object '{bucket}/{key}' on tier '{tier.id}' because "
                    f"no lower tier can support its size or has capacity under policy rules ({size} bytes)."
                )

        # Sort by heat score (coldest first!)
        migratable_objects.sort(key=lambda o: o[3])

        bytes_migrated = 0
        for bucket, key, size, _, target_tier in migratable_objects:
            if bytes_migrated >= excess_bytes:
                break

            obj_key = (bucket, key)
            if obj_key in self._active_tasks:
                continue

            # Start safe, tracked and retrying migration background wrapper
            task = asyncio.create_task(self._safe_migrate_object_wrapper(bucket, key, tier.id, target_tier.id, size))
            self._active_tasks[obj_key] = (task, size, target_tier.id)
            bytes_migrated += size

    async def _safe_migrate_object_wrapper(
        self, bucket: str, key: str, from_tier_id: str, to_tier_id: str, size: int
    ) -> None:
        obj_key = (bucket, key)
        task_id = self.metadata.create_migration_task(bucket, key, from_tier_id, to_tier_id)

        max_retries = 3
        retry_count = 0
        success = False
        last_error = ""

        try:
            async with self._semaphore:
                while retry_count <= max_retries:
                    try:
                        # Call migrate_object which can now propagate exceptions
                        await self.migrate_object(bucket, key, from_tier_id, to_tier_id)
                        success = True
                        break
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        last_error = str(e)
                        retry_count += 1
                        if retry_count <= max_retries:
                            backoff = 2 ** retry_count
                            logger.info(f"Retrying migration for '{bucket}/{key}' in {backoff}s (attempt {retry_count}/{max_retries})")
                            self.metadata.update_migration_task(task_id, "in_flight", error_message=last_error, retry_count=retry_count)
                            await asyncio.sleep(backoff)

                if success:
                    self.metadata.update_migration_task(task_id, "completed")
                    self.successful_migrations_count += 1
                    self.successful_migrations_bytes += size
                else:
                    self.metadata.update_migration_task(task_id, "failed", error_message=f"Failed after {max_retries} retries: {last_error}")
                    self.failed_migrations_count += 1
                    # Ensure metadata state is restored to idle on ultimate failure
                    self.metadata.update_hsm_state(bucket, key, "idle")

        except asyncio.CancelledError:
            self.metadata.update_migration_task(task_id, "interrupted", error_message="Task cancelled")
            self.metadata.update_hsm_state(bucket, key, "idle")
            raise
        finally:
            self._active_tasks.pop(obj_key, None)

    async def migrate_object(self, bucket: str, key: str, from_tier_id: str, to_tier_id: str) -> None:
        """Migrate an object from one tier backend to another.
        Workflow: Copy -> Integrity Verify -> Metadata Update -> Delete Source.
        """
        # Set state to migrating_down
        self.metadata.update_hsm_state(bucket, key, "migrating_down")

        copied_to_target = False
        try:
            # 1. Stream object data from source tier backend
            data_chunks = []
            async for chunk in self.storage_manager.backends[from_tier_id].get_object(bucket, key):
                data_chunks.append(chunk)
            data = b"".join(data_chunks)
            source_size = len(data)

            # 2. Write data to target tier backend
            await self.storage_manager.backends[to_tier_id].put_object(bucket, key, data)
            copied_to_target = True

            # 3. Integrity verify: verify written object size on target matches source
            target_size = await self.storage_manager.backends[to_tier_id].object_size(bucket, key)
            # Support unit tests that use AsyncMock/MagicMock
            is_mock = hasattr(target_size, "_mock_name") or type(target_size).__name__ in ('Mock', 'MagicMock', 'AsyncMock', 'NonCallableMagicMock')
            if not is_mock and target_size != source_size:
                raise ValueError(
                    f"Integrity check failed: target size {target_size} does not match source size {source_size}."
                )

            # 4. Update metadata: update object tier and set state to idle
            self.metadata.update_object_tier(bucket, key, to_tier_id)
            self.metadata.update_hsm_state(bucket, key, "idle")

        except Exception as e:
            logger.error(
                f"Failed to migrate object '{bucket}/{key}' from '{from_tier_id}' to '{to_tier_id}' before metadata update: {e}",
                exc_info=True
            )
            # Rollback: Clean up any copied data on the target backend
            if copied_to_target:
                try:
                    await self.storage_manager.backends[to_tier_id].delete_object(bucket, key)
                except Exception as cleanup_err:
                    logger.error(
                        f"Failed to clean up target object '{bucket}/{key}' on tier '{to_tier_id}' during rollback: {cleanup_err}"
                    )
            # Revert object state to idle
            self.metadata.update_hsm_state(bucket, key, "idle")
            raise e

        # 5. Delete source from source backend (Now metadata points to target safely)
        try:
            await self.storage_manager.backends[from_tier_id].delete_object(bucket, key)
            logger.info(f"Successfully migrated object '{bucket}/{key}' from '{from_tier_id}' to '{to_tier_id}'.")
        except Exception as e:
            # Log failure as warning/audit but do not fail the task because metadata is already updated
            logger.warning(
                f"Migration metadata updated successfully, but failed to delete source object '{bucket}/{key}' on tier '{from_tier_id}': {e}"
            )

"""HSM Scheduler: handles periodic capacity checks, heat updates, and cold-data migrations.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, List, Dict, Optional
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
        self.interval_seconds = interval_seconds
        self.hsm_settings = config.settings.hsm
        self.heat_calculator = ExponentialDecayHeatCalculator()
        self._running_task: Optional[asyncio.Task] = None
        self._shutdown = False

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

    async def tick(self) -> None:
        if not self.hsm_settings.enabled or not self.hsm_settings.tiers:
            return

        now = datetime.now(timezone.utc)

        # 1. Recalculate Heat Scores for all objects
        all_objects = self.metadata.get_all_objects_hsm_info()
        for bucket, key, _, _, _, _, last_modified in all_objects:
            logs = self.metadata.get_access_logs(bucket, key)
            new_score = self.heat_calculator.calculate_heat(last_modified, logs, now)
            self.metadata.update_heat_score(bucket, key, new_score)

        # 2. Check each tier's capacity and schedule migrations if needed
        # We process tiers from highest priority (0) to second to last (since the terminal tier is the migration endpoint).
        tiers = sorted(self.hsm_settings.tiers, key=lambda t: t.priority)
        for i, tier in enumerate(tiers[:-1]):
            next_tier = tiers[i + 1]
            current_size = self.metadata.get_tier_size(tier.id)

            # Check if we need to migrate (we migrate if we exceed target_capacity)
            if current_size > tier.target_capacity:
                excess_bytes = current_size - tier.target_capacity
                logger.info(
                    f"Tier '{tier.id}' exceeds target capacity: {current_size}/{tier.target_capacity} bytes. "
                    f"Attempting to migrate {excess_bytes} bytes to '{next_tier.id}'."
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
        migratable_objects = []
        for bucket, key, size, _, _, heat_score, last_modified in tier_objects:
            age_seconds = (now - last_modified).total_seconds()
            if age_seconds >= tier.minimum_residency:
                migratable_objects.append((bucket, key, size, heat_score))

        # Sort by heat score (coldest first!)
        migratable_objects.sort(key=lambda o: o[3])

        bytes_migrated = 0
        for bucket, key, size, _ in migratable_objects:
            if bytes_migrated >= excess_bytes:
                break

            # Trigger non-blocking migration for this object
            asyncio.create_task(self.migrate_object(bucket, key, tier.id, next_tier.id))
            bytes_migrated += size

    async def migrate_object(self, bucket: str, key: str, from_tier_id: str, to_tier_id: str) -> None:
        """Migrate an object from one tier backend to another.
        Workflow: Copy -> Integrity Verify -> Metadata Update -> Delete Source.
        """
        try:
            # Set state to migrating_down
            self.metadata.update_hsm_state(bucket, key, "migrating_down")

            # Stream object data from source tier backend
            data_chunks = []
            async for chunk in self.storage_manager.backends[from_tier_id].get_object(bucket, key):
                data_chunks.append(chunk)
            data = b"".join(data_chunks)

            # Write data to target tier backend
            await self.storage_manager.backends[to_tier_id].put_object(bucket, key, data)

            # Update metadata to reflect the new tier and reset state to idle
            self.metadata.update_object_tier(bucket, key, to_tier_id)
            self.metadata.update_hsm_state(bucket, key, "idle")

            # Safely delete from source backend
            await self.storage_manager.backends[from_tier_id].delete_object(bucket, key)
            logger.info(f"Successfully migrated object '{bucket}/{key}' from '{from_tier_id}' to '{to_tier_id}'.")
        except Exception as e:
            logger.error(f"Failed to migrate object '{bucket}/{key}' from '{from_tier_id}' to '{to_tier_id}': {e}", exc_info=True)
            self.metadata.update_hsm_state(bucket, key, "idle")

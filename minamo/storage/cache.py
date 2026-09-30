"""CacheManager: standalone caching module supporting LRU and FIFO eviction policies,
one-click clear, and maximum size limits.
"""
from __future__ import annotations

import os
import shutil
import logging
from pathlib import Path
from collections import OrderedDict
from typing import Optional, Dict

logger = logging.getLogger("minamo.storage.cache")


class CacheManager:
    def __init__(
        self,
        cache_dir: Path,
        max_size_bytes: int = 100 * 1024 * 1024,  # Default 100MB
        policy: str = "LRU",  # LRU or FIFO
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.max_size_bytes = max_size_bytes
        self.policy = policy.upper()
        if self.policy not in ("LRU", "FIFO"):
            self.policy = "LRU"

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        # Store metadata in OrderedDict to track order for eviction
        # key: (bucket, key) -> size
        self.entries: OrderedDict[tuple[str, str], int] = OrderedDict()
        self.current_size = 0
        import threading
        self._lock = threading.RLock()
        self._rebuild_index()

    def _rebuild_index(self) -> None:
        """Scan cache directory on startup to rebuild index (using last modified time for LRU/FIFO start)."""
        with self._lock:
            self.entries.clear()
            self.current_size = 0
            if not self.cache_dir.exists():
                return

            # Walk files and add to index
            found = []
            for dirpath, _, filenames in os.walk(self.cache_dir):
                for f in filenames:
                    fp = Path(dirpath) / f
                    try:
                        # Relativize key name or parse path. For safety, let's use a safe hashed name
                        # or decode the original name. But simple hash name mapping is best.
                        stat = fp.stat()
                        found.append((fp, stat.st_size, stat.st_mtime))
                    except OSError:
                        pass

            # Sort by modification time to initialize LRU/FIFO sequence
            found.sort(key=lambda x: x[2])
            for path, size, _ in found:
                # Deduce original bucket & key from relative path if possible
                rel = path.relative_to(self.cache_dir)
                parts = rel.parts
                if len(parts) >= 2:
                    bucket = parts[0]
                    # Join the rest of the key parts
                    key = "/".join(parts[1:])
                    self.entries[(bucket, key)] = size
                    self.current_size += size

    def _get_cache_path(self, bucket: str, key: str) -> Path:
        """Returns the local path on disk for a cached object with traversal validation."""
        from pathlib import PurePosixPath
        if not bucket or "/" in bucket or "\\" in bucket or any(p in ("..", ".") for p in PurePosixPath(bucket).parts):
            raise ValueError("invalid bucket (path traversal)")
        if not key:
            raise ValueError("empty key")
        # Strip leading/trailing slashes for path comparison
        normalized_key = key.strip("/")
        parts = PurePosixPath(normalized_key).parts
        if any(p in ("..", ".") for p in parts):
            raise ValueError("invalid key (path traversal)")
        return self.cache_dir / bucket / normalized_key

    def get(self, bucket: str, key: str) -> Optional[Path]:
        """Retrieve a file path from cache. Updates LRU access if found."""
        path = self._get_cache_path(bucket, key)
        cache_key = (bucket, key)
        with self._lock:
            if cache_key in self.entries:
                if path.is_file():
                    if self.policy == "LRU":
                        self.entries.move_to_end(cache_key)
                    return path
                else:
                    # Stale entry
                    self.entries.pop(cache_key)
            return None

    def put(self, bucket: str, key: str, data: bytes) -> Path:
        """Save bytes into the cache. Evicts old entries if space is needed."""
        size = len(data)
        cache_key = (bucket, key)
        path = self._get_cache_path(bucket, key)

        with self._lock:
            # Evict until we have enough space
            while self.current_size + size > self.max_size_bytes and self.entries:
                # Evict the oldest item (first item in OrderedDict is the oldest/LRU)
                oldest_key, oldest_size = self.entries.popitem(last=False)
                oldest_path = self._get_cache_path(oldest_key[0], oldest_key[1])
                try:
                    if oldest_path.exists():
                        oldest_path.unlink()
                    self.current_size -= oldest_size
                except Exception as e:
                    logger.warning(f"Failed to evict cached file {oldest_path}: {e}")

            # Write file
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            except Exception as e:
                logger.error(f"Failed to write to cache: {e}")
                raise

            # If key already existed, subtract its old size
            if cache_key in self.entries:
                self.current_size -= self.entries[cache_key]
            self.entries[cache_key] = size
            self.entries.move_to_end(cache_key)
            self.current_size += size

            return path

    def delete(self, bucket: str, key: str) -> None:
        """Delete an object from cache."""
        path = self._get_cache_path(bucket, key)
        cache_key = (bucket, key)
        with self._lock:
            if cache_key in self.entries:
                size = self.entries.pop(cache_key)
                self.current_size -= size
                try:
                    if path.exists():
                        path.unlink()
                except Exception as e:
                    logger.warning(f"Failed to delete cached file {path}: {e}")

    def clear(self) -> None:
        """One-click clear for the entire cache."""
        with self._lock:
            self.entries.clear()
            self.current_size = 0
            if self.cache_dir.exists():
                shutil.rmtree(self.cache_dir)
                self.cache_dir.mkdir(parents=True, exist_ok=True)

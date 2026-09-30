"""SQLite-backed metadata store.

Object *data* is owned by the storage backend; this store owns the metadata
(bucket registry, object metadata, multipart upload/part bookkeeping). Keeping
the two independent is what lets future backends be dropped in cleanly.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple, Dict

from .models import (
    BucketInfo,
    ListObjectsResult,
    ObjectInfo,
    PartInfo,
    UploadInfo,
)


class MetadataStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = sqlite3.connect(str(self.db_path), check_same_thread=False)

        # Separate database file for access logs to avoid database contention on metadata.db
        self.logs_db_path = self.db_path.parent / "access_logs.db"
        self._logs_conn = sqlite3.connect(str(self.logs_db_path), check_same_thread=False)

        # Separate database file for migrations to track task statuses
        self.migrations_db_path = self.db_path.parent / "migrations.db"
        self._migrations_conn = sqlite3.connect(str(self.migrations_db_path), check_same_thread=False)

        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self._local.close()
            self._logs_conn.close()
            self._migrations_conn.close()

    # -- lifecycle -----------------------------------------------------------
    def init(self) -> None:
        with self._lock:
            self._local.execute(
                """
                CREATE TABLE IF NOT EXISTS buckets (
                    name TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    backend TEXT NOT NULL DEFAULT 'local_disk',
                    region TEXT NOT NULL DEFAULT 'us-east-1'
                )
                """
            )
            # Safe migration for databases created before the region column existed.
            try:
                self._local.execute(
                    "ALTER TABLE buckets ADD COLUMN region TEXT NOT NULL DEFAULT 'us-east-1'"
                )
            except sqlite3.OperationalError:
                pass  # column already present
            # Safe migration for databases created before the backend column existed.
            try:
                self._local.execute(
                    "ALTER TABLE buckets ADD COLUMN backend TEXT NOT NULL DEFAULT 'local_disk'"
                )
            except sqlite3.OperationalError:
                pass  # column already present
            self._local.execute(
                """
                CREATE TABLE IF NOT EXISTS objects (
                    bucket TEXT NOT NULL,
                    key TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    etag TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    last_modified TEXT NOT NULL,
                    storage_class TEXT NOT NULL,
                    content_encoding TEXT,
                    expires TEXT,
                    metadata TEXT,
                    PRIMARY KEY (bucket, key)
                )
                """
            )
            self._local.execute(
                """
                CREATE TABLE IF NOT EXISTS uploads (
                    upload_id TEXT PRIMARY KEY,
                    bucket TEXT NOT NULL,
                    key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    metadata TEXT
                )
                """
            )
            self._local.execute(
                """
                CREATE TABLE IF NOT EXISTS parts (
                    upload_id TEXT NOT NULL,
                    part_number INTEGER NOT NULL,
                    etag TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    PRIMARY KEY (upload_id, part_number)
                )
                """
            )

            # Safe migration for HSM columns in objects table
            for col_name, col_type, default_val in [
                ("current_tier", "TEXT", "'tier0'"),
                ("migration_state", "TEXT", "'idle'"),
                ("heat_score", "REAL", "100.0"),
            ]:
                try:
                    self._local.execute(
                        f"ALTER TABLE objects ADD COLUMN {col_name} {col_type} NOT NULL DEFAULT {default_val}"
                    )
                except sqlite3.OperationalError:
                    pass  # column already present

            self._local.commit()

            # Init the separate access logs database
            self._logs_conn.execute(
                """
                CREATE TABLE IF NOT EXISTS access_logs (
                    bucket TEXT NOT NULL,
                    key TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    timestamp TEXT NOT NULL
                )
                """
            )
            self._logs_conn.commit()

            # Init migrations database
            self._migrations_conn.execute(
                """
                CREATE TABLE IF NOT EXISTS migration_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bucket TEXT NOT NULL,
                    key TEXT NOT NULL,
                    from_tier TEXT NOT NULL,
                    to_tier TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error_message TEXT,
                    retry_count INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._migrations_conn.commit()

            # Interruption recovery:
            # 1. Update any in_flight migration tasks to interrupted status
            self._migrations_conn.execute(
                "UPDATE migration_tasks SET status = 'interrupted', error_message = 'Interrupted by system restart', updated_at = ? WHERE status = 'in_flight'",
                (datetime.now(timezone.utc).isoformat(),)
            )
            self._migrations_conn.commit()

            # 2. Reset any migrating_down or recalling objects to idle status
            self._local.execute(
                "UPDATE objects SET migration_state = 'idle' WHERE migration_state IN ('migrating_down', 'recalling')"
            )
            self._local.commit()

    # -- buckets -------------------------------------------------------------
    def create_bucket(self, name: str, created_at: datetime, backend: str = "local_disk", region: str = "us-east-1") -> None:
        with self._lock:
            self._local.execute(
                "INSERT OR REPLACE INTO buckets (name, created_at, backend, region) VALUES (?, ?, ?, ?)",
                (name, created_at.isoformat(), backend, region),
            )
            self._local.commit()

    def bucket_exists(self, name: str) -> bool:
        with self._lock:
            cur = self._local.execute("SELECT 1 FROM buckets WHERE name = ?", (name,))
            return cur.fetchone() is not None

    def get_bucket(self, name: str) -> Optional[BucketInfo]:
        with self._lock:
            cur = self._local.execute(
                "SELECT name, created_at, backend, region FROM buckets WHERE name = ?", (name,)
            )
            row = cur.fetchone()
            if not row:
                return None
            return BucketInfo(
                name=row[0],
                created_at=datetime.fromisoformat(row[1]),
                backend=row[2],
                region=row[3],
            )

    def delete_bucket(self, name: str) -> None:
        with self._lock:
            self._local.execute("DELETE FROM buckets WHERE name = ?", (name,))
            self._local.execute("DELETE FROM objects WHERE bucket = ?", (name,))
            self._local.commit()

    def list_buckets(self) -> List[BucketInfo]:
        with self._lock:
            cur = self._local.execute(
                "SELECT name, created_at, backend, region FROM buckets ORDER BY name"
            )
            return [
                BucketInfo(
                    name=r[0],
                    created_at=datetime.fromisoformat(r[1]),
                    backend=r[2],
                    region=r[3],
                )
                for r in cur.fetchall()
            ]

    # -- objects -------------------------------------------------------------
    def _row_to_object(self, row) -> ObjectInfo:
        return ObjectInfo(
            key=row[0],
            size=row[1],
            etag=row[2],
            content_type=row[3],
            last_modified=datetime.fromisoformat(row[4]),
            storage_class=row[5],
            content_encoding=row[6],
            expires=row[7],
            metadata=json.loads(row[8]) if row[8] else {},
            current_tier=row[9] if len(row) > 9 else "tier0",
            migration_state=row[10] if len(row) > 10 else "idle",
            heat_score=row[11] if len(row) > 11 else 100.0,
        )

    def put_object(self, bucket: str, info: ObjectInfo) -> None:
        with self._lock:
            current_tier = getattr(info, "current_tier", "tier0")
            migration_state = getattr(info, "migration_state", "idle")
            heat_score = getattr(info, "heat_score", 100.0)
            self._local.execute(
                """
                INSERT OR REPLACE INTO objects
                (bucket, key, size, etag, content_type, last_modified,
                 storage_class, content_encoding, expires, metadata,
                 current_tier, migration_state, heat_score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    bucket,
                    info.key,
                    info.size,
                    info.etag,
                    info.content_type,
                    info.last_modified.isoformat(),
                    info.storage_class,
                    info.content_encoding,
                    info.expires,
                    json.dumps(info.metadata),
                    current_tier,
                    migration_state,
                    heat_score,
                ),
            )
            self._local.commit()

    def get_object(self, bucket: str, key: str) -> Optional[ObjectInfo]:
        with self._lock:
            cur = self._local.execute(
                """
                SELECT key, size, etag, content_type, last_modified, storage_class,
                       content_encoding, expires, metadata, current_tier, migration_state, heat_score
                FROM objects WHERE bucket = ? AND key = ?
                """,
                (bucket, key),
            )
            row = cur.fetchone()
            return self._row_to_object(row) if row else None

    def delete_object(self, bucket: str, key: str) -> None:
        with self._lock:
            self._local.execute(
                "DELETE FROM objects WHERE bucket = ? AND key = ?", (bucket, key)
            )
            self._local.commit()
            self._logs_conn.execute(
                "DELETE FROM access_logs WHERE bucket = ? AND key = ?", (bucket, key)
            )
            self._logs_conn.commit()

    def prune_access_logs(self, cutoff: datetime) -> None:
        with self._lock:
            self._logs_conn.execute(
                "DELETE FROM access_logs WHERE timestamp < ?",
                (cutoff.isoformat(),)
            )
            self._logs_conn.commit()

    def list_objects(
        self,
        bucket: str,
        prefix: str = "",
        delimiter: str = "",
        max_keys: int = 1000,
        start_after: str = "",
        continuation_token: Optional[str] = None,
    ) -> ListObjectsResult:
        with self._lock:
            start = start_after
            if continuation_token:
                start = continuation_token
            if prefix:
                like = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                cur = self._local.execute(
                    "SELECT key, size, etag, content_type, last_modified, storage_class, "
                    "content_encoding, expires, metadata, current_tier, migration_state, heat_score FROM objects "
                    "WHERE bucket = ? AND key LIKE ? ESCAPE '\\' AND key > ? ORDER BY key LIMIT ?",
                    (bucket, like + "%", start, max_keys + 1),
                )
            else:
                cur = self._local.execute(
                    "SELECT key, size, etag, content_type, last_modified, storage_class, "
                    "content_encoding, expires, metadata, current_tier, migration_state, heat_score FROM objects "
                    "WHERE bucket = ? AND key > ? ORDER BY key LIMIT ?",
                    (bucket, start, max_keys + 1),
                )
            rows = cur.fetchall()
            truncated = len(rows) > max_keys
            if truncated:
                rows = rows[:max_keys]
            objects: List[ObjectInfo] = [self._row_to_object(r) for r in rows]

            common_prefixes: List[str] = []
            if delimiter:
                seen: set[str] = set()
                filtered_objects: List[ObjectInfo] = []
                prefix_len = len(prefix)
                for obj in objects:
                    if obj.key.startswith(prefix):
                        rest = obj.key[prefix_len:]
                        if delimiter in rest:
                            seen.add(prefix + rest.split(delimiter, 1)[0] + delimiter)
                        else:
                            filtered_objects.append(obj)
                    else:
                        filtered_objects.append(obj)
                common_prefixes = sorted(seen)
                objects = filtered_objects

            next_token = objects[-1].key if truncated else None
            return ListObjectsResult(
                objects=objects,
                common_prefixes=sorted(common_prefixes),
                is_truncated=truncated,
                next_continuation_token=next_token,
            )

    # -- multipart uploads ---------------------------------------------------
    def create_upload(
        self,
        bucket: str,
        key: str,
        upload_id: str,
        created_at: datetime,
        content_type: str = "application/octet-stream",
        metadata: Optional[Dict[str, str]] = None,
    ) -> None:
        with self._lock:
            self._local.execute(
                "INSERT INTO uploads (upload_id, bucket, key, created_at, content_type, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    upload_id,
                    bucket,
                    key,
                    created_at.isoformat(),
                    content_type,
                    json.dumps(metadata or {}),
                ),
            )
            self._local.commit()

    def get_upload(self, upload_id: str) -> Optional[UploadInfo]:
        with self._lock:
            cur = self._local.execute(
                "SELECT upload_id, bucket, key, created_at, content_type, metadata "
                "FROM uploads WHERE upload_id = ?",
                (upload_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return UploadInfo(
                upload_id=row[0],
                bucket=row[1],
                key=row[2],
                created_at=datetime.fromisoformat(row[3]),
                content_type=row[4],
                metadata=json.loads(row[5]) if row[5] else {},
            )

    def put_part(self, upload_id: str, part_number: int, etag: str, size: int) -> None:
        with self._lock:
            self._local.execute(
                "INSERT OR REPLACE INTO parts (upload_id, part_number, etag, size) "
                "VALUES (?, ?, ?, ?)",
                (upload_id, part_number, etag, size),
            )
            self._local.commit()

    def list_parts(
        self, upload_id: str, max_parts: int = 1000, part_number_marker: int = 0
    ) -> List[PartInfo]:
        with self._lock:
            cur = self._local.execute(
                "SELECT part_number, etag, size FROM parts WHERE upload_id = ? "
                "AND part_number > ? ORDER BY part_number LIMIT ?",
                (upload_id, part_number_marker, max_parts + 1),
            )
            rows = cur.fetchall()
            truncated = len(rows) > max_parts
            if truncated:
                rows = rows[:max_parts]
            parts = [PartInfo(part_number=r[0], etag=r[1], size=r[2]) for r in rows]
            return parts

    def get_parts(self, upload_id: str) -> List[PartInfo]:
        with self._lock:
            cur = self._local.execute(
                "SELECT part_number, etag, size FROM parts WHERE upload_id = ? ORDER BY part_number",
                (upload_id,),
            )
            return [PartInfo(part_number=r[0], etag=r[1], size=r[2]) for r in cur.fetchall()]

    def delete_upload(self, upload_id: str) -> None:
        with self._lock:
            self._local.execute("DELETE FROM parts WHERE upload_id = ?", (upload_id,))
            self._local.execute("DELETE FROM uploads WHERE upload_id = ?", (upload_id,))
            self._local.commit()

    # -- HSM methods --------------------------------------------------------
    def get_tier_size(self, tier_id: str) -> int:
        with self._lock:
            cur = self._local.execute(
                "SELECT SUM(size) FROM objects WHERE current_tier = ?", (tier_id,)
            )
            row = cur.fetchone()
            return row[0] if row and row[0] is not None else 0

    def update_object_tier(self, bucket: str, key: str, tier_id: str) -> None:
        with self._lock:
            self._local.execute(
                "UPDATE objects SET current_tier = ? WHERE bucket = ? AND key = ?",
                (tier_id, bucket, key),
            )
            self._local.commit()

    def update_hsm_state(self, bucket: str, key: str, state: str) -> None:
        with self._lock:
            self._local.execute(
                "UPDATE objects SET migration_state = ? WHERE bucket = ? AND key = ?",
                (state, bucket, key),
            )
            self._local.commit()

    def update_heat_score(self, bucket: str, key: str, score: float) -> None:
        with self._lock:
            self._local.execute(
                "UPDATE objects SET heat_score = ? WHERE bucket = ? AND key = ?",
                (score, bucket, key),
            )
            self._local.commit()

    def log_access(self, bucket: str, key: str, event_type: str) -> None:
        ts_str = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._logs_conn.execute(
                "INSERT INTO access_logs (bucket, key, event_type, timestamp) VALUES (?, ?, ?, ?)",
                (bucket, key, event_type, ts_str),
            )
            self._logs_conn.commit()

        try:
            import asyncio
            from minamo.admin.ws import ws_manager
            loop = asyncio.get_running_loop()
            if loop and loop.is_running():
                loop.create_task(
                    ws_manager.broadcast("access_log", {
                        "bucket": bucket,
                        "key": key,
                        "event_type": event_type,
                        "timestamp": ts_str,
                    })
                )
        except Exception:
            pass

    def get_access_logs(self, bucket: str, key: str) -> List[Tuple[str, datetime]]:
        with self._lock:
            cur = self._logs_conn.execute(
                "SELECT event_type, timestamp FROM access_logs WHERE bucket = ? AND key = ? ORDER BY timestamp DESC",
                (bucket, key),
            )
            return [
                (row[0], datetime.fromisoformat(row[1]))
                for row in cur.fetchall()
            ]

    def get_all_access_logs(self) -> Dict[Tuple[str, str], List[Tuple[str, datetime]]]:
        with self._lock:
            cur = self._logs_conn.execute(
                "SELECT bucket, key, event_type, timestamp FROM access_logs ORDER BY timestamp DESC"
            )
            logs: Dict[Tuple[str, str], List[Tuple[str, datetime]]] = {}
            for row in cur.fetchall():
                b, k, event_type, ts_str = row
                logs.setdefault((b, k), []).append((event_type, datetime.fromisoformat(ts_str)))
            return logs

    def update_heat_scores_bulk(self, updates: List[Tuple[float, str, str]]) -> None:
        with self._lock:
            self._local.executemany(
                "UPDATE objects SET heat_score = ? WHERE bucket = ? AND key = ?",
                updates
            )
            self._local.commit()

    def get_all_objects_hsm_info(self) -> List[Tuple[str, str, int, str, str, float, datetime]]:
        with self._lock:
            cur = self._local.execute(
                "SELECT bucket, key, size, current_tier, migration_state, heat_score, last_modified FROM objects"
            )
            return [
                (row[0], row[1], row[2], row[3], row[4], row[5], datetime.fromisoformat(row[6]))
                for row in cur.fetchall()
            ]

    # -- Migration Task DB helpers -------------------------------------------
    def create_migration_task(self, bucket: str, key: str, from_tier: str, to_tier: str) -> int:
        now_str = datetime.now(timezone.utc).isoformat()
        with self._lock:
            cursor = self._migrations_conn.execute(
                """
                INSERT INTO migration_tasks (bucket, key, from_tier, to_tier, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'in_flight', ?, ?)
                """,
                (bucket, key, from_tier, to_tier, now_str, now_str)
            )
            self._migrations_conn.commit()
            task_id = cursor.lastrowid

        try:
            import asyncio
            from minamo.admin.ws import ws_manager
            loop = asyncio.get_running_loop()
            if loop and loop.is_running():
                loop.create_task(
                    ws_manager.broadcast("hsm_task_updated", {
                        "id": task_id,
                        "bucket": bucket,
                        "key": key,
                        "from_tier": from_tier,
                        "to_tier": to_tier,
                        "status": "in_flight",
                        "error_message": None,
                        "retry_count": 0,
                        "created_at": now_str,
                        "updated_at": now_str,
                    })
                )
        except Exception:
            pass

        return task_id

    def update_migration_task(self, task_id: int, status: str, error_message: Optional[str] = None, retry_count: Optional[int] = None) -> None:
        now_str = datetime.now(timezone.utc).isoformat()
        with self._lock:
            if error_message is not None and retry_count is not None:
                self._migrations_conn.execute(
                    """
                    UPDATE migration_tasks
                    SET status = ?, error_message = ?, retry_count = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (status, error_message, retry_count, now_str, task_id)
                )
            elif error_message is not None:
                self._migrations_conn.execute(
                    """
                    UPDATE migration_tasks
                    SET status = ?, error_message = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (status, error_message, now_str, task_id)
                )
            elif retry_count is not None:
                self._migrations_conn.execute(
                    """
                    UPDATE migration_tasks
                    SET status = ?, retry_count = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (status, retry_count, now_str, task_id)
                )
            else:
                self._migrations_conn.execute(
                    """
                    UPDATE migration_tasks
                    SET status = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (status, now_str, task_id)
                )
            self._migrations_conn.commit()

        try:
            import asyncio
            from minamo.admin.ws import ws_manager
            loop = asyncio.get_running_loop()
            if loop and loop.is_running():
                loop.create_task(
                    ws_manager.broadcast("hsm_task_updated", {
                        "id": task_id,
                        "status": status,
                        "error_message": error_message,
                        "retry_count": retry_count,
                        "updated_at": now_str,
                    })
                )
        except Exception:
            pass

    def get_migration_tasks(self) -> List[Tuple[int, str, str, str, str, str, Optional[str], int, str, str]]:
        with self._lock:
            cur = self._migrations_conn.execute(
                """
                SELECT id, bucket, key, from_tier, to_tier, status, error_message, retry_count, created_at, updated_at
                FROM migration_tasks ORDER BY id DESC
                """
            )
            return cur.fetchall()

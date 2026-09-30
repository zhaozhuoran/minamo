"""WebSocket Event Manager and Real-Time Event Protocol for Minamo Admin Console.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Set, Optional
from fastapi import WebSocket, WebSocketDisconnect

from .auth import validate_admin_session

logger = logging.getLogger("minamo.admin.ws")


class WebSocketManager:
    """Manages active WebSocket connections, handles authentication, and broadcasts real-time events.
    """

    def __init__(self) -> None:
        self.active_connections: Set[WebSocket] = set()
        self._periodic_task: Optional[asyncio.Task] = None

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active_connections.add(websocket)
        logger.debug(f"WebSocket client connected. Total active connections: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket) -> None:
        self.active_connections.discard(websocket)
        logger.debug(f"WebSocket client disconnected. Remaining connections: {len(self.active_connections)}")

    async def send_personal_message(self, message: Dict[str, Any], websocket: WebSocket) -> None:
        try:
            await websocket.send_text(json.dumps(message))
        except Exception as e:
            logger.warning(f"Error sending personal WS message: {e}")

    async def broadcast(self, event_type: str, data: Dict[str, Any]) -> None:
        if not self.active_connections:
            return

        payload = {
            "type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": data,
        }
        message_str = json.dumps(payload)

        disconnected: Set[WebSocket] = set()
        for connection in list(self.active_connections):
            try:
                await connection.send_text(message_str)
            except Exception as e:
                logger.debug(f"Failed to send to WS client, queuing for disconnect: {e}")
                disconnected.add(connection)

        for conn in disconnected:
            self.disconnect(conn)

    async def authenticate_connection(self, websocket: WebSocket, app_state: Any) -> bool:
        """Authenticate WS client using HTTP Cookie or initial JSON auth message.
        """
        # 1. Check HTTP Cookie minamo_admin_token
        cookie_token = websocket.cookies.get("minamo_admin_token")
        if cookie_token and validate_admin_session(cookie_token):
            return True

        # 2. Check header X-Admin-Token
        header_token = websocket.headers.get("x-admin-token")
        if header_token and validate_admin_session(header_token):
            return True

        # 3. Check dev mode enforce_signature = False
        settings = getattr(app_state, "settings", None)
        if settings and not settings.enforce_signature:
            return True

        # 4. Fallback: Wait for initial JSON authentication message within 5 seconds
        try:
            raw_msg = await asyncio.wait_for(websocket.receive_text(), timeout=5.0)
            data = json.loads(raw_msg)
            if data.get("type") == "auth":
                token = data.get("token")
                if token and validate_admin_session(token):
                    await self.send_personal_message(
                        {"type": "auth_result", "status": "ok", "message": "Authenticated successfully"},
                        websocket,
                    )
                    return True
        except (asyncio.TimeoutError, json.JSONDecodeError, Exception) as e:
            logger.warning(f"WebSocket auth message failed or timed out: {e}")

        await self.send_personal_message(
            {"type": "auth_result", "status": "error", "message": "Authentication failed"},
            websocket,
        )
        await websocket.close(code=1008, reason="Unauthorized")
        return False


# Global singleton instance
ws_manager = WebSocketManager()


async def get_overview_data(app_state: Any) -> Dict[str, Any]:
    metadata = getattr(app_state, "metadata", None)
    settings = getattr(app_state, "settings", None)
    scheduler = getattr(app_state, "scheduler", None)
    cache = getattr(app_state, "cache", None)
    config = getattr(app_state, "config", None)

    storage_bytes = 0
    objects_count = 0
    buckets_count = 0
    recent_activity = []

    if metadata:
        all_objs = metadata.get_all_objects_hsm_info()
        objects_count = len(all_objs)
        storage_bytes = sum(obj[2] for obj in all_objs)
        buckets = metadata.list_buckets()
        buckets_count = len(buckets)

        all_logs = metadata.get_all_access_logs()
        flat_logs = []
        for (b, k), events in all_logs.items():
            for event_type, ts in events:
                flat_logs.append({
                    "bucket": b,
                    "key": k,
                    "event_type": event_type,
                    "timestamp": ts.isoformat(),
                })
        flat_logs.sort(key=lambda x: x["timestamp"], reverse=True)
        recent_activity = flat_logs[:15]

    cache_bytes = cache.current_size if cache else 0
    cache_max_bytes = cache.max_size_bytes if cache else 0
    cache_entries = len(cache.entries) if cache else 0

    active_migrations = len(getattr(scheduler, "_active_tasks", {})) if scheduler else 0
    migrated_bytes = getattr(scheduler, "successful_migrations_bytes", 0) if scheduler else 0

    tiers_info = []
    if settings and getattr(settings, "hsm", None) and settings.hsm.tiers:
        for t in settings.hsm.tiers:
            t_size = metadata.get_tier_size(t.id) if metadata else 0
            t_objs = len([o for o in metadata.get_all_objects_hsm_info() if o[3] == t.id]) if metadata else 0
            tiers_info.append({
                "id": t.id,
                "backend": t.backend,
                "priority": t.priority,
                "current_size": t_size,
                "object_count": t_objs,
                "target_capacity": t.target_capacity,
                "high_watermark": t.high_watermark,
                "limit": t.limit,
            })

    scheduler_status = "Disabled"
    if settings and settings.hsm.enabled:
        if scheduler and getattr(scheduler, "_running_task", None) and not scheduler._running_task.done():
            scheduler_status = "Running"
        else:
            scheduler_status = "Stopped"

    backends_status = []
    if config and hasattr(config, "backend_configs"):
        for b_id, b_cfg in config.backend_configs.items():
            backends_status.append({
                "id": b_id,
                "type": b_cfg.get("type", "unknown"),
                "status": "Configured",
            })

    return {
        "storage_bytes": storage_bytes,
        "objects_count": objects_count,
        "buckets_count": buckets_count,
        "cache": {
            "current_bytes": cache_bytes,
            "max_bytes": cache_max_bytes,
            "entries_count": cache_entries,
        },
        "hsm": {
            "enabled": settings.hsm.enabled if settings else False,
            "active_migrations": active_migrations,
            "migrated_bytes": migrated_bytes,
            "status": scheduler_status,
        },
        "tiers": tiers_info,
        "system_status": {
            "s3_api": "Operational",
            "metadata_db": "Operational" if metadata else "Offline",
            "hsm_scheduler": "Operational" if scheduler_status == "Running" else scheduler_status,
            "backends": backends_status,
        },
        "recent_activity": recent_activity,
    }


async def start_periodic_metrics_broadcast(app_state: Any, interval: float = 2.0) -> None:
    """Background task to push system metrics / overview data periodically."""
    while True:
        try:
            await asyncio.sleep(interval)
            if ws_manager.active_connections:
                overview_data = await get_overview_data(app_state)
                await ws_manager.broadcast("overview_update", overview_data)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in periodic metrics WS broadcast loop: {e}")
            await asyncio.sleep(interval)

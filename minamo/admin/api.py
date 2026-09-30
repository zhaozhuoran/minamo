"""Admin Console REST API endpoints and WebSocket connection handler.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect

from .auth import authenticate_admin_credentials, create_admin_session, require_admin
from .ws import ws_manager

api_router = APIRouter(prefix="/api/admin", tags=["admin"])


# -- Request / Response Schemas ----------------------------------------------
class LoginRequest(BaseModel):
    access_key: Optional[str] = None
    secret_key: Optional[str] = None
    password: Optional[str] = None


class CreateBucketRequest(BaseModel):
    name: str
    backend: str = "local_disk"
    region: str = "us-east-1"


class HsmControlRequest(BaseModel):
    action: str  # "pause", "resume", "tick"


# -- WebSocket Route ---------------------------------------------------------
@api_router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)
    authenticated = await ws_manager.authenticate_connection(websocket, websocket.app.state)
    if not authenticated:
        return

    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    await ws_manager.send_personal_message({"type": "pong"}, websocket)
            except Exception:
                pass
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception:
        ws_manager.disconnect(websocket)


# -- Auth Routes -------------------------------------------------------------
@api_router.post("/auth/login")
async def login(req: LoginRequest, request: Request):
    settings = getattr(request.app.state, "settings", None)
    valid = authenticate_admin_credentials(
        access_key=req.access_key,
        secret_key=req.secret_key,
        password=req.password,
        settings=settings,
    )
    if not valid:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_admin_session()
    return {"token": token, "valid": True}


# -- Overview Route ----------------------------------------------------------
@api_router.get("/overview", dependencies=[Depends(require_admin)])
async def get_overview(request: Request):
    app_state = request.app.state
    metadata = getattr(app_state, "metadata", None)
    settings = getattr(app_state, "settings", None)
    scheduler = getattr(app_state, "scheduler", None)
    cache = getattr(app_state, "cache", None)
    backend = getattr(app_state, "backend", None)
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

        # Get recent activity from access logs
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

    # Tiers breakdown
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

    # System status
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


# -- Buckets Routes ----------------------------------------------------------
@api_router.get("/buckets", dependencies=[Depends(require_admin)])
async def list_buckets(request: Request):
    metadata = request.app.state.metadata
    if not metadata:
        return []

    buckets = metadata.list_buckets()
    all_objs = metadata.get_all_objects_hsm_info()

    result = []
    for b in buckets:
        b_objs = [o for o in all_objs if o[0] == b.name]
        total_sz = sum(o[2] for o in b_objs)
        result.append({
            "name": b.name,
            "created_at": b.created_at.isoformat(),
            "backend": b.backend,
            "region": b.region,
            "object_count": len(b_objs),
            "total_size": total_sz,
        })
    return result


@api_router.post("/buckets", dependencies=[Depends(require_admin)])
async def create_bucket(req: CreateBucketRequest, request: Request):
    metadata = request.app.state.metadata
    backend = request.app.state.backend

    if metadata.bucket_exists(req.name):
        raise HTTPException(status_code=400, detail=f"Bucket '{req.name}' already exists")

    now = datetime.now(timezone.utc)
    metadata.create_bucket(req.name, now, backend=req.backend, region=req.region)

    if backend and hasattr(backend, "create_bucket"):
        try:
            await backend.create_bucket(req.name)
        except Exception:
            pass

    res = {
        "name": req.name,
        "created_at": now.isoformat(),
        "backend": req.backend,
        "region": req.region,
        "object_count": 0,
        "total_size": 0,
    }
    await ws_manager.broadcast("bucket_created", res)
    return res


@api_router.delete("/buckets/{name}", dependencies=[Depends(require_admin)])
async def delete_bucket(name: str, request: Request, force: bool = False):
    metadata = request.app.state.metadata
    backend = request.app.state.backend

    if not metadata.bucket_exists(name):
        raise HTTPException(status_code=404, detail=f"Bucket '{name}' not found")

    objs = metadata.list_objects(name, max_keys=1).objects
    if objs and not force:
        raise HTTPException(status_code=400, detail="Bucket is not empty. Use force=true to delete with all objects.")

    if objs and force:
        # Delete all objects in bucket
        all_objs = metadata.list_objects(name, max_keys=10000).objects
        for o in all_objs:
            if backend and hasattr(backend, "delete_object"):
                try:
                    await backend.delete_object(name, o.key)
                except Exception:
                    pass
            metadata.delete_object(name, o.key)

    metadata.delete_bucket(name)
    if backend and hasattr(backend, "delete_bucket"):
        try:
            await backend.delete_bucket(name)
        except Exception:
            pass

    await ws_manager.broadcast("bucket_deleted", {"name": name})
    return {"message": f"Bucket '{name}' deleted successfully"}


@api_router.get("/buckets/{name}/objects", dependencies=[Depends(require_admin)])
async def list_bucket_objects(name: str, request: Request, prefix: str = "", max_keys: int = 1000):
    metadata = request.app.state.metadata
    if not metadata.bucket_exists(name):
        raise HTTPException(status_code=404, detail=f"Bucket '{name}' not found")

    res = metadata.list_objects(name, prefix=prefix, max_keys=max_keys)
    objs = []
    for o in res.objects:
        objs.append({
            "key": o.key,
            "size": o.size,
            "etag": o.etag,
            "content_type": o.content_type,
            "current_tier": getattr(o, "current_tier", "tier0"),
            "migration_state": getattr(o, "migration_state", "idle"),
            "heat_score": getattr(o, "heat_score", 100.0),
            "last_modified": o.last_modified.isoformat(),
        })
    return {
        "bucket": name,
        "objects": objs,
        "is_truncated": res.is_truncated,
        "next_continuation_token": res.next_continuation_token,
    }


@api_router.delete("/buckets/{name}/objects/{key:path}", dependencies=[Depends(require_admin)])
async def delete_object(name: str, key: str, request: Request):
    metadata = request.app.state.metadata
    backend = request.app.state.backend

    if not metadata.get_object(name, key):
        raise HTTPException(status_code=404, detail=f"Object '{key}' not found in bucket '{name}'")

    if backend and hasattr(backend, "delete_object"):
        try:
            await backend.delete_object(name, key)
        except Exception:
            pass

    metadata.delete_object(name, key)
    await ws_manager.broadcast("object_deleted", {"bucket": name, "key": key})
    return {"message": f"Object '{key}' deleted successfully"}


# -- Tiers / HSM Routes ------------------------------------------------------
@api_router.get("/tiers", dependencies=[Depends(require_admin)])
async def list_tiers(request: Request):
    metadata = request.app.state.metadata
    settings = request.app.state.settings

    if not settings or not settings.hsm or not settings.hsm.tiers:
        return []

    all_objs = metadata.get_all_objects_hsm_info() if metadata else []

    result = []
    for t in settings.hsm.tiers:
        tier_objs = [o for o in all_objs if o[3] == t.id]
        t_size = sum(o[2] for o in tier_objs)
        heat_scores = [o[5] for o in tier_objs]

        result.append({
            "id": t.id,
            "backend": t.backend,
            "priority": t.priority,
            "target_capacity": t.target_capacity,
            "high_watermark": t.high_watermark,
            "limit": t.limit,
            "minimum_residency": t.minimum_residency,
            "current_size": t_size,
            "object_count": len(tier_objs),
            "heat_score_stats": {
                "min": min(heat_scores) if heat_scores else 0.0,
                "max": max(heat_scores) if heat_scores else 0.0,
                "avg": round(sum(heat_scores) / len(heat_scores), 2) if heat_scores else 0.0,
            }
        })
    return result


# -- Backends Routes ---------------------------------------------------------
@api_router.get("/backends", dependencies=[Depends(require_admin)])
async def list_backends(request: Request):
    config = request.app.state.config
    settings = request.app.state.settings

    result = []
    if config and hasattr(config, "backend_configs"):
        for b_id, b_cfg in config.backend_configs.items():
            b_type = b_cfg.get("type", "unknown")
            is_active = (b_id == settings.backend) if settings else False

            status = "Configured"
            if b_type == "local_disk":
                root_path = Path(b_cfg.get("root", "data/localdisk"))
                status = "Healthy" if root_path.exists() else "Configured"

            result.append({
                "id": b_id,
                "type": b_type,
                "is_default": is_active,
                "status": status,
                "config": {k: v for k, v in b_cfg.items() if "secret" not in k and "key" not in k},
            })
    return result


# -- HSM Scheduler Routes ----------------------------------------------------
@api_router.get("/hsm", dependencies=[Depends(require_admin)])
async def get_hsm_status(request: Request):
    scheduler = request.app.state.scheduler
    settings = request.app.state.settings
    metadata = request.app.state.metadata

    if not settings or not settings.hsm:
        return {"enabled": False, "status": "Disabled"}

    status_str = "Disabled"
    if settings.hsm.enabled:
        if scheduler and getattr(scheduler, "_running_task", None) and not scheduler._running_task.done():
            status_str = "Running"
        else:
            status_str = "Stopped"

    active_tasks = []
    succ_count = 0
    succ_bytes = 0
    failed_count = 0
    history = []

    if scheduler and hasattr(scheduler, "get_migration_status"):
        st = scheduler.get_migration_status()
        active_tasks = st.get("active_tasks", [])
        succ_count = st.get("successful_migrations_count", 0)
        succ_bytes = st.get("successful_migrations_bytes", 0)
        failed_count = st.get("failed_migrations_count", 0)

    if metadata and hasattr(metadata, "get_migration_tasks"):
        raw_tasks = metadata.get_migration_tasks()
        for task in raw_tasks[:50]:  # Limit to 50 most recent
            # (id, bucket, key, from_tier, to_tier, status, error_message, retry_count, created_at, updated_at)
            history.append({
                "id": task[0],
                "bucket": task[1],
                "key": task[2],
                "from_tier": task[3],
                "to_tier": task[4],
                "status": task[5],
                "error_message": task[6],
                "retry_count": task[7],
                "created_at": task[8],
                "updated_at": task[9],
            })

    return {
        "enabled": settings.hsm.enabled,
        "status": status_str,
        "overflow_policy": settings.hsm.overflow_policy,
        "max_concurrent_migrations": settings.hsm.max_concurrent_migrations,
        "active_tasks_count": len(active_tasks),
        "active_tasks": active_tasks,
        "successful_migrations_count": succ_count,
        "successful_migrations_bytes": succ_bytes,
        "failed_migrations_count": failed_count,
        "history": history,
    }


@api_router.post("/hsm/control", dependencies=[Depends(require_admin)])
async def control_hsm(req: HsmControlRequest, request: Request):
    scheduler = request.app.state.scheduler
    if not scheduler:
        raise HTTPException(status_code=400, detail="HSM Scheduler is not enabled or available")

    res = {}
    if req.action == "pause":
        await scheduler.stop()
        res = {"message": "HSM Scheduler paused", "status": "Stopped"}
    elif req.action == "resume":
        scheduler.start()
        res = {"message": "HSM Scheduler resumed", "status": "Running"}
    elif req.action == "tick":
        import asyncio
        asyncio.create_task(scheduler.tick())
        res = {"message": "HSM Scheduler tick triggered in background", "status": "Running"}
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action '{req.action}'")

    await ws_manager.broadcast("hsm_status_changed", {"action": req.action, "result": res})
    return res


# -- Cache Routes ------------------------------------------------------------
@api_router.get("/cache", dependencies=[Depends(require_admin)])
async def get_cache_info(request: Request):
    cache = request.app.state.cache
    if not cache:
        return {
            "current_size": 0,
            "max_size_bytes": 0,
            "entries_count": 0,
            "policy": "LRU",
            "cache_dir": "",
        }

    return {
        "current_size": cache.current_size,
        "max_size_bytes": cache.max_size_bytes,
        "entries_count": len(cache.entries),
        "policy": cache.policy,
        "cache_dir": str(cache.cache_dir),
    }


@api_router.post("/cache/clear", dependencies=[Depends(require_admin)])
async def clear_cache(request: Request):
    cache = request.app.state.cache
    if cache:
        cache.clear()
    await ws_manager.broadcast("cache_cleared", {})
    return {"message": "Cache cleared successfully"}


# -- Logs Routes -------------------------------------------------------------
@api_router.get("/logs/access", dependencies=[Depends(require_admin)])
async def get_access_logs(
    request: Request,
    bucket: Optional[str] = None,
    event_type: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
):
    metadata = request.app.state.metadata
    if not metadata:
        return {"logs": [], "total": 0}

    all_logs = metadata.get_all_access_logs()
    flat_logs = []
    for (b, k), events in all_logs.items():
        if bucket and b != bucket:
            continue
        for evt, ts in events:
            if event_type and evt.upper() != event_type.upper():
                continue
            flat_logs.append({
                "bucket": b,
                "key": k,
                "event_type": evt,
                "timestamp": ts.isoformat(),
            })

    flat_logs.sort(key=lambda x: x["timestamp"], reverse=True)
    total = len(flat_logs)
    paginated = flat_logs[offset : offset + limit]

    return {"logs": paginated, "total": total}


@api_router.get("/logs/app", dependencies=[Depends(require_admin)])
async def get_app_logs(
    request: Request,
    level: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 200,
):
    settings = request.app.state.settings
    logs_dir = settings.logs_dir if settings else Path("logs")
    latest_log = logs_dir / "latest.log"

    if not latest_log.exists():
        return {"logs": []}

    lines = []
    try:
        with latest_log.open("r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()

        for line in reversed(all_lines):
            line_str = line.strip()
            if not line_str:
                continue

            if level and level.upper() not in line_str.upper():
                continue

            if search and search.lower() not in line_str.lower():
                continue

            lines.append(line_str)
            if len(lines) >= limit:
                break
    except Exception as e:
        lines = [f"Error reading log file: {e}"]

    return {"logs": lines}


# -- Security Route ----------------------------------------------------------
@api_router.get("/security", dependencies=[Depends(require_admin)])
async def get_security_status(request: Request):
    settings = request.app.state.settings
    access_key = settings.access_key if settings else "minamo"
    masked_key = access_key[:3] + "***" if len(access_key) >= 3 else "***"

    return {
        "mode": "single_key",
        "global_access_key": masked_key,
        "multi_key_supported": False,
        "permissions_supported": False,
        "message": (
            "Authentication is currently configured with a single global access key. "
            "Multi-key management and bucket-level permissions are not yet configured."
        ),
    }

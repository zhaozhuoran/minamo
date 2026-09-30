"""Admin Console Application Factory.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from .api import api_router
from .ws import ws_manager, start_periodic_metrics_broadcast
from ..config import ConfigManager

logger = logging.getLogger("minamo.admin")

STATIC_DIR = Path(__file__).parent / "static"


def create_admin_app(s3_app: FastAPI | None = None, config: ConfigManager | None = None) -> FastAPI:
    config = config or (s3_app.state.config if s3_app and hasattr(s3_app.state, "config") else ConfigManager())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if s3_app:
            # Share state references from the main S3 app
            app.state.settings = getattr(s3_app.state, "settings", config.settings)
            app.state.config = getattr(s3_app.state, "config", config)
            app.state.metadata = getattr(s3_app.state, "metadata", None)
            app.state.service = getattr(s3_app.state, "service", None)
            app.state.backend = getattr(s3_app.state, "backend", None)
            app.state.state = getattr(s3_app.state, "state", None)
            app.state.scheduler = getattr(s3_app.state, "scheduler", None)
            app.state.cache = getattr(s3_app.state, "cache", None)
        else:
            app.state.settings = config.settings
            app.state.config = config

        broadcast_task = asyncio.create_task(start_periodic_metrics_broadcast(app.state))

        yield

        broadcast_task.cancel()
        try:
            await broadcast_task
        except asyncio.CancelledError:
            pass

    admin_app = FastAPI(title="Minamo Admin Console", version="0.1.0", lifespan=lifespan)
    admin_app.include_router(api_router)

    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    admin_app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @admin_app.get("/", response_class=HTMLResponse)
    async def serve_index(request: Request):
        index_file = STATIC_DIR / "index.html"
        if index_file.exists():
            return FileResponse(index_file, media_type='text/html')
        return HTMLResponse("<h1>Minamo Admin Console</h1><p>Frontend loading...</p>")

    @admin_app.get("/{full_path:path}")
    async def serve_spa_route(full_path: str, request: Request):
        # Serve static asset if exists within STATIC_DIR, otherwise fallback to index.html for SPA routes
        try:
            resolved_static = STATIC_DIR.resolve()
            target_file = (STATIC_DIR / full_path).resolve()
            if target_file.is_relative_to(resolved_static) and target_file.is_file():
                return FileResponse(target_file)
        except ValueError:
            pass

        index_file = STATIC_DIR / "index.html"
        if index_file.exists():
            return FileResponse(index_file, media_type='text/html')
        return HTMLResponse("<h1>Minamo Admin Console</h1>")

    return admin_app

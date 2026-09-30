"""Application factory and dependency wiring.

This module is the composition root: it constructs the configuration, the
metadata store, the storage backend and the S3 service, and wires them into the
FastAPI app. Everything is injected through ``app.state`` so handlers stay
decoupled from concrete implementations.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from .api.presign import presign_router
from .api.responses import error_xml
from .api.router import router
from .config import ConfigManager
from .metadata.store import MetadataStore
from .service.errors import S3Error
from .service.s3_service import S3Service
from .state import StateManager
from .storage.cache import CacheManager
from .storage.factory import create_backend
from .utils.logger import setup_logging

logger = logging.getLogger("minamo.app")


def create_app(config: ConfigManager | None = None, log_level: int = logging.INFO) -> FastAPI:
    config = config or ConfigManager()
    config.ensure_dirs()
    setup_logging(config.settings.logs_dir, level=log_level)

    metadata = MetadataStore(config.settings.metadata_dir / "metadata.db")
    metadata.init()
    state = StateManager(config.settings.state_dir)
    cache = CacheManager(config.settings.cache_dir)

    scheduler = None
    if config.settings.hsm.enabled:
        from .storage.manager import StorageManager
        from .storage.scheduler import HsmScheduler
        backend = StorageManager(config, state, metadata)
        # Check environment variable for shorter polling interval during testing
        import os
        interval = float(os.getenv("MINAMO_SCHEDULER_INTERVAL", "600.0"))
        scheduler = HsmScheduler(config, metadata, backend, interval_seconds=interval)
    else:
        backend = create_backend(config.settings.backend, config, state)

    service = S3Service(metadata, backend, region=config.settings.region)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = config.settings
        app.state.config = config
        app.state.metadata = metadata
        app.state.service = service
        app.state.backend = backend
        app.state.state = state
        app.state.scheduler = scheduler
        app.state.cache = cache

        if scheduler:
            scheduler.start()

        yield

        if scheduler:
            await scheduler.stop()
        metadata.close()

    app = FastAPI(title="Minamo", version="0.1.0", lifespan=lifespan)
    app.include_router(presign_router)
    app.include_router(router)

    @app.middleware("http")
    async def logging_middleware(request: Request, call_next):
        logger.debug("request: %s %s", request.method, str(request.url))
        response = await call_next(request)
        logger.debug("response: %s %s %d", request.method, str(request.url), response.status_code)
        return response

    @app.exception_handler(S3Error)
    async def handle_s3_error(request: Request, exc: S3Error) -> Response:
        logger.warning("s3_error: code=%s message=%s path=%s status=%d",
                        exc.code, exc.message, request.url.path, exc.http_status)
        request_id = getattr(request.state, "request_id", "unknown")
        body = error_xml(exc.code, exc.message, request.url.path, request_id)
        return Response(body, status_code=exc.http_status, media_type="application/xml")

    @app.exception_handler(ValueError)
    async def handle_value_error(request: Request, exc: ValueError) -> Response:
        logger.warning("value_error: %s", str(exc))
        request_id = getattr(request.state, "request_id", "unknown")
        msg = str(exc)
        # Standard S3 error code mapping for value validation or XML parsing error
        code = "MalformedXML" if "XML" in msg or "xml" in msg.lower() else "InvalidArgument"
        body = error_xml(code, msg, request.url.path, request_id)
        return Response(body, status_code=400, media_type="application/xml")

    @app.exception_handler(Exception)
    async def handle_generic_exception(request: Request, exc: Exception) -> Response:
        # Avoid intercepting standard S3Error and ValueError which have more specific handlers
        if isinstance(exc, S3Error):
            return await handle_s3_error(request, exc)
        if isinstance(exc, ValueError):
            return await handle_value_error(request, exc)

        logger.error("Unhandled exception occurred", exc_info=exc)
        request_id = getattr(request.state, "request_id", "unknown")
        body = error_xml("InternalError", "We encountered an internal error. Please try again.", request.url.path, request_id)
        return Response(body, status_code=500, media_type="application/xml")

    return app


app = create_app()

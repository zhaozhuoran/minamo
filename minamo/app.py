"""Application factory and dependency wiring.

This module is the composition root: it constructs the configuration, the
metadata store, the storage backend and the S3 service, and wires them into the
FastAPI app. Everything is injected through ``app.state`` so handlers stay
decoupled from concrete implementations.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from .api.presign import presign_router
from .api.responses import error_xml
from .api.router import router
from .config import Settings, get_settings
from .metadata.store import MetadataStore
from .service.errors import S3Error
from .service.s3_service import S3Service
from .storage.factory import create_backend


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.ensure_dirs()

    metadata = MetadataStore(settings.metadata_root / "metadata.db")
    metadata.init()
    backend = create_backend(settings.backend, settings.data_root)
    service = S3Service(metadata, backend)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.metadata = metadata
        app.state.service = service
        app.state.backend = backend
        yield

    app = FastAPI(title="Minamo", version="0.1.0", lifespan=lifespan)
    app.include_router(router)
    app.include_router(presign_router)

    @app.exception_handler(S3Error)
    async def handle_s3_error(request: Request, exc: S3Error) -> Response:
        request_id = getattr(request.state, "request_id", "unknown")
        body = error_xml(exc.code, exc.message, request.url.path, request_id)
        return Response(body, status_code=exc.http_status, media_type="application/xml")

    return app


app = create_app()

"""FastAPI application factory."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from edutoon import __version__
from edutoon.api import v1_router
from edutoon.core.config import Settings, get_settings
from edutoon.core.context import bind_request_id
from edutoon.core.errors import register_exception_handlers
from edutoon.db import dispose_engine, get_engine
from edutoon.providers.cache import Redis, get_redis_client
from edutoon.providers.storage import Storage, get_storage_client

REQUEST_ID_HEADER = "X-Request-Id"


def configure_logging(log_level: str) -> None:
    """Configure structlog for JSON output with a request_id on every line."""
    level = logging.getLevelNamesMapping().get(log_level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", level=level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    redis_client: Redis = get_redis_client(settings.REDIS_URL)
    storage_client: Storage = get_storage_client(
        endpoint_url=settings.STORAGE_ENDPOINT_URL,
        access_key_id=settings.STORAGE_ACCESS_KEY_ID,
        secret_access_key=settings.STORAGE_SECRET_ACCESS_KEY,
    )
    app.state.engine = get_engine()
    app.state.redis = redis_client
    app.state.storage = storage_client
    try:
        yield
    finally:
        await redis_client.aclose()
        await dispose_engine()


def create_app() -> FastAPI:
    settings: Settings = get_settings()
    configure_logging(settings.LOG_LEVEL)
    log = structlog.get_logger()

    app = FastAPI(
        title="EduToon AI API",
        version=__version__,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.APP_BASE_URL],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)

    @app.middleware("http")
    async def request_id_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        request.state.request_id = request_id
        with bind_request_id(request_id):
            log.info("request.start", method=request.method, path=request.url.path)
            response = await call_next(request)
            log.info("request.end", status_code=response.status_code)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        database = await _check_database(request.app.state.engine)
        redis_status = await _check_redis(request.app.state.redis)
        storage_status = await _check_storage(request.app.state.storage, settings.BUCKET_UPLOADS)
        healthy = database == "ok" and redis_status == "ok" and storage_status == "ok"
        return {
            "status": "ok" if healthy else "degraded",
            "environment": settings.ENVIRONMENT,
            "version": __version__,
            "database": database,
            "redis": redis_status,
            "storage": storage_status,
        }

    app.include_router(v1_router)

    return app


async def _check_database(engine: AsyncEngine) -> str:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - health check must never raise
        return "unreachable"
    return "ok"


async def _check_redis(redis_client: Redis) -> str:
    try:
        await redis_client.ping()
    except Exception:  # noqa: BLE001 - health check must never raise
        return "unreachable"
    return "ok"


async def _check_storage(storage_client: Storage, bucket: str) -> str:
    try:
        await storage_client.ping(bucket=bucket)
    except Exception:  # noqa: BLE001 - health check must never raise
        return "unreachable"
    return "ok"


app = create_app()

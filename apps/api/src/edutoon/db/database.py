"""Async engine construction and lifecycle.

This module owns the single application-wide :class:`AsyncEngine`. Everything
else (the FastAPI lifespan, the session factory, ad-hoc scripts) goes through
:func:`get_engine` so there is exactly one connection pool per process.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from edutoon.core.config import get_settings

# Deterministic constraint/index names so migrations and (future) model
# metadata always agree. Applied to the declarative ``Base`` metadata.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

_engine: AsyncEngine | None = None


def new_engine(url: str | None = None) -> AsyncEngine:
    """Build a fresh engine. Callers are responsible for disposing it."""
    settings = get_settings()
    return create_async_engine(
        url or settings.DATABASE_URL,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        future=True,
    )


def get_engine() -> AsyncEngine:
    """Return the process-wide engine, creating it on first use."""
    global _engine
    if _engine is None:
        _engine = new_engine()
    return _engine


async def dispose_engine() -> None:
    """Dispose the process-wide engine and its pool (idempotent)."""
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None

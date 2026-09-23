"""Database foundation: engine, session factory and declarative base.

Phase 2 wires the async SQLAlchemy plumbing and the Alembic migration
environment. No ORM models or repositories exist yet — the schema is defined
entirely by the hand-written migrations under ``alembic/versions``.
"""

from edutoon.db.base import Base
from edutoon.db.database import (
    NAMING_CONVENTION,
    dispose_engine,
    get_engine,
    new_engine,
)
from edutoon.db.session import get_session, get_sessionmaker

__all__ = [
    "NAMING_CONVENTION",
    "Base",
    "dispose_engine",
    "get_engine",
    "get_session",
    "get_sessionmaker",
    "new_engine",
]

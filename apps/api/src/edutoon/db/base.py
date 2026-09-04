"""Declarative base for all (future) ORM models.

No models are defined in Phase 2. This exists so that:
  * Alembic has a stable ``target_metadata`` to point at, and
  * every model added later inherits the shared naming convention.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

from edutoon.db.database import NAMING_CONVENTION


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

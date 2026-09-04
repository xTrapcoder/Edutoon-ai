"""Data access for the existing schema (migrations 0001-0005).

One module per table. Each exposes a frozen record type and plain
``async def`` functions taking an :class:`~sqlalchemy.ext.asyncio.AsyncSession`
as their first argument — no repository classes, no hidden session state.

Repositories query through SQLAlchemy Core (``db.tables``), never the
provider SDKs, and never commit: the caller (currently ``db.session.get_session``)
owns the transaction boundary.
"""

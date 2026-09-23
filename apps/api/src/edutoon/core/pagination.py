"""Keyset ("seek") pagination over existing tables.

Every table this backs has a ``created_at timestamptz`` and a UUIDv7 ``id``
(time-ordered), so ``(created_at, id)`` DESC is a stable, gap-free sort key —
no OFFSET, no skipped/duplicated rows under concurrent inserts.

Cursors are opaque to callers: encode a ``(created_at, id)`` pair, hand it
back on the next request.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, Table, tuple_
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.errors import InvalidCursorError, ValidationError

_CURSOR_SEPARATOR = "|"
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: list[T]
    next_cursor: str | None


def encode_cursor(created_at: datetime, id_: UUID) -> str:
    raw = f"{created_at.isoformat()}{_CURSOR_SEPARATOR}{id_}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        created_at_raw, id_raw = raw.split(_CURSOR_SEPARATOR)
        return datetime.fromisoformat(created_at_raw), UUID(id_raw)
    except (ValueError, binascii.Error) as exc:
        raise InvalidCursorError("Pagination cursor is malformed.") from exc


async def paginate_rows(
    session: AsyncSession,
    stmt: Select[Any],
    *,
    table: Table,
    limit: int = DEFAULT_PAGE_SIZE,
    cursor: str | None = None,
) -> tuple[Sequence[RowMapping], str | None]:
    """Apply a keyset ``WHERE``/``ORDER BY``/``LIMIT`` to ``stmt`` and run it.

    ``stmt`` must select from ``table`` and must not already have its own
    ``order_by``/``limit``. Returns the page's rows and the cursor for the
    next page (``None`` once exhausted).

    Raises :class:`~edutoon.core.errors.ValidationError` for an out-of-range
    ``limit`` rather than silently clamping it - a caller passing ``0`` or
    ``1_000_000`` almost certainly has a bug worth surfacing, not a value
    worth quietly reinterpreting.
    """
    if limit <= 0 or limit > MAX_PAGE_SIZE:
        raise ValidationError(f"limit must be between 1 and {MAX_PAGE_SIZE} (got {limit}).")

    if cursor is not None:
        cursor_created_at, cursor_id = decode_cursor(cursor)
        stmt = stmt.where(
            tuple_(table.c.created_at, table.c.id) < (cursor_created_at, cursor_id)
        )

    stmt = stmt.order_by(table.c.created_at.desc(), table.c.id.desc()).limit(limit + 1)

    result = await session.execute(stmt)
    rows = result.mappings().all()

    has_more = len(rows) > limit
    page_rows = rows[:limit]

    next_cursor = None
    if has_more and page_rows:
        last = page_rows[-1]
        next_cursor = encode_cursor(last["created_at"], last["id"])

    return page_rows, next_cursor

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from edutoon.core.errors import InvalidCursorError, ValidationError
from edutoon.core.pagination import MAX_PAGE_SIZE, decode_cursor, encode_cursor, paginate_rows
from edutoon.db.tables import users as users_table


def test_cursor_round_trips():
    created_at = datetime(2026, 1, 1, 12, 30, tzinfo=UTC)
    id_ = uuid4()

    cursor = encode_cursor(created_at, id_)

    assert decode_cursor(cursor) == (created_at, id_)


def test_decode_cursor_rejects_garbage():
    with pytest.raises(InvalidCursorError):
        decode_cursor("xx")  # invalid base64 padding


async def test_paginate_rows_rejects_zero_or_negative_limit(db_session):
    for bad_limit in (0, -1):
        with pytest.raises(ValidationError):
            await paginate_rows(
                db_session, select(users_table), table=users_table, limit=bad_limit
            )


async def test_paginate_rows_rejects_limit_over_the_max(db_session):
    with pytest.raises(ValidationError):
        await paginate_rows(
            db_session, select(users_table), table=users_table, limit=MAX_PAGE_SIZE + 1
        )


async def test_paginate_rows_accepts_the_inclusive_boundary_limits(db_session):
    # Must not raise - 1 and MAX_PAGE_SIZE are valid, not "just past invalid".
    await paginate_rows(db_session, select(users_table), table=users_table, limit=1)
    await paginate_rows(db_session, select(users_table), table=users_table, limit=MAX_PAGE_SIZE)

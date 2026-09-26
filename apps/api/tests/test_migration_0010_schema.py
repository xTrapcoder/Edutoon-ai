"""Migration 0010's schema, checked against the live catalogue.

The repositories deliberately turn any ``IntegrityError`` into a
``ConflictError``, which hides *which* constraint fired. These tests go around
the repositories with raw SQL so each rule is proven to live in the database
itself - not merely in the API schemas a future caller might skip - and each
expectation is taken from :mod:`edutoon.domain.generation_settings`, so the
database and the code cannot quietly disagree about what a setting may hold.

The upgrade and downgrade themselves are in
``test_migration_0010_upgrade_downgrade.py``.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from edutoon.domain import generation_settings as gs
from edutoon.repositories import users as users_repo


async def _owner_id(db_session):
    return (await users_repo.create(db_session, email=f"{uuid4()}@example.com")).id


async def _insert(db_session, owner_id, **columns):
    """INSERT a project naming only ``columns`` beyond the required ones."""
    names = ["owner_id", "title", "source_type", *columns]
    placeholders = ", ".join(f":{name}" for name in names)
    row = (
        await db_session.execute(
            text(
                f"INSERT INTO projects ({', '.join(names)}) VALUES ({placeholders}) "
                "RETURNING id, language, orientation, target_duration_ms"
            ),
            {"owner_id": owner_id, "title": "P", "source_type": "topic", **columns},
        )
    ).one()
    return row


async def _rejected_by(db_session, owner_id, constraint: str, **columns) -> None:
    with pytest.raises(IntegrityError) as excinfo:
        async with db_session.begin_nested():
            await _insert(db_session, owner_id, **columns)
    assert constraint in str(excinfo.value)


async def _column(db_session, column: str):
    row = (
        await db_session.execute(
            text(
                "SELECT data_type, is_nullable, column_default "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'projects' "
                "AND column_name = :c"
            ),
            {"c": column},
        )
    ).one_or_none()
    assert row is not None, f"projects.{column} does not exist"
    return row


# --- columns ---------------------------------------------------------------------------


async def test_orientation_is_a_non_null_text_column_defaulting_to_horizontal(db_session):
    column = await _column(db_session, "orientation")

    assert column.data_type == "text"
    assert column.is_nullable == "NO"
    assert "'horizontal'" in column.column_default


async def test_target_duration_is_a_non_null_bigint_column_of_milliseconds(db_session):
    column = await _column(db_session, "target_duration_ms")

    assert column.data_type == "bigint"
    assert column.is_nullable == "NO"
    assert column.column_default == str(gs.DEFAULT_TARGET_DURATION_MS)


async def test_language_stays_a_non_null_text_column_defaulting_to_en_gb(db_session):
    column = await _column(db_session, "language")

    assert column.data_type == "text"
    assert column.is_nullable == "NO"
    assert "'en-GB'" in column.column_default


# --- defaults --------------------------------------------------------------------------


async def test_an_insert_naming_no_setting_takes_the_domain_defaults(db_session):
    """Backwards compatibility, and the database's defaults held to the code's: an
    INSERT that predates migration 0010 (and so names none of its columns) is still
    valid, and gets exactly what :mod:`edutoon.domain.generation_settings` says.
    """
    row = await _insert(db_session, await _owner_id(db_session))

    assert row.orientation == gs.DEFAULT_ORIENTATION
    assert row.target_duration_ms == gs.DEFAULT_TARGET_DURATION_MS
    assert row.language == gs.DEFAULT_LANGUAGE


# --- orientation -----------------------------------------------------------------------


@pytest.mark.parametrize("orientation", sorted(gs.ORIENTATIONS))
async def test_orientation_check_accepts_each_domain_value(db_session, orientation):
    row = await _insert(db_session, await _owner_id(db_session), orientation=orientation)

    assert row.orientation == orientation


@pytest.mark.parametrize("orientation", ["", "Horizontal", "VERTICAL", "square", "landscape"])
async def test_orientation_check_rejects_anything_else(db_session, orientation):
    await _rejected_by(
        db_session,
        await _owner_id(db_session),
        "ck_projects_orientation",
        orientation=orientation,
    )


async def test_orientation_rejects_null(db_session):
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await _insert(db_session, await _owner_id(db_session), orientation=None)


# --- target duration -------------------------------------------------------------------


@pytest.mark.parametrize(
    "duration",
    [
        gs.MIN_TARGET_DURATION_MS,
        gs.MIN_TARGET_DURATION_MS + 1,
        gs.DEFAULT_TARGET_DURATION_MS,
        gs.MAX_TARGET_DURATION_MS - 1,
        gs.MAX_TARGET_DURATION_MS,
    ],
)
async def test_target_duration_check_accepts_the_bounds_and_values_between(db_session, duration):
    row = await _insert(db_session, await _owner_id(db_session), target_duration_ms=duration)

    assert row.target_duration_ms == duration


@pytest.mark.parametrize(
    "duration",
    [
        gs.MIN_TARGET_DURATION_MS - 1,
        1,
        0,
        -1,
        -gs.MIN_TARGET_DURATION_MS,
        gs.MAX_TARGET_DURATION_MS + 1,
        61 * gs.MS_PER_MINUTE,
    ],
)
async def test_target_duration_check_rejects_values_outside_one_to_sixty_minutes(
    db_session, duration
):
    await _rejected_by(
        db_session,
        await _owner_id(db_session),
        "ck_projects_target_duration_ms_range",
        target_duration_ms=duration,
    )


async def test_target_duration_rejects_null(db_session):
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await _insert(db_session, await _owner_id(db_session), target_duration_ms=None)


# --- language --------------------------------------------------------------------------


@pytest.mark.parametrize("language", sorted(gs.SUPPORTED_LANGUAGES))
async def test_language_check_accepts_each_supported_language(db_session, language):
    row = await _insert(db_session, await _owner_id(db_session), language=language)

    assert row.language == language


@pytest.mark.parametrize("language", ["", "fr-FR", "en", "en_IN", "en-in", "EN-IN", "en-US"])
async def test_language_check_rejects_anything_else(db_session, language):
    await _rejected_by(
        db_session, await _owner_id(db_session), "ck_projects_language", language=language
    )


# --- the checks hold on UPDATE too -----------------------------------------------------


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("orientation", "square", "ck_projects_orientation"),
        ("target_duration_ms", 0, "ck_projects_target_duration_ms_range"),
        (
            "target_duration_ms",
            gs.MAX_TARGET_DURATION_MS + 1,
            "ck_projects_target_duration_ms_range",
        ),
        ("language", "fr-FR", "ck_projects_language"),
    ],
)
async def test_the_checks_also_refuse_an_update(db_session, column, value, constraint):
    row = await _insert(db_session, await _owner_id(db_session))

    with pytest.raises(IntegrityError) as excinfo:
        async with db_session.begin_nested():
            await db_session.execute(
                text(f"UPDATE projects SET {column} = :v WHERE id = :id"),
                {"v": value, "id": row.id},
            )

    assert constraint in str(excinfo.value)

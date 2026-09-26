from __future__ import annotations

from uuid import uuid4

from sqlalchemy import text

from edutoon.repositories import projects as projects_repo
from edutoon.repositories import users as users_repo


async def _make_user(db_session):
    return await users_repo.create(db_session, email=f"{uuid4()}@example.com")


async def test_create_and_get_by_id(db_session):
    owner = await _make_user(db_session)

    project = await projects_repo.create(
        db_session, owner_id=owner.id, title="My video", source_type="topic"
    )

    assert project.owner_id == owner.id
    assert project.status == "draft"
    assert project.language == "en-IN"
    assert await projects_repo.get_by_id(db_session, project.id) == project


async def test_get_by_id_returns_none_when_missing(db_session):
    assert await projects_repo.get_by_id(db_session, uuid4()) is None


async def test_list_by_owner_only_returns_that_owners_projects(db_session):
    owner_a = await _make_user(db_session)
    owner_b = await _make_user(db_session)
    project_a = await projects_repo.create(
        db_session, owner_id=owner_a.id, title="A", source_type="topic"
    )
    await projects_repo.create(db_session, owner_id=owner_b.id, title="B", source_type="topic")

    page = await projects_repo.list_by_owner(db_session, owner_a.id)

    assert [item.id for item in page.items] == [project_a.id]


async def test_list_by_owner_paginates_across_pages_with_no_duplicates_or_gaps(db_session):
    owner = await _make_user(db_session)
    other_owner = await _make_user(db_session)
    created = [
        await projects_repo.create(
            db_session, owner_id=owner.id, title=f"P{i}", source_type="topic"
        )
        for i in range(5)
    ]
    # Interleaved with another owner's project - must never leak into the pages below.
    await projects_repo.create(
        db_session, owner_id=other_owner.id, title="Not mine", source_type="topic"
    )

    collected: list = []
    cursor: str | None = None
    pages_fetched = 0
    while True:
        page = await projects_repo.list_by_owner(db_session, owner.id, limit=2, cursor=cursor)
        collected.extend(item.id for item in page.items)
        pages_fetched += 1
        assert pages_fetched <= 10, "pagination did not terminate - possible cursor bug"
        cursor = page.next_cursor
        if cursor is None:
            break

    # No gaps: every created project was returned exactly once (no duplicates).
    assert len(collected) == len(set(collected)) == 5
    assert set(collected) == {project.id for project in created}
    # Newest first and stable across the cursor boundary (UUIDv7 ids are
    # time-ordered, so this also matches insertion order).
    assert collected == [project.id for project in reversed(created)]
    # Page sizes: two full pages of 2, then a final page of 1.
    assert pages_fetched == 3


# --- generation settings (migration 0010) ------------------------------------------------


async def _stored_settings(db_session, project_id):
    """What is actually in the row, read around the repository."""
    return (
        await db_session.execute(
            text(
                "SELECT orientation, target_duration_ms, language FROM projects WHERE id = :id"
            ),
            {"id": project_id},
        )
    ).one()


async def test_create_without_settings_takes_the_defaults(db_session):
    owner = await _make_user(db_session)

    project = await projects_repo.create(
        db_session, owner_id=owner.id, title="My video", source_type="topic"
    )

    assert project.orientation == "horizontal"
    assert project.target_duration_ms == 300_000
    assert project.language == "en-IN"
    stored = await _stored_settings(db_session, project.id)
    assert (stored.orientation, stored.target_duration_ms, stored.language) == (
        "horizontal",
        300_000,
        "en-IN",
    )


async def test_create_persists_the_settings_it_is_given(db_session):
    owner = await _make_user(db_session)

    project = await projects_repo.create(
        db_session,
        owner_id=owner.id,
        title="Portrait explainer",
        source_type="pdf",
        language="en-GB",
        orientation="vertical",
        target_duration_ms=1_800_000,
    )

    assert project.orientation == "vertical"
    assert project.target_duration_ms == 1_800_000
    assert project.language == "en-GB"
    assert await projects_repo.get_by_id(db_session, project.id) == project
    stored = await _stored_settings(db_session, project.id)
    assert (stored.orientation, stored.target_duration_ms, stored.language) == (
        "vertical",
        1_800_000,
        "en-GB",
    )


async def test_update_changes_only_the_settings_it_is_given(db_session):
    owner = await _make_user(db_session)
    project = await projects_repo.create(
        db_session, owner_id=owner.id, title="My video", source_type="topic"
    )

    updated = await projects_repo.update(
        db_session, project_id=project.id, owner_id=owner.id, orientation="vertical"
    )

    assert updated is not None
    assert updated.orientation == "vertical"
    assert updated.target_duration_ms == project.target_duration_ms
    assert updated.language == project.language
    assert updated.title == project.title
    stored = await _stored_settings(db_session, project.id)
    assert stored.orientation == "vertical"


async def test_update_persists_every_setting_together(db_session):
    owner = await _make_user(db_session)
    project = await projects_repo.create(
        db_session, owner_id=owner.id, title="My video", source_type="topic"
    )

    updated = await projects_repo.update(
        db_session,
        project_id=project.id,
        owner_id=owner.id,
        orientation="vertical",
        target_duration_ms=3_600_000,
        language="en-GB",
    )

    assert updated is not None
    assert (updated.orientation, updated.target_duration_ms, updated.language) == (
        "vertical",
        3_600_000,
        "en-GB",
    )
    assert await projects_repo.get_by_id_for_owner(db_session, project.id, owner.id) == updated
    stored = await _stored_settings(db_session, project.id)
    assert (stored.orientation, stored.target_duration_ms, stored.language) == (
        "vertical",
        3_600_000,
        "en-GB",
    )


async def test_list_by_owner_carries_each_projects_settings(db_session):
    owner = await _make_user(db_session)
    await projects_repo.create(
        db_session,
        owner_id=owner.id,
        title="Portrait",
        source_type="topic",
        orientation="vertical",
        target_duration_ms=60_000,
    )

    page = await projects_repo.list_by_owner(db_session, owner.id)

    [item] = page.items
    assert (item.orientation, item.target_duration_ms) == ("vertical", 60_000)

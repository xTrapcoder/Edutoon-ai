from __future__ import annotations

from uuid import uuid4

from edutoon.repositories import users as users_repo


async def test_create_returns_the_persisted_row(db_session):
    email = f"{uuid4()}@example.com"

    user = await users_repo.create(db_session, email=email, display_name="Ada")

    assert user.id is not None
    assert user.email == email
    assert user.display_name == "Ada"
    assert user.status == "active"


async def test_get_by_id_round_trips(db_session):
    created = await users_repo.create(db_session, email=f"{uuid4()}@example.com")

    fetched = await users_repo.get_by_id(db_session, created.id)

    assert fetched == created


async def test_get_by_id_returns_none_when_missing(db_session):
    assert await users_repo.get_by_id(db_session, uuid4()) is None


async def test_get_by_email_and_clerk_user_id(db_session):
    email = f"{uuid4()}@example.com"
    clerk_id = f"clerk_{uuid4()}"
    created = await users_repo.create(db_session, email=email, clerk_user_id=clerk_id)

    assert await users_repo.get_by_email(db_session, email) == created
    assert await users_repo.get_by_clerk_user_id(db_session, clerk_id) == created


async def test_list_page_paginates_newest_first(db_session):
    created = [
        await users_repo.create(db_session, email=f"{uuid4()}@example.com") for _ in range(3)
    ]

    first_page = await users_repo.list_page(db_session, limit=2)

    assert len(first_page.items) == 2
    assert first_page.next_cursor is not None
    # UUIDv7 ids are time-ordered, so DESC by (created_at, id) matches insertion order.
    assert [item.id for item in first_page.items] == [created[2].id, created[1].id]

    second_page = await users_repo.list_page(
        db_session, limit=2, cursor=first_page.next_cursor
    )

    assert [item.id for item in second_page.items] == [created[0].id]
    assert second_page.next_cursor is None

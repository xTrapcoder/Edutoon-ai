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

    # The table may already hold other, legitimate users (a development database does), so this
    # only reasons about the rows it created: it walks every page and checks the paging itself.
    first_page = await users_repo.list_page(db_session, limit=2)

    # Three rows of ours exist, so a page of two is always full and always has a successor.
    assert len(first_page.items) == 2
    assert first_page.next_cursor is not None

    pages = [first_page]
    while pages[-1].next_cursor is not None:
        pages.append(await users_repo.list_page(db_session, limit=2, cursor=pages[-1].next_cursor))

    assert all(len(page.items) <= 2 for page in pages)
    assert all(page.next_cursor is not None for page in pages[:-1])
    listed = [item.id for page in pages for item in page.items]
    assert len(listed) == len(set(listed)), "a user was listed on two pages"

    # UUIDv7 ids are time-ordered, so DESC by (created_at, id) matches insertion order: ours
    # come back newest first, wherever other users fall around them.
    ours = {user.id for user in created}
    assert [user_id for user_id in listed if user_id in ours] == [
        created[2].id,
        created[1].id,
        created[0].id,
    ]

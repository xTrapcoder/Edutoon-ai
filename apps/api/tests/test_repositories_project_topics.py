from __future__ import annotations

from uuid import uuid4

import pytest

from edutoon.core.errors import ConflictError
from edutoon.repositories import project_topics as project_topics_repo
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import users as users_repo
from edutoon.repositories.project_topics import NewProjectTopic


async def _make_project(db_session):
    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    return await projects_repo.create(
        db_session, owner_id=owner.id, title="P", source_type="topic"
    )


async def test_create_and_get_by_id(db_session):
    project = await _make_project(db_session)

    topic = await project_topics_repo.create(db_session, project_id=project.id, title="Intro")

    assert topic.status == "proposed"
    assert topic.parent_id is None
    assert await project_topics_repo.get_by_id(db_session, topic.id) == topic


async def test_child_topic_references_parent(db_session):
    project = await _make_project(db_session)
    parent = await project_topics_repo.create(db_session, project_id=project.id, title="Root")

    child = await project_topics_repo.create(
        db_session, project_id=project.id, title="Child", parent_id=parent.id, depth=1
    )

    assert child.parent_id == parent.id


async def test_list_by_project(db_session):
    project = await _make_project(db_session)
    topic = await project_topics_repo.create(db_session, project_id=project.id, title="Intro")

    page = await project_topics_repo.list_by_project(db_session, project.id)

    assert [item.id for item in page.items] == [topic.id]


# --- origin + keywords (migration 0009) ---------------------------------------------


async def test_create_defaults_to_a_user_topic_with_no_keywords(db_session):
    """A topic made exactly as before migration 0009 - no new arguments - is a
    user topic with an empty (never NULL) keyword list.
    """
    project = await _make_project(db_session)

    topic = await project_topics_repo.create(db_session, project_id=project.id, title="Intro")

    assert topic.origin == "user"
    assert topic.keywords == []


async def test_create_persists_origin_status_and_keywords(db_session):
    project = await _make_project(db_session)

    topic = await project_topics_repo.create(
        db_session,
        project_id=project.id,
        title="Photosynthesis",
        origin="source",
        status="accepted",
        keywords=["chlorophyll", "light-dependent reactions"],
    )

    assert topic.origin == "source"
    assert topic.status == "accepted"
    assert topic.keywords == ["chlorophyll", "light-dependent reactions"]
    assert await project_topics_repo.get_by_id(db_session, topic.id) == topic


@pytest.mark.parametrize("origin", ["source", "user", "suggested"])
async def test_every_allowed_origin_can_be_stored(db_session, origin):
    project = await _make_project(db_session)

    topic = await project_topics_repo.create(
        db_session, project_id=project.id, title="T", origin=origin
    )

    assert topic.origin == origin


async def test_an_unknown_origin_is_rejected(db_session):
    project = await _make_project(db_session)

    with pytest.raises(ConflictError):
        await project_topics_repo.create(
            db_session, project_id=project.id, title="T", origin="hallucinated"
        )


async def test_keywords_round_trip_as_a_list_of_str_preserving_order_and_unicode(db_session):
    """``keywords`` is a real ``text[]`` - what comes back is a plain
    ``list[str]``, in the order written, with non-ASCII text intact.
    """
    project = await _make_project(db_session)
    keywords = ["zeta", "Éclair", "naïve café", "alpha", "日本語"]

    topic = await project_topics_repo.create(
        db_session, project_id=project.id, title="T", keywords=keywords
    )
    fetched = await project_topics_repo.get_by_id(db_session, topic.id)

    assert fetched is not None
    assert type(fetched.keywords) is list
    assert all(type(keyword) is str for keyword in fetched.keywords)
    assert fetched.keywords == keywords


async def test_keywords_accepts_any_sequence_not_only_a_list(db_session):
    project = await _make_project(db_session)

    topic = await project_topics_repo.create(
        db_session, project_id=project.id, title="T", keywords=("a", "b")
    )

    assert topic.keywords == ["a", "b"]


# --- create_many ----------------------------------------------------------------


async def test_create_many_with_an_empty_list_is_a_noop(db_session):
    assert await project_topics_repo.create_many(db_session, []) == []


async def test_create_many_inserts_every_row_with_all_fields(db_session):
    project = await _make_project(db_session)

    created = await project_topics_repo.create_many(
        db_session,
        [
            NewProjectTopic(
                project_id=project.id,
                title="Second",
                position=1,
                origin="suggested",
                keywords=("x",),
            ),
            NewProjectTopic(
                project_id=project.id,
                title="First",
                position=0,
                summary="A summary.",
                origin="source",
                status="accepted",
            ),
        ],
    )

    by_title = {topic.title: topic for topic in created}
    assert by_title["First"].summary == "A summary."
    assert by_title["First"].origin == "source"
    assert by_title["First"].status == "accepted"
    assert by_title["First"].keywords == []
    assert by_title["Second"].origin == "suggested"
    assert by_title["Second"].status == "proposed"
    assert by_title["Second"].keywords == ["x"]


async def test_create_many_returns_rows_in_a_deterministic_order(db_session):
    project = await _make_project(db_session)

    created = await project_topics_repo.create_many(
        db_session,
        [
            NewProjectTopic(project_id=project.id, title="B", position=1),
            NewProjectTopic(project_id=project.id, title="A", position=0),
        ],
    )

    assert [topic.title for topic in created] == ["A", "B"]


async def test_create_many_builds_a_tree_one_depth_level_at_a_time(db_session):
    """The documented usage: roots first, then children pointing at the
    returned root ids - mapped by ``(parent_id, position)``, never by index.
    """
    project = await _make_project(db_session)
    roots = await project_topics_repo.create_many(
        db_session,
        [
            NewProjectTopic(project_id=project.id, title="Root A", position=0),
            NewProjectTopic(project_id=project.id, title="Root B", position=1),
        ],
    )
    root_by_position = {root.position: root for root in roots}

    children = await project_topics_repo.create_many(
        db_session,
        [
            NewProjectTopic(
                project_id=project.id,
                title="A1",
                parent_id=root_by_position[0].id,
                position=0,
                depth=1,
            ),
            NewProjectTopic(
                project_id=project.id,
                title="B1",
                parent_id=root_by_position[1].id,
                position=0,
                depth=1,
            ),
        ],
    )

    parent_of = {child.title: child.parent_id for child in children}
    assert parent_of == {"A1": root_by_position[0].id, "B1": root_by_position[1].id}
    # Same position 0 under two different parents is fine.
    assert {child.position for child in children} == {0}


async def test_create_many_rejects_a_duplicate_position_as_a_conflict(db_session):
    project = await _make_project(db_session)

    with pytest.raises(ConflictError):
        await project_topics_repo.create_many(
            db_session,
            [
                NewProjectTopic(project_id=project.id, title="A", position=0),
                NewProjectTopic(project_id=project.id, title="B", position=0),
            ],
        )


# --- get_by_id_for_project ----------------------------------------------------------


async def test_get_by_id_for_project_returns_the_topic_in_its_own_project(db_session):
    project = await _make_project(db_session)
    topic = await project_topics_repo.create(db_session, project_id=project.id, title="T")

    fetched = await project_topics_repo.get_by_id_for_project(db_session, topic.id, project.id)

    assert fetched == topic


async def test_get_by_id_for_project_hides_another_projects_topic(db_session):
    """Rule 9: a topic that exists but lives in a different project looks
    exactly like one that does not exist.
    """
    project_a = await _make_project(db_session)
    project_b = await _make_project(db_session)
    topic = await project_topics_repo.create(db_session, project_id=project_a.id, title="T")

    in_other_project = await project_topics_repo.get_by_id_for_project(
        db_session, topic.id, project_b.id
    )
    unknown_id = await project_topics_repo.get_by_id_for_project(
        db_session, uuid4(), project_a.id
    )

    assert in_other_project is None
    assert unknown_id is None


# --- list_all_by_project ------------------------------------------------------------


async def test_list_all_by_project_orders_shallowest_first_then_by_position(db_session):
    project = await _make_project(db_session)
    # Inserted deliberately out of order.
    root_b = await project_topics_repo.create(
        db_session, project_id=project.id, title="Root B", position=1
    )
    root_a = await project_topics_repo.create(
        db_session, project_id=project.id, title="Root A", position=0
    )
    child = await project_topics_repo.create(
        db_session,
        project_id=project.id,
        title="Child",
        parent_id=root_a.id,
        position=0,
        depth=1,
    )

    listed = await project_topics_repo.list_all_by_project(db_session, project.id)

    assert [topic.id for topic in listed] == [root_a.id, root_b.id, child.id]


async def test_list_all_by_project_is_unpaginated_and_scoped_to_the_project(db_session):
    project = await _make_project(db_session)
    other = await _make_project(db_session)
    for position in range(60):  # more than the default page size of 50
        await project_topics_repo.create(
            db_session, project_id=project.id, title=f"T{position}", position=position
        )
    await project_topics_repo.create(db_session, project_id=other.id, title="Not mine")

    listed = await project_topics_repo.list_all_by_project(db_session, project.id)

    assert len(listed) == 60
    assert all(topic.project_id == project.id for topic in listed)


async def test_list_all_by_project_is_empty_for_a_project_with_no_topics(db_session):
    project = await _make_project(db_session)

    assert await project_topics_repo.list_all_by_project(db_session, project.id) == []


# --- next_position ----------------------------------------------------------------


async def test_next_position_is_zero_when_there_are_no_topics(db_session):
    project = await _make_project(db_session)

    assert await project_topics_repo.next_position(
        db_session, project_id=project.id, parent_id=None
    ) == 0


async def test_next_position_follows_the_highest_existing_position(db_session):
    project = await _make_project(db_session)
    await project_topics_repo.create(db_session, project_id=project.id, title="A", position=0)
    # A gap (positions need not be contiguous, e.g. after a topic was removed).
    await project_topics_repo.create(db_session, project_id=project.id, title="B", position=4)

    assert await project_topics_repo.next_position(
        db_session, project_id=project.id, parent_id=None
    ) == 5


async def test_next_position_is_tracked_per_parent(db_session):
    project = await _make_project(db_session)
    root = await project_topics_repo.create(
        db_session, project_id=project.id, title="Root", position=0
    )
    await project_topics_repo.create(
        db_session, project_id=project.id, title="C0", parent_id=root.id, position=0, depth=1
    )
    await project_topics_repo.create(
        db_session, project_id=project.id, title="C1", parent_id=root.id, position=1, depth=1
    )

    assert await project_topics_repo.next_position(
        db_session, project_id=project.id, parent_id=root.id
    ) == 2
    # The roots' counter is untouched by the children.
    assert await project_topics_repo.next_position(
        db_session, project_id=project.id, parent_id=None
    ) == 1


async def test_next_position_ignores_other_projects(db_session):
    project = await _make_project(db_session)
    other = await _make_project(db_session)
    await project_topics_repo.create(db_session, project_id=other.id, title="X", position=7)

    assert await project_topics_repo.next_position(
        db_session, project_id=project.id, parent_id=None
    ) == 0


# --- update_for_project ---------------------------------------------------------------


async def test_update_for_project_changes_only_the_given_fields(db_session):
    project = await _make_project(db_session)
    topic = await project_topics_repo.create(
        db_session, project_id=project.id, title="Keep", summary="Old", keywords=["k"]
    )

    updated = await project_topics_repo.update_for_project(
        db_session, topic_id=topic.id, project_id=project.id, status="accepted"
    )

    assert updated is not None
    assert updated.status == "accepted"
    assert updated.title == "Keep"
    assert updated.summary == "Old"
    assert updated.keywords == ["k"]


async def test_update_for_project_can_clear_a_nullable_field(db_session):
    project = await _make_project(db_session)
    topic = await project_topics_repo.create(
        db_session, project_id=project.id, title="T", summary="Something"
    )

    updated = await project_topics_repo.update_for_project(
        db_session, topic_id=topic.id, project_id=project.id, summary=None
    )

    assert updated is not None
    assert updated.summary is None


async def test_update_for_project_does_not_touch_another_projects_topic(db_session):
    project_a = await _make_project(db_session)
    project_b = await _make_project(db_session)
    topic = await project_topics_repo.create(db_session, project_id=project_a.id, title="T")

    updated = await project_topics_repo.update_for_project(
        db_session, topic_id=topic.id, project_id=project_b.id, status="rejected"
    )

    assert updated is None
    assert (await project_topics_repo.get_by_id(db_session, topic.id)).status == "proposed"


async def test_update_for_project_with_no_fields_is_a_scoped_read(db_session):
    project = await _make_project(db_session)
    other = await _make_project(db_session)
    topic = await project_topics_repo.create(db_session, project_id=project.id, title="T")

    assert (
        await project_topics_repo.update_for_project(
            db_session, topic_id=topic.id, project_id=project.id
        )
        == topic
    )
    assert (
        await project_topics_repo.update_for_project(
            db_session, topic_id=topic.id, project_id=other.id
        )
        is None
    )


async def test_update_for_project_rejects_an_invalid_status(db_session):
    project = await _make_project(db_session)
    topic = await project_topics_repo.create(db_session, project_id=project.id, title="T")

    with pytest.raises(ConflictError):
        await project_topics_repo.update_for_project(
            db_session, topic_id=topic.id, project_id=project.id, status="published"
        )


# --- delete_derived_by_project ----------------------------------------------------------


async def test_delete_derived_by_project_keeps_user_topics_and_removes_the_rest(db_session):
    project = await _make_project(db_session)
    user = await project_topics_repo.create(
        db_session, project_id=project.id, title="Mine", position=0, origin="user"
    )
    source = await project_topics_repo.create(
        db_session, project_id=project.id, title="From PDF", position=1, origin="source"
    )
    suggested = await project_topics_repo.create(
        db_session, project_id=project.id, title="Guessed", position=2, origin="suggested"
    )

    deleted = await project_topics_repo.delete_derived_by_project(db_session, project.id)

    assert deleted == 2
    remaining = await project_topics_repo.list_all_by_project(db_session, project.id)
    assert [topic.id for topic in remaining] == [user.id]
    assert await project_topics_repo.get_by_id(db_session, source.id) is None
    assert await project_topics_repo.get_by_id(db_session, suggested.id) is None


async def test_delete_derived_by_project_cascades_to_descendants(db_session):
    project = await _make_project(db_session)
    parent = await project_topics_repo.create(
        db_session, project_id=project.id, title="Parent", origin="source"
    )
    child = await project_topics_repo.create(
        db_session,
        project_id=project.id,
        title="Child",
        parent_id=parent.id,
        depth=1,
        origin="source",
    )

    await project_topics_repo.delete_derived_by_project(db_session, project.id)

    assert await project_topics_repo.get_by_id(db_session, parent.id) is None
    assert await project_topics_repo.get_by_id(db_session, child.id) is None


async def test_delete_derived_by_project_leaves_other_projects_alone(db_session):
    project = await _make_project(db_session)
    other = await _make_project(db_session)
    theirs = await project_topics_repo.create(
        db_session, project_id=other.id, title="Theirs", origin="source"
    )

    assert await project_topics_repo.delete_derived_by_project(db_session, project.id) == 0
    assert await project_topics_repo.get_by_id(db_session, theirs.id) == theirs

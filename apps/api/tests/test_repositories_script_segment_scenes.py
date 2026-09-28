"""``script_segment_scenes`` (Phase 9, migration 0015) - persistence shape:
whole-set replacement (DELETE + INSERT in one SAVEPOINT, never a per-row
UPDATE), unique scene ordering, cascade and CHECK-constraint behaviour.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from edutoon.db.tables import script_segments as script_segments_table
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import script_segment_scenes as scenes_repo
from edutoon.repositories import scripts as scripts_repo
from edutoon.repositories import users as users_repo
from edutoon.repositories.script_segment_scenes import NewScene
from edutoon.repositories.scripts import NewScriptSegment

MODEL = "claude-sonnet-5"


async def _make_script_with_segments(db_session, *, segment_count: int = 2):
    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    project = await projects_repo.create(
        db_session, owner_id=owner.id, title="P", source_type="pdf"
    )
    script = await scripts_repo.create(db_session, project_id=project.id, llm_model=MODEL)
    segments = await scripts_repo.add_segments(
        db_session,
        script_id=script.id,
        segments=[
            NewScriptSegment(
                segment_index=i, narration_text=f"Segment {i}.", estimated_duration_ms=1000
            )
            for i in range(segment_count)
        ],
    )
    return project, script, segments


def _scene(index: int = 0, **overrides) -> NewScene:
    defaults: dict = {
        "scene_index": index,
        "start_ms": index * 1000,
        "duration_ms": 1000,
        "template": "title_card",
        "content": {"title": f"Scene {index}"},
        "source_audio_content_hash": "a" * 64,
    }
    return NewScene(**{**defaults, **overrides})


# --- replace_for_segment: whole-set replacement -----------------------------------------


async def test_replace_creates_the_given_scenes(db_session):
    _, _, [segment] = await _make_script_with_segments(db_session, segment_count=1)

    rows = await scenes_repo.replace_for_segment(
        db_session, script_segment_id=segment.id, scenes=[_scene(0), _scene(1, start_ms=1000)]
    )

    assert [r.scene_index for r in rows] == [0, 1]
    assert all(r.script_segment_id == segment.id for r in rows)


async def test_replace_removes_scenes_not_in_the_new_set(db_session):
    _, _, [segment] = await _make_script_with_segments(db_session, segment_count=1)
    await scenes_repo.replace_for_segment(
        db_session,
        script_segment_id=segment.id,
        scenes=[_scene(0), _scene(1, start_ms=1000), _scene(2, start_ms=2000)],
    )

    replaced = await scenes_repo.replace_for_segment(
        db_session, script_segment_id=segment.id, scenes=[_scene(0)]
    )

    assert [r.scene_index for r in replaced] == [0]
    current = await scenes_repo.list_by_segment_id(db_session, segment.id)
    assert [r.scene_index for r in current] == [0]


async def test_replace_with_an_empty_set_clears_the_segments_scenes(db_session):
    _, _, [segment] = await _make_script_with_segments(db_session, segment_count=1)
    await scenes_repo.replace_for_segment(
        db_session, script_segment_id=segment.id, scenes=[_scene(0)]
    )

    cleared = await scenes_repo.replace_for_segment(
        db_session, script_segment_id=segment.id, scenes=[]
    )

    assert cleared == []
    assert await scenes_repo.list_by_segment_id(db_session, segment.id) == []


async def test_replace_does_not_touch_another_segments_scenes(db_session):
    _, _, [first, second] = await _make_script_with_segments(db_session, segment_count=2)
    await scenes_repo.replace_for_segment(
        db_session, script_segment_id=first.id, scenes=[_scene(0)]
    )
    await scenes_repo.replace_for_segment(
        db_session, script_segment_id=second.id, scenes=[_scene(0), _scene(1, start_ms=1000)]
    )

    await scenes_repo.replace_for_segment(db_session, script_segment_id=first.id, scenes=[])

    assert await scenes_repo.list_by_segment_id(db_session, first.id) == []
    assert len(await scenes_repo.list_by_segment_id(db_session, second.id)) == 2


async def test_a_replacement_that_changes_scene_count_sums_exactly_to_the_new_total(db_session):
    _, _, [segment] = await _make_script_with_segments(db_session, segment_count=1)
    await scenes_repo.replace_for_segment(
        db_session, script_segment_id=segment.id, scenes=[_scene(0, duration_ms=5000)]
    )

    new_scenes = [
        _scene(0, duration_ms=2000),
        _scene(1, start_ms=2000, duration_ms=1500),
        _scene(2, start_ms=3500, duration_ms=1500),
    ]
    await scenes_repo.replace_for_segment(
        db_session, script_segment_id=segment.id, scenes=new_scenes
    )

    rows = await scenes_repo.list_by_segment_id(db_session, segment.id)
    assert sum(r.duration_ms for r in rows) == 5000


# --- reads: ordering, ownership scoping -------------------------------------------------


async def test_list_by_segment_id_is_empty_when_none_exist(db_session):
    assert await scenes_repo.list_by_segment_id(db_session, uuid4()) == []


async def test_list_by_segment_id_orders_by_scene_index(db_session):
    _, _, [segment] = await _make_script_with_segments(db_session, segment_count=1)
    await scenes_repo.replace_for_segment(
        db_session,
        script_segment_id=segment.id,
        scenes=[_scene(2, start_ms=2000), _scene(0), _scene(1, start_ms=1000)],
    )

    rows = await scenes_repo.list_by_segment_id(db_session, segment.id)

    assert [r.scene_index for r in rows] == [0, 1, 2]


async def test_list_by_script_orders_by_segment_then_scene(db_session):
    project, script, [first, second] = await _make_script_with_segments(db_session, segment_count=2)
    await scenes_repo.replace_for_segment(
        db_session, script_segment_id=second.id, scenes=[_scene(0)]
    )
    await scenes_repo.replace_for_segment(
        db_session, script_segment_id=first.id, scenes=[_scene(0), _scene(1, start_ms=1000)]
    )

    rows = await scenes_repo.list_by_script(db_session, project_id=project.id, script_id=script.id)

    assert [(r.script_segment_id, r.scene_index) for r in rows] == [
        (first.id, 0),
        (first.id, 1),
        (second.id, 0),
    ]


async def test_list_by_script_for_another_project_yields_nothing(db_session):
    project, script, [segment] = await _make_script_with_segments(db_session, segment_count=1)
    await scenes_repo.replace_for_segment(
        db_session, script_segment_id=segment.id, scenes=[_scene(0)]
    )
    other_owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    other_project = await projects_repo.create(
        db_session, owner_id=other_owner.id, title="Other", source_type="pdf"
    )

    rows = await scenes_repo.list_by_script(
        db_session, project_id=other_project.id, script_id=script.id
    )
    assert rows == []


# --- cascade + CHECK constraints (DB-level guarantees) -----------------------------------


async def test_deleting_the_segment_cascades_to_its_scenes(db_session):
    _, _, [segment] = await _make_script_with_segments(db_session, segment_count=1)
    await scenes_repo.replace_for_segment(
        db_session, script_segment_id=segment.id, scenes=[_scene(0)]
    )

    await db_session.execute(
        script_segments_table.delete().where(script_segments_table.c.id == segment.id)
    )

    assert await scenes_repo.list_by_segment_id(db_session, segment.id) == []


async def test_a_duplicate_scene_index_within_a_segment_is_rejected(db_session):
    _, _, [segment] = await _make_script_with_segments(db_session, segment_count=1)

    with pytest.raises(IntegrityError):
        await scenes_repo.replace_for_segment(
            db_session,
            script_segment_id=segment.id,
            scenes=[_scene(0), _scene(0, start_ms=1000)],
        )


async def test_a_non_positive_duration_is_rejected_by_the_database(db_session):
    _, _, [segment] = await _make_script_with_segments(db_session, segment_count=1)

    with pytest.raises(IntegrityError):
        await scenes_repo.replace_for_segment(
            db_session, script_segment_id=segment.id, scenes=[_scene(0, duration_ms=0)]
        )


async def test_a_negative_start_is_rejected_by_the_database(db_session):
    _, _, [segment] = await _make_script_with_segments(db_session, segment_count=1)

    with pytest.raises(IntegrityError):
        await scenes_repo.replace_for_segment(
            db_session, script_segment_id=segment.id, scenes=[_scene(0, start_ms=-1)]
        )


async def test_an_unknown_template_is_rejected_by_the_database(db_session):
    _, _, [segment] = await _make_script_with_segments(db_session, segment_count=1)

    with pytest.raises(IntegrityError):
        await scenes_repo.replace_for_segment(
            db_session, script_segment_id=segment.id, scenes=[_scene(0, template="section_recap")]
        )


async def test_a_replacement_is_atomic_a_failure_leaves_the_old_set_untouched(db_session):
    """A failing replacement (an invalid new scene) rolls back inside its own
    SAVEPOINT, including the DELETE that already ran - the segment's
    previous scene set is left exactly as it was, never partially cleared.
    """
    _, _, [segment] = await _make_script_with_segments(db_session, segment_count=1)
    await scenes_repo.replace_for_segment(
        db_session, script_segment_id=segment.id, scenes=[_scene(0), _scene(1, start_ms=1000)]
    )

    with pytest.raises(IntegrityError):
        await scenes_repo.replace_for_segment(
            db_session,
            script_segment_id=segment.id,
            scenes=[_scene(0, template="section_recap")],
        )

    rows = await scenes_repo.list_by_segment_id(db_session, segment.id)
    assert [r.scene_index for r in rows] == [0, 1]

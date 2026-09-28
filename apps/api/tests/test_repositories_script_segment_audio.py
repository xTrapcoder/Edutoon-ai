"""``script_segment_audio`` (Phase 8, migration 0014) - persistence shape:
one *current* row per segment (unlike the append-only ``script_segments`` it
belongs to), the CHECK that makes "ready" and "has playable audio" the same
fact at the database level, and cascade/ownership-scoping behaviour.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from edutoon.db.tables import script_segments as script_segments_table
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import script_segment_audio as audio_repo
from edutoon.repositories import scripts as scripts_repo
from edutoon.repositories import users as users_repo
from edutoon.repositories.scripts import NewScriptSegment

MODEL = "claude-sonnet-5"
BUCKET = "edutoon-segments"


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


def _ready_fields(**overrides):
    defaults = {
        "status": "ready",
        "voice_model": "en_GB-cori-medium",
        "is_temporary_voice_substitute": False,
        "content_hash": "a" * 64,
        "storage_bucket": BUCKET,
        "storage_key": "projects/x/scripts/y/segments/z/audio.wav",
        "format": "wav",
        "sample_rate": 22050,
        "channels": 1,
        "measured_duration_ms": 4000,
    }
    return {**defaults, **overrides}


# --- upsert: one current row per segment ---------------------------------------------------


async def test_upsert_creates_a_row_when_none_exists(db_session):
    _, _, [segment] = await _make_script_with_segments(db_session, segment_count=1)

    record = await audio_repo.upsert(db_session, script_segment_id=segment.id, **_ready_fields())

    assert record.script_segment_id == segment.id
    assert record.status == "ready"
    assert record.measured_duration_ms == 4000


async def test_upsert_replaces_the_existing_row_not_a_second_one(db_session):
    _, _, [segment] = await _make_script_with_segments(db_session, segment_count=1)

    first = await audio_repo.upsert(
        db_session,
        script_segment_id=segment.id,
        **_ready_fields(measured_duration_ms=1000, content_hash="a" * 64),
    )
    second = await audio_repo.upsert(
        db_session,
        script_segment_id=segment.id,
        **_ready_fields(measured_duration_ms=2000, content_hash="b" * 64),
    )

    assert first.id == second.id  # same row, updated - not a new one
    current = await audio_repo.get_by_segment_id(db_session, segment.id)
    assert current is not None
    assert current.measured_duration_ms == 2000
    assert current.content_hash == "b" * 64


async def test_a_failed_attempt_can_be_upserted_over_by_a_ready_one(db_session):
    _, _, [segment] = await _make_script_with_segments(db_session, segment_count=1)

    await audio_repo.upsert(
        db_session,
        script_segment_id=segment.id,
        status="failed",
        voice_model="en_GB-cori-medium",
        is_temporary_voice_substitute=False,
        content_hash="a" * 64,
        error="synthesis timed out",
    )
    ready = await audio_repo.upsert(
        db_session, script_segment_id=segment.id, **_ready_fields(content_hash="a" * 64)
    )

    assert ready.status == "ready"
    assert ready.error is None


# --- reads: existence, ownership scoping ----------------------------------------------------


async def test_get_by_segment_id_returns_none_when_absent(db_session):
    assert await audio_repo.get_by_segment_id(db_session, uuid4()) is None


async def test_get_by_segment_id_for_script_scopes_to_project_and_script(db_session):
    project, script, [segment] = await _make_script_with_segments(db_session, segment_count=1)
    await audio_repo.upsert(db_session, script_segment_id=segment.id, **_ready_fields())

    found = await audio_repo.get_by_segment_id_for_script(
        db_session, project_id=project.id, script_id=script.id, script_segment_id=segment.id
    )
    assert found is not None

    other_project, other_script, _ = await _make_script_with_segments(db_session)
    assert (
        await audio_repo.get_by_segment_id_for_script(
            db_session,
            project_id=other_project.id,
            script_id=script.id,
            script_segment_id=segment.id,
        )
        is None
    )
    assert (
        await audio_repo.get_by_segment_id_for_script(
            db_session,
            project_id=project.id,
            script_id=other_script.id,
            script_segment_id=segment.id,
        )
        is None
    )


async def test_list_by_script_returns_only_this_scripts_segments(db_session):
    project, script, segments = await _make_script_with_segments(db_session, segment_count=2)
    for segment in segments:
        await audio_repo.upsert(db_session, script_segment_id=segment.id, **_ready_fields())
    _, other_script, other_segments = await _make_script_with_segments(db_session)
    await audio_repo.upsert(db_session, script_segment_id=other_segments[0].id, **_ready_fields())

    rows = await audio_repo.list_by_script(db_session, project_id=project.id, script_id=script.id)

    assert {row.script_segment_id for row in rows} == {s.id for s in segments}


async def test_list_by_script_for_another_project_yields_nothing(db_session):
    project, script, segments = await _make_script_with_segments(db_session, segment_count=1)
    await audio_repo.upsert(db_session, script_segment_id=segments[0].id, **_ready_fields())
    other_owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    other_project = await projects_repo.create(
        db_session, owner_id=other_owner.id, title="Other", source_type="pdf"
    )

    rows = await audio_repo.list_by_script(
        db_session, project_id=other_project.id, script_id=script.id
    )
    assert rows == []


# --- storage-location listing (project deletion cleanup) -----------------------------------


async def test_list_storage_locations_only_includes_ready_rows(db_session):
    project, script, segments = await _make_script_with_segments(db_session, segment_count=2)
    await audio_repo.upsert(
        db_session,
        script_segment_id=segments[0].id,
        **_ready_fields(storage_key="projects/p/scripts/s/segments/0/audio.wav"),
    )
    await audio_repo.upsert(
        db_session,
        script_segment_id=segments[1].id,
        status="failed",
        voice_model="en_GB-cori-medium",
        is_temporary_voice_substitute=False,
        content_hash="c" * 64,
        error="boom",
    )

    locations = await audio_repo.list_storage_locations_for_project(db_session, project.id)

    assert len(locations) == 1
    assert locations[0].bucket == BUCKET
    assert locations[0].key == "projects/p/scripts/s/segments/0/audio.wav"


async def test_list_storage_locations_is_empty_for_a_project_with_no_audio(db_session):
    project = await projects_repo.create(
        db_session,
        owner_id=(await users_repo.create(db_session, email=f"{uuid4()}@example.com")).id,
        title="Empty",
        source_type="topic",
    )
    assert await audio_repo.list_storage_locations_for_project(db_session, project.id) == []


# --- cascade + CHECK constraints (DB-level guarantees) --------------------------------------


async def test_deleting_the_segment_cascades_to_its_audio(db_session):
    _, _, [segment] = await _make_script_with_segments(db_session, segment_count=1)
    await audio_repo.upsert(db_session, script_segment_id=segment.id, **_ready_fields())

    await db_session.execute(
        script_segments_table.delete().where(script_segments_table.c.id == segment.id)
    )

    assert await audio_repo.get_by_segment_id(db_session, segment.id) is None


async def test_ready_without_storage_fields_is_rejected_by_the_database(db_session):
    _, _, [segment] = await _make_script_with_segments(db_session, segment_count=1)

    with pytest.raises(IntegrityError):
        await audio_repo.upsert(
            db_session,
            script_segment_id=segment.id,
            status="ready",
            voice_model="en_GB-cori-medium",
            is_temporary_voice_substitute=False,
            content_hash="a" * 64,
            # storage_bucket/key/format/sample_rate/channels/measured_duration_ms
            # all omitted - "ready" must carry them all, or none at all.
        )


async def test_a_negative_measured_duration_is_rejected_by_the_database(db_session):
    _, _, [segment] = await _make_script_with_segments(db_session, segment_count=1)

    with pytest.raises(IntegrityError):
        await audio_repo.upsert(
            db_session, script_segment_id=segment.id, **_ready_fields(measured_duration_ms=-1)
        )


async def test_an_unknown_status_is_rejected_by_the_database(db_session):
    _, _, [segment] = await _make_script_with_segments(db_session, segment_count=1)

    with pytest.raises(IntegrityError):
        await audio_repo.upsert(
            db_session,
            script_segment_id=segment.id,
            status="pending",  # not a valid status - Phase 8's "no row means pending"
            voice_model="en_GB-cori-medium",
            is_temporary_voice_substitute=False,
            content_hash="a" * 64,
        )

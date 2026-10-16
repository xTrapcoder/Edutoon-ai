"""``services/topic_extraction.py`` - the Topic Extraction pipeline (Phase 5,
Step 4).

The LLM is a real ``LLM`` around a real ``AsyncAnthropic`` with only
``messages.create`` replaced (same pattern as ``test_services_scripts.py``) -
never a network call, but the whole pipeline (settings -> limits -> passage
selection -> provider -> domain rules -> persistence) runs for real against the
transactional database.

The one property every failure test is really about: **a failed extraction never
destroys the last successful derived topic set.** Each starts from a genuine
successful run, breaks the second one in a different place, and compares a full
snapshot of the topics and their evidence before and after.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from anthropic import AsyncAnthropic
from anthropic.types import Message, ToolUseBlock, Usage
from fpdf import FPDF

import edutoon.db.session as session_module
from edutoon.core.config import Settings, get_settings
from edutoon.core.errors import ConflictError, NotFoundError, ValidationError
from edutoon.db.database import dispose_engine, get_engine
from edutoon.db.session import get_sessionmaker
from edutoon.db.tables import users as users_table
from edutoon.domain.topic import TopicLimits
from edutoon.providers import errors as provider_errors
from edutoon.providers.llm import LLM
from edutoon.repositories import jobs as jobs_repo
from edutoon.repositories import project_topics as project_topics_repo
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import scripts as scripts_repo
from edutoon.repositories import source_chunks as source_chunks_repo
from edutoon.repositories import topic_citations as topic_citations_repo
from edutoon.repositories import uploaded_sources as uploaded_sources_repo
from edutoon.repositories import users as users_repo
from edutoon.repositories.scripts import NewScriptSegment
from edutoon.repositories.source_chunks import NewSourceChunk
from edutoon.services import audit_logs as audit_logs_service
from edutoon.services import job_errors
from edutoon.services import topic_extraction as service
from edutoon.services import uploaded_sources as uploaded_sources_service
from tests.leaky_errors import assert_clean, leaky_anthropic, leaky_db_error
from tests.storage_isolation import UPLOADS_BUCKET
from tests.usage_helpers import NO_USAGE, usage_of

# Stands in for an external provider (OpenAI / Anthropic): these tests prove how our code
# handles a response, never how a real provider behaves. See "Provider-mocked tests"
# in apps/api/README.md.
pytestmark = pytest.mark.provider_mocked

MODEL = "claude-sonnet-5"


# --- builders --------------------------------------------------------------------------


def _message(tool_input: dict[str, Any], *, input_tokens: int, output_tokens: int) -> Message:
    return Message(
        id="msg_1",
        content=[
            ToolUseBlock(type="tool_use", id="toolu_1", name="submit_topics", input=tool_input)
        ],
        model=MODEL,
        role="assistant",
        type="message",
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _llm_returning(
    monkeypatch: Any,
    tool_input: dict[str, Any],
    *,
    input_tokens: int = 100,
    output_tokens: int = 200,
) -> tuple[LLM, AsyncMock]:
    client = AsyncAnthropic(api_key="sk-ant-test-not-a-real-key")
    create = AsyncMock(
        return_value=_message(tool_input, input_tokens=input_tokens, output_tokens=output_tokens)
    )
    monkeypatch.setattr(client.messages, "create", create)
    return LLM(client, model=MODEL), create


def _fake_llm(monkeypatch: Any, topics: list[dict[str, Any]], **usage: int):
    return _llm_returning(monkeypatch, {"topics": topics}, **usage)


def _llm_raising(monkeypatch: Any, exc: Exception) -> LLM:
    client = AsyncAnthropic(api_key="sk-ant-test-not-a-real-key")
    monkeypatch.setattr(client.messages, "create", AsyncMock(side_effect=exc))
    return LLM(client, model=MODEL)


def _topic(
    ref: str,
    title: str,
    *,
    parent: str | None = None,
    summary: str = "",
    keywords: tuple[str, ...] = (),
    evidence: tuple[Any, ...] = (),
    seed: Any = None,
) -> dict[str, Any]:
    topic: dict[str, Any] = {
        "ref": ref,
        "title": title,
        "summary": summary,
        "keywords": list(keywords),
        "evidence_chunk_ids": [str(e) for e in evidence],
    }
    if parent is not None:
        topic["parent_ref"] = parent
    if seed is not None:
        topic["seed_ref"] = str(seed)
    return topic


async def _project(db_session, *, source_type: str = "pdf"):
    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    project = await projects_repo.create(
        db_session, owner_id=owner.id, title="Photosynthesis", source_type=source_type
    )
    return owner, project


async def _add_chunks(db_session, project, texts: list[str]):
    """One parsed source with one page-sized chunk per text; returns the chunk
    records in page order.
    """
    source = await uploaded_sources_repo.create(
        db_session,
        project_id=project.id,
        original_filename="notes.pdf",
        storage_bucket=UPLOADS_BUCKET,
        storage_key=f"{project.id}/{uuid4()}.pdf",
        byte_size=1024,
        checksum_sha256=uuid4().hex + uuid4().hex[:32],
    )
    chunks = await source_chunks_repo.create_many(
        db_session,
        [
            NewSourceChunk(
                source_id=source.id,
                project_id=project.id,
                chunk_index=i,
                content=text,
                page_from=i + 1,
                page_to=i + 1,
            )
            for i, text in enumerate(texts)
        ],
    )
    return sorted(chunks, key=lambda chunk: chunk.chunk_index)


async def _add_seed(
    db_session, project, title: str, *, position: int, status: str = "accepted", **fields: Any
):
    return await project_topics_repo.create(
        db_session,
        project_id=project.id,
        title=title,
        position=position,
        origin="user",
        status=status,
        **fields,
    )


async def _enqueue(db_session, owner, project, *, key: str | None = None, replace: bool = False):
    return await service.enqueue_topic_extraction(
        db_session,
        project_id=project.id,
        owner_id=owner.id,
        idempotency_key=key or str(uuid4()),
        replace=replace,
    )


async def _extract(db_session, llm, owner, project, *, replace: bool = False):
    """Enqueue and run in one go; returns the finished job."""
    enqueued = await _enqueue(db_session, owner, project, replace=replace)
    return await service.run_extract_topics_job(db_session, llm, job_id=enqueued.job.id)


async def _topics(db_session, project):
    return await project_topics_repo.list_all_by_project(db_session, project.id)


async def _snapshot(db_session, project) -> list[tuple[Any, ...]]:
    """Everything observable about a project's topics and their evidence."""
    topics = await _topics(db_session, project)
    evidence = await topic_citations_repo.list_evidence_for_topics(
        db_session, project_id=project.id, topic_ids=[t.id for t in topics]
    )
    return [
        (
            t.id,
            t.title,
            t.origin,
            t.status,
            t.parent_id,
            t.position,
            t.depth,
            t.summary,
            tuple(t.keywords),
            tuple(e.source_chunk_id for e in evidence.get(t.id, [])),
        )
        for t in topics
    ]


def _evidence_by_title(topics, evidence) -> dict[str, set[UUID]]:
    return {t.title: {e.source_chunk_id for e in evidence.get(t.id, [])} for t in topics}


def _use_settings(monkeypatch: Any, **overrides: Any) -> Settings:
    settings = get_settings().model_copy(update=overrides)
    monkeypatch.setattr(service, "get_settings", lambda: settings)
    return settings


def _user_message(create: AsyncMock) -> str:
    return str(create.await_args.kwargs["messages"][0]["content"])


async def _first_successful_run(db_session, monkeypatch):
    """A project with two user topics, three pages, and a genuinely successful
    pdf+keywords extraction behind it - the "last good topic set" the failure
    tests must never lose.
    """
    owner, project = await _project(db_session)
    chunks = await _add_chunks(db_session, project, ["Page one.", "Page two.", "Page three."])
    seed_a = await _add_seed(db_session, project, "Photosynthesis", position=0)
    seed_b = await _add_seed(db_session, project, "Respiration", position=1)
    llm, _ = _fake_llm(
        monkeypatch,
        [
            _topic(
                "t1",
                "Photosynthesis",
                summary="From p.1.",
                evidence=(chunks[0].id,),
                seed=seed_a.id,
            ),
            _topic("t2", "Light reactions", parent="t1", evidence=(chunks[1].id,)),
            _topic("t3", "Cell structure", evidence=(chunks[2].id,)),
        ],
    )
    job = await _extract(db_session, llm, owner, project)
    assert job.status == "succeeded"
    return owner, project, chunks, seed_a, seed_b


# --- PDF only ---------------------------------------------------------------------------


async def test_pdf_only_persists_source_topics_with_their_citations(db_session, monkeypatch):
    owner, project = await _project(db_session)
    c = await _add_chunks(
        db_session, project, ["Light is captured.", "Sugar is made.", "Cells respire."]
    )
    llm, _ = _fake_llm(
        monkeypatch,
        [
            _topic(
                "t1",
                "Photosynthesis",
                summary="How plants make food.",
                keywords=("light", "glucose"),
                evidence=(c[0].id, c[1].id),
            ),
            _topic("t2", "Light reactions", parent="t1", evidence=(c[0].id,)),
            _topic("t3", "Calvin cycle", parent="t1", evidence=(c[1].id,)),
            _topic("t4", "Respiration", evidence=(c[2].id,)),
        ],
        input_tokens=321,
        output_tokens=123,
    )

    job = await _extract(db_session, llm, owner, project)

    assert job.status == "succeeded"
    topics = await _topics(db_session, project)
    by_title = {t.title: t for t in topics}
    assert {t.origin for t in topics} == {"source"}
    assert {t.status for t in topics} == {"proposed"}
    root, light, calvin, respiration = (
        by_title[n] for n in ("Photosynthesis", "Light reactions", "Calvin cycle", "Respiration")
    )
    assert (root.parent_id, root.depth, root.position) == (None, 0, 0)
    assert (light.parent_id, light.depth, light.position) == (root.id, 1, 0)
    assert (calvin.parent_id, calvin.depth, calvin.position) == (root.id, 1, 1)
    assert (respiration.parent_id, respiration.depth, respiration.position) == (None, 0, 1)
    assert (root.summary, root.keywords) == ("How plants make food.", ["light", "glucose"])
    evidence = await topic_citations_repo.list_evidence_for_topics(
        db_session, project_id=project.id, topic_ids=[t.id for t in topics]
    )
    assert _evidence_by_title(topics, evidence) == {
        "Photosynthesis": {c[0].id, c[1].id},
        "Light reactions": {c[0].id},
        "Calvin cycle": {c[1].id},
        "Respiration": {c[2].id},
    }
    assert job.result == {
        "mode": "pdf",
        "topic_count": 4,
        "chunks_total": 3,
        "chunks_used": 3,
        "truncated": False,
        "dropped_uncited": 0,
        "dropped_over_limit": 0,
        "replaced": False,
        "input_tokens": 321,
        "output_tokens": 123,
        "usage": usage_of("topic_extraction", 321, 123),
        "llm_model": MODEL,
    }


async def test_pdf_only_never_creates_a_topic_the_material_does_not_support(
    db_session, monkeypatch
):
    """A topic citing nothing valid - or a real chunk the model was never shown
    because the budget left it out - is not persisted, and neither is the
    citation.
    """
    owner, project = await _project(db_session)
    # Six long pages, a budget that shows only the first and last of them.
    c = await _add_chunks(db_session, project, [f"page {i} " + "x" * 1000 for i in range(6)])
    _use_settings(monkeypatch, TOPIC_INPUT_BUDGET_CHARS=900)
    shown, unseen = c[0], c[3]
    llm, _ = _fake_llm(
        monkeypatch,
        [
            _topic("t1", "Backed by a shown page", evidence=(shown.id,)),
            _topic("t2", "Cites an invented id", evidence=(uuid4(),)),
            _topic("t3", "Cites a page the model never saw", evidence=(unseen.id,)),
        ],
    )

    job = await _extract(db_session, llm, owner, project)

    assert job.status == "succeeded"
    assert [t.title for t in await _topics(db_session, project)] == ["Backed by a shown page"]
    assert job.result["dropped_uncited"] == 2


async def test_the_input_budget_is_configurable_and_truncation_is_reported(db_session, monkeypatch):
    owner, project = await _project(db_session)
    chunks = await _add_chunks(db_session, project, ["y" * 2000 for _ in range(10)])
    _use_settings(monkeypatch, TOPIC_INPUT_BUDGET_CHARS=2000)
    llm, create = _fake_llm(monkeypatch, [_topic("t1", "A topic", evidence=(chunks[0].id,))])

    job = await _extract(db_session, llm, owner, project)

    assert job.status == "succeeded"
    assert job.result["chunks_total"] == 10
    assert job.result["chunks_used"] < 10
    assert job.result["truncated"] is True
    shown = sum(1 for c in chunks if f"[{c.id}]" in _user_message(create))
    assert shown == job.result["chunks_used"]  # what was reported is what was sent


async def test_a_document_within_the_budget_is_not_truncated(db_session, monkeypatch):
    owner, project = await _project(db_session)
    chunks = await _add_chunks(db_session, project, ["short page", "another short page"])
    llm, _ = _fake_llm(monkeypatch, [_topic("t1", "A topic", evidence=(chunks[0].id,))])

    job = await _extract(db_session, llm, owner, project)

    assert (job.result["chunks_total"], job.result["chunks_used"]) == (2, 2)
    assert job.result["truncated"] is False


# --- no usable input -------------------------------------------------------------------


async def test_a_project_with_no_material_and_no_topics_cannot_be_enqueued(db_session):
    owner, project = await _project(db_session)

    with pytest.raises(ValidationError, match="nothing to extract"):
        await _enqueue(db_session, owner, project)

    assert (await jobs_repo.list_by_project(db_session, project.id)).items == []
    logs = await audit_logs_service.list_audit_logs_for_project(db_session, project.id)
    assert logs.items == []


async def test_a_source_that_yielded_no_text_counts_as_no_material(db_session):
    """A scanned PDF parses to a source row but zero chunks: nothing to work
    from, and it must not be mistaken for material.
    """
    owner, project = await _project(db_session)
    await uploaded_sources_repo.create(
        db_session,
        project_id=project.id,
        original_filename="scan.pdf",
        storage_bucket=UPLOADS_BUCKET,
        storage_key=f"{project.id}/scan.pdf",
        byte_size=10,
        checksum_sha256=uuid4().hex + uuid4().hex[:32],
    )

    with pytest.raises(ValidationError):
        await _enqueue(db_session, owner, project)


async def test_a_job_that_finds_nothing_to_extract_at_run_time_fails_cleanly(
    db_session, monkeypatch
):
    owner, project = await _project(db_session)
    job = await jobs_repo.create(
        db_session,
        project_id=project.id,
        kind="extract_topics",
        payload={"project_id": str(project.id), "replace": False},
    )
    llm, create = _fake_llm(monkeypatch, [])

    finished = await service.run_extract_topics_job(db_session, llm, job_id=job.id)

    assert finished.status == "failed"
    assert "nothing to extract" in finished.error
    create.assert_not_awaited()  # never spend an API call on nothing


# --- keywords only ---------------------------------------------------------------------


async def test_keywords_only_keeps_the_users_topics_and_adds_unverified_suggestions(
    db_session, monkeypatch
):
    owner, project = await _project(db_session, source_type="topic")
    photosynthesis = await _add_seed(db_session, project, "Photosynthesis", position=0)
    respiration = await _add_seed(db_session, project, "Respiration", position=1)
    llm, _ = _fake_llm(
        monkeypatch,
        [
            # The model rewords the user's topic and cites something it made up.
            _topic(
                "t1",
                "Photosynthesis!!",
                summary="How plants make food.",
                keywords=("light", "glucose"),
                evidence=(uuid4(),),
                seed=photosynthesis.id,
            ),
            _topic("t2", "Light reactions", parent="t1", evidence=(uuid4(),)),
            _topic("t3", "Calvin cycle", parent="t1"),
        ],
    )

    job = await _extract(db_session, llm, owner, project)

    assert job.status == "succeeded"
    assert (job.result["mode"], job.result["chunks_total"], job.result["truncated"]) == (
        "keywords",
        0,
        False,
    )
    topics = {t.title: t for t in await _topics(db_session, project)}
    assert set(topics) == {"Photosynthesis", "Respiration", "Light reactions", "Calvin cycle"}
    # The user's topics: same rows, same wording, still theirs.
    assert topics["Photosynthesis"].id == photosynthesis.id
    assert topics["Respiration"].id == respiration.id
    assert {topics["Photosynthesis"].origin, topics["Respiration"].origin} == {"user"}
    assert topics["Photosynthesis"].status == "accepted"
    # ...enriched, never rewritten.
    assert topics["Photosynthesis"].summary == "How plants make food."
    assert topics["Photosynthesis"].keywords == ["light", "glucose"]
    # Suggestions: unverified, so no evidence anywhere.
    for title in ("Light reactions", "Calvin cycle"):
        assert topics[title].origin == "suggested"
        assert topics[title].parent_id == photosynthesis.id
        assert topics[title].depth == 1
    evidence = await topic_citations_repo.list_evidence_for_topics(
        db_session, project_id=project.id, topic_ids=[t.id for t in topics.values()]
    )
    assert evidence == {}


async def test_keywords_only_needs_no_pdf_chunks(db_session, monkeypatch):
    owner, project = await _project(db_session, source_type="topic")
    await _add_seed(db_session, project, "Photosynthesis", position=0)
    llm, create = _fake_llm(monkeypatch, [])

    job = await _extract(db_session, llm, owner, project)

    assert job.status == "succeeded"
    assert "Passages:" not in _user_message(create)
    assert job.result["topic_count"] == 1  # just the user's own topic, kept


async def test_a_users_own_summary_and_keywords_are_never_overwritten(db_session, monkeypatch):
    owner, project = await _project(db_session, source_type="topic")
    seed = await _add_seed(
        db_session,
        project,
        "Photosynthesis",
        position=0,
        summary="My own note.",
        keywords=["mine"],
    )
    llm, _ = _fake_llm(
        monkeypatch,
        [_topic("t1", "Photosynthesis", summary="Model text.", keywords=("model",), seed=seed.id)],
    )

    await _extract(db_session, llm, owner, project)

    [kept] = await _topics(db_session, project)
    assert (kept.summary, kept.keywords) == ("My own note.", ["mine"])


async def test_a_rejected_user_topic_is_neither_offered_nor_expanded(db_session, monkeypatch):
    owner, project = await _project(db_session, source_type="topic")
    kept = await _add_seed(db_session, project, "Photosynthesis", position=0)
    rejected = await _add_seed(
        db_session, project, "Dropped by the user", position=1, status="rejected"
    )
    llm, create = _fake_llm(monkeypatch, [_topic("t1", "New idea")])

    job = await _extract(db_session, llm, owner, project)

    message = _user_message(create)
    assert str(kept.id) in message and str(rejected.id) not in message
    topics = {t.title: t for t in await _topics(db_session, project)}
    assert topics["Dropped by the user"].status == "rejected"  # untouched
    assert topics["New idea"].position > rejected.position  # placed clear of its position
    assert job.status == "succeeded"


# --- PDF + keywords ---------------------------------------------------------------------


async def test_pdf_plus_keywords_preserves_every_seed_and_nests_subtopics_under_them(
    db_session, monkeypatch
):
    owner, project = await _project(db_session)
    c = await _add_chunks(db_session, project, ["About light.", "About glucose.", "About cells."])
    photosynthesis = await _add_seed(db_session, project, "Photosynthesis", position=0)
    respiration = await _add_seed(db_session, project, "Respiration", position=1)
    llm, _ = _fake_llm(
        monkeypatch,
        [
            _topic(
                "t1",
                "photosynthesis (the model's wording)",
                summary="From page 1.",
                keywords=("chlorophyll",),
                evidence=(c[0].id,),
                seed=photosynthesis.id,
            ),
            _topic("t2", "Light reactions", parent="t1", evidence=(c[1].id,)),
            _topic("t3", "Cell structure", evidence=(c[2].id,)),
            _topic("t4", "Uncited extra"),
            # Respiration is not covered by the pages and is not mentioned.
        ],
    )

    job = await _extract(db_session, llm, owner, project)

    assert job.status == "succeeded"
    assert (job.result["mode"], job.result["topic_count"], job.result["dropped_uncited"]) == (
        "pdf_keywords",
        4,
        1,
    )
    topics = await _topics(db_session, project)
    by_title = {t.title: t for t in topics}
    assert sorted(by_title) == [
        "Cell structure",
        "Light reactions",
        "Photosynthesis",
        "Respiration",
    ]
    # Each user topic exactly once, the same row, in the user's wording.
    assert [t.id for t in topics if t.origin == "user"] == [photosynthesis.id, respiration.id]
    # The covered one is nested under; the uncovered one stays visible, unbacked.
    assert by_title["Light reactions"].parent_id == photosynthesis.id
    assert (by_title["Light reactions"].origin, by_title["Cell structure"].origin) == (
        "source",
        "source",
    )
    assert by_title["Cell structure"].position == 2  # after both user topics
    evidence = await topic_citations_repo.list_evidence_for_topics(
        db_session, project_id=project.id, topic_ids=[t.id for t in topics]
    )
    assert _evidence_by_title(topics, evidence) == {
        "Photosynthesis": {c[0].id},
        "Light reactions": {c[1].id},
        "Cell structure": {c[2].id},
        "Respiration": set(),
    }


# --- replacement rules -------------------------------------------------------------------


async def test_running_again_without_replace_is_refused_and_nothing_changes(
    db_session, monkeypatch
):
    owner, project, *_ = await _first_successful_run(db_session, monkeypatch)
    before = await _snapshot(db_session, project)

    with pytest.raises(ConflictError, match="already been extracted"):
        await _enqueue(db_session, owner, project, replace=False)

    assert await _snapshot(db_session, project) == before
    assert len((await jobs_repo.list_by_project(db_session, project.id)).items) == 1


async def test_replace_swaps_the_derived_topics_and_keeps_the_users_own(db_session, monkeypatch):
    owner, project, chunks, seed_a, seed_b = await _first_successful_run(db_session, monkeypatch)
    old_derived = [t for t in await _topics(db_session, project) if t.origin != "user"]
    assert {t.title for t in old_derived} == {"Light reactions", "Cell structure"}
    llm, _ = _fake_llm(
        monkeypatch,
        [
            _topic("t1", "Photosynthesis", evidence=(chunks[2].id,), seed=seed_a.id),
            _topic("t2", "Dark reactions", parent="t1", evidence=(chunks[1].id,)),
        ],
    )

    job = await _extract(db_session, llm, owner, project, replace=True)

    assert job.status == "succeeded"
    assert job.result["replaced"] is True
    topics = await _topics(db_session, project)
    by_title = {t.title: t for t in topics}
    # The previous derived set is gone...
    for gone in old_derived:
        assert await project_topics_repo.get_by_id(db_session, gone.id) is None
    assert set(by_title) == {"Photosynthesis", "Respiration", "Dark reactions"}
    # ...the user's topics are the very same rows...
    assert by_title["Photosynthesis"].id == seed_a.id and by_title["Respiration"].id == seed_b.id
    assert by_title["Photosynthesis"].summary == "From p.1."  # not reset by the replacement
    # ...and the evidence reflects only the latest run.
    evidence = await topic_citations_repo.list_evidence_for_topics(
        db_session, project_id=project.id, topic_ids=[t.id for t in topics]
    )
    assert _evidence_by_title(topics, evidence) == {
        "Photosynthesis": {chunks[2].id},
        "Dark reactions": {chunks[1].id},
        "Respiration": set(),
    }


async def _add_script_referencing(db_session, project, topic):
    return await scripts_repo.create(
        db_session,
        project_id=project.id,
        llm_model=MODEL,
        segments=[NewScriptSegment(0, "Narration.", 1000, topic_id=topic.id)],
    )


@pytest.mark.parametrize("which", ["a derived topic", "a user topic"])
async def test_replace_is_refused_while_a_script_references_a_topic(db_session, monkeypatch, which):
    owner, project, _chunks, seed_a, _seed_b = await _first_successful_run(db_session, monkeypatch)
    topics = await _topics(db_session, project)
    referenced = (
        seed_a if which == "a user topic" else next(t for t in topics if t.origin != "user")
    )
    await _add_script_referencing(db_session, project, referenced)
    before = await _snapshot(db_session, project)

    with pytest.raises(ConflictError, match="script"):
        await _enqueue(db_session, owner, project, replace=True)

    assert await _snapshot(db_session, project) == before
    assert len((await jobs_repo.list_by_project(db_session, project.id)).items) == 1  # no new job


async def test_a_script_created_after_enqueue_still_blocks_replacement_at_run_time(
    db_session, monkeypatch
):
    owner, project, chunks, seed_a, _ = await _first_successful_run(db_session, monkeypatch)
    enqueued = await _enqueue(db_session, owner, project, replace=True)
    await _add_script_referencing(db_session, project, seed_a)  # arrives while the job waits
    before = await _snapshot(db_session, project)
    llm, create = _fake_llm(monkeypatch, [_topic("t1", "Replacement", evidence=(chunks[0].id,))])

    job = await service.run_extract_topics_job(db_session, llm, job_id=enqueued.job.id)

    assert job.status == "failed"
    assert "script" in job.error
    assert await _snapshot(db_session, project) == before
    create.assert_not_awaited()


async def test_a_script_created_during_the_model_call_still_blocks_replacement(
    db_session, monkeypatch
):
    """The model call can take minutes, so a script may be made from the topics
    after every check made before it. The write step looks again, right before it
    deletes anything, and the previous topics survive untouched.
    """
    owner, project, chunks, _seed_a, _ = await _first_successful_run(db_session, monkeypatch)
    derived = next(t for t in await _topics(db_session, project) if t.origin != "user")
    enqueued = await _enqueue(db_session, owner, project, replace=True)
    before = await _snapshot(db_session, project)
    llm, create = _fake_llm(monkeypatch, [_topic("t1", "Replacement", evidence=(chunks[0].id,))])
    answer = create.return_value

    async def _a_script_appears_then_the_model_answers(*args: Any, **kwargs: Any) -> Message:
        await _add_script_referencing(db_session, project, derived)
        return answer  # type: ignore[no-any-return]

    create.side_effect = _a_script_appears_then_the_model_answers

    job = await service.run_extract_topics_job(db_session, llm, job_id=enqueued.job.id)

    create.assert_awaited_once()  # the earlier checks passed; only the last one caught it
    assert job.status == "failed"
    assert "script" in job.error
    assert await _snapshot(db_session, project) == before
    assert await project_topics_repo.get_by_id(db_session, derived.id) is not None


async def test_another_projects_script_does_not_block_replacement(db_session, monkeypatch):
    owner, project, chunks, seed_a, _ = await _first_successful_run(db_session, monkeypatch)
    _other_owner, other_project = await _project(db_session)
    other_topic = await _add_seed(db_session, other_project, "Elsewhere", position=0)
    await _add_script_referencing(db_session, other_project, other_topic)
    llm, _ = _fake_llm(
        monkeypatch,
        [
            _topic("t1", "Photosynthesis", evidence=(chunks[0].id,), seed=seed_a.id),
            _topic("t2", "Replacement child", parent="t1", evidence=(chunks[1].id,)),
        ],
    )

    job = await _extract(db_session, llm, owner, project, replace=True)

    assert job.status == "succeeded", job.error


async def test_derived_topics_that_appear_after_enqueue_fail_a_non_replacing_run_untouched(
    db_session, monkeypatch
):
    owner, project = await _project(db_session)
    chunks = await _add_chunks(db_session, project, ["Page."])
    enqueued = await _enqueue(db_session, owner, project, replace=False)
    late = await project_topics_repo.create(
        db_session, project_id=project.id, title="Appeared meanwhile", origin="source"
    )
    llm, create = _fake_llm(monkeypatch, [_topic("t1", "New", evidence=(chunks[0].id,))])

    job = await service.run_extract_topics_job(db_session, llm, job_id=enqueued.job.id)

    assert job.status == "failed"
    assert [t.id for t in await _topics(db_session, project)] == [late.id]
    create.assert_not_awaited()


# --- failure safety: the last good topic set is never lost --------------------------------


async def _failed_replace_preserves_everything(db_session, monkeypatch, make_llm):
    """Run a genuine successful extraction, then a ``replace`` run built by
    ``make_llm(context)`` - which is expected to fail somewhere. Returns the
    second job and a full snapshot of the topics before and after it.
    """
    context = await _first_successful_run(db_session, monkeypatch)
    owner, project = context[0], context[1]
    before = await _snapshot(db_session, project)
    assert any(row[2] != "user" for row in before)  # there really is a derived set to protect

    job = await _extract(db_session, make_llm(context), owner, project, replace=True)

    return job, before, await _snapshot(db_session, project)


async def test_a_failing_model_call_preserves_the_previous_topics(db_session, monkeypatch):
    job, before, after = await _failed_replace_preserves_everything(
        db_session,
        monkeypatch,
        lambda ctx: _llm_raising(monkeypatch, RuntimeError("the API is down")),
    )

    assert job.status == "failed" and job.error == job_errors.GENERIC_ERROR
    assert "API is down" not in job.error
    assert after == before


async def test_provider_validation_failure_preserves_the_previous_topics(db_session, monkeypatch):
    """The model answered, but not with an outline (LLMResponseError)."""

    def malformed(_ctx):
        llm, _ = _llm_returning(monkeypatch, {"topics": "definitely not a list"})
        return llm

    job, before, after = await _failed_replace_preserves_everything(
        db_session, monkeypatch, malformed
    )

    assert job.status == "failed" and "topics" in job.error
    assert after == before


@pytest.mark.parametrize(
    ("what", "topics", "expected_error"),
    [
        (
            "a cycle",
            [
                _topic("a", "A", parent="b", evidence=("{c0}",)),
                _topic("b", "B", parent="a", evidence=("{c0}",)),
            ],
            "cycle",
        ),
        ("a blank title", [_topic("a", "   ", evidence=("{c0}",))], "blank"),
        ("nothing cited", [_topic("a", "Uncited")], "previous topics were kept"),
    ],
)
async def test_domain_validation_failure_preserves_the_previous_topics(
    db_session, monkeypatch, what, topics, expected_error
):
    def rejected_by_the_rules(ctx):
        chunks = ctx[2]
        filled = [
            {
                **t,
                "evidence_chunk_ids": [
                    str(chunks[0].id) if e == "{c0}" else e for e in t["evidence_chunk_ids"]
                ],
            }
            for t in topics
        ]
        llm, _ = _fake_llm(monkeypatch, filled)
        return llm

    job, before, after = await _failed_replace_preserves_everything(
        db_session, monkeypatch, rejected_by_the_rules
    )

    assert job.status == "failed", what
    assert expected_error in job.error
    assert after == before


async def test_replacing_with_an_empty_outline_is_refused_rather_than_wiping_the_old_set(
    db_session, monkeypatch
):
    """A seeds-only outline is valid in itself, but if the model returned
    nothing useful, replacing a real derived set with it would be data loss.
    """

    def returns_nothing(_ctx):
        llm, _ = _fake_llm(monkeypatch, [])
        return llm

    job, before, after = await _failed_replace_preserves_everything(
        db_session, monkeypatch, returns_nothing
    )

    assert job.status == "failed" and "previous topics were kept" in job.error
    assert after == before


async def test_a_first_extraction_may_legitimately_add_nothing_to_the_users_topics(
    db_session, monkeypatch
):
    """The guard above only protects an existing derived set: with none to lose,
    "just the user's own topics" is a fine result.
    """
    owner, project = await _project(db_session, source_type="topic")
    await _add_seed(db_session, project, "Photosynthesis", position=0)
    llm, _ = _fake_llm(monkeypatch, [])

    job = await _extract(db_session, llm, owner, project, replace=True)

    assert job.status == "succeeded" and job.result["topic_count"] == 1


@pytest.mark.parametrize("failing_step", ["create_many", "add_many"])
async def test_a_persistence_failure_rolls_back_everything_including_the_deletion(
    db_session, monkeypatch, failing_step
):
    """The writes are the dangerous part: the old derived topics are deleted
    first. Fail the insert (after the delete) or the citations (after the insert)
    and the whole savepoint must unwind - old set intact, no half-written new one.
    """

    def fresh_valid_outline(ctx):
        chunks, seed_a = ctx[2], ctx[3]
        llm, _ = _fake_llm(
            monkeypatch,
            [
                _topic("t1", "Photosynthesis", evidence=(chunks[0].id,), seed=seed_a.id),
                _topic("t2", "A brand new topic", parent="t1", evidence=(chunks[1].id,)),
            ],
        )
        target = project_topics_repo if failing_step == "create_many" else topic_citations_repo

        async def boom(*args: Any, **kwargs: Any):
            raise RuntimeError(f"{failing_step}: disk full")

        monkeypatch.setattr(target, failing_step, boom)
        return llm

    job, before, after = await _failed_replace_preserves_everything(
        db_session, monkeypatch, fresh_valid_outline
    )

    assert job.status == "failed"
    assert job.error == job_errors.GENERIC_ERROR
    assert "disk full" not in job.error and failing_step not in job.error
    assert after == before
    assert "A brand new topic" not in {row[1] for row in after}


async def test_a_failed_first_extraction_leaves_the_users_topics_untouched(db_session, monkeypatch):
    owner, project = await _project(db_session, source_type="topic")
    seed = await _add_seed(db_session, project, "Photosynthesis", position=0)
    before = await _snapshot(db_session, project)

    job = await _extract(
        db_session, _llm_raising(monkeypatch, RuntimeError("boom")), owner, project
    )

    assert job.status == "failed"
    assert await _snapshot(db_session, project) == before
    assert (await project_topics_repo.get_by_id(db_session, seed.id)) is not None


# --- the job lifecycle ---------------------------------------------------------------------


async def test_a_successful_job_records_its_lifecycle(db_session, monkeypatch):
    owner, project = await _project(db_session)
    chunks = await _add_chunks(db_session, project, ["Page."])
    llm, _ = _fake_llm(monkeypatch, [_topic("t1", "T", evidence=(chunks[0].id,))])
    enqueued = await _enqueue(db_session, owner, project)
    assert enqueued.job.status == "queued"

    job = await service.run_extract_topics_job(db_session, llm, job_id=enqueued.job.id)

    assert (job.status, job.attempts, job.error) == ("succeeded", 1, None)
    assert job.started_at is not None and job.finished_at is not None


async def test_a_failed_job_records_the_error_and_never_raises(db_session, monkeypatch):
    owner, project = await _project(db_session)
    await _add_chunks(db_session, project, ["Page."])
    enqueued = await _enqueue(db_session, owner, project)

    job = await service.run_extract_topics_job(
        db_session,
        _llm_raising(monkeypatch, RuntimeError("upstream exploded")),
        job_id=enqueued.job.id,
    )

    assert (job.status, job.attempts, job.error) == ("failed", 1, job_errors.GENERIC_ERROR)
    assert "exploded" not in job.error
    # The call never got an answer, so nothing was spent - and the job says so.
    assert job.result == {"usage": NO_USAGE}
    assert job.started_at is not None and job.finished_at is not None


async def test_running_a_job_that_does_not_exist_is_the_only_error_that_raises(
    db_session, monkeypatch
):
    llm, _ = _fake_llm(monkeypatch, [])

    with pytest.raises(NotFoundError):
        await service.run_extract_topics_job(db_session, llm, job_id=uuid4())


@pytest.mark.parametrize(
    "payload",
    [{}, {"replace": False}, {"project_id": "not-a-uuid"}],
    ids=["empty", "no project_id", "project_id not a uuid"],
)
async def test_a_corrupt_payload_fails_the_job_instead_of_crashing(
    db_session, monkeypatch, payload
):
    _owner, project = await _project(db_session)
    job = await jobs_repo.create(
        db_session, project_id=project.id, kind="extract_topics", payload=payload
    )
    llm, create = _fake_llm(monkeypatch, [])

    finished = await service.run_extract_topics_job(db_session, llm, job_id=job.id)

    assert finished.status == "failed"
    assert "project_id" in finished.error
    create.assert_not_awaited()


# --- enqueue: ownership, idempotency, audit -------------------------------------------------


async def test_enqueue_creates_a_queued_job_with_the_expected_payload(db_session):
    owner, project = await _project(db_session)
    await _add_chunks(db_session, project, ["Page."])

    result = await service.enqueue_topic_extraction(
        db_session, project_id=project.id, owner_id=owner.id, idempotency_key="key-1", replace=False
    )

    assert result.replayed is False
    job = result.job
    assert (job.kind, job.status, job.idempotency_key) == ("extract_topics", "queued", "key-1")
    assert job.payload == {"project_id": str(project.id), "replace": False}


async def test_enqueue_is_idempotent_on_the_key(db_session):
    owner, project = await _project(db_session)
    await _add_chunks(db_session, project, ["Page."])

    first = await _enqueue(db_session, owner, project, key="same")
    second = await _enqueue(db_session, owner, project, key="same")

    assert (first.replayed, second.replayed) == (False, True)
    assert second.job.id == first.job.id
    assert len((await jobs_repo.list_by_project(db_session, project.id)).items) == 1
    logs = await audit_logs_service.list_audit_logs_for_entity(
        db_session, entity_type="topic_extraction", entity_id=first.job.id
    )
    assert len(logs.items) == 1  # a replay is not audited twice


async def test_a_replay_returns_the_original_job_even_after_it_finished_or_failed(
    db_session, monkeypatch
):
    owner, project = await _project(db_session)
    await _add_chunks(db_session, project, ["Page."])
    first = await _enqueue(db_session, owner, project, key="k")
    failed = await service.run_extract_topics_job(
        db_session, _llm_raising(monkeypatch, RuntimeError("no")), job_id=first.job.id
    )

    replay = await _enqueue(db_session, owner, project, key="k", replace=True)

    assert replay.replayed is True
    assert (replay.job.id, replay.job.status) == (failed.id, "failed")


async def test_the_same_key_is_independent_across_projects(db_session):
    owner, project_a = await _project(db_session)
    project_b = await projects_repo.create(
        db_session, owner_id=owner.id, title="B", source_type="pdf"
    )
    await _add_chunks(db_session, project_a, ["A."])
    await _add_chunks(db_session, project_b, ["B."])

    a = await _enqueue(db_session, owner, project_a, key="shared")
    b = await _enqueue(db_session, owner, project_b, key="shared")

    assert b.replayed is False and b.job.id != a.job.id


async def test_enqueue_writes_an_audit_log(db_session):
    owner, project = await _project(db_session)
    await _add_chunks(db_session, project, ["Page."])
    await _add_seed(db_session, project, "Photosynthesis", position=0)

    result = await _enqueue(db_session, owner, project, replace=False)

    logs = await audit_logs_service.list_audit_logs_for_entity(
        db_session, entity_type="topic_extraction", entity_id=result.job.id
    )
    [log] = logs.items
    assert log.action == "topics.extraction_enqueued"
    assert (log.project_id, log.actor_id) == (project.id, owner.id)
    assert log.context == {"job_id": str(result.job.id), "replace": False, "mode": "pdf_keywords"}


async def test_a_refused_enqueue_writes_no_audit_log(db_session, monkeypatch):
    owner, project, *_ = await _first_successful_run(db_session, monkeypatch)
    before = await audit_logs_service.list_audit_logs_for_project(db_session, project.id)

    with pytest.raises(ConflictError):
        await _enqueue(db_session, owner, project, replace=False)

    after = await audit_logs_service.list_audit_logs_for_project(db_session, project.id)
    assert len(after.items) == len(before.items)


async def test_another_users_project_is_a_404_and_creates_nothing(db_session):
    _owner, project = await _project(db_session)
    await _add_chunks(db_session, project, ["Page."])
    intruder = await users_repo.create(db_session, email=f"{uuid4()}@example.com")

    with pytest.raises(NotFoundError):
        await service.enqueue_topic_extraction(
            db_session, project_id=project.id, owner_id=intruder.id, idempotency_key="k"
        )

    assert (await jobs_repo.list_by_project(db_session, project.id)).items == []
    assert (
        await audit_logs_service.list_audit_logs_for_project(db_session, project.id)
    ).items == []


async def test_a_nonexistent_project_is_a_404(db_session):
    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")

    with pytest.raises(NotFoundError):
        await service.enqueue_topic_extraction(
            db_session, project_id=uuid4(), owner_id=owner.id, idempotency_key="k"
        )


# --- at most one active extraction per project -----------------------------------------------


async def test_a_second_extraction_is_refused_while_one_is_queued(db_session):
    owner, project = await _project(db_session)
    await _add_chunks(db_session, project, ["Page."])
    first = await _enqueue(db_session, owner, project, key="one")

    with pytest.raises(ConflictError, match="already queued or running"):
        await _enqueue(db_session, owner, project, key="two")

    assert [j.id for j in (await jobs_repo.list_by_project(db_session, project.id)).items] == [
        first.job.id
    ]


async def test_a_second_extraction_is_refused_while_one_is_running(db_session):
    owner, project = await _project(db_session)
    await _add_chunks(db_session, project, ["Page."])
    first = await _enqueue(db_session, owner, project, key="one")
    await jobs_repo.update(db_session, job_id=first.job.id, status="running")

    with pytest.raises(ConflictError, match="already queued or running"):
        await _enqueue(db_session, owner, project, key="two")


@pytest.mark.parametrize("terminal", ["succeeded", "failed", "cancelled"])
async def test_a_finished_extraction_does_not_block_the_next(db_session, terminal):
    owner, project = await _project(db_session)
    await _add_chunks(db_session, project, ["Page."])
    old = await _enqueue(db_session, owner, project, key="one")
    await jobs_repo.update(
        db_session,
        job_id=old.job.id,
        status=terminal,
        started_at=None if terminal == "cancelled" else old.job.created_at,
        finished_at=old.job.created_at if terminal != "cancelled" else None,
    )

    again = await _enqueue(db_session, owner, project, key="two")

    assert again.replayed is False


async def test_an_active_extraction_in_another_project_does_not_block(db_session):
    owner, project_a = await _project(db_session)
    project_b = await projects_repo.create(
        db_session, owner_id=owner.id, title="B", source_type="pdf"
    )
    await _add_chunks(db_session, project_a, ["A."])
    await _add_chunks(db_session, project_b, ["B."])
    await _enqueue(db_session, owner, project_a, key="a")

    other = await _enqueue(db_session, owner, project_b, key="b")

    assert other.replayed is False


async def test_only_extract_topics_jobs_count_towards_the_guard(db_session):
    owner, project = await _project(db_session)
    await _add_chunks(db_session, project, ["Page."])
    await jobs_repo.create(db_session, project_id=project.id, kind="build_evidence")

    result = await _enqueue(db_session, owner, project)

    assert result.replayed is False


async def test_two_simultaneous_enqueues_cannot_both_pass_the_active_check(monkeypatch):
    """The race the advisory lock exists for. Two independent connections: the
    first has enqueued but not yet committed; the second must *wait* for it (not
    sail past a check that sees nothing) and then be refused. Real engine, real
    commits, manual cleanup - same approach as the worker's SKIP LOCKED test.
    """
    # The audit table is append-only and cannot be cleaned up, so keep it out of
    # a test that commits for real; the guard under test does not depend on it.
    monkeypatch.setattr(service.audit_logs_service, "record_audit_log", AsyncMock())
    engine = get_engine()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as setup:
        owner = await users_repo.create(setup, email=f"{uuid4()}@example.com")
        project = await projects_repo.create(
            setup, owner_id=owner.id, title="Race", source_type="topic"
        )
        await _add_seed(setup, project, "Photosynthesis", position=0)
        await setup.commit()

    try:
        async with sessionmaker() as first, sessionmaker() as second:
            winner = await service.enqueue_topic_extraction(
                first, project_id=project.id, owner_id=owner.id, idempotency_key="first"
            )  # holds the project's lock, still uncommitted
            loser = asyncio.create_task(
                service.enqueue_topic_extraction(
                    second, project_id=project.id, owner_id=owner.id, idempotency_key="second"
                )
            )
            await asyncio.sleep(0.6)
            assert not loser.done(), "the second enqueue did not wait for the first"

            await first.commit()

            with pytest.raises(ConflictError, match="already queued or running"):
                await asyncio.wait_for(loser, timeout=10)
            await second.rollback()

        async with sessionmaker() as check:
            jobs = (await jobs_repo.list_by_project(check, project.id)).items
        assert [j.id for j in jobs] == [winner.job.id]
    finally:
        async with engine.begin() as cleanup:
            await cleanup.execute(users_table.delete().where(users_table.c.id == owner.id))
        await dispose_engine()
        session_module._sessionmaker = None


# --- limits & settings --------------------------------------------------------------------


def test_topic_limits_are_built_from_settings_field_for_field():
    settings = get_settings().model_copy(
        update={
            "MAX_TOPICS_PER_EXTRACTION": 7,
            "MAX_TOPIC_DEPTH": 2,
            "MAX_TOPIC_TITLE_CHARS": 33,
            "MAX_TOPIC_SUMMARY_CHARS": 77,
            "MAX_TOPIC_KEYWORDS": 3,
            "MAX_TOPIC_KEYWORD_CHARS": 11,
            "MAX_TOPIC_EVIDENCE": 2,
        }
    )

    assert service.topic_limits_from_settings(settings) == TopicLimits(7, 2, 33, 77, 3, 11, 2)


async def test_the_limits_from_settings_reach_the_provider_explicitly(db_session, monkeypatch):
    owner, project = await _project(db_session)
    chunks = await _add_chunks(db_session, project, ["Page."])
    _use_settings(monkeypatch, MAX_TOPICS_PER_EXTRACTION=7, MAX_TOPIC_TITLE_CHARS=33)
    llm, create = _fake_llm(monkeypatch, [_topic("t1", "T", evidence=(chunks[0].id,))])

    await _extract(db_session, llm, owner, project)

    schema = create.await_args.kwargs["tools"][0]["input_schema"]["properties"]["topics"]
    assert schema["maxItems"] == 7
    assert schema["items"]["properties"]["title"]["maxLength"] == 33
    assert "at most 7 topics" in _user_message(create)


async def test_a_run_that_exceeds_the_configured_topic_limit_fails_and_preserves_the_old_set(
    db_session, monkeypatch
):
    owner, project, chunks, *_ = await _first_successful_run(db_session, monkeypatch)
    before = await _snapshot(db_session, project)
    _use_settings(monkeypatch, MAX_TOPICS_PER_EXTRACTION=2)
    llm, _ = _fake_llm(
        monkeypatch, [_topic(f"t{i}", f"Topic {i}", evidence=(chunks[0].id,)) for i in range(4)]
    )

    job = await _extract(db_session, llm, owner, project, replace=True)

    assert job.status == "failed" and "limit is 2" in job.error
    assert await _snapshot(db_session, project) == before


def test_the_new_settings_are_optional_with_safe_defaults():
    """An ``.env`` from before Phase 5 must still boot: the required list is
    unchanged and every new setting has a default.
    """
    required = {name for name, field in Settings.model_fields.items() if field.is_required()}

    assert required == {
        "ENVIRONMENT",
        "DATABASE_URL",
        "DATABASE_DIRECT_URL",
        "REDIS_URL",
        "STORAGE_ENDPOINT_URL",
        "STORAGE_ACCESS_KEY_ID",
        "STORAGE_SECRET_ACCESS_KEY",
        "BUCKET_UPLOADS",
        "BUCKET_ASSETS",
        "BUCKET_SEGMENTS",
        "BUCKET_OUTPUTS",
        "CLERK_JWKS_URL",
        "CLERK_ISSUER",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    }
    settings = Settings(_env_file=None)  # only what conftest puts in the environment
    assert settings.TOPIC_INPUT_BUDGET_CHARS == 48_000
    assert (settings.MAX_TOPICS_PER_EXTRACTION, settings.MAX_TOPIC_DEPTH) == (30, 3)
    for name in (
        "MAX_TOPIC_TITLE_CHARS",
        "MAX_TOPIC_SUMMARY_CHARS",
        "MAX_TOPIC_KEYWORDS",
        "MAX_TOPIC_KEYWORD_CHARS",
        "MAX_TOPIC_EVIDENCE",
    ):
        assert getattr(settings, name) >= 1


# --- extraction is only ever explicit -------------------------------------------------------


class _FakeStorage:
    """Just enough of ``Storage`` for ``upload_source``: it records the write."""

    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []

    async def put_object(self, **kwargs: Any) -> None:
        self.puts.append(kwargs)


def _pdf(pages: list[str]) -> bytes:
    doc = FPDF()
    for text in pages:
        doc.add_page()
        doc.set_font("Helvetica", size=12)
        doc.cell(text=text)
    return bytes(doc.output())


async def test_uploading_a_pdf_still_indexes_it_but_never_starts_topic_extraction(db_session):
    owner, project = await _project(db_session)
    storage = _FakeStorage()

    source = await uploaded_sources_service.upload_source(
        db_session,
        storage,  # type: ignore[arg-type]
        project_id=project.id,
        uploader_id=owner.id,
        original_filename="notes.pdf",
        content_type="application/pdf",
        content=_pdf(["Photosynthesis makes food.", "Respiration releases energy."]),
        bucket=UPLOADS_BUCKET,
        max_pages=50,
    )

    assert source.status == "parsed"
    jobs = (await jobs_repo.list_by_project(db_session, project.id)).items
    # Phase 4 behaviour, unchanged: exactly one indexing job for the source...
    assert [job.kind for job in jobs] == ["build_evidence"]
    assert jobs[0].status == "queued"
    assert jobs[0].payload == {"source_id": str(source.id)}
    assert jobs[0].idempotency_key == str(source.id)
    # ...and extraction stays something the user must ask for.
    assert (
        await jobs_repo.get_active_by_project_kind(
            db_session, project_id=project.id, kind="extract_topics"
        )
        is None
    )
    assert await project_topics_repo.list_all_by_project(db_session, project.id) == []


# --- the prompt's size does not depend on free-text project fields -----------------------


async def test_an_enormous_project_title_and_description_cannot_inflate_the_prompt(
    db_session, monkeypatch
):
    """A project's title and description are unbounded free text and reach the
    prompt outside the passage budget, so the service clips what it sends. The
    stored project is left exactly as it was.
    """
    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    project = await projects_repo.create(
        db_session,
        owner_id=owner.id,
        title="T" * 50_000,
        description="D" * 500_000,
        source_type="pdf",
        language="en-GB",
    )
    chunks = await _add_chunks(db_session, project, ["Plants make sugar."])
    llm, create = _fake_llm(monkeypatch, [_topic("t1", "Sugar", evidence=(chunks[0].id,))])

    job = await _extract(db_session, llm, owner, project)

    assert job.status == "succeeded"
    message = _user_message(create)
    assert message.count("T") < 400
    assert message.count("D") < 2_100  # the clipped description, and nothing else with a D
    assert "…" in message
    stored = await projects_repo.get_by_id(db_session, project.id)
    assert stored is not None
    assert len(stored.title) == 50_000 and len(stored.description or "") == 500_000


async def test_an_ordinary_project_brief_is_sent_unchanged(db_session, monkeypatch):
    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    project = await projects_repo.create(
        db_session,
        owner_id=owner.id,
        title="How photosynthesis works",
        description="A short explainer for year 9.",
        source_type="pdf",
    )
    chunks = await _add_chunks(db_session, project, ["Plants make sugar."])
    llm, create = _fake_llm(monkeypatch, [_topic("t1", "Sugar", evidence=(chunks[0].id,))])

    await _extract(db_session, llm, owner, project)

    message = _user_message(create)
    assert "Subject: How photosynthesis works" in message
    assert "Description: A short explainer for year 9." in message
    assert "Language: en-IN" in message  # the default: this project named none


@pytest.mark.parametrize(
    ("language", "asked_for", "not_asked_for"),
    [
        ("en-IN", "Indian English", "British English"),
        ("en-GB", "British English", "Indian English"),
    ],
)
async def test_the_prompt_is_written_for_the_projects_own_language(
    db_session, monkeypatch, language, asked_for, not_asked_for
):
    """The stored ``projects.language`` is the only thing that picks the language the
    topics are asked to be written in - it flows project -> service -> prompt.
    """
    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    project = await projects_repo.create(
        db_session,
        owner_id=owner.id,
        title="How photosynthesis works",
        source_type="pdf",
        language=language,
    )
    chunks = await _add_chunks(db_session, project, ["Plants make sugar."])
    llm, create = _fake_llm(monkeypatch, [_topic("t1", "Sugar", evidence=(chunks[0].id,))])

    job = await _extract(db_session, llm, owner, project)

    assert job.status == "succeeded"
    message = _user_message(create)
    assert f"Language: {language}" in message
    assert asked_for in message
    assert not_asked_for not in message


# --- what the owner is told when a job fails -----------------------------------------------
#
# A job's error is returned by GET /jobs, so it is written for the owner: a fixed
# sentence for a provider or database failure, never the failure's own text (see
# services/job_errors.py). A failed extraction still leaves the previous topics as
# they were - that is asserted alongside, since it is what the failure is about.


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, provider_errors.NOT_AUTHORISED),
        (404, provider_errors.MODEL_UNAVAILABLE),
        (429, provider_errors.RATE_LIMITED),
        (529, provider_errors.UNAVAILABLE),
    ],
)
async def test_a_provider_failure_tells_the_owner_a_fixed_sentence_and_nothing_else(
    db_session, monkeypatch, status, expected
):
    job, before, after = await _failed_replace_preserves_everything(
        db_session, monkeypatch, lambda ctx: _llm_raising(monkeypatch, leaky_anthropic(status))
    )

    assert job.status == "failed" and job.error == expected
    assert_clean(job.error)
    assert after == before


async def test_a_database_failure_while_saving_shows_no_sql(db_session, monkeypatch):
    def fresh_valid_outline(ctx):
        chunks, seed_a = ctx[2], ctx[3]
        llm, _ = _fake_llm(
            monkeypatch,
            [
                _topic("t1", "Photosynthesis", evidence=(chunks[0].id,), seed=seed_a.id),
                _topic("t2", "A brand new topic", parent="t1", evidence=(chunks[1].id,)),
            ],
        )

        async def boom(*args: Any, **kwargs: Any):
            raise leaky_db_error()

        monkeypatch.setattr(project_topics_repo, "create_many", boom)
        return llm

    job, before, after = await _failed_replace_preserves_everything(
        db_session, monkeypatch, fresh_valid_outline
    )

    assert job.status == "failed" and job.error == job_errors.DATABASE_ERROR
    assert_clean(job.error)
    assert after == before


async def test_an_error_the_owner_can_act_on_is_still_shown_in_full(db_session, monkeypatch):
    """Hiding the leaky text must not hide the useful text: a failure that is
    about the owner's own request still says what was wrong.
    """
    owner, project = await _project(db_session)  # nothing to extract from is a 400 at enqueue,
    await _add_chunks(db_session, project, ["Page."])  # so make a job whose payload is broken
    job = await jobs_repo.create(
        db_session, project_id=project.id, kind="extract_topics", payload={"replace": False}
    )
    llm, _ = _fake_llm(monkeypatch, [])

    finished = await service.run_extract_topics_job(db_session, llm, job_id=job.id)

    assert finished.error == "The extraction job has no valid project_id in its payload."

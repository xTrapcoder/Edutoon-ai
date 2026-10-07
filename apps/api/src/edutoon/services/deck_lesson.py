"""Writing a lesson from a PowerPoint deck - the deck engine's three jobs and its storyboard.

A project whose source is a ``.pptx`` (``projects.source_type == "pptx"``) runs the same stages as
any other - topics, claims, script, narration, storyboard, video - under the same job kinds, the
same endpoints and the same review pages. What differs is *who writes*: no model is called. The
deck's slides are read from their stored structure and :mod:`edutoon.domain.deck_plan` does the
planning, deterministically and for free. The worker routes a job here when its project is a deck
(``worker.dispatch``); a PDF project never reaches this module.

- **Topics** (``extract_topics``): the slides are grouped into topics by the deck's own sections
  (or into runs of neighbouring slides), each citing the slides it covers, and saved as accepted -
  a reader can still reject or rename any of them before the script is written.
- **Claims** (``extract_claims``): every statement a slide makes becomes a claim, ``supported``
  because it is *quoted* from the slide (:func:`edutoon.domain.deck.claim_is_quoted`) and citing
  that slide's chunk. There is no judge and no embedding to weigh, so the ledger records the
  engine, not a model: ``llm_model`` is :data:`DECK_ENGINE_MODEL` and ``embedding_model`` is
  :data:`NO_EMBEDDING_MODEL`.
- **Script** (``generate_script``): the accepted topics' verified statements are fitted to the
  project's target length - the important ones, never padded - and written as one continuous
  narration. The result goes through the same provenance rules as a model-written script
  (:func:`edutoon.domain.script_provenance.filter_and_validate`), so a script that cites nothing,
  or brings in a name or figure no claim contains, is refused here exactly as it would be there.
- **Storyboard**: :class:`DeckStoryboardComposer` stands where the language model stands in
  ``services.storyboard``, composing each segment's scenes from what its narration speaks.

Everything downstream - Piper narration, the Pillow renderer, FFmpeg - is the same code a PDF
project uses.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.config import get_settings
from edutoon.core.errors import ConflictError, JobError, NotFoundError, ValidationError
from edutoon.domain.deck import DeckSlide, claim_is_quoted, slide_from_json
from edutoon.domain.deck_plan import (
    Statement,
    TopicGroup,
    extract_statements,
    lesson_title_of,
    plan_lesson,
    plan_topics,
    recurring_texts,
    slide_role,
)
from edutoon.domain.deck_scenes import Role, compose_scenes, match_slide
from edutoon.domain.script import ScriptDraft, ScriptSegmentDraft
from edutoon.domain.script_provenance import GroundingReference, filter_and_validate
from edutoon.domain.topic_tree import accepted_topics
from edutoon.providers.llm import (
    StoryboardSceneDraft,
    StoryboardSceneSetDraft,
    StoryboardSegmentBrief,
    TokenUsage,
)
from edutoon.repositories import claim_verifications as claim_verifications_repo
from edutoon.repositories import jobs as jobs_repo
from edutoon.repositories import project_topics as project_topics_repo
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import scripts as scripts_repo
from edutoon.repositories import source_chunks as source_chunks_repo
from edutoon.repositories import topic_citations as topic_citations_repo
from edutoon.repositories.claim_verifications import NewClaimCitation
from edutoon.repositories.jobs import JobRecord
from edutoon.repositories.project_topics import NewProjectTopic
from edutoon.repositories.source_chunks import SourceChunkRecord
from edutoon.services.job_errors import describe_job_failure
from edutoon.services.llm_usage import new_usage_report

log = structlog.get_logger()

DECK_ENGINE_MODEL = "deck-engine/1"
"""What the claim ledger and the script record as the writer: the deterministic deck engine."""

NO_EMBEDDING_MODEL = "none"
"""A deck's claims are quoted, not retrieved, so there is no embedding model to name."""

SOURCE_TYPE_DECK = "pptx"


# --- routing ---------------------------------------------------------------------------


async def job_is_for_deck(session: AsyncSession, job_id: UUID) -> bool:
    """Whether the job belongs to a PowerPoint project - what the worker asks to decide whether
    the deck engine or the model-backed service runs it. A job without a readable project is not
    a deck's: it goes the usual way and fails there as it always has."""
    job = await jobs_repo.get_by_id(session, job_id)
    if job is None:
        return False
    return await project_is_deck(session, job.project_id)


async def project_is_deck(session: AsyncSession, project_id: UUID) -> bool:
    project = await projects_repo.get_by_id(session, project_id)
    return project is not None and project.source_type == SOURCE_TYPE_DECK


# --- reading the deck ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LoadedDeck:
    slides: tuple[DeckSlide, ...]
    chunk_by_slide: dict[int, SourceChunkRecord]

    @property
    def by_index(self) -> dict[int, DeckSlide]:
        return {slide.index: slide for slide in self.slides}


async def load_deck(session: AsyncSession, project_id: UUID) -> LoadedDeck:
    """The project's slides, in deck order, read from the stored structure."""
    chunks = await source_chunks_repo.list_by_project_in_order(session, project_id)
    slides: list[DeckSlide] = []
    chunk_by_slide: dict[int, SourceChunkRecord] = {}
    for chunk in chunks:
        if chunk.structure is None:
            continue
        slide = slide_from_json(chunk.structure)
        slides.append(slide)
        chunk_by_slide[slide.index] = chunk
    if not slides:
        raise ValidationError("This project has no parsed slides yet. Upload a presentation first.")
    slides.sort(key=lambda slide: slide.index)
    return LoadedDeck(slides=tuple(slides), chunk_by_slide=chunk_by_slide)


async def _slides_of_topics(
    session: AsyncSession, project_id: UUID, topic_ids: list[UUID]
) -> dict[UUID, list[int]]:
    evidence = await topic_citations_repo.list_evidence_for_topics(
        session, project_id=project_id, topic_ids=topic_ids
    )
    return {
        topic_id: sorted({item.page_from for item in items if item.page_from is not None})
        for topic_id, items in evidence.items()
    }


# --- job plumbing ----------------------------------------------------------------------


async def _run_job(
    session: AsyncSession,
    job_id: UUID,
    kind: str,
    work: Callable[[JobRecord], Awaitable[dict[str, object]]],
    *,
    on_failure: Callable[[JobRecord], Awaitable[None]] | None = None,
) -> JobRecord:
    """Mark the job running, do ``work``, and end it ``succeeded`` with its result - or ``failed``
    with an error written for its owner. Never raises for a failure of the work itself, matching
    the model-backed ``run_*_job`` functions; only a ``job_id`` that does not exist is a caller
    bug."""
    job = await jobs_repo.get_by_id(session, job_id)
    if job is None:
        raise NotFoundError(f"Job {job_id} not found.")
    running = await jobs_repo.update(
        session,
        job_id=job.id,
        status="running",
        started_at=datetime.now(UTC),
        attempts=job.attempts + 1,
    )
    assert running is not None  # just fetched by this id
    try:
        # The work's writes are one savepoint, so a failure leaves the project as it was.
        async with session.begin_nested():
            result = await work(running)
    except Exception as exc:  # noqa: BLE001 - any failure fails the job, never raises
        error = describe_job_failure(exc, job_id=running.id, kind=kind)
        if on_failure is not None:
            await on_failure(running)
        failed = await jobs_repo.update(
            session,
            job_id=running.id,
            status="failed",
            finished_at=datetime.now(UTC),
            error=error,
            result={"usage": new_usage_report().as_dict(), "engine": DECK_ENGINE_MODEL},
        )
        assert failed is not None
        return failed
    done = await jobs_repo.update(
        session,
        job_id=running.id,
        status="succeeded",
        finished_at=datetime.now(UTC),
        result=result,
    )
    assert done is not None
    return done


def _project_id_of(job: JobRecord) -> UUID:
    try:
        return UUID(str(job.payload["project_id"]))
    except (KeyError, ValueError) as exc:
        raise JobError("The job has no valid project_id in its payload.") from exc


# --- topics ----------------------------------------------------------------------------


async def run_extract_topics_job(session: AsyncSession, *, job_id: UUID) -> JobRecord:
    """Group a deck's slides into topics (``extract_topics`` for a deck project)."""

    async def work(job: JobRecord) -> dict[str, object]:
        project_id = _project_id_of(job)
        replace = bool(job.payload.get("replace", False))
        settings = get_settings()
        project = await projects_repo.get_by_id(session, project_id)
        if project is None:
            raise NotFoundError(f"Project {project_id} not found.")

        existing = await project_topics_repo.list_all_by_project(session, project_id)
        derived = [topic for topic in existing if topic.origin != "user"]
        if replace:
            if await project_topics_repo.is_referenced_by_scripts(session, project_id):
                raise ConflictError(
                    "A script already references this project's topics, so they cannot be "
                    "replaced. Generate a new script from the current topics instead."
                )
        elif derived:
            raise ConflictError(
                "Topics have already been extracted for this project. Run the extraction "
                "again with replace to overwrite the derived topics (your own topics are kept)."
            )

        deck = await load_deck(session, project_id)
        max_topics = max(
            1,
            min(
                settings.MAX_SCRIPT_SECTIONS,
                project.target_duration_ms // settings.MIN_SECTION_DURATION_MS,
            ),
        )
        plan = plan_topics(deck.slides, max_topics=max_topics, fallback_title=project.title)

        await topic_citations_repo.delete_by_project(session, project_id)
        if replace:
            await project_topics_repo.delete_derived_by_project(session, project_id)

        first_position = (
            max(
                (t.position for t in existing if t.parent_id is None and t.origin == "user"),
                default=-1,
            )
            + 1
        )
        created = await project_topics_repo.create_many(
            session,
            [
                NewProjectTopic(
                    project_id=project_id,
                    title=group.title,
                    summary=group.summary or None,
                    position=first_position + number,
                    depth=0,
                    origin="source",
                    status="accepted",
                    keywords=group.keywords,
                )
                for number, group in enumerate(plan.groups)
            ],
        )
        by_position = {topic.position: topic for topic in created}
        for number, group in enumerate(plan.groups):
            topic = by_position[first_position + number]
            cited = await topic_citations_repo.add_many(
                session,
                project_id=project_id,
                topic_id=topic.id,
                source_chunk_ids=[deck.chunk_by_slide[i].id for i in group.slide_indexes],
            )
            if len(cited) != len(group.slide_indexes):
                raise ValueError(f"Topic {group.title!r} cites material outside this project.")

        return {
            "mode": "pdf",
            "topic_count": len(plan.groups),
            "chunks_total": len(deck.slides),
            "chunks_used": len(deck.slides),
            "truncated": False,
            "dropped_uncited": 0,
            "replaced": replace,
            "input_tokens": 0,
            "output_tokens": 0,
            "usage": new_usage_report().as_dict(),
            "llm_model": DECK_ENGINE_MODEL,
            "engine": DECK_ENGINE_MODEL,
            "lesson_title": plan.lesson_title,
        }

    return await _run_job(session, job_id, "extract_topics", work)


# --- claims ----------------------------------------------------------------------------


async def run_extract_claims_job(session: AsyncSession, *, job_id: UUID) -> JobRecord:
    """Turn every statement the accepted topics' slides make into a verified claim
    (``extract_claims`` for a deck project). Additive: a statement that already has a claim for
    its topic is left alone, so running it again after accepting more topics covers only those."""

    async def work(job: JobRecord) -> dict[str, object]:
        project_id = _project_id_of(job)
        topics = accepted_topics(await project_topics_repo.list_all_by_project(session, project_id))
        if not topics:
            raise ValidationError(
                "There are no accepted topics to extract claims from. Accept at least one "
                "topic first."
            )
        deck = await load_deck(session, project_id)
        recurring = recurring_texts(deck.slides)
        slides_of = await _slides_of_topics(session, project_id, [t.id for t in topics])
        by_index = deck.by_index

        created = 0
        skipped_topics = 0
        for topic in topics:
            have = set(await claim_verifications_repo.list_claim_texts_by_topic(session, topic.id))
            added = 0
            for slide_index in slides_of.get(topic.id, []):
                slide, chunk = by_index.get(slide_index), deck.chunk_by_slide.get(slide_index)
                if slide is None or chunk is None:
                    continue
                for statement in extract_statements(slide, recurring=recurring):
                    if statement.text in have or not claim_is_quoted(statement.text, chunk.content):
                        continue
                    await claim_verifications_repo.create(
                        session,
                        project_id=project_id,
                        claim_text=statement.text,
                        status="supported",
                        embedding_model=NO_EMBEDDING_MODEL,
                        llm_model=DECK_ENGINE_MODEL,
                        rationale=f"Quoted from slide {slide_index}.",
                        topic_id=topic.id,
                        citations=[NewClaimCitation(source_chunk_id=chunk.id, similarity=1.0)],
                    )
                    have.add(statement.text)
                    added += 1
            created += added
            skipped_topics += 0 if added else 1

        return {
            "eligible_topic_count": len(topics),
            "processed_topic_count": len(topics) - skipped_topics,
            "skipped_topic_count": skipped_topics,
            "failed_topic_count": 0,
            "failures": [],
            "claims_created": created,
            "status_counts": {"supported": created},
            "replaced": bool(job.payload.get("replace", False)),
            "usage": new_usage_report().as_dict(),
            "embeddings_model": NO_EMBEDDING_MODEL,
            "llm_model": DECK_ENGINE_MODEL,
            "engine": DECK_ENGINE_MODEL,
        }

    return await _run_job(session, job_id, "extract_claims", work)


# --- script ----------------------------------------------------------------------------


async def run_generate_script_job(session: AsyncSession, *, job_id: UUID) -> JobRecord:
    """Write the lesson's narration from the verified statements (``generate_script`` for a deck
    project). On success the pending script becomes a ``draft``; on any failure it is ``failed``
    and an approved script, if there is one, is untouched."""

    script_ref: dict[str, UUID] = {}

    async def work(job: JobRecord) -> dict[str, object]:
        project_id = _project_id_of(job)
        try:
            script_id = UUID(str(job.payload["script_id"]))
            topic_ids = [UUID(str(item)) for item in job.payload["topic_ids"]]
        except (KeyError, ValueError, TypeError) as exc:
            raise JobError("The generation job has no valid script or topics.") from exc
        script_ref["project_id"], script_ref["script_id"] = project_id, script_id
        settings = get_settings()
        project = await projects_repo.get_by_id(session, project_id)
        if project is None:
            raise NotFoundError(f"Project {project_id} not found.")

        all_topics = {
            t.id: t for t in await project_topics_repo.list_all_by_project(session, project_id)
        }
        topics = [all_topics[tid] for tid in topic_ids if tid in all_topics]
        if not topics:
            raise JobError("None of this generation's accepted topics could be found any more.")

        deck = await load_deck(session, project_id)
        by_index = deck.by_index
        slides_of = await _slides_of_topics(session, project_id, [t.id for t in topics])

        chunk_slide = {chunk.id: index for index, chunk in deck.chunk_by_slide.items()}
        claim_by_key: dict[tuple[int, str], UUID] = {}
        claim_texts: dict[UUID, list[str]] = {}
        for topic in topics:
            supported = await claim_verifications_repo.list_supported_by_topic(
                session, project_id=project_id, topic_id=topic.id
            )
            if not supported:
                raise ValidationError(
                    f"Topic '{topic.title}' has no verified evidence; generate again once it does."
                )
            citations = await claim_verifications_repo.list_citations_for(
                session, [claim.id for claim in supported]
            )
            for claim in supported:
                for citation in citations.get(claim.id, []):
                    slide_index = chunk_slide.get(citation.source_chunk_id)
                    if slide_index is not None:
                        claim_by_key[(slide_index, claim.claim_text)] = claim.id
                claim_texts.setdefault(topic.id, []).append(claim.claim_text)

        groups = [
            TopicGroup(title=topic.title, slide_indexes=tuple(slides_of.get(topic.id, [])))
            for topic in topics
        ]
        lesson_title = lesson_title_of(deck.slides, fallback=project.title)
        plan = plan_lesson(
            deck.slides,
            groups,
            lesson_title=lesson_title,
            target_ms=project.target_duration_ms,
            words_per_minute=settings.DECK_SPEECH_WORDS_PER_MINUTE,
            allowed=frozenset(claim_by_key),
        )

        segments: list[ScriptSegmentDraft] = []
        for index, segment in enumerate(plan.segments):
            topic_id = topics[segment.topic_index].id if segment.topic_index is not None else None
            segments.append(
                ScriptSegmentDraft(
                    segment_index=index,
                    narration_text=segment.narration_text,
                    claim_ids=[str(claim_by_key[s.key]) for s in segment.statements],
                    estimated_duration_ms=0,  # re-derived from the words by filter_and_validate
                    connective=segment.connective,
                    topic_id=topic_id,
                )
            )

        titles_of = {
            topic.id: [
                by_index[i].title or "" for i in slides_of.get(topic.id, []) if i in by_index
            ]
            for topic in topics
        }
        grounding = GroundingReference(
            topic_texts={
                topic.id: " ".join(
                    [
                        topic.title,
                        lesson_title,
                        *titles_of[topic.id],
                        *claim_texts.get(topic.id, []),
                    ]
                )
                for topic in topics
            },
            frame_text=" ".join([project.title, lesson_title, *(t.title for t in topics)]),
        )
        validated = filter_and_validate(
            ScriptDraft(title=lesson_title, segments=segments),
            set(claim_by_key.values()),
            settings.DECK_SPEECH_WORDS_PER_MINUTE,
            grounding=grounding,
        )
        attached = await scripts_repo.attach_draft(
            session,
            project_id=project_id,
            script_id=script_id,
            draft=validated,
            llm_model=DECK_ENGINE_MODEL,
        )
        if attached is None:
            raise ConflictError(f"Script {script_id} is no longer pending; cannot attach a draft.")

        target = project.target_duration_ms
        difference = validated.estimated_duration_ms - target
        return {
            "script_id": str(script_id),
            "segment_count": len(validated.segments),
            "topic_count": len(topics),
            "estimated_duration_ms": validated.estimated_duration_ms,
            "target_duration_ms": target,
            "duration_difference_ms": difference,
            "duration_difference_pct": round(difference / target * 100, 1) if target else 0.0,
            "correction_applied": False,
            "statements_total": plan.statements_total,
            "statements_dropped": plan.statements_dropped,
            "slides_dropped": plan.slides_dropped,
            "input_tokens": 0,
            "output_tokens": 0,
            "usage": new_usage_report().as_dict(),
            "llm_model": DECK_ENGINE_MODEL,
            "engine": DECK_ENGINE_MODEL,
        }

    async def mark_script_failed(job: JobRecord) -> None:
        project_id = script_ref.get("project_id") or _project_id_of(job)
        script_id = script_ref.get("script_id")
        if script_id is None:
            try:
                script_id = UUID(str(job.payload["script_id"]))
            except (KeyError, ValueError):
                return
        await scripts_repo.set_status(
            session,
            project_id=project_id,
            script_id=script_id,
            status="failed",
            llm_model=DECK_ENGINE_MODEL,
        )

    return await _run_job(session, job_id, "generate_script", work, on_failure=mark_script_failed)


# --- storyboard ------------------------------------------------------------------------


class DeckStoryboardComposer:
    """Composes a segment's scenes for a deck lesson, standing where the language model stands in
    ``services.storyboard`` (the same ``compose_storyboard_scenes`` call, the same validation and
    provenance checks on what it returns). Built once per job from the stored slides; it never
    touches the database or a provider while composing."""

    model = DECK_ENGINE_MODEL

    def __init__(
        self,
        slides: tuple[DeckSlide, ...],
        *,
        lesson_title: str,
        topic_titles: tuple[str, ...],
    ) -> None:
        recurring = recurring_texts(slides)
        self._slides = {slide.index: slide for slide in slides}
        self._statements: dict[int, tuple[Statement, ...]] = {
            slide.index: extract_statements(slide, recurring=recurring)
            for slide in slides
            if slide_role(slide, is_first=slide.index == slides[0].index) in {"content", "section"}
        }
        self._lesson_title = lesson_title
        self._topic_titles = topic_titles

    async def compose_storyboard_scenes(
        self, segment: StoryboardSegmentBrief, *, max_scenes: int
    ) -> tuple[StoryboardSceneSetDraft, TokenUsage]:
        narration = segment.narration_text
        role: Role = "other"
        if narration.startswith("Welcome."):
            role = "intro"
        elif narration.startswith("To summarise"):
            role = "outro"
        match = match_slide(narration, self._statements, self._slides) if role == "other" else None
        if match is not None:
            role = "slide"
        title = self._slides[match.slide_index].title if match is not None else None
        scenes = compose_scenes(
            narration,
            role=role,
            match=match,
            slide_title=title,
            lesson_title=self._lesson_title,
            topic_titles=self._topic_titles,
            max_scenes=max_scenes,
        )
        if not scenes:
            scenes = [("title_card", {"title": self._lesson_title[:60]})]
        draft = StoryboardSceneSetDraft(
            scenes=tuple(StoryboardSceneDraft(template=t, content=c) for t, c in scenes)
        )
        return draft, TokenUsage(input_tokens=0, output_tokens=0)


async def build_storyboard_composer(
    session: AsyncSession, *, job_id: UUID
) -> DeckStoryboardComposer:
    """The composer for the storyboard job ``job_id`` of a deck project."""
    job = await jobs_repo.get_by_id(session, job_id)
    if job is None:
        raise NotFoundError(f"Job {job_id} not found.")
    project = await projects_repo.get_by_id(session, job.project_id)
    if project is None:
        raise NotFoundError(f"Project {job.project_id} not found.")
    deck = await load_deck(session, job.project_id)
    topics = accepted_topics(await project_topics_repo.list_all_by_project(session, job.project_id))
    return DeckStoryboardComposer(
        deck.slides,
        lesson_title=lesson_title_of(deck.slides, fallback=project.title),
        topic_titles=tuple(topic.title for topic in topics),
    )


# Re-exported for the worker.
__all__ = [
    "DECK_ENGINE_MODEL",
    "DeckStoryboardComposer",
    "build_storyboard_composer",
    "job_is_for_deck",
    "project_is_deck",
    "run_extract_claims_job",
    "run_extract_topics_job",
    "run_generate_script_job",
]

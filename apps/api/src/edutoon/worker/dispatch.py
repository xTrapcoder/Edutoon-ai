"""Job-kind -> handler dispatch table (Phase D).

Every handler here has the same shape: given a session, the provider clients
it needs, the job's id, and (Phase 7) an optional ``sessionmaker`` for a
handler that reports interim progress from its own short, independent
transactions, run exactly one existing ``services.*.run_*_job`` function for
the job named by ``job_id``. This module never implements job business logic
itself - it only wires a job's ``kind`` (the ``jobs.kind`` CHECK constraint
from migration 0003, widened by migration 0012) to the service function that
already knows how to run it.

``jobs.kind`` currently allows eight values; seven have a handler implemented
in the service layer today (``evidence.run_build_evidence_job``,
``scripts.run_generate_script_job``,
``topic_extraction.run_extract_topics_job``,
``claim_extraction.run_extract_claims_job``,
``script_audio.run_synthesise_voice_job`` - Phase 8,
``storyboard.run_generate_storyboard_job`` - Phase 9,
``render.run_render_video_job`` - Phase 10). The rest
(``parse_source``) has no service-layer implementation yet
- deliberately out of scope - so it is simply absent from
``JOB_HANDLERS``. ``worker.runner`` treats a missing handler as a normal,
terminal job failure (not a crash), so a job of one of those kinds is
handled safely rather than silently stalling forever.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from edutoon.core.config import Settings
from edutoon.providers.embeddings import Embeddings, embeddings_from_settings
from edutoon.providers.llm import LLM, get_llm_client
from edutoon.providers.renderer import Renderer, get_renderer
from edutoon.providers.storage import Storage, get_storage_client
from edutoon.repositories.jobs import JobRecord
from edutoon.services import claim_extraction as claim_extraction_service
from edutoon.services import evidence as evidence_service
from edutoon.services import render as render_service
from edutoon.services import script_audio as script_audio_service
from edutoon.services import scripts as scripts_service
from edutoon.services import storyboard as storyboard_service
from edutoon.services import topic_extraction as topic_extraction_service


@dataclass(frozen=True, slots=True)
class WorkerProviders:
    """The provider clients job handlers need, bundled once at worker
    startup - the worker-process equivalent of ``app.state`` in ``main.py``.

    There are two LLM clients, one per configured model, and a handler uses the
    one for its task: ``llm`` (``LLM_MODEL``) for claim judging, topic
    extraction and - Phase 9 - storyboard scene composition, ``script_llm``
    (``SCRIPT_LLM_MODEL``) for writing scripts. ``script_llm`` has no default
    on purpose: falling back to ``llm`` would make a forgotten wiring
    silently write scripts with the wrong model.

    ``storage`` and ``voices_dir`` are Phase 8's: ``script_audio``'s handler
    uploads synthesised audio to ``BUCKET_SEGMENTS`` and reads Piper voice
    files from ``voices_dir`` (``PIPER_VOICES_DIR`` - see
    ``scripts/download_piper_voices.py``).

    ``renderer`` is Phase 10's: the rendering engine ``render_video`` jobs drive (a Pillow
    compositor feeding FFmpeg - see ``providers.renderer``), reading and writing videos through
    ``storage``. It is ``None`` only in tests that never run a render; a ``render_video`` job
    that reaches a worker without one fails with a message rather than crashing it.
    """

    embeddings: Embeddings
    llm: LLM
    script_llm: LLM
    storage: Storage
    voices_dir: Path
    renderer: Renderer | None = None


def build_worker_providers(settings: Settings) -> WorkerProviders:
    """The worker's provider clients, built once at start-up from ``settings``.

    The two LLM clients share one API key and differ only in the model they are
    bound to, so ``SCRIPT_LLM_MODEL`` really does choose the model that writes
    scripts, and ``LLM_MODEL`` still serves everything else.
    """
    return WorkerProviders(
        embeddings=embeddings_from_settings(settings),
        llm=get_llm_client(api_key=settings.ANTHROPIC_API_KEY, model=settings.LLM_MODEL),
        script_llm=get_llm_client(
            api_key=settings.ANTHROPIC_API_KEY, model=settings.SCRIPT_LLM_MODEL
        ),
        storage=get_storage_client(
            endpoint_url=settings.STORAGE_ENDPOINT_URL,
            access_key_id=settings.STORAGE_ACCESS_KEY_ID,
            secret_access_key=settings.STORAGE_SECRET_ACCESS_KEY,
        ),
        voices_dir=Path(settings.PIPER_VOICES_DIR),
        renderer=get_renderer(ffmpeg=settings.FFMPEG_BINARY, ffprobe=settings.FFPROBE_BINARY),
    )


JobHandler = Callable[
    [AsyncSession, WorkerProviders, UUID, "async_sessionmaker[AsyncSession] | None"],
    Awaitable[JobRecord],
]


async def _handle_build_evidence(
    session: AsyncSession,
    providers: WorkerProviders,
    job_id: UUID,
    sessionmaker: async_sessionmaker[AsyncSession] | None,
) -> JobRecord:
    return await evidence_service.run_build_evidence_job(
        session, providers.embeddings, job_id=job_id
    )


async def _handle_generate_script(
    session: AsyncSession,
    providers: WorkerProviders,
    job_id: UUID,
    sessionmaker: async_sessionmaker[AsyncSession] | None,
) -> JobRecord:
    return await scripts_service.run_generate_script_job(
        session, providers.script_llm, job_id=job_id, sessionmaker=sessionmaker
    )


async def _handle_extract_topics(
    session: AsyncSession,
    providers: WorkerProviders,
    job_id: UUID,
    sessionmaker: async_sessionmaker[AsyncSession] | None,
) -> JobRecord:
    return await topic_extraction_service.run_extract_topics_job(
        session, providers.llm, job_id=job_id
    )


async def _handle_extract_claims(
    session: AsyncSession,
    providers: WorkerProviders,
    job_id: UUID,
    sessionmaker: async_sessionmaker[AsyncSession] | None,
) -> JobRecord:
    return await claim_extraction_service.run_extract_claims_job(
        session, providers.embeddings, providers.llm, job_id=job_id
    )


async def _handle_synthesise_voice(
    session: AsyncSession,
    providers: WorkerProviders,
    job_id: UUID,
    sessionmaker: async_sessionmaker[AsyncSession] | None,
) -> JobRecord:
    return await script_audio_service.run_synthesise_voice_job(
        session,
        providers.storage,
        job_id=job_id,
        voices_dir=providers.voices_dir,
        sessionmaker=sessionmaker,
    )


async def _handle_generate_storyboard(
    session: AsyncSession,
    providers: WorkerProviders,
    job_id: UUID,
    sessionmaker: async_sessionmaker[AsyncSession] | None,
) -> JobRecord:
    return await storyboard_service.run_generate_storyboard_job(
        session, providers.llm, job_id=job_id, sessionmaker=sessionmaker
    )


async def _handle_render_video(
    session: AsyncSession,
    providers: WorkerProviders,
    job_id: UUID,
    sessionmaker: async_sessionmaker[AsyncSession] | None,
) -> JobRecord:
    if providers.renderer is None:
        raise RuntimeError("This worker has no video renderer configured.")
    return await render_service.run_render_video_job(
        session,
        providers.storage,
        providers.renderer,
        job_id=job_id,
        sessionmaker=sessionmaker,
    )


JOB_HANDLERS: dict[str, JobHandler] = {
    evidence_service.BUILD_EVIDENCE_JOB_KIND: _handle_build_evidence,
    scripts_service.GENERATE_SCRIPT_JOB_KIND: _handle_generate_script,
    topic_extraction_service.EXTRACT_TOPICS_JOB_KIND: _handle_extract_topics,
    claim_extraction_service.EXTRACT_CLAIMS_JOB_KIND: _handle_extract_claims,
    script_audio_service.SYNTHESISE_VOICE_JOB_KIND: _handle_synthesise_voice,
    storyboard_service.GENERATE_STORYBOARD_JOB_KIND: _handle_generate_storyboard,
    render_service.RENDER_VIDEO_JOB_KIND: _handle_render_video,
}

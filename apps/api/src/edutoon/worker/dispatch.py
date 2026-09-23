"""Job-kind -> handler dispatch table (Phase D).

Every handler here has the same shape: given a session and the provider
clients it needs, run exactly one existing ``services.*.run_*_job``
function for the job named by ``job_id``. This module never implements job
business logic itself - it only wires a job's ``kind`` (the ``jobs.kind``
CHECK constraint from migration 0003) to the service function that already
knows how to run it.

``jobs.kind`` currently allows six values; only two have a handler
implemented anywhere in the service layer today
(``evidence.run_build_evidence_job``, ``scripts.run_generate_script_job``).
The rest (``parse_source``, ``extract_topics``, ``synthesise_voice``,
``render_video``) have no service-layer implementation yet - deliberately
out of scope for this phase - so they are simply absent from
``JOB_HANDLERS``. ``worker.runner`` treats a missing handler as a normal,
terminal job failure (not a crash), so a job of one of those kinds is
handled safely rather than silently stalling forever.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.providers.embeddings import Embeddings
from edutoon.providers.llm import LLM
from edutoon.repositories.jobs import JobRecord
from edutoon.services import evidence as evidence_service
from edutoon.services import scripts as scripts_service


@dataclass(frozen=True, slots=True)
class WorkerProviders:
    """The provider clients job handlers need, bundled once at worker
    startup - the worker-process equivalent of ``app.state`` in ``main.py``.
    """

    embeddings: Embeddings
    llm: LLM


JobHandler = Callable[[AsyncSession, WorkerProviders, UUID], Awaitable[JobRecord]]


async def _handle_build_evidence(
    session: AsyncSession, providers: WorkerProviders, job_id: UUID
) -> JobRecord:
    return await evidence_service.run_build_evidence_job(
        session, providers.embeddings, job_id=job_id
    )


async def _handle_generate_script(
    session: AsyncSession, providers: WorkerProviders, job_id: UUID
) -> JobRecord:
    return await scripts_service.run_generate_script_job(session, providers.llm, job_id=job_id)


JOB_HANDLERS: dict[str, JobHandler] = {
    evidence_service.BUILD_EVIDENCE_JOB_KIND: _handle_build_evidence,
    scripts_service.GENERATE_SCRIPT_JOB_KIND: _handle_generate_script,
}

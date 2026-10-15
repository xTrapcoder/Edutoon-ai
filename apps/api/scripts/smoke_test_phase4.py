"""THROWAWAY Phase 4 real-provider smoke test.

Exercises the existing service/repository/provider layers *directly* - no
HTTP, no Clerk - against the REAL configured OpenAI and Anthropic APIs and
the real local Postgres/MinIO (docker-compose). It duplicates no production
logic: every step below is a straight call into the same
``edutoon.services``/``edutoon.repositories``/``edutoon.providers`` modules
the API and worker already use.

Not part of the test suite (pytest's ``testpaths`` is ``tests``, so this file
is never collected) and safe to delete once verification is done.

Run from ``apps/api`` so ``Settings``'s relative ``.env`` resolves:

    uv run python scripts/smoke_test_phase4.py

Never prints API keys - only the configured model names (not secret).
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from fpdf import FPDF
from sqlalchemy import delete

from edutoon.core.config import get_settings
from edutoon.db.database import dispose_engine
from edutoon.db.session import get_sessionmaker
from edutoon.db.tables import users as users_table
from edutoon.providers.embeddings import get_embeddings_client
from edutoon.providers.llm import get_llm_client
from edutoon.providers.storage import get_storage_client
from edutoon.repositories import jobs as jobs_repo
from edutoon.repositories import source_chunks as source_chunks_repo
from edutoon.services import evidence as evidence_service
from edutoon.services import projects as projects_service
from edutoon.services import scripts as scripts_service
from edutoon.services import uploaded_sources as uploaded_sources_service
from edutoon.services import users as users_service

PAGES = [
    "Photosynthesis is the process by which green plants convert sunlight "
    "into chemical energy stored in glucose.",
    "Chlorophyll in plant leaves absorbs sunlight, mainly in the red and "
    "blue wavelengths of the visible spectrum.",
    "The process of photosynthesis releases oxygen as a by-product into "
    "the surrounding atmosphere.",
]

CLAIM_TEXT = "Photosynthesis converts sunlight into chemical energy in green plants."


def _make_pdf(pages: list[str]) -> bytes:
    """Same in-memory, no-download PDF fixture pattern as
    ``tests/test_uploaded_sources_api.py``'s ``_make_pdf`` helper.
    """
    doc = FPDF()
    for text in pages:
        doc.add_page()
        doc.set_font("Helvetica", size=12)
        doc.cell(text=text)
    return bytes(doc.output())


async def main() -> None:
    settings = get_settings()
    print("=== configuration (no secrets) ===")
    print(f"environment       = {settings.ENVIRONMENT}")
    print(f"embedding_model   = {settings.EMBEDDING_MODEL}")
    print(f"llm_model         = {settings.LLM_MODEL}")
    print(f"script_llm_model  = {settings.SCRIPT_LLM_MODEL}")
    print()

    embeddings = get_embeddings_client(
        api_key=settings.OPENAI_API_KEY, model=settings.EMBEDDING_MODEL
    )
    llm = get_llm_client(api_key=settings.ANTHROPIC_API_KEY, model=settings.LLM_MODEL)
    storage = get_storage_client(
        endpoint_url=settings.STORAGE_ENDPOINT_URL,
        access_key_id=settings.STORAGE_ACCESS_KEY_ID,
        secret_access_key=settings.STORAGE_SECRET_ACCESS_KEY,
    )

    results: dict[str, object] = {}
    sessionmaker = get_sessionmaker()

    async with sessionmaker() as session:
        user = None
        project = None
        source = None

        try:
            user = await users_service.create_user(
                session,
                email=f"smoke-test-{uuid4()}@example.com",
                role="admin",
                clerk_user_id=f"smoke_test_{uuid4()}",
                display_name="Phase 4 Smoke Test",
            )
            await session.commit()
            print(f"[1] created temp user            id={user.id}")

            project = await projects_service.create_project(
                session, owner_id=user.id, title="Phase 4 Smoke Test", source_type="pdf"
            )
            await session.commit()
            print(f"[2] created temp project         id={project.id}")

            pdf_bytes = _make_pdf(PAGES)
            print(f"[3] built local PDF fixture      pages={len(PAGES)} bytes={len(pdf_bytes)}")

            source = await uploaded_sources_service.upload_source(
                session,
                storage,
                project_id=project.id,
                uploader_id=user.id,
                original_filename="smoke-test.pdf",
                content_type="application/pdf",
                content=pdf_bytes,
                bucket=settings.BUCKET_UPLOADS,
                max_pages=settings.MAX_PDF_PAGES,
            )
            await session.commit()
            print(
                f"[4] ran ingestion/parsing flow   status={source.status} "
                f"page_count={source.page_count}"
            )
            results["pdf_page_count"] = source.page_count
            results["parse_status"] = source.status

            if source.status != "parsed":
                raise RuntimeError(f"PDF failed to parse (status={source.status}); stopping.")

            job = await jobs_repo.get_by_idempotency_key(
                session,
                project_id=project.id,
                kind=evidence_service.BUILD_EVIDENCE_JOB_KIND,
                idempotency_key=str(source.id),
            )
            if job is None:
                raise RuntimeError("upload_source did not enqueue a build_evidence job.")
            print(f"    build_evidence job enqueued   id={job.id} status={job.status}")

            # --- Real OpenAI embeddings (indexing) -----------------------------
            try:
                ran_job = await evidence_service.run_build_evidence_job(
                    session, embeddings, job_id=job.id
                )
                await session.commit()
                print(
                    f"[5] ran evidence/indexing flow   status={ran_job.status} "
                    f"result={ran_job.result}"
                )
                results["indexing_status"] = ran_job.status
                if ran_job.status != "succeeded":
                    raise RuntimeError(f"Indexing job failed: {ran_job.error}")

                chunks = await source_chunks_repo.list_by_source_in_order(session, source.id)
                embedded = [c for c in chunks if c.embedding_model is not None]
                results["chunk_count"] = len(chunks)
                results["embedded_count"] = len(embedded)
                results["embedding_success"] = bool(chunks) and len(embedded) == len(chunks)
                print(
                    f"[6] real OpenAI embeddings       chunks={len(chunks)} "
                    f"embedded={len(embedded)} success={results['embedding_success']}"
                )
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                await session.rollback()
                results["embedding_success"] = False
                results["embedding_error"] = f"{type(exc).__name__}: {exc}"
                print(f"    EMBEDDING PHASE ERROR: {type(exc).__name__}: {exc}")

            # --- Real OpenAI embed(claim) + pgvector search + real Anthropic judge
            try:
                verification = await evidence_service.verify_claim(
                    session, embeddings, llm, project_id=project.id, claim_text=CLAIM_TEXT
                )
                await session.commit()
                results["vector_search_success"] = True
                results["claim_status"] = verification.status
                results["claim_rationale"] = verification.rationale
                print(
                    f"[7] real Anthropic claim check   status={verification.status}\n"
                    f"    rationale: {verification.rationale}"
                )
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                await session.rollback()
                results["vector_search_success"] = False
                results["claim_error"] = f"{type(exc).__name__}: {exc}"
                print(f"    CLAIM VERIFICATION PHASE ERROR: {type(exc).__name__}: {exc}")

            # --- Real Anthropic script generation ------------------------------
            try:
                enqueued = await scripts_service.enqueue_script_generation(
                    session,
                    project_id=project.id,
                    owner_id=user.id,
                    idempotency_key=str(uuid4()),
                )
                await session.commit()

                ran_script_job = await scripts_service.run_generate_script_job(
                    session, llm, job_id=enqueued.job.id
                )
                await session.commit()
                results["script_job_status"] = ran_script_job.status
                print(f"[8] real Anthropic script gen    job_status={ran_script_job.status}")

                if ran_script_job.status == "succeeded":
                    detail = await scripts_service.assemble_script_detail(
                        session,
                        project_id=project.id,
                        owner_id=user.id,
                        script_id=enqueued.script.id,
                    )
                    narration = "\n".join(seg.segment.narration_text for seg in detail.segments)
                    results["script_text"] = narration
                    print("    generated script narration:")
                    print("    " + narration.replace("\n", "\n    "))
                else:
                    results["script_error"] = ran_script_job.error
                    print(f"    script job failed: {ran_script_job.error}")
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                await session.rollback()
                results["script_error"] = f"{type(exc).__name__}: {exc}"
                print(f"    SCRIPT GENERATION PHASE ERROR/SKIPPED: {type(exc).__name__}: {exc}")

        except Exception as exc:  # noqa: BLE001 - top-level: report, then still clean up
            print(f"SMOKE TEST ABORTED: {type(exc).__name__}: {exc}")
            results["fatal_error"] = f"{type(exc).__name__}: {exc}"
            await session.rollback()

        # --- Cleanup: reuse existing delete paths, never invent new ones -------
        cleanup_ok = True
        try:
            if source is not None:
                await storage.delete_object(bucket=source.storage_bucket, key=source.storage_key)
            if project is not None and user is not None:
                # Cascades (ON DELETE CASCADE, migrations 0003/0004/0006/0007) to
                # jobs, uploaded_sources, source_chunks, claim_verifications
                # (+citations), scripts (+segments/+claims) for this project.
                await projects_service.delete_project(
                    session, project_id=project.id, owner_id=user.id
                )
            if user is not None:
                # No users_repo.delete() exists; this is the smallest reuse of
                # the existing table definition rather than a new production
                # function, and only ever targets this run's own temp user.
                await session.execute(delete(users_table).where(users_table.c.id == user.id))
            await session.commit()
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            cleanup_ok = False
            await session.rollback()
            print(f"CLEANUP ERROR: {type(exc).__name__}: {exc}")
        results["cleanup_ok"] = cleanup_ok

    await dispose_engine()

    print()
    print("=== SUMMARY ===")
    for key, value in results.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    asyncio.run(main())

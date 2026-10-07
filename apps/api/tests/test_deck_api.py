# ruff: noqa: F811 - pytest fixtures imported from test_uploaded_sources_api
"""A PowerPoint deck through the real API and the real worker, from upload to an approved script.

Nothing here talks to a provider: ``LLM._create`` and ``Embeddings._embed_batch`` are replaced with
functions that fail the test, so the journey passing proves a deck project is never sent to a model
or an embeddings service - the claim the deck engine makes. Everything else is real: the upload
route, the Postgres tables, the job queue, ``process_one_job`` and the services behind it.

Same no-real-Clerk pattern as ``test_uploaded_sources_api.py`` (whose fixtures are reused).
"""

from __future__ import annotations

import io
import zipfile
from uuid import UUID, uuid4

import pytest
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from edutoon.domain.storyboard_provenance import resolve_provenance
from edutoon.domain.storyboard_template import validate_scene_content
from edutoon.providers.embeddings import Embeddings
from edutoon.providers.llm import LLM, StoryboardSegmentBrief
from edutoon.providers.storage import Storage
from edutoon.repositories import jobs as jobs_repo
from edutoon.repositories import scripts as scripts_repo
from edutoon.services import deck_lesson
from edutoon.worker.dispatch import WorkerProviders
from edutoon.worker.runner import process_one_job
from tests.pdf_helpers import make_pdf
from tests.pptx_helpers import BLANK, SlideSpec, make_deck, sample_training_deck
from tests.test_uploaded_sources_api import (  # noqa: F401 - fixtures
    auth_headers,
    client_factory,
    rsa_keypair,
)

# Stands in for the provider boundary: these tests prove no provider is reached, not how one
# behaves.
pytestmark = pytest.mark.provider_mocked

PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def _key() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid4())}


@pytest.fixture
def no_provider(monkeypatch) -> None:
    async def _refuse(*_args, **_kwargs):
        raise AssertionError("A deck project must never reach a model or embeddings provider.")

    monkeypatch.setattr(LLM, "_create", _refuse)
    monkeypatch.setattr(Embeddings, "_embed_batch", _refuse)


@pytest.fixture
async def worker_sessionmaker(db_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    connection = await db_session.connection()
    return async_sessionmaker(
        bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )


def _providers() -> WorkerProviders:
    llm = LLM(AsyncAnthropic(api_key="sk-ant-test-not-a-real-key"), model="claude-sonnet-5")
    return WorkerProviders(
        embeddings=Embeddings(
            AsyncOpenAI(api_key="sk-test-not-a-real-key"), model="text-embedding-3-small"
        ),
        llm=llm,
        script_llm=llm,
        storage=Storage(client=None),
        voices_dir=None,  # type: ignore[arg-type]  # narration is not run here
    )


async def _project(client, headers, *, target_ms: int = 300_000) -> str:
    response = await client.post(
        "/v1/projects",
        json={"title": "Safety induction", "source_type": "pptx", "target_duration_ms": target_ms},
        headers={**headers, **_key()},
    )
    assert response.status_code == 201, response.text
    assert response.json()["source_type"] == "pptx"
    return response.json()["id"]  # type: ignore[no-any-return]


async def _upload(client, headers, project_id, content, *, filename="induction.pptx", mime=PPTX):
    return await client.post(
        f"/v1/projects/{project_id}/sources",
        files={"file": (filename, content, mime)},
        headers={**headers, **_key()},
    )


async def _run_one_job(db_session, worker_sessionmaker, job_id: str):
    assert await process_one_job(worker_sessionmaker, _providers()) is True
    job = await jobs_repo.get_by_id(db_session, UUID(job_id))
    assert job is not None
    return job


# --- upload ------------------------------------------------------------------------------


async def test_a_deck_is_parsed_into_one_chunk_per_slide_and_needs_no_indexing(
    client_factory, auth_headers, db_session, no_provider
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _project(client, headers)
        response = await _upload(client, headers, project_id, sample_training_deck())
        assert response.status_code == 201, response.text
        source = response.json()
        assert source["kind"] == "pptx" and source["content_type"] == PPTX
        assert source["status"] == "parsed"
        assert source["page_count"] == 13  # slides in the deck, the hidden one included
        assert source["failure_reason"] is None

        chunks = (
            await client.get(
                f"/v1/projects/{project_id}/sources/{source['id']}/chunks", headers=headers
            )
        ).json()["items"]
    assert len(chunks) == 12  # the hidden slide is not read
    assert [c["page_from"] for c in chunks] == list(range(1, 13))  # the deck's own numbering
    assert chunks[3]["content"].startswith("Our Shared Responsibility\n- Safety is everyone's")

    # A deck needs no embeddings, so no indexing job is queued for it.
    assert (
        await jobs_repo.get_by_idempotency_key(
            db_session,
            project_id=UUID(project_id),
            kind="build_evidence",
            idempotency_key=source["id"],
        )
        is None
    )


@pytest.mark.parametrize("mime", ["application/octet-stream", ""])
async def test_a_deck_a_browser_could_not_name_is_accepted_by_its_file_name_and_bytes(
    client_factory, auth_headers, mime
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _project(client, headers)
        response = await _upload(client, headers, project_id, sample_training_deck(), mime=mime)
    assert response.status_code == 201
    assert response.json()["content_type"] == PPTX  # recorded as what it is, not what was claimed


_OLD_BINARY = b"\xd0\xcf\x11\xe0" + b"0" * 300


@pytest.mark.parametrize(
    ("content", "filename", "mime", "message"),
    [
        pytest.param(
            _OLD_BINARY, "old.ppt", "application/vnd.ms-powerpoint", "Older .ppt", id="legacy-type"
        ),
        pytest.param(
            _OLD_BINARY, "old.ppt", "application/octet-stream", "Older .ppt", id="legacy-name"
        ),
        pytest.param(make_pdf(), "fake.pptx", PPTX, "PowerPoint", id="pdf-called-pptx"),
        pytest.param(
            sample_training_deck(), "deck.pdf", "application/pdf", "a PDF", id="deck-called-pdf"
        ),
        pytest.param(
            b"plain text", "notes.txt", "text/plain", "Only PDF and PowerPoint", id="text"
        ),
        pytest.param(sample_training_deck()[:600], "cut.pptx", PPTX, "PowerPoint", id="truncated"),
    ],
)
async def test_a_file_that_is_not_what_it_says_is_refused(
    client_factory, auth_headers, content, filename, mime, message
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _project(client, headers)
        response = await _upload(client, headers, project_id, content, filename=filename, mime=mime)
    assert response.status_code == 422
    assert message in response.text


async def test_a_deck_that_cannot_be_read_is_recorded_as_failed_with_a_reason(
    client_factory, auth_headers
):
    headers = auth_headers()
    # A zip that claims to be a presentation (it has the part) but whose contents are not one.
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("ppt/presentation.xml", "<not-a-presentation/>")
    garbled = buffer.getvalue()
    async with client_factory() as client:
        project_id = await _project(client, headers)
        response = await _upload(client, headers, project_id, garbled)
    assert response.status_code == 201
    assert response.json()["status"] == "failed"
    assert "could not be read" in response.json()["failure_reason"]


async def test_a_deck_of_pictures_has_no_text_and_says_so(client_factory, auth_headers):
    headers = auth_headers()
    pictures = make_deck([SlideSpec(layout=BLANK), SlideSpec(layout=BLANK)])
    async with client_factory() as client:
        project_id = await _project(client, headers)
        response = await _upload(client, headers, project_id, pictures)
    assert response.json()["status"] == "failed"
    assert "No text could be read from this presentation" in response.json()["failure_reason"]


async def test_a_deck_over_the_slide_limit_is_failed_with_the_count(
    client_factory, auth_headers, monkeypatch
):
    monkeypatch.setenv("MAX_DECK_SLIDES", "5")
    from edutoon.core.config import get_settings

    get_settings.cache_clear()
    try:
        headers = auth_headers()
        async with client_factory() as client:
            project_id = await _project(client, headers)
            response = await _upload(client, headers, project_id, sample_training_deck())
        body = response.json()
        assert body["status"] == "failed" and body["page_count"] == 13
        assert "13 slides; the limit is 5" in body["failure_reason"]
    finally:
        monkeypatch.delenv("MAX_DECK_SLIDES")
        get_settings.cache_clear()


# --- the whole journey -------------------------------------------------------------------


async def test_a_deck_becomes_topics_claims_and_a_continuous_script_with_no_provider(
    client_factory, auth_headers, db_session, worker_sessionmaker, no_provider
):
    headers = auth_headers()
    async with client_factory() as client:
        client._transport.app.state.script_llm = _providers().script_llm  # type: ignore[attr-defined]
        project_id = await _project(client, headers)
        await _upload(client, headers, project_id, sample_training_deck())
        base = f"/v1/projects/{project_id}"

        # topics - the deck's own sections, saved accepted, each citing its slides
        queued = await client.post(f"{base}/topics/extraction", headers={**headers, **_key()})
        assert queued.status_code == 202, queued.text
        job = await _run_one_job(db_session, worker_sessionmaker, queued.json()["id"])
        assert job.status == "succeeded", job.error
        assert job.result["llm_model"] == deck_lesson.DECK_ENGINE_MODEL
        topics = (await client.get(f"{base}/topics", headers=headers)).json()["items"]
        assert [t["title"] for t in topics] == ["Why Safety Matters", "In an Emergency"]
        assert {t["status"] for t in topics} == {"accepted"} and {t["origin"] for t in topics} == {
            "source"
        }
        assert [e["page_from"] for e in topics[0]["evidence"]] == [4, 5, 6]

        # claims - every statement, quoted from its slide and cited to it
        queued = await client.post(
            f"{base}/claim-verifications/extraction", headers={**headers, **_key()}
        )
        assert queued.status_code == 202, queued.text
        job = await _run_one_job(db_session, worker_sessionmaker, queued.json()["id"])
        assert job.status == "succeeded", job.error
        claims = (
            await client.get(f"{base}/claim-verifications?limit=100", headers=headers)
        ).json()["items"]
        assert len(claims) == 28
        assert {c["status"] for c in claims} == {"supported"}
        assert {c["llm_model"] for c in claims} == {deck_lesson.DECK_ENGINE_MODEL}
        assert all(len(c["citations"]) == 1 for c in claims)

        # script - a draft that reads as one lesson
        queued = await client.post(f"{base}/scripts", headers={**headers, **_key()})
        assert queued.status_code == 202, queued.text
        script_id = queued.json()["id"]
        job = await _run_one_job(
            db_session,
            worker_sessionmaker,
            (await _job_of_kind(db_session, project_id, "generate_script")),
        )
        assert job.status == "succeeded", job.error
        assert job.result["engine"] == deck_lesson.DECK_ENGINE_MODEL
        detail = (await client.get(f"{base}/scripts/{script_id}", headers=headers)).json()
        assert detail["status"] == "draft" and detail["llm_model"] == deck_lesson.DECK_ENGINE_MODEL
        narration = [s["narration_text"] for s in detail["segments"]]
        assert (
            narration[0].startswith("Welcome. In this lesson")
            and "Thank you for watching" in narration[-1]
        )
        assert len(narration) == 9  # welcome, seven slides with something to say, summary
        # every slide segment cites its claims; the welcome and the summary assert nothing
        assert [len(s["claims"]) for s in detail["segments"]][0] == 0
        assert all(len(s["claims"]) > 0 for s in detail["segments"][1:-1])
        assert 0 < detail["estimated_duration_ms"] < 300_000

        approved = await client.post(
            f"{base}/scripts/{script_id}/approval", headers={**headers, **_key()}
        )
        assert approved.status_code == 200 and approved.json()["status"] == "approved"

    # storyboard - the composer answers every segment with scenes that meet the real contracts
    project_uuid = UUID(project_id)
    job = await jobs_repo.create(
        db_session,
        project_id=project_uuid,
        kind="generate_storyboard",
        payload={},
        idempotency_key=str(uuid4()),
    )
    composer = await deck_lesson.build_storyboard_composer(db_session, job_id=job.id)
    segments = await scripts_repo.list_segments(
        db_session, project_id=project_uuid, script_id=UUID(script_id)
    )
    for segment in segments:
        draft, usage = await composer.compose_storyboard_scenes(
            StoryboardSegmentBrief(
                segment_index=segment.segment_index,
                narration_text=segment.narration_text,
                orientation="horizontal",
            ),
            max_scenes=4,
        )
        assert usage.input_tokens == 0 and usage.output_tokens == 0
        assert 1 <= len(draft.scenes) <= 4
        for scene in draft.scenes:
            validate_scene_content(scene.template, scene.content)
            assert (
                resolve_provenance(scene.template, scene.content, segment.narration_text).fallback
                is None
            )


async def _job_of_kind(db_session, project_id: str, kind: str) -> str:
    job = await jobs_repo.get_active_by_project_kind(
        db_session, project_id=UUID(project_id), kind=kind
    )
    assert job is not None
    return str(job.id)


async def test_a_pdf_project_is_still_routed_to_the_model(db_session):
    """The deck engine is chosen by the project's source type and nothing else."""
    from edutoon.repositories import projects as projects_repo
    from edutoon.repositories import users as users_repo

    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    pdf_project = await projects_repo.create(
        db_session, owner_id=owner.id, title="P", source_type="pdf"
    )
    deck_project = await projects_repo.create(
        db_session, owner_id=owner.id, title="D", source_type="pptx"
    )
    assert not await deck_lesson.project_is_deck(db_session, pdf_project.id)
    assert await deck_lesson.project_is_deck(db_session, deck_project.id)

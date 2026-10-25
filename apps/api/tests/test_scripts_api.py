"""``/v1/projects/{project_id}/scripts`` - Script Generation Engine API
(Phase 7: duration-aware, topic-scoped generation).

Same no-real-Clerk pattern as ``test_projects_api.py`` (tokens signed
locally, ``providers.clerk.get_signing_key`` monkeypatched) and the same
``get_session`` override so nothing here persists past its own rolled-back
transaction.

Generation runs out of band: the endpoint only enqueues a ``generate_script``
job. Where a test needs a script that has actually reached ``draft`` it drives
``services.scripts.run_generate_script_job`` directly with a fake ``LLM`` -
a real ``LLM``/``AsyncAnthropic`` pair with only ``messages.create``
monkeypatched (answering both the Phase 7 outline and section tool calls),
exactly as ``test_worker.py`` does - so no network call is ever made but the
orchestration is genuine.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
from anthropic import AsyncAnthropic
from anthropic.types import Message, ToolUseBlock
from anthropic.types import Usage as AnthropicUsage
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey

from edutoon.api.dependencies import get_current_user
from edutoon.core.config import get_settings
from edutoon.db.session import get_session
from edutoon.main import create_app
from edutoon.providers import clerk
from edutoon.providers.cache import get_redis_client
from edutoon.providers.llm import LLM
from edutoon.repositories import claim_verifications as claim_verifications_repo
from edutoon.repositories import jobs as jobs_repo
from edutoon.repositories import project_topics as project_topics_repo
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import source_chunks as source_chunks_repo
from edutoon.repositories import uploaded_sources as uploaded_sources_repo
from edutoon.repositories import users as users_repo
from edutoon.repositories.claim_verifications import NewClaimCitation
from edutoon.repositories.source_chunks import NewSourceChunk
from edutoon.services import scripts as scripts_service
from tests.idempotency_helpers import INVALID_KEYS, idempotency_status
from tests.storage_isolation import UPLOADS_BUCKET

# Stands in for an external provider (OpenAI / Anthropic): these tests prove how our code
# handles a response, never how a real provider behaves. See "Provider-mocked tests"
# in apps/api/README.md.
pytestmark = pytest.mark.provider_mocked

TEST_ISSUER = "https://test.clerk.accounts.dev"
SCRIPT_MODEL = "claude-sonnet-5"


@pytest.fixture(autouse=True)
def _no_duration_correction(monkeypatch):
    """These tests use short, realistic narration text, far shorter than any
    project's minimum 60-second target - Phase 7's bounded duration-correction
    pass would otherwise fire for every one of them. Disabled here since none
    of these tests are themselves about duration correction (that has its own
    coverage in ``test_services_scripts.py``).
    """
    monkeypatch.setenv("MAX_SCRIPT_CORRECTION_ATTEMPTS", "0")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --- auth plumbing (mirrors test_projects_api.py) -----------------------------------


def _generate_rsa_keypair() -> tuple[bytes, RSAPublicKey]:
    private_key: RSAPrivateKey = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return private_pem, private_key.public_key()


def _make_token(private_pem: bytes, *, sub: str, email: str) -> str:
    now = int(time.time())
    payload = {"sub": sub, "email": email, "iat": now, "exp": now + 3600, "iss": TEST_ISSUER}
    return jwt.encode(payload, private_pem, algorithm="RS256")


@pytest.fixture(scope="module")
def rsa_keypair() -> tuple[bytes, RSAPublicKey]:
    return _generate_rsa_keypair()


@pytest.fixture
def auth_headers(rsa_keypair, monkeypatch) -> Callable[[], dict[str, str]]:
    """Each call mints a fresh, distinct authenticated user's headers."""
    private_pem, public_key = rsa_keypair
    monkeypatch.setattr(clerk, "get_signing_key", lambda token: public_key)

    def _make() -> dict[str, str]:
        token = _make_token(private_pem, sub=f"clerk_{uuid4()}", email=f"{uuid4()}@example.com")
        return {"Authorization": f"Bearer {token}"}

    return _make


@pytest.fixture
def script_llm_model() -> str:
    """The model the app's script client (``app.state.script_llm``) is bound to.
    Override it with ``@pytest.mark.parametrize("script_llm_model", [...])``.
    """
    return get_settings().SCRIPT_LLM_MODEL


def _script_llm(model: str) -> LLM:
    """The client the API queues scripts for - never called by these tests."""
    return LLM(AsyncAnthropic(api_key="sk-ant-test-not-a-real-key"), model=model)


@pytest.fixture
async def client_factory(
    db_session, script_llm_model
) -> AsyncIterator[Callable[[], httpx.AsyncClient]]:
    app = create_app()

    async def _session_override():
        yield db_session

    app.dependency_overrides[get_session] = _session_override
    # ASGITransport never runs the app's lifespan, so app.state.redis - which
    # the idempotency dependency reads - is set here exactly as lifespan()
    # would have set it.
    app.state.redis = get_redis_client(get_settings().REDIS_URL)
    app.state.script_llm = _script_llm(script_llm_model)

    def _make() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    yield _make

    await app.state.redis.aclose()


def _headers(base: dict[str, str], key: str | None = None) -> dict[str, str]:
    return {**base, "Idempotency-Key": key or str(uuid4())}


# --- fixtures for source-backed projects -----------------------------------------


async def _create_project(client: httpx.AsyncClient, headers: dict[str, str]) -> str:
    response = await client.post(
        "/v1/projects",
        json={"title": "How photosynthesis works", "source_type": "pdf"},
        headers=_headers(headers),
    )
    assert response.status_code == 201
    return response.json()["id"]  # type: ignore[no-any-return]


async def _add_supported_claims(db_session, project_id: str | UUID, texts: list[str]):
    """An accepted topic with ``texts`` as its *supported, cited* claim
    verifications, straight through the repositories - the Evidence Engine's
    own flow has its own coverage; here it just needs to be true that the
    project has an accepted topic with something to say. Returns
    ``(topic, claims)``.
    """
    project_id = UUID(str(project_id))
    position = await project_topics_repo.next_position(
        db_session, project_id=project_id, parent_id=None
    )
    topic = await project_topics_repo.create(
        db_session,
        project_id=project_id,
        title="A topic",
        status="accepted",
        origin="source",
        position=position,
    )
    source = await uploaded_sources_repo.create(
        db_session,
        project_id=project_id,
        original_filename="notes.pdf",
        storage_bucket=UPLOADS_BUCKET,
        storage_key=f"{project_id}/{uuid4()}.pdf",
        byte_size=1024,
        checksum_sha256=uuid4().hex + uuid4().hex[:32],
    )
    chunks = await source_chunks_repo.create_many(
        db_session,
        [
            NewSourceChunk(source_id=source.id, project_id=project_id, chunk_index=i, content=text)
            for i, text in enumerate(texts)
        ],
    )
    verifications = []
    for text, chunk in zip(texts, chunks, strict=True):
        verifications.append(
            await claim_verifications_repo.create(
                db_session,
                project_id=project_id,
                claim_text=text,
                status="supported",
                embedding_model="text-embedding-3-small",
                llm_model=SCRIPT_MODEL,
                topic_id=topic.id,
                citations=[NewClaimCitation(source_chunk_id=chunk.id, similarity=0.9)],
            )
        )
    return topic, verifications


def _fake_llm(monkeypatch: Any, topic_id: UUID, segments: list[dict[str, Any]]) -> LLM:
    """An ``LLM`` whose ``messages.create`` answers both Phase 7 calls: the
    outline call (one topic, ``topic_id``) and the section call (``segments``).
    """
    client = AsyncAnthropic(api_key="sk-ant-test-not-a-real-key")

    async def _create(*, tool_choice: dict[str, str], **_kwargs: Any) -> Message:
        name = tool_choice["name"]
        if name == "submit_outline":
            tool_input: dict[str, Any] = {
                "title": "How photosynthesis works",
                "topics": [{"topic_id": str(topic_id), "purpose": "p", "key_concepts": []}],
            }
        elif name == "submit_section":
            tool_input = {"segments": segments}
        else:  # pragma: no cover - a test bug, not a production path
            raise AssertionError(f"unexpected tool_choice: {tool_choice}")
        return Message(
            id="msg_1",
            content=[ToolUseBlock(type="tool_use", id="toolu_1", name=name, input=tool_input)],
            model=SCRIPT_MODEL,
            role="assistant",
            type="message",
            usage=AnthropicUsage(input_tokens=100, output_tokens=200),
        )

    monkeypatch.setattr(client.messages, "create", AsyncMock(side_effect=_create))
    return LLM(client, model=SCRIPT_MODEL)


async def _run_pending_job(db_session, project_id: str, llm: LLM) -> None:
    """Drive the project's queued ``generate_script`` job to completion."""
    page = await jobs_repo.list_by_project(db_session, UUID(project_id))
    [job] = [j for j in page.items if j.kind == "generate_script" and j.status == "queued"]
    await scripts_service.run_generate_script_job(db_session, llm, job_id=job.id)


async def _drafted_project(
    client: httpx.AsyncClient, db_session, headers: dict[str, str], monkeypatch
) -> tuple[str, str, list[Any]]:
    """A project whose script has been generated and is sitting at ``draft``.
    Returns ``(project_id, script_id, claims)``.
    """
    project_id = await _create_project(client, headers)
    topic, claims = await _add_supported_claims(
        db_session,
        project_id,
        ["Plants convert light to chemical energy.", "Chlorophyll absorbs light."],
    )
    enqueue = await client.post(f"/v1/projects/{project_id}/scripts", headers=_headers(headers))
    assert enqueue.status_code == 202
    script_id = enqueue.json()["id"]

    llm = _fake_llm(
        monkeypatch,
        topic.id,
        [
            {"narration_text": "Here is how it works.", "claim_ids": [], "connective": True},
            {
                "narration_text": "Plants turn sunlight into the energy they need.",
                "claim_ids": [str(claims[0].id)],
            },
            {
                "narration_text": "Chlorophyll is the pigment that captures that light.",
                "claim_ids": [str(claims[1].id)],
            },
        ],
    )
    await _run_pending_job(db_session, project_id, llm)
    return project_id, script_id, claims


# --- POST: enqueue ---------------------------------------------------------------


async def test_enqueue_returns_202_with_a_pending_script(client_factory, auth_headers, db_session):
    headers = auth_headers()

    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        await _add_supported_claims(db_session, project_id, ["A verified fact."])

        response = await client.post(
            f"/v1/projects/{project_id}/scripts", headers=_headers(headers)
        )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending"
    assert body["project_id"] == project_id
    assert body["llm_model"] == get_settings().SCRIPT_LLM_MODEL
    assert body["estimated_duration_ms"] is None


async def test_enqueue_makes_the_project_processing(client_factory, auth_headers, db_session):
    headers = auth_headers()

    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        await _add_supported_claims(db_session, project_id, ["A verified fact."])
        await client.post(f"/v1/projects/{project_id}/scripts", headers=_headers(headers))

        project = await client.get(f"/v1/projects/{project_id}", headers=headers)

    assert project.json()["status"] == "processing"


@pytest.mark.parametrize("script_llm_model", ["script-model-y"])
async def test_enqueue_queues_the_script_for_the_script_clients_model(
    client_factory, auth_headers, db_session
):
    """The model comes from the API's script client (``SCRIPT_LLM_MODEL``), not from
    whichever client judges claims - the two are bound to different models here.
    """
    headers = auth_headers()

    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        await _add_supported_claims(db_session, project_id, ["A verified fact."])
        response = await client.post(
            f"/v1/projects/{project_id}/scripts", headers=_headers(headers)
        )

    assert response.status_code == 202
    assert response.json()["llm_model"] == "script-model-y"
    assert response.json()["llm_model"] != get_settings().LLM_MODEL


async def test_enqueue_requires_a_supported_claim(client_factory, auth_headers):
    headers = auth_headers()

    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        response = await client.post(
            f"/v1/projects/{project_id}/scripts", headers=_headers(headers)
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"


async def test_enqueue_requires_an_idempotency_key(client_factory, auth_headers, db_session):
    headers = auth_headers()

    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        await _add_supported_claims(db_session, project_id, ["A verified fact."])

        response = await client.post(f"/v1/projects/{project_id}/scripts", headers=headers)

    assert response.status_code == 422


async def test_enqueue_rejects_unknown_body_fields(client_factory, auth_headers, db_session):
    headers = auth_headers()

    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        await _add_supported_claims(db_session, project_id, ["A verified fact."])

        response = await client.post(
            f"/v1/projects/{project_id}/scripts",
            json={"llm_model": "claude-opus-5"},
            headers=_headers(headers),
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_enqueue_replays_on_a_repeated_key(client_factory, auth_headers, db_session):
    headers = auth_headers()

    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        await _add_supported_claims(db_session, project_id, ["A verified fact."])
        key = str(uuid4())

        first = await client.post(
            f"/v1/projects/{project_id}/scripts", headers=_headers(headers, key)
        )
        second = await client.post(
            f"/v1/projects/{project_id}/scripts", headers=_headers(headers, key)
        )

    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    page = await jobs_repo.list_by_project(db_session, UUID(project_id))
    assert len([j for j in page.items if j.kind == "generate_script"]) == 1


async def test_enqueuing_again_does_not_touch_a_still_pending_script(
    client_factory, auth_headers, db_session
):
    """Phase 7 (migration 0013): a ``pending`` placeholder is no longer
    "active", so queuing a second generation must not supersede the first
    one before it has even run - only a *successful* attach may.
    """
    headers = auth_headers()

    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        await _add_supported_claims(db_session, project_id, ["A verified fact."])

        first = await client.post(f"/v1/projects/{project_id}/scripts", headers=_headers(headers))
        second = await client.post(f"/v1/projects/{project_id}/scripts", headers=_headers(headers))

        listed = await client.get(f"/v1/projects/{project_id}/scripts", headers=headers)

    first_id, second_id = first.json()["id"], second.json()["id"]
    assert first_id != second_id
    by_id = {item["id"]: item["status"] for item in listed.json()["items"]}
    assert by_id[first_id] == "pending"
    assert by_id[second_id] == "pending"


async def test_completing_a_regeneration_supersedes_the_previous_draft(
    client_factory, auth_headers, db_session, monkeypatch
):
    headers = auth_headers()

    async with client_factory() as client:
        project_id, first_script_id, claims = await _drafted_project(
            client, db_session, headers, monkeypatch
        )
        # Re-enqueue for the *same* accepted topic - it already has supported
        # evidence, so nothing more needs adding.
        second = await client.post(f"/v1/projects/{project_id}/scripts", headers=_headers(headers))
        assert second.status_code == 202
        llm = _fake_llm(
            monkeypatch,
            claims[0].topic_id,
            [
                {
                    "narration_text": "Plants need light to make energy.",
                    "claim_ids": [str(claims[0].id)],
                }
            ],
        )
        await _run_pending_job(db_session, project_id, llm)

        listed = await client.get(f"/v1/projects/{project_id}/scripts", headers=headers)

    by_id = {item["id"]: item["status"] for item in listed.json()["items"]}
    assert by_id[first_script_id] == "superseded"
    assert by_id[second.json()["id"]] == "draft"


# --- GET: list -----------------------------------------------------------------


async def test_list_is_empty_for_a_project_with_no_scripts(client_factory, auth_headers):
    headers = auth_headers()

    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        response = await client.get(f"/v1/projects/{project_id}/scripts", headers=headers)

    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


async def test_list_returns_newest_first(client_factory, auth_headers, db_session, monkeypatch):
    headers = auth_headers()

    async with client_factory() as client:
        project_id, script_id, _claims = await _drafted_project(
            client, db_session, headers, monkeypatch
        )
        # a second generation supersedes the drafted one
        second = await client.post(f"/v1/projects/{project_id}/scripts", headers=_headers(headers))

        listed = await client.get(f"/v1/projects/{project_id}/scripts", headers=headers)

    items = listed.json()["items"]
    assert [item["id"] for item in items] == [second.json()["id"], script_id]


# --- GET: detail -------------------------------------------------------------


async def test_detail_shows_segments_and_their_cited_claims(
    client_factory, auth_headers, db_session, monkeypatch
):
    headers = auth_headers()

    async with client_factory() as client:
        project_id, script_id, claims = await _drafted_project(
            client, db_session, headers, monkeypatch
        )
        response = await client.get(
            f"/v1/projects/{project_id}/scripts/{script_id}", headers=headers
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "draft"
    assert body["estimated_duration_ms"] > 0
    assert [s["segment_index"] for s in body["segments"]] == [0, 1, 2]
    # the connective opening segment cites nothing
    assert body["segments"][0]["claims"] == []
    # the fact-bearing segments cite the claim they were built from
    assert [c["id"] for c in body["segments"][1]["claims"]] == [str(claims[0].id)]
    assert body["segments"][1]["claims"][0]["status"] == "supported"
    assert body["segments"][1]["claims"][0]["claim_text"] == (
        "Plants convert light to chemical energy."
    )


async def test_detail_for_an_unknown_script_is_404(client_factory, auth_headers):
    headers = auth_headers()

    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        response = await client.get(f"/v1/projects/{project_id}/scripts/{uuid4()}", headers=headers)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# --- POST: approval --------------------------------------------------------------


async def test_approve_advances_a_draft_to_approved(
    client_factory, auth_headers, db_session, monkeypatch
):
    headers = auth_headers()

    async with client_factory() as client:
        project_id, script_id, _claims = await _drafted_project(
            client, db_session, headers, monkeypatch
        )
        response = await client.post(
            f"/v1/projects/{project_id}/scripts/{script_id}/approval",
            headers=_headers(headers),
        )

    assert response.status_code == 200
    assert response.json()["status"] == "approved"


async def test_approve_rejects_a_script_that_is_not_a_draft(
    client_factory, auth_headers, db_session
):
    headers = auth_headers()

    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        await _add_supported_claims(db_session, project_id, ["A verified fact."])
        enqueue = await client.post(f"/v1/projects/{project_id}/scripts", headers=_headers(headers))
        script_id = enqueue.json()["id"]  # still 'pending'

        response = await client.post(
            f"/v1/projects/{project_id}/scripts/{script_id}/approval",
            headers=_headers(headers),
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


async def test_approve_requires_an_idempotency_key(
    client_factory, auth_headers, db_session, monkeypatch
):
    headers = auth_headers()

    async with client_factory() as client:
        project_id, script_id, _claims = await _drafted_project(
            client, db_session, headers, monkeypatch
        )
        response = await client.post(
            f"/v1/projects/{project_id}/scripts/{script_id}/approval", headers=headers
        )

    assert response.status_code == 422


async def test_approve_replays_on_a_repeated_key(
    client_factory, auth_headers, db_session, monkeypatch
):
    headers = auth_headers()

    async with client_factory() as client:
        project_id, script_id, _claims = await _drafted_project(
            client, db_session, headers, monkeypatch
        )
        key = str(uuid4())
        first = await client.post(
            f"/v1/projects/{project_id}/scripts/{script_id}/approval",
            headers=_headers(headers, key),
        )
        second = await client.post(
            f"/v1/projects/{project_id}/scripts/{script_id}/approval",
            headers=_headers(headers, key),
        )

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()


async def test_approve_unknown_script_is_404(client_factory, auth_headers):
    headers = auth_headers()

    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        response = await client.post(
            f"/v1/projects/{project_id}/scripts/{uuid4()}/approval",
            headers=_headers(headers),
        )

    assert response.status_code == 404


# --- ownership isolation (rule 9: not-owned -> 404, never 403) --------------------


async def test_another_users_project_is_404_everywhere(
    client_factory, auth_headers, db_session, monkeypatch
):
    owner_headers = auth_headers()
    other_headers = auth_headers()

    async with client_factory() as client:
        project_id, script_id, _claims = await _drafted_project(
            client, db_session, owner_headers, monkeypatch
        )

        enqueue = await client.post(
            f"/v1/projects/{project_id}/scripts", headers=_headers(other_headers)
        )
        listed = await client.get(f"/v1/projects/{project_id}/scripts", headers=other_headers)
        detail = await client.get(
            f"/v1/projects/{project_id}/scripts/{script_id}", headers=other_headers
        )
        approval = await client.post(
            f"/v1/projects/{project_id}/scripts/{script_id}/approval",
            headers=_headers(other_headers),
        )

    assert enqueue.status_code == 404
    assert listed.status_code == 404
    assert detail.status_code == 404
    assert approval.status_code == 404
    for response in (enqueue, listed, detail, approval):
        assert response.json()["error"]["code"] == "not_found"


async def test_a_script_from_another_project_is_404(
    client_factory, auth_headers, db_session, monkeypatch
):
    """The caller owns both projects, but project A's script must not be
    reachable under project B's path.
    """
    headers = auth_headers()

    async with client_factory() as client:
        project_a, script_a, _claims = await _drafted_project(
            client, db_session, headers, monkeypatch
        )
        project_b = await _create_project(client, headers)

        detail = await client.get(f"/v1/projects/{project_b}/scripts/{script_a}", headers=headers)
        approval = await client.post(
            f"/v1/projects/{project_b}/scripts/{script_a}/approval",
            headers=_headers(headers),
        )

    assert detail.status_code == 404
    assert approval.status_code == 404


async def test_enqueue_to_a_nonexistent_project_is_404(client_factory, auth_headers):
    async with client_factory() as client:
        response = await client.post(
            f"/v1/projects/{uuid4()}/scripts", headers=_headers(auth_headers())
        )

    assert response.status_code == 404


# --- idempotency: genuine concurrent duplicate -> 409 ----------------------------


async def test_concurrent_enqueue_duplicate_returns_409(db_session):
    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    project = await projects_repo.create(
        db_session, owner_id=owner.id, title="P", source_type="pdf"
    )
    await _add_supported_claims(db_session, str(project.id), ["A verified fact."])

    app = create_app()

    async def _session_override():
        yield db_session

    app.dependency_overrides[get_session] = _session_override
    app.dependency_overrides[get_current_user] = lambda: owner
    app.state.redis = get_redis_client(get_settings().REDIS_URL)
    app.state.script_llm = _script_llm(get_settings().SCRIPT_LLM_MODEL)

    path = f"/v1/projects/{project.id}/scripts"
    headers = {"Idempotency-Key": str(uuid4())}

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            first, second = await asyncio.gather(
                client.post(path, headers=headers),
                client.post(path, headers=headers),
            )
    finally:
        await app.state.redis.aclose()

    assert sorted((first.status_code, second.status_code)) == [202, 409]


# --- the Idempotency-Key header, and durability before the response --------------------


async def _ready_to_post(route: str, client, db_session, headers, monkeypatch) -> str:
    """The path of a mutating route with everything it needs already in place:
    for ``enqueue`` a project with a verified claim; for ``approve`` a drafted script.
    """
    if route == "enqueue":
        project_id = await _create_project(client, headers)
        await _add_supported_claims(db_session, project_id, ["A verified fact."])
        return f"/v1/projects/{project_id}/scripts"
    project_id, script_id, _claims = await _drafted_project(
        client, db_session, headers, monkeypatch
    )
    return f"/v1/projects/{project_id}/scripts/{script_id}/approval"


@pytest.mark.parametrize("route", ["enqueue", "approve"])
async def test_a_mutation_is_committed_before_its_result_is_cached_or_returned(
    client_factory, auth_headers, db_session, redis_client, monkeypatch, route
):
    """The route commits for itself, before anything is cached: at the moment it does,
    the dedupe record must still say "in_progress" - a cached "completed" would promise
    work that a failed commit could still lose.
    """
    headers = auth_headers()
    async with client_factory() as client:
        path = await _ready_to_post(route, client, db_session, headers, monkeypatch)
        key = str(uuid4())
        seen_at_commit: list[str | None] = []
        real_commit = db_session.commit

        async def _spying_commit() -> None:
            seen_at_commit.append(await idempotency_status(redis_client, path, key))
            await real_commit()

        monkeypatch.setattr(db_session, "commit", _spying_commit)
        response = await client.post(path, headers=_headers(headers, key))

    assert response.status_code in (200, 202), response.text
    assert seen_at_commit == ["in_progress"]
    assert await idempotency_status(redis_client, path, key) == "completed"


@pytest.mark.parametrize("route", ["enqueue", "approve"])
async def test_a_commit_that_fails_leaves_no_cached_success_behind(
    client_factory, auth_headers, db_session, redis_client, monkeypatch, route
):
    headers = auth_headers()
    async with client_factory() as client:
        path = await _ready_to_post(route, client, db_session, headers, monkeypatch)
        key = str(uuid4())

        async def _failing_commit() -> None:
            raise RuntimeError("the database went away")

        monkeypatch.setattr(db_session, "commit", _failing_commit)
        with pytest.raises(RuntimeError, match="went away"):
            await client.post(path, headers=_headers(headers, key))

    # Nothing says the work was done, and the key is free for the client's retry.
    assert await idempotency_status(redis_client, path, key) is None


@pytest.mark.parametrize("route", ["enqueue", "approve"])
@pytest.mark.parametrize("key", INVALID_KEYS)
async def test_an_empty_or_oversized_idempotency_key_is_rejected_and_nothing_is_written(
    client_factory, auth_headers, db_session, monkeypatch, route, key
):
    headers = auth_headers()
    async with client_factory() as client:
        path = await _ready_to_post(route, client, db_session, headers, monkeypatch)
        project_path = path.split("/scripts")[0]
        scripts_before = (await client.get(f"{project_path}/scripts", headers=headers)).json()
        jobs_before = (await client.get(f"{project_path}/jobs", headers=headers)).json()

        response = await client.post(path, headers={**headers, "Idempotency-Key": key})

        assert response.status_code == 422
        [error] = response.json()["error"]["details"]["field_errors"]
        assert error["path"] == "header.idempotency-key"
        scripts_after = (await client.get(f"{project_path}/scripts", headers=headers)).json()
        jobs_after = (await client.get(f"{project_path}/jobs", headers=headers)).json()
        assert (scripts_after, jobs_after) == (scripts_before, jobs_before)


async def test_an_idempotency_key_of_the_maximum_length_is_accepted(
    client_factory, auth_headers, db_session
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        await _add_supported_claims(db_session, project_id, ["A verified fact."])
        response = await client.post(
            f"/v1/projects/{project_id}/scripts", headers=_headers(headers, "k" * 255)
        )

    assert response.status_code == 202, response.text

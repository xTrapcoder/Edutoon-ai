"""``/v1/projects/{project_id}/scripts`` - Script Generation Engine API (SG-5).

Same no-real-Clerk pattern as ``test_projects_api.py`` (tokens signed
locally, ``providers.clerk.get_signing_key`` monkeypatched) and the same
``get_session`` override so nothing here persists past its own rolled-back
transaction.

Generation runs out of band: the endpoint only enqueues a ``generate_script``
job. Where a test needs a script that has actually reached ``draft`` it drives
``services.scripts.run_generate_script_job`` directly with a fake ``LLM`` -
a real ``LLM``/``AsyncAnthropic`` pair with only ``messages.create``
monkeypatched, exactly as ``test_services_scripts.py`` does - so no network
call is ever made but the orchestration is genuine.
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
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import source_chunks as source_chunks_repo
from edutoon.repositories import uploaded_sources as uploaded_sources_repo
from edutoon.repositories import users as users_repo
from edutoon.repositories.claim_verifications import NewClaimCitation
from edutoon.repositories.source_chunks import NewSourceChunk
from edutoon.services import scripts as scripts_service

TEST_ISSUER = "https://test.clerk.accounts.dev"
SCRIPT_MODEL = "claude-sonnet-5"


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
async def client_factory(db_session) -> AsyncIterator[Callable[[], httpx.AsyncClient]]:
    app = create_app()

    async def _session_override():
        yield db_session

    app.dependency_overrides[get_session] = _session_override
    # ASGITransport never runs the app's lifespan, so app.state.redis - which
    # the idempotency dependency reads - is set here exactly as lifespan()
    # would have set it.
    app.state.redis = get_redis_client(get_settings().REDIS_URL)

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
    """Insert ``texts`` as *supported, cited* claim verifications on the
    project, straight through the repositories - the Evidence Engine's own
    flow has its own coverage; here it just needs to be true that the
    project has something to say.
    """
    project_id = UUID(str(project_id))
    source = await uploaded_sources_repo.create(
        db_session,
        project_id=project_id,
        original_filename="notes.pdf",
        storage_bucket="edutoon-uploads",
        storage_key=f"{project_id}/{uuid4()}.pdf",
        byte_size=1024,
        checksum_sha256=uuid4().hex + uuid4().hex[:32],
    )
    chunks = await source_chunks_repo.create_many(
        db_session,
        [
            NewSourceChunk(
                source_id=source.id, project_id=project_id, chunk_index=i, content=text
            )
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
                citations=[NewClaimCitation(source_chunk_id=chunk.id, similarity=0.9)],
            )
        )
    return verifications


def _fake_llm(monkeypatch: Any, segments: list[dict[str, Any]]) -> LLM:
    client = AsyncAnthropic(api_key="sk-ant-test-not-a-real-key")
    message = Message(
        id="msg_1",
        content=[
            ToolUseBlock(
                type="tool_use",
                id="toolu_1",
                name="submit_script",
                input={"title": "How photosynthesis works", "segments": segments},
            )
        ],
        model=SCRIPT_MODEL,
        role="assistant",
        type="message",
        usage=AnthropicUsage(input_tokens=100, output_tokens=200),
    )
    monkeypatch.setattr(client.messages, "create", AsyncMock(return_value=message))
    return LLM(client, model=SCRIPT_MODEL)


async def _run_pending_job(db_session, project_id: str, llm: LLM) -> None:
    """Drive the project's queued ``generate_script`` job to completion."""
    page = await jobs_repo.list_by_project(db_session, UUID(project_id))
    [job] = [j for j in page.items if j.kind == "generate_script"]
    await scripts_service.run_generate_script_job(db_session, llm, job_id=job.id)


async def _drafted_project(
    client: httpx.AsyncClient, db_session, headers: dict[str, str], monkeypatch
) -> tuple[str, str, list[Any]]:
    """A project whose script has been generated and is sitting at ``draft``.
    Returns ``(project_id, script_id, claims)``.
    """
    project_id = await _create_project(client, headers)
    claims = await _add_supported_claims(
        db_session,
        project_id,
        ["Plants convert light to chemical energy.", "Chlorophyll absorbs light."],
    )
    enqueue = await client.post(
        f"/v1/projects/{project_id}/scripts", headers=_headers(headers)
    )
    assert enqueue.status_code == 202
    script_id = enqueue.json()["id"]

    llm = _fake_llm(
        monkeypatch,
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


async def test_enqueue_supersedes_the_previous_script(client_factory, auth_headers, db_session):
    headers = auth_headers()

    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        await _add_supported_claims(db_session, project_id, ["A verified fact."])

        first = await client.post(
            f"/v1/projects/{project_id}/scripts", headers=_headers(headers)
        )
        second = await client.post(
            f"/v1/projects/{project_id}/scripts", headers=_headers(headers)
        )

        listed = await client.get(f"/v1/projects/{project_id}/scripts", headers=headers)

    first_id, second_id = first.json()["id"], second.json()["id"]
    assert first_id != second_id
    by_id = {item["id"]: item["status"] for item in listed.json()["items"]}
    assert by_id[first_id] == "superseded"
    assert by_id[second_id] == "pending"


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
        second = await client.post(
            f"/v1/projects/{project_id}/scripts", headers=_headers(headers)
        )

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
        response = await client.get(
            f"/v1/projects/{project_id}/scripts/{uuid4()}", headers=headers
        )

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
        enqueue = await client.post(
            f"/v1/projects/{project_id}/scripts", headers=_headers(headers)
        )
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
        listed = await client.get(
            f"/v1/projects/{project_id}/scripts", headers=other_headers
        )
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

        detail = await client.get(
            f"/v1/projects/{project_b}/scripts/{script_a}", headers=headers
        )
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

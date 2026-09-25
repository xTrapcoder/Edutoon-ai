"""``/v1/projects/{project_id}/topics`` - the Topic Understanding API (Phase 5,
Step 6).

Same no-real-Clerk pattern as ``test_scripts_api.py`` (tokens signed locally,
``providers.clerk.get_signing_key`` monkeypatched) and the same ``get_session``
override, so nothing here persists past its own rolled-back transaction.

Extraction runs out of band: ``POST /topics/extraction`` only enqueues a job.
Where a test needs topics that an extraction really produced it drives
``services.topic_extraction.run_extract_topics_job`` with a real ``LLM`` around a
real ``AsyncAnthropic`` whose ``messages.create`` alone is replaced - no network
call is ever made, but the whole pipeline behind the endpoints is genuine.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
from anthropic import AsyncAnthropic
from anthropic.types import Message, ToolUseBlock, Usage
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey

from edutoon.core.config import get_settings
from edutoon.db.session import get_session
from edutoon.main import create_app
from edutoon.providers import clerk
from edutoon.providers.cache import get_redis_client
from edutoon.providers.llm import LLM
from edutoon.repositories import jobs as jobs_repo
from edutoon.repositories import project_topics as project_topics_repo
from edutoon.repositories import source_chunks as source_chunks_repo
from edutoon.repositories import topic_citations as topic_citations_repo
from edutoon.repositories import uploaded_sources as uploaded_sources_repo
from edutoon.repositories.source_chunks import NewSourceChunk
from edutoon.services import audit_logs as audit_logs_service
from edutoon.services import topic_extraction as topic_extraction_service

TEST_ISSUER = "https://test.clerk.accounts.dev"
MODEL = "claude-sonnet-5"


# --- auth plumbing (mirrors test_scripts_api.py) ----------------------------------------


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
    # ASGITransport never runs the app's lifespan, so app.state.redis - which the
    # idempotency dependency reads - is set here exactly as lifespan() would.
    app.state.redis = get_redis_client(get_settings().REDIS_URL)

    def _make() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    yield _make

    await app.state.redis.aclose()


def _headers(base: dict[str, str], key: str | None = None) -> dict[str, str]:
    return {**base, "Idempotency-Key": key or str(uuid4())}


# --- builders ---------------------------------------------------------------------------


async def _create_project(client: httpx.AsyncClient, headers: dict[str, str]) -> str:
    response = await client.post(
        "/v1/projects",
        json={"title": "How photosynthesis works", "source_type": "pdf"},
        headers=_headers(headers),
    )
    assert response.status_code == 201
    return response.json()["id"]  # type: ignore[no-any-return]


async def _add_keywords(
    client: httpx.AsyncClient, headers: dict[str, str], project_id: str, keywords: list[str]
) -> list[dict[str, Any]]:
    response = await client.post(
        f"/v1/projects/{project_id}/topics",
        json={"keywords": keywords},
        headers=_headers(headers),
    )
    assert response.status_code == 201, response.text
    return response.json()["items"]  # type: ignore[no-any-return]


async def _add_chunks(db_session, project_id: str, texts: list[str]):
    """One parsed source with one page-sized chunk per text, in page order."""
    pid = UUID(project_id)
    source = await uploaded_sources_repo.create(
        db_session,
        project_id=pid,
        original_filename="notes.pdf",
        storage_bucket="edutoon-uploads",
        storage_key=f"{pid}/{uuid4()}.pdf",
        byte_size=1024,
        checksum_sha256=uuid4().hex + uuid4().hex[:32],
    )
    chunks = await source_chunks_repo.create_many(
        db_session,
        [
            NewSourceChunk(
                source_id=source.id,
                project_id=pid,
                chunk_index=i,
                content=text,
                page_from=i + 1,
                page_to=i + 1,
            )
            for i, text in enumerate(texts)
        ],
    )
    return source, sorted(chunks, key=lambda chunk: chunk.chunk_index)


def _fake_llm(monkeypatch: Any, topics: list[dict[str, Any]]) -> LLM:
    client = AsyncAnthropic(api_key="sk-ant-test-not-a-real-key")
    message = Message(
        id="msg_1",
        content=[
            ToolUseBlock(
                type="tool_use", id="toolu_1", name="submit_topics", input={"topics": topics}
            )
        ],
        model=MODEL,
        role="assistant",
        type="message",
        usage=Usage(input_tokens=100, output_tokens=200),
    )
    monkeypatch.setattr(client.messages, "create", AsyncMock(return_value=message))
    return LLM(client, model=MODEL)


async def _run_pending_extraction(db_session, project_id: str, llm: LLM):
    page = await jobs_repo.list_by_project(db_session, UUID(project_id))
    [job] = [j for j in page.items if j.kind == "extract_topics" and j.status == "queued"]
    return await topic_extraction_service.run_extract_topics_job(db_session, llm, job_id=job.id)


async def _audit_actions(db_session, project_id: str) -> list[str]:
    page = await audit_logs_service.list_audit_logs_for_project(db_session, UUID(project_id))
    return [entry.action for entry in page.items]


async def _topics(client: httpx.AsyncClient, headers: dict[str, str], project_id: str):
    response = await client.get(f"/v1/projects/{project_id}/topics", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["items"]


# =========================================================================================
# POST /topics - the user's own keywords
# =========================================================================================


async def test_add_keywords_creates_accepted_user_topics_in_order(client_factory, auth_headers):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        response = await client.post(
            f"/v1/projects/{project_id}/topics",
            json={"keywords": ["Photosynthesis", "Chlorophyll", "The Calvin cycle"]},
            headers=_headers(headers),
        )

    assert response.status_code == 201
    items = response.json()["items"]
    assert [item["title"] for item in items] == [
        "Photosynthesis",
        "Chlorophyll",
        "The Calvin cycle",
    ]
    assert [item["position"] for item in items] == [0, 1, 2]
    for item in items:
        assert item["origin"] == "user"
        assert item["status"] == "accepted"
        assert item["parent_id"] is None
        assert item["depth"] == 0
        assert item["keywords"] == []
        assert item["evidence"] == []
        assert item["project_id"] == project_id


async def test_add_keywords_normalises_spacing_and_collapses_case_duplicates(
    client_factory, auth_headers
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        items = await _add_keywords(
            client, headers, project_id, ["  Light   reactions ", "light reactions", "Stomata"]
        )

    assert [item["title"] for item in items] == ["Light reactions", "Stomata"]


async def test_add_keywords_appends_after_existing_roots_including_derived_ones(
    client_factory, auth_headers, db_session
):
    """A later keyword must not take a position an extracted root already holds."""
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        await _add_keywords(client, headers, project_id, ["Photosynthesis"])
        await project_topics_repo.create(
            db_session,
            project_id=UUID(project_id),
            title="Extracted root",
            position=5,
            origin="suggested",
        )
        items = await _add_keywords(client, headers, project_id, ["Respiration"])

    assert items[0]["position"] == 6


async def test_add_keywords_is_recorded_in_the_audit_log(client_factory, auth_headers, db_session):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        await _add_keywords(client, headers, project_id, ["Photosynthesis", "Chlorophyll"])

    page = await audit_logs_service.list_audit_logs_for_project(db_session, UUID(project_id))
    [entry] = [e for e in page.items if e.action == "topics.keywords_added"]
    assert entry.context["count"] == 2
    assert len(entry.context["topic_ids"]) == 2


async def test_add_keywords_requires_an_idempotency_key(client_factory, auth_headers):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        response = await client.post(
            f"/v1/projects/{project_id}/topics", json={"keywords": ["Cells"]}, headers=headers
        )
        assert response.status_code == 422
        assert await _topics(client, headers, project_id) == []


async def test_add_keywords_replays_the_original_response_for_the_same_key(
    client_factory, auth_headers
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        key = str(uuid4())
        first = await client.post(
            f"/v1/projects/{project_id}/topics",
            json={"keywords": ["Photosynthesis"]},
            headers=_headers(headers, key),
        )
        second = await client.post(
            f"/v1/projects/{project_id}/topics",
            json={"keywords": ["Photosynthesis"]},
            headers=_headers(headers, key),
        )
        stored = await _topics(client, headers, project_id)

    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert len(stored) == 1


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"keywords": []},
        {"keywords": "Photosynthesis"},
        {"keywords": [1, 2]},
        {"keywords": [None]},
        {"keywords": ["Cells"], "origin": "source"},
        {"keywords": ["Cells"], "status": "accepted"},
        {"keywords": ["x"] * 201},
        {"keywords": ["x" * 2001]},
    ],
)
async def test_add_keywords_rejects_a_malformed_body_with_422(client_factory, auth_headers, body):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        response = await client.post(
            f"/v1/projects/{project_id}/topics", json=body, headers=_headers(headers)
        )
        assert response.status_code == 422
        assert await _topics(client, headers, project_id) == []


async def test_add_keywords_rejects_a_blank_keyword_and_writes_nothing(
    client_factory, auth_headers
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        response = await client.post(
            f"/v1/projects/{project_id}/topics",
            json={"keywords": ["Photosynthesis", "   "]},
            headers=_headers(headers),
        )
        assert response.status_code == 400
        assert await _topics(client, headers, project_id) == []


async def test_add_keywords_rejects_an_over_long_keyword_and_says_the_limit(
    client_factory, auth_headers
):
    limit = get_settings().MAX_TOPIC_TITLE_CHARS
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        response = await client.post(
            f"/v1/projects/{project_id}/topics",
            json={"keywords": ["ok", "x" * (limit + 1)]},
            headers=_headers(headers),
        )
        assert response.status_code == 400
        assert str(limit) in response.json()["error"]["message"]
        assert await _topics(client, headers, project_id) == []


async def test_add_keywords_rejects_more_than_the_limit_in_one_request(
    client_factory, auth_headers
):
    limit = get_settings().MAX_TOPICS_PER_EXTRACTION
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        response = await client.post(
            f"/v1/projects/{project_id}/topics",
            json={"keywords": [f"Keyword {n}" for n in range(limit + 1)]},
            headers=_headers(headers),
        )
        assert response.status_code == 400
        assert await _topics(client, headers, project_id) == []


async def test_add_keywords_refuses_to_exceed_the_limit_across_requests(
    client_factory, auth_headers
):
    """Beyond the limit every extraction would fail validation, so the API
    refuses the keyword that would cross it - and adds none of the batch.
    """
    limit = get_settings().MAX_TOPICS_PER_EXTRACTION
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        await _add_keywords(client, headers, project_id, [f"Keyword {n}" for n in range(limit - 1)])
        response = await client.post(
            f"/v1/projects/{project_id}/topics",
            json={"keywords": ["One more", "And another"]},
            headers=_headers(headers),
        )
        assert response.status_code == 400
        assert len(await _topics(client, headers, project_id)) == limit - 1


async def test_add_keywords_rejects_a_keyword_the_project_already_has(client_factory, auth_headers):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        await _add_keywords(client, headers, project_id, ["Photosynthesis"])
        response = await client.post(
            f"/v1/projects/{project_id}/topics",
            json={"keywords": ["Chlorophyll", "  photosynthesis "]},
            headers=_headers(headers),
        )
        assert response.status_code == 409
        # Nothing from the batch was written - not even the new keyword.
        assert [t["title"] for t in await _topics(client, headers, project_id)] == [
            "Photosynthesis"
        ]


async def test_add_keywords_is_refused_while_an_extraction_is_in_flight(
    client_factory, auth_headers
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        await _add_keywords(client, headers, project_id, ["Photosynthesis"])
        enqueue = await client.post(
            f"/v1/projects/{project_id}/topics/extraction", headers=_headers(headers)
        )
        assert enqueue.status_code == 202

        response = await client.post(
            f"/v1/projects/{project_id}/topics",
            json={"keywords": ["Respiration"]},
            headers=_headers(headers),
        )
        assert response.status_code == 409
        assert [t["title"] for t in await _topics(client, headers, project_id)] == [
            "Photosynthesis"
        ]


async def test_add_keywords_to_another_users_project_is_404_and_writes_nothing(
    client_factory, auth_headers, db_session
):
    owner, intruder = auth_headers(), auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, owner)
        response = await client.post(
            f"/v1/projects/{project_id}/topics",
            json={"keywords": ["Photosynthesis"]},
            headers=_headers(intruder),
        )
        assert response.status_code == 404
        assert await _topics(client, owner, project_id) == []
    assert await _audit_actions(db_session, project_id) == []


async def test_add_keywords_to_an_unknown_project_is_404(client_factory, auth_headers):
    async with client_factory() as client:
        response = await client.post(
            f"/v1/projects/{uuid4()}/topics",
            json={"keywords": ["Photosynthesis"]},
            headers=_headers(auth_headers()),
        )
    assert response.status_code == 404


async def test_topics_routes_require_authentication(client_factory):
    project_id = uuid4()
    topic_id = uuid4()
    async with client_factory() as client:
        responses = [
            await client.get(f"/v1/projects/{project_id}/topics"),
            await client.post(
                f"/v1/projects/{project_id}/topics",
                json={"keywords": ["Cells"]},
                headers={"Idempotency-Key": "k"},
            ),
            await client.post(
                f"/v1/projects/{project_id}/topics/extraction", headers={"Idempotency-Key": "k"}
            ),
            await client.patch(
                f"/v1/projects/{project_id}/topics/{topic_id}",
                json={"status": "accepted"},
                headers={"Idempotency-Key": "k"},
            ),
        ]
    assert [r.status_code for r in responses] == [401, 401, 401, 401]


# =========================================================================================
# POST /topics/extraction
# =========================================================================================


async def test_enqueue_extraction_returns_202_with_a_queued_job(client_factory, auth_headers):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        await _add_keywords(client, headers, project_id, ["Photosynthesis"])
        response = await client.post(
            f"/v1/projects/{project_id}/topics/extraction", headers=_headers(headers)
        )

    assert response.status_code == 202
    body = response.json()
    assert body["kind"] == "extract_topics"
    assert body["status"] == "queued"
    assert body["project_id"] == project_id
    assert body["payload"] == {"project_id": project_id, "replace": False}
    assert body["result"] is None


async def test_enqueue_extraction_passes_replace_through_to_the_job(
    client_factory, auth_headers, db_session
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        await _add_keywords(client, headers, project_id, ["Photosynthesis"])
        response = await client.post(
            f"/v1/projects/{project_id}/topics/extraction",
            json={"replace": True},
            headers=_headers(headers),
        )

    assert response.status_code == 202
    assert response.json()["payload"]["replace"] is True


@pytest.mark.parametrize("body", [{"replace": "maybe"}, {"replace": False, "model": "x"}, [1]])
async def test_enqueue_extraction_rejects_a_malformed_body_with_422(
    client_factory, auth_headers, body
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        await _add_keywords(client, headers, project_id, ["Photosynthesis"])
        response = await client.post(
            f"/v1/projects/{project_id}/topics/extraction", json=body, headers=_headers(headers)
        )
        assert response.status_code == 422
        jobs = await client.get(f"/v1/projects/{project_id}/jobs", headers=headers)
    assert jobs.json()["items"] == []


async def test_enqueue_extraction_requires_an_idempotency_key(client_factory, auth_headers):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        await _add_keywords(client, headers, project_id, ["Photosynthesis"])
        response = await client.post(
            f"/v1/projects/{project_id}/topics/extraction", headers=headers
        )
    assert response.status_code == 422


async def test_enqueue_extraction_with_nothing_to_extract_from_is_400(client_factory, auth_headers):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        response = await client.post(
            f"/v1/projects/{project_id}/topics/extraction", headers=_headers(headers)
        )
        assert response.status_code == 400
        jobs = await client.get(f"/v1/projects/{project_id}/jobs", headers=headers)
    assert jobs.json()["items"] == []


async def test_enqueue_extraction_replays_the_same_job_for_the_same_key(
    client_factory, auth_headers
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        await _add_keywords(client, headers, project_id, ["Photosynthesis"])
        key = str(uuid4())
        first = await client.post(
            f"/v1/projects/{project_id}/topics/extraction", headers=_headers(headers, key)
        )
        second = await client.post(
            f"/v1/projects/{project_id}/topics/extraction", headers=_headers(headers, key)
        )
        jobs = await client.get(f"/v1/projects/{project_id}/jobs", headers=headers)

    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    assert len(jobs.json()["items"]) == 1


async def test_a_second_extraction_while_one_is_queued_is_409(client_factory, auth_headers):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        await _add_keywords(client, headers, project_id, ["Photosynthesis"])
        first = await client.post(
            f"/v1/projects/{project_id}/topics/extraction", headers=_headers(headers)
        )
        second = await client.post(
            f"/v1/projects/{project_id}/topics/extraction", headers=_headers(headers)
        )
        jobs = await client.get(f"/v1/projects/{project_id}/jobs", headers=headers)

    assert first.status_code == 202
    assert second.status_code == 409
    assert len(jobs.json()["items"]) == 1


async def test_enqueue_extraction_for_another_users_project_is_404(client_factory, auth_headers):
    owner, intruder = auth_headers(), auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, owner)
        await _add_keywords(client, owner, project_id, ["Photosynthesis"])
        response = await client.post(
            f"/v1/projects/{project_id}/topics/extraction", headers=_headers(intruder)
        )
        jobs = await client.get(f"/v1/projects/{project_id}/jobs", headers=owner)
    assert response.status_code == 404
    assert jobs.json()["items"] == []


async def test_a_completed_extraction_needs_replace_to_run_again(
    client_factory, auth_headers, db_session, monkeypatch
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        await _add_keywords(client, headers, project_id, ["Photosynthesis"])
        await client.post(f"/v1/projects/{project_id}/topics/extraction", headers=_headers(headers))
        job = await _run_pending_extraction(
            db_session,
            project_id,
            _fake_llm(
                monkeypatch,
                [
                    {
                        "ref": "a",
                        "title": "Photosynthesis",
                        "summary": "",
                        "keywords": [],
                        "evidence_chunk_ids": [],
                    },
                    {
                        "ref": "b",
                        "title": "Light reactions",
                        "parent_ref": "a",
                        "summary": "",
                        "keywords": [],
                        "evidence_chunk_ids": [],
                    },
                ],
            ),
        )
        assert job.status == "succeeded"

        again = await client.post(
            f"/v1/projects/{project_id}/topics/extraction", headers=_headers(headers)
        )
        with_replace = await client.post(
            f"/v1/projects/{project_id}/topics/extraction",
            json={"replace": True},
            headers=_headers(headers),
        )

    assert again.status_code == 409
    assert with_replace.status_code == 202


# =========================================================================================
# GET /topics
# =========================================================================================


async def test_list_topics_is_empty_for_a_new_project(client_factory, auth_headers):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        response = await client.get(f"/v1/projects/{project_id}/topics", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"items": []}


async def test_list_topics_is_in_reading_order_not_insertion_order(
    client_factory, auth_headers, db_session
):
    """Depth-first, siblings by position - whatever order the rows were made in."""
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        pid = UUID(project_id)
        second = await project_topics_repo.create(
            db_session, project_id=pid, title="Second", position=1
        )
        first = await project_topics_repo.create(
            db_session, project_id=pid, title="First", position=0
        )
        await project_topics_repo.create(
            db_session, project_id=pid, title="Second/b", parent_id=second.id, position=1, depth=1
        )
        await project_topics_repo.create(
            db_session, project_id=pid, title="Second/a", parent_id=second.id, position=0, depth=1
        )
        await project_topics_repo.create(
            db_session, project_id=pid, title="First/a", parent_id=first.id, position=0, depth=1
        )
        items = await _topics(client, headers, project_id)

    assert [item["title"] for item in items] == [
        "First",
        "First/a",
        "Second",
        "Second/a",
        "Second/b",
    ]


async def test_list_topics_shows_evidence_pages_and_leaves_uncited_topics_empty(
    client_factory, auth_headers, db_session
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        source, chunks = await _add_chunks(
            db_session, project_id, ["Plants make sugar from light.", "Chlorophyll is green."]
        )
        cited = await project_topics_repo.create(
            db_session, project_id=UUID(project_id), title="Cited", position=0, origin="source"
        )
        await project_topics_repo.create(
            db_session, project_id=UUID(project_id), title="Bare", position=1, origin="suggested"
        )
        await topic_citations_repo.add_many(
            db_session,
            project_id=UUID(project_id),
            topic_id=cited.id,
            source_chunk_ids=[chunks[1].id, chunks[0].id],
        )
        items = await _topics(client, headers, project_id)

    by_title = {item["title"]: item for item in items}
    assert by_title["Bare"]["evidence"] == []
    # Evidence is in document order, and points at the source and its pages.
    assert by_title["Cited"]["evidence"] == [
        {
            "source_chunk_id": str(chunks[0].id),
            "source_id": str(source.id),
            "page_from": 1,
            "page_to": 1,
        },
        {
            "source_chunk_id": str(chunks[1].id),
            "source_id": str(source.id),
            "page_from": 2,
            "page_to": 2,
        },
    ]


async def test_list_topics_for_another_users_project_is_404(client_factory, auth_headers):
    owner, intruder = auth_headers(), auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, owner)
        await _add_keywords(client, owner, project_id, ["Photosynthesis"])
        response = await client.get(f"/v1/projects/{project_id}/topics", headers=intruder)
    assert response.status_code == 404
    assert "Photosynthesis" not in response.text


async def test_list_topics_only_returns_this_projects_topics(client_factory, auth_headers):
    headers = auth_headers()
    async with client_factory() as client:
        mine = await _create_project(client, headers)
        other = await _create_project(client, headers)
        await _add_keywords(client, headers, mine, ["Mine"])
        await _add_keywords(client, headers, other, ["Theirs"])
        items = await _topics(client, headers, mine)
    assert [item["title"] for item in items] == ["Mine"]


# =========================================================================================
# PATCH /topics/{topic_id}
# =========================================================================================


async def _one_topic(client, headers) -> tuple[str, dict[str, Any]]:
    project_id = await _create_project(client, headers)
    [topic] = await _add_keywords(client, headers, project_id, ["Photosynthesis"])
    return project_id, topic


async def test_patch_status_updates_the_topic_and_is_audited(
    client_factory, auth_headers, db_session
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id, topic = await _one_topic(client, headers)
        response = await client.patch(
            f"/v1/projects/{project_id}/topics/{topic['id']}",
            json={"status": "rejected"},
            headers=_headers(headers),
        )
        stored = await _topics(client, headers, project_id)

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    assert stored[0]["status"] == "rejected"
    page = await audit_logs_service.list_audit_logs_for_project(db_session, UUID(project_id))
    [entry] = [e for e in page.items if e.action == "topics.updated"]
    assert entry.entity_id == UUID(topic["id"])
    assert entry.context == {
        "fields": ["status"],
        "status_from": "accepted",
        "status_to": "rejected",
    }


async def test_patch_can_change_wording_and_clear_the_summary(client_factory, auth_headers):
    headers = auth_headers()
    async with client_factory() as client:
        project_id, topic = await _one_topic(client, headers)
        url = f"/v1/projects/{project_id}/topics/{topic['id']}"
        edited = await client.patch(
            url,
            json={"title": "  Light   reactions ", "summary": "  Where light becomes ATP. "},
            headers=_headers(headers),
        )
        cleared = await client.patch(url, json={"summary": None}, headers=_headers(headers))

    assert edited.status_code == 200
    assert edited.json()["title"] == "Light reactions"
    assert edited.json()["summary"] == "Where light becomes ATP."
    assert cleared.json()["summary"] is None
    assert cleared.json()["title"] == "Light reactions"  # untouched by the second patch


async def test_patch_treats_a_blank_summary_as_clearing_it(client_factory, auth_headers):
    headers = auth_headers()
    async with client_factory() as client:
        project_id, topic = await _one_topic(client, headers)
        url = f"/v1/projects/{project_id}/topics/{topic['id']}"
        await client.patch(url, json={"summary": "Something"}, headers=_headers(headers))
        response = await client.patch(url, json={"summary": "   "}, headers=_headers(headers))
    assert response.json()["summary"] is None


async def test_patch_that_changes_nothing_is_a_successful_no_op(
    client_factory, auth_headers, db_session
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id, topic = await _one_topic(client, headers)
        response = await client.patch(
            f"/v1/projects/{project_id}/topics/{topic['id']}",
            json={"status": "accepted", "title": "Photosynthesis"},
            headers=_headers(headers),
        )

    assert response.status_code == 200
    assert response.json()["updated_at"] == topic["updated_at"]
    assert "topics.updated" not in await _audit_actions(db_session, project_id)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"status": None},
        {"title": None},
        {"status": "archived"},
        {"status": 3},
        {"origin": "source"},
        {"keywords": ["x"]},
        {"parent_id": None},
        {"position": 0},
        {"depth": 0},
        {"status": "accepted", "evidence": []},
        {"title": "x" * 2001},
    ],
)
async def test_patch_rejects_a_malformed_body_with_422_and_changes_nothing(
    client_factory, auth_headers, body
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id, topic = await _one_topic(client, headers)
        response = await client.patch(
            f"/v1/projects/{project_id}/topics/{topic['id']}",
            json=body,
            headers=_headers(headers),
        )
        stored = await _topics(client, headers, project_id)

    assert response.status_code == 422
    assert stored == [topic]


async def test_patch_requires_an_idempotency_key(client_factory, auth_headers):
    headers = auth_headers()
    async with client_factory() as client:
        project_id, topic = await _one_topic(client, headers)
        response = await client.patch(
            f"/v1/projects/{project_id}/topics/{topic['id']}",
            json={"status": "rejected"},
            headers=headers,
        )
        stored = await _topics(client, headers, project_id)
    assert response.status_code == 422
    assert stored[0]["status"] == "accepted"


async def test_patch_replays_the_original_response_for_the_same_key(
    client_factory, auth_headers, db_session
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id, topic = await _one_topic(client, headers)
        url = f"/v1/projects/{project_id}/topics/{topic['id']}"
        key = str(uuid4())
        first = await client.patch(url, json={"status": "rejected"}, headers=_headers(headers, key))
        second = await client.patch(
            url, json={"status": "rejected"}, headers=_headers(headers, key)
        )

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert (await _audit_actions(db_session, project_id)).count("topics.updated") == 1


@pytest.mark.parametrize("bad_title", ["   ", " \t "])
async def test_patch_rejects_a_blank_title_with_400(client_factory, auth_headers, bad_title):
    headers = auth_headers()
    async with client_factory() as client:
        project_id, topic = await _one_topic(client, headers)
        response = await client.patch(
            f"/v1/projects/{project_id}/topics/{topic['id']}",
            json={"title": bad_title},
            headers=_headers(headers),
        )
        stored = await _topics(client, headers, project_id)
    assert response.status_code == 400
    assert stored[0]["title"] == "Photosynthesis"


async def test_patch_rejects_an_over_long_title_and_summary_with_the_limit(
    client_factory, auth_headers
):
    settings = get_settings()
    headers = auth_headers()
    async with client_factory() as client:
        project_id, topic = await _one_topic(client, headers)
        url = f"/v1/projects/{project_id}/topics/{topic['id']}"
        long_title = await client.patch(
            url,
            json={"title": "x" * (settings.MAX_TOPIC_TITLE_CHARS + 1)},
            headers=_headers(headers),
        )
        long_summary = await client.patch(
            url,
            json={"summary": "x" * (settings.MAX_TOPIC_SUMMARY_CHARS + 1)},
            headers=_headers(headers),
        )
        stored = await _topics(client, headers, project_id)

    assert long_title.status_code == long_summary.status_code == 400
    assert str(settings.MAX_TOPIC_TITLE_CHARS) in long_title.json()["error"]["message"]
    assert str(settings.MAX_TOPIC_SUMMARY_CHARS) in long_summary.json()["error"]["message"]
    assert stored == [topic]


async def test_patch_will_not_give_two_of_the_users_topics_the_same_title(
    client_factory, auth_headers
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        first, second = await _add_keywords(client, headers, project_id, ["Cells", "Tissues"])
        response = await client.patch(
            f"/v1/projects/{project_id}/topics/{second['id']}",
            json={"title": " CELLS "},
            headers=_headers(headers),
        )
        stored = await _topics(client, headers, project_id)

    assert response.status_code == 409
    assert [t["title"] for t in stored] == ["Cells", "Tissues"]
    assert first["title"] == "Cells"


async def test_patch_may_recase_a_topics_own_title(client_factory, auth_headers):
    """A topic is not its own duplicate."""
    headers = auth_headers()
    async with client_factory() as client:
        project_id, topic = await _one_topic(client, headers)
        response = await client.patch(
            f"/v1/projects/{project_id}/topics/{topic['id']}",
            json={"title": "PHOTOSYNTHESIS"},
            headers=_headers(headers),
        )
    assert response.status_code == 200
    assert response.json()["title"] == "PHOTOSYNTHESIS"


async def test_patch_returns_the_topics_evidence(client_factory, auth_headers, db_session):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        _, chunks = await _add_chunks(db_session, project_id, ["Plants make sugar."])
        topic = await project_topics_repo.create(
            db_session, project_id=UUID(project_id), title="Sugar", origin="source"
        )
        await topic_citations_repo.add_many(
            db_session,
            project_id=UUID(project_id),
            topic_id=topic.id,
            source_chunk_ids=[chunks[0].id],
        )
        response = await client.patch(
            f"/v1/projects/{project_id}/topics/{topic.id}",
            json={"status": "accepted"},
            headers=_headers(headers),
        )

    assert response.status_code == 200
    assert [e["source_chunk_id"] for e in response.json()["evidence"]] == [str(chunks[0].id)]


async def test_patch_a_topic_of_another_users_project_is_404_and_unchanged(
    client_factory, auth_headers
):
    owner, intruder = auth_headers(), auth_headers()
    async with client_factory() as client:
        project_id, topic = await _one_topic(client, owner)
        response = await client.patch(
            f"/v1/projects/{project_id}/topics/{topic['id']}",
            json={"status": "rejected", "title": "Hijacked"},
            headers=_headers(intruder),
        )
        stored = await _topics(client, owner, project_id)
    assert response.status_code == 404
    assert stored == [topic]


async def test_patch_a_topic_through_the_wrong_project_is_404_and_unchanged(
    client_factory, auth_headers
):
    """The caller owns both projects, but the topic belongs to only one."""
    headers = auth_headers()
    async with client_factory() as client:
        home, topic = await _one_topic(client, headers)
        elsewhere = await _create_project(client, headers)
        response = await client.patch(
            f"/v1/projects/{elsewhere}/topics/{topic['id']}",
            json={"status": "rejected"},
            headers=_headers(headers),
        )
        stored = await _topics(client, headers, home)
    assert response.status_code == 404
    assert stored == [topic]


async def test_patch_an_unknown_topic_is_404(client_factory, auth_headers):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        response = await client.patch(
            f"/v1/projects/{project_id}/topics/{uuid4()}",
            json={"status": "rejected"},
            headers=_headers(headers),
        )
    assert response.status_code == 404


async def test_patch_wording_is_refused_while_an_extraction_is_in_flight_but_status_is_not(
    client_factory, auth_headers
):
    """The review decision is always safe; the wording is what a running
    extraction could overwrite.
    """
    headers = auth_headers()
    async with client_factory() as client:
        project_id, topic = await _one_topic(client, headers)
        enqueue = await client.post(
            f"/v1/projects/{project_id}/topics/extraction", headers=_headers(headers)
        )
        assert enqueue.status_code == 202
        url = f"/v1/projects/{project_id}/topics/{topic['id']}"

        wording = await client.patch(url, json={"summary": "Mine"}, headers=_headers(headers))
        review = await client.patch(url, json={"status": "rejected"}, headers=_headers(headers))
        stored = await _topics(client, headers, project_id)

    assert wording.status_code == 409
    assert review.status_code == 200
    assert stored[0]["summary"] is None
    assert stored[0]["status"] == "rejected"


# =========================================================================================
# End to end - keywords -> extraction -> outline
# =========================================================================================


async def test_keywords_only_flow_end_to_end(client_factory, auth_headers, db_session, monkeypatch):
    """Add keywords, enqueue, let the job run with a mocked model, then read the
    outline and the job result back through the API.
    """
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        [seed] = await _add_keywords(client, headers, project_id, ["Photosynthesis"])
        enqueue = await client.post(
            f"/v1/projects/{project_id}/topics/extraction", headers=_headers(headers)
        )
        job_id = enqueue.json()["id"]

        llm = _fake_llm(
            monkeypatch,
            [
                {
                    "ref": "a",
                    "title": "Photosynthesis",
                    "seed_ref": seed["id"],
                    "summary": "How plants turn light into sugar.",
                    "keywords": ["light", "sugar"],
                    "evidence_chunk_ids": [],
                },
                {
                    "ref": "b",
                    "title": "Light reactions",
                    "parent_ref": "a",
                    "summary": "",
                    "keywords": [],
                    "evidence_chunk_ids": [],
                },
            ],
        )
        job = await _run_pending_extraction(db_session, project_id, llm)
        assert str(job.id) == job_id and job.status == "succeeded"

        items = await _topics(client, headers, project_id)
        jobs = await client.get(f"/v1/projects/{project_id}/jobs", headers=headers)

    assert [(i["title"], i["origin"], i["depth"]) for i in items] == [
        ("Photosynthesis", "user", 0),
        ("Light reactions", "suggested", 1),
    ]
    assert items[0]["id"] == seed["id"]  # the user's own topic kept its identity
    assert items[0]["summary"] == "How plants turn light into sugar."
    assert items[0]["keywords"] == ["light", "sugar"]
    assert items[1]["parent_id"] == seed["id"]
    assert items[1]["evidence"] == []  # suggested: nothing in the material backs it

    [job_body] = jobs.json()["items"]
    assert job_body["status"] == "succeeded"
    assert job_body["result"]["mode"] == "keywords"
    assert job_body["result"]["topic_count"] == 2
    assert job_body["result"]["truncated"] is False


async def test_pdf_flow_end_to_end_cites_the_pages_it_used(
    client_factory, auth_headers, db_session, monkeypatch
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        source, chunks = await _add_chunks(
            db_session,
            project_id,
            ["Plants make sugar from light and water.", "Chlorophyll absorbs red and blue light."],
        )
        enqueue = await client.post(
            f"/v1/projects/{project_id}/topics/extraction", headers=_headers(headers)
        )
        assert enqueue.status_code == 202

        llm = _fake_llm(
            monkeypatch,
            [
                {
                    "ref": "a",
                    "title": "Making sugar",
                    "summary": "Light and water become sugar.",
                    "keywords": ["sugar"],
                    "evidence_chunk_ids": [str(chunks[0].id)],
                },
                {
                    "ref": "b",
                    "title": "Chlorophyll",
                    "parent_ref": "a",
                    "summary": "The pigment that catches light.",
                    "keywords": [],
                    "evidence_chunk_ids": [str(chunks[1].id)],
                },
                # Made up: cites nothing, so it must not survive.
                {
                    "ref": "c",
                    "title": "Uncited claim",
                    "summary": "",
                    "keywords": [],
                    "evidence_chunk_ids": [],
                },
            ],
        )
        job = await _run_pending_extraction(db_session, project_id, llm)
        assert job.status == "succeeded"
        items = await _topics(client, headers, project_id)

    assert [(i["title"], i["origin"], i["status"]) for i in items] == [
        ("Making sugar", "source", "proposed"),
        ("Chlorophyll", "source", "proposed"),
    ]
    assert items[0]["evidence"] == [
        {
            "source_chunk_id": str(chunks[0].id),
            "source_id": str(source.id),
            "page_from": 1,
            "page_to": 1,
        }
    ]
    assert items[1]["evidence"][0]["page_from"] == 2
    assert job.result is not None and job.result["dropped_uncited"] == 1


async def test_a_patch_that_would_change_nothing_still_cannot_read_another_projects_topic(
    client_factory, auth_headers
):
    """The no-op path returns the stored topic, so it must be project-scoped in
    its own right - it must not lean on the later UPDATE (which the no-op skips)
    to notice the topic is somebody else's.
    """
    headers = auth_headers()
    async with client_factory() as client:
        _, topic = await _one_topic(client, headers)
        elsewhere = await _create_project(client, headers)
        response = await client.patch(
            f"/v1/projects/{elsewhere}/topics/{topic['id']}",
            json={"status": topic["status"]},  # already what it is: a no-op
            headers=_headers(headers),
        )
    assert response.status_code == 404
    assert topic["title"] not in response.text


async def test_patch_replay_does_not_reapply_the_original_change(client_factory, auth_headers):
    """A replay must return the first response *without re-running it*: after
    the topic has since been changed again, replaying the first request must not
    put the first change back.
    """
    headers = auth_headers()
    async with client_factory() as client:
        project_id, topic = await _one_topic(client, headers)
        url = f"/v1/projects/{project_id}/topics/{topic['id']}"
        key = str(uuid4())
        first = await client.patch(url, json={"title": "First"}, headers=_headers(headers, key))
        await client.patch(url, json={"title": "Second"}, headers=_headers(headers))
        replayed = await client.patch(url, json={"title": "First"}, headers=_headers(headers, key))
        stored = await _topics(client, headers, project_id)

    assert first.status_code == replayed.status_code == 200
    assert replayed.json() == first.json()
    assert stored[0]["title"] == "Second"


async def test_the_openapi_document_describes_the_four_topic_operations(client_factory):
    """FastAPI only builds the schema when /openapi.json is requested, so a body
    or response type it cannot describe would otherwise first fail at /docs.
    """
    async with client_factory() as client:
        response = await client.get("/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    base = "/v1/projects/{project_id}/topics"
    assert set(paths[base]) == {"get", "post"}
    assert set(paths[f"{base}/extraction"]) == {"post"}
    assert set(paths[f"{base}/{{topic_id}}"]) == {"patch"}
    for path, method in [
        (base, "post"),
        (f"{base}/extraction", "post"),
        (f"{base}/{{topic_id}}", "patch"),
    ]:
        header_names = {p["name"] for p in paths[path][method]["parameters"] if p["in"] == "header"}
        assert "idempotency-key" in header_names

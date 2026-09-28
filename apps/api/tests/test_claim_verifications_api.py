"""``/v1/projects/{project_id}/claim-verifications`` - Phase E API surface.

Same no-real-Clerk pattern as ``test_projects_api.py`` (tokens signed
locally, ``providers.clerk.get_signing_key`` monkeypatched) and the same
transactional ``get_session`` override so nothing persists past a test.

The embeddings and LLM providers are replaced with in-process stubs via
``app.dependency_overrides`` - ``verify_claim``'s retrieval + judgement flow
is exercised for real, but no network call is made. Indexed source material
is inserted straight through the repositories (the indexing pipeline itself
is Phase B's concern and already has its own tests).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey

from edutoon.api.dependencies import get_current_user, get_embeddings, get_llm
from edutoon.core.config import get_settings
from edutoon.db.session import get_session
from edutoon.domain.claim_extraction import ClaimCandidateDraft, ClaimExtractionDraft
from edutoon.main import create_app
from edutoon.providers import clerk
from edutoon.providers.cache import get_redis_client
from edutoon.providers.llm import ClaimTopicBrief, Passage, TokenUsage, Verdict
from edutoon.repositories import claim_verifications as claim_verifications_repo
from edutoon.repositories import project_topics as project_topics_repo
from edutoon.repositories import source_chunks as source_chunks_repo
from edutoon.repositories import uploaded_sources as uploaded_sources_repo
from edutoon.repositories.source_chunks import NewSourceChunk
from tests.idempotency_helpers import INVALID_KEYS, idempotency_status

# Stands in for an external provider (OpenAI / Anthropic): these tests prove how our code
# handles a response, never how a real provider behaves. See "Provider-mocked tests"
# in apps/api/README.md.
pytestmark = pytest.mark.provider_mocked

TEST_ISSUER = "https://test.clerk.accounts.dev"
IDEMPOTENCY_HEADERS = {"Idempotency-Key": "test-key-1"}

EMBEDDING_MODEL = "text-embedding-3-small"
LLM_MODEL = "claude-sonnet-5"
QUERY_VECTOR = [1.0] + [0.0] * 1535


# --- provider stubs --------------------------------------------------------------------


class _StubEmbeddings:
    """Every text embeds to the same vector as the seeded chunk, so
    similarity search always returns it as the top candidate.
    """

    model = EMBEDDING_MODEL

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(QUERY_VECTOR) for _ in texts]


class _StubLLM:
    model = LLM_MODEL

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.extract_calls: list[str] = []  # topic titles asked about
        self._status = "unsupported"
        self._rationale = "Not mentioned in the source."
        self._cite = "none"  # "none" | "all"
        self._candidates: list[str] = []

    def will_return(
        self, status: str, *, rationale: str = "As judged.", cite: str = "none"
    ) -> None:
        self._status = status
        self._rationale = rationale
        self._cite = cite

    def will_propose(self, *texts: str) -> None:
        """What ``extract_claims`` (Phase 6) proposes as candidates for the
        next topic it is asked about.
        """
        self._candidates = list(texts)

    async def judge_claim(self, claim: str, passages: list[Passage]) -> Verdict:
        self.calls.append(claim)
        cited = [p.id for p in passages] if self._cite == "all" else []
        return Verdict(
            status=self._status, rationale=self._rationale, cited_passage_ids=cited
        )

    async def extract_claims(
        self, topic: ClaimTopicBrief, passages: list, *, limits
    ) -> tuple[ClaimExtractionDraft, TokenUsage]:
        self.extract_calls.append(topic.title)
        draft = ClaimExtractionDraft(
            candidates=tuple(ClaimCandidateDraft(text=text) for text in self._candidates)
        )
        return draft, TokenUsage(input_tokens=1, output_tokens=1)


# --- auth plumbing (mirrors test_projects_api.py) -------------------------------------


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
def stub_llm() -> _StubLLM:
    return _StubLLM()


@pytest.fixture
def stub_embeddings() -> _StubEmbeddings:
    return _StubEmbeddings()


@pytest.fixture
async def client_factory(
    db_session, stub_llm, stub_embeddings
) -> AsyncIterator[Callable[[], httpx.AsyncClient]]:
    app = create_app()

    async def _session_override():
        try:
            yield db_session
            await db_session.commit()
        except Exception:
            await db_session.rollback()
            raise

    app.dependency_overrides[get_session] = _session_override
    app.dependency_overrides[get_embeddings] = lambda: stub_embeddings
    app.dependency_overrides[get_llm] = lambda: stub_llm
    # ASGITransport never runs the app's lifespan, so app.state.redis - which
    # the idempotency dependency reads - is set here exactly as lifespan()
    # would have set it.
    app.state.redis = get_redis_client(get_settings().REDIS_URL)

    def _make() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    yield _make

    await app.state.redis.aclose()


# --- helpers -------------------------------------------------------------------------


async def _create_project(client: httpx.AsyncClient, headers: dict[str, str]) -> str:
    response = await client.post(
        "/v1/projects",
        json={"title": "Source material", "source_type": "pdf"},
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 201
    return response.json()["id"]  # type: ignore[no-any-return]


async def _seed_indexed_chunk(
    db_session, project_id: str, content: str = "The sky is blue."
) -> str:
    """Insert one already-embedded chunk into ``project_id`` directly through
    the repositories (the retrieval side is what these tests exercise; the
    indexing pipeline has its own Phase B coverage).
    """
    source = await uploaded_sources_repo.create(
        db_session,
        project_id=project_id,
        original_filename="notes.pdf",
        storage_bucket="edutoon-uploads",
        storage_key=f"{project_id}/{uuid4()}.pdf",
        byte_size=1024,
        checksum_sha256=uuid4().hex + uuid4().hex[:32],
    )
    [chunk] = await source_chunks_repo.create_many(
        db_session,
        [
            NewSourceChunk(
                source_id=source.id, project_id=project_id, chunk_index=0, content=content
            )
        ],
    )
    embedded = await source_chunks_repo.update_embedding(
        db_session, chunk_id=chunk.id, embedding=QUERY_VECTOR, embedding_model=EMBEDDING_MODEL
    )
    assert embedded is not None
    return str(chunk.id)


def _verify_headers(headers: dict[str, str], key: str | None = None) -> dict[str, str]:
    return {**headers, "Idempotency-Key": key or str(uuid4())}


async def _seed_accepted_topic(db_session, project_id: str, title: str = "A topic"):
    return await project_topics_repo.create(
        db_session, project_id=project_id, title=title, status="accepted"
    )


# --- POST: happy path --------------------------------------------------------------


async def test_verify_claim_supported_persists_verdict_with_citation(
    client_factory, auth_headers, stub_llm, db_session
):
    headers = auth_headers()
    stub_llm.will_return("supported", rationale="Stated verbatim.", cite="all")

    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        chunk_id = await _seed_indexed_chunk(db_session, project_id)

        response = await client.post(
            f"/v1/projects/{project_id}/claim-verifications",
            json={"claim_text": "The sky is blue."},
            headers=_verify_headers(headers),
        )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "supported"
    assert body["claim_text"] == "The sky is blue."
    assert body["rationale"] == "Stated verbatim."
    assert body["embedding_model"] == EMBEDDING_MODEL
    assert body["llm_model"] == LLM_MODEL
    assert body["project_id"] == project_id
    assert [c["source_chunk_id"] for c in body["citations"]] == [chunk_id]
    assert 0.99 <= body["citations"][0]["similarity"] <= 1.0
    assert stub_llm.calls == ["The sky is blue."]


async def test_verify_claim_supported_without_citation_is_downgraded(
    client_factory, auth_headers, stub_llm, db_session
):
    """The LLM saying "supported" but naming no passage must not surface as
    a supported verdict - the service downgrades it and no citation rows
    are written.
    """
    headers = auth_headers()
    stub_llm.will_return("supported", cite="none")

    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        await _seed_indexed_chunk(db_session, project_id)

        response = await client.post(
            f"/v1/projects/{project_id}/claim-verifications",
            json={"claim_text": "Something the source does not say."},
            headers=_verify_headers(headers),
        )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "unsupported"
    assert body["citations"] == []


async def test_verify_claim_with_no_indexed_material_is_unsupported_without_calling_the_llm(
    client_factory, auth_headers, stub_llm
):
    headers = auth_headers()

    async with client_factory() as client:
        project_id = await _create_project(client, headers)

        response = await client.post(
            f"/v1/projects/{project_id}/claim-verifications",
            json={"claim_text": "Anything at all."},
            headers=_verify_headers(headers),
        )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "unsupported"
    assert body["citations"] == []
    assert stub_llm.calls == []


# --- GET list / GET detail --------------------------------------------------------


async def test_list_and_detail_round_trip(client_factory, auth_headers, stub_llm, db_session):
    headers = auth_headers()
    stub_llm.will_return("supported", cite="all")

    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        chunk_id = await _seed_indexed_chunk(db_session, project_id)

        first = await client.post(
            f"/v1/projects/{project_id}/claim-verifications",
            json={"claim_text": "Claim one."},
            headers=_verify_headers(headers),
        )
        second = await client.post(
            f"/v1/projects/{project_id}/claim-verifications",
            json={"claim_text": "Claim two."},
            headers=_verify_headers(headers),
        )
        assert first.status_code == second.status_code == 201

        list_resp = await client.get(
            f"/v1/projects/{project_id}/claim-verifications", headers=headers
        )
        detail_resp = await client.get(
            f"/v1/projects/{project_id}/claim-verifications/{first.json()['id']}",
            headers=headers,
        )

    assert list_resp.status_code == 200
    listed = list_resp.json()
    assert listed["next_cursor"] is None
    # newest first
    assert [item["claim_text"] for item in listed["items"]] == ["Claim two.", "Claim one."]
    # list items carry their citations, N+1-free
    assert [c["source_chunk_id"] for c in listed["items"][0]["citations"]] == [chunk_id]

    assert detail_resp.status_code == 200
    assert detail_resp.json() == first.json()


async def test_list_is_empty_for_a_project_with_no_verifications(client_factory, auth_headers):
    headers = auth_headers()

    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        response = await client.get(
            f"/v1/projects/{project_id}/claim-verifications", headers=headers
        )

    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


async def test_detail_for_unknown_verification_returns_404(client_factory, auth_headers):
    headers = auth_headers()

    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        response = await client.get(
            f"/v1/projects/{project_id}/claim-verifications/{uuid4()}", headers=headers
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# --- ownership isolation (rule 9: not-owned -> 404, never 403) --------------------


async def test_another_users_project_returns_404_not_403(
    client_factory, auth_headers, stub_llm, db_session
):
    owner_headers = auth_headers()
    other_headers = auth_headers()

    async with client_factory() as client:
        project_id = await _create_project(client, owner_headers)
        await _seed_indexed_chunk(db_session, project_id)

        post_resp = await client.post(
            f"/v1/projects/{project_id}/claim-verifications",
            json={"claim_text": "Hijack attempt."},
            headers=_verify_headers(other_headers),
        )
        list_resp = await client.get(
            f"/v1/projects/{project_id}/claim-verifications", headers=other_headers
        )
        detail_resp = await client.get(
            f"/v1/projects/{project_id}/claim-verifications/{uuid4()}", headers=other_headers
        )

    assert post_resp.status_code == 404
    assert list_resp.status_code == 404
    assert detail_resp.status_code == 404
    for response in (post_resp, list_resp, detail_resp):
        assert response.json()["error"]["code"] == "not_found"
    assert stub_llm.calls == []  # never reached the verification flow


async def test_verification_from_another_project_returns_404(
    client_factory, auth_headers, stub_llm, db_session
):
    """Two-level ownership: the caller owns both projects, but a verification
    belonging to project A must not be reachable under project B's path.
    """
    headers = auth_headers()
    stub_llm.will_return("unsupported")

    async with client_factory() as client:
        project_a = await _create_project(client, headers)
        project_b = await _create_project(client, headers)
        await _seed_indexed_chunk(db_session, project_a)

        created = await client.post(
            f"/v1/projects/{project_a}/claim-verifications",
            json={"claim_text": "Belongs to A."},
            headers=_verify_headers(headers),
        )
        verification_id = created.json()["id"]

        cross = await client.get(
            f"/v1/projects/{project_b}/claim-verifications/{verification_id}", headers=headers
        )
        b_list = await client.get(
            f"/v1/projects/{project_b}/claim-verifications", headers=headers
        )

    assert cross.status_code == 404
    assert b_list.json()["items"] == []


async def test_post_to_nonexistent_project_returns_404(client_factory, auth_headers):
    async with client_factory() as client:
        response = await client.post(
            f"/v1/projects/{uuid4()}/claim-verifications",
            json={"claim_text": "No such project."},
            headers=_verify_headers(auth_headers()),
        )

    assert response.status_code == 404


# --- idempotency (rule 8) --------------------------------------------------------


async def test_missing_idempotency_key_is_rejected(client_factory, auth_headers):
    headers = auth_headers()

    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        response = await client.post(
            f"/v1/projects/{project_id}/claim-verifications",
            json={"claim_text": "No key supplied."},
            headers=headers,
        )

    assert response.status_code == 422


async def test_repeat_with_same_key_replays_first_verdict(
    client_factory, auth_headers, stub_llm, db_session
):
    headers = auth_headers()
    stub_llm.will_return("supported", cite="all")

    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        await _seed_indexed_chunk(db_session, project_id)
        key = str(uuid4())

        first = await client.post(
            f"/v1/projects/{project_id}/claim-verifications",
            json={"claim_text": "Judge me once."},
            headers=_verify_headers(headers, key),
        )
        second = await client.post(
            f"/v1/projects/{project_id}/claim-verifications",
            json={"claim_text": "Judge me once."},
            headers=_verify_headers(headers, key),
        )

        list_resp = await client.get(
            f"/v1/projects/{project_id}/claim-verifications", headers=headers
        )

    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()  # replayed, not a fresh verdict
    assert len(list_resp.json()["items"]) == 1
    assert stub_llm.calls == ["Judge me once."]  # the LLM ran exactly once


async def test_concurrent_duplicate_returns_409(db_session, stub_llm, stub_embeddings):
    """A genuine concurrent duplicate (same key, still in flight) is a 409,
    not a second verification. ``get_current_user`` is overridden to a fixed
    persisted user so neither request touches ``db_session`` during auth -
    only the idempotency-lock winner ever runs a statement on it (same
    pattern as ``test_uploaded_sources_api.py``'s concurrent test).
    """
    from edutoon.repositories import projects as projects_repo
    from edutoon.repositories import users as users_repo

    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    project = await projects_repo.create(
        db_session, owner_id=owner.id, title="P", source_type="pdf"
    )

    app = create_app()

    async def _session_override():
        # Plain form (no commit/rollback wrapper): the two requests race over
        # this one session and only the winner touches it, so a wrapper here
        # would itself race the winner's use of the session object.
        yield db_session

    app.dependency_overrides[get_session] = _session_override
    app.dependency_overrides[get_current_user] = lambda: owner
    app.dependency_overrides[get_embeddings] = lambda: stub_embeddings
    app.dependency_overrides[get_llm] = lambda: stub_llm
    app.state.redis = get_redis_client(get_settings().REDIS_URL)

    path = f"/v1/projects/{project.id}/claim-verifications"
    headers = {"Idempotency-Key": str(uuid4())}
    payload = {"claim_text": "Race me."}

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response_a, response_b = await asyncio.gather(
                client.post(path, json=payload, headers=headers),
                client.post(path, json=payload, headers=headers),
            )
    finally:
        await app.state.redis.aclose()

    statuses = sorted((response_a.status_code, response_b.status_code))
    assert statuses == [201, 409]


# --- validation (rule 10 + blank claim) -----------------------------------------


async def test_rejects_unknown_fields(client_factory, auth_headers):
    headers = auth_headers()

    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        response = await client.post(
            f"/v1/projects/{project_id}/claim-verifications",
            json={"claim_text": "Fine.", "sneaky": "field"},
            headers=_verify_headers(headers),
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_rejects_blank_claim(client_factory, auth_headers):
    headers = auth_headers()

    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        response = await client.post(
            f"/v1/projects/{project_id}/claim-verifications",
            json={"claim_text": "   "},
            headers=_verify_headers(headers),
        )

    assert response.status_code == 422


async def test_rejects_missing_claim(client_factory, auth_headers):
    headers = auth_headers()

    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        response = await client.post(
            f"/v1/projects/{project_id}/claim-verifications",
            json={},
            headers=_verify_headers(headers),
        )

    assert response.status_code == 422


# --- the Idempotency-Key header, and durability before the response --------------------


async def test_a_verdict_is_committed_before_it_is_cached_or_returned(
    client_factory, auth_headers, stub_llm, db_session, redis_client, monkeypatch
):
    """The route commits for itself, before anything is cached: at the moment it does,
    the dedupe record must still say "in_progress" - a cached "completed" would promise
    a verdict that a failed commit could still lose. (This module's session override
    also commits once the response is out, so the spy sees a second call; only the
    first is the route's own.)
    """
    headers = auth_headers()
    stub_llm.will_return("supported", cite="all")
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        await _seed_indexed_chunk(db_session, project_id)
        path = f"/v1/projects/{project_id}/claim-verifications"
        key = str(uuid4())
        seen_at_commit: list[str | None] = []
        real_commit = db_session.commit

        async def _spying_commit() -> None:
            seen_at_commit.append(await idempotency_status(redis_client, path, key))
            await real_commit()

        monkeypatch.setattr(db_session, "commit", _spying_commit)
        response = await client.post(
            path, json={"claim_text": "The sky is blue."}, headers=_verify_headers(headers, key)
        )

    assert response.status_code == 201, response.text
    assert seen_at_commit[0] == "in_progress"
    assert await idempotency_status(redis_client, path, key) == "completed"


async def test_a_commit_that_fails_leaves_no_cached_success_behind(
    client_factory, auth_headers, db_session, redis_client, monkeypatch
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        path = f"/v1/projects/{project_id}/claim-verifications"
        key = str(uuid4())

        async def _failing_commit() -> None:
            raise RuntimeError("the database went away")

        monkeypatch.setattr(db_session, "commit", _failing_commit)
        with pytest.raises(RuntimeError, match="went away"):
            await client.post(
                path, json={"claim_text": "The sky is blue."}, headers=_verify_headers(headers, key)
            )

    # Nothing says the verdict was stored, and the key is free for the client's retry.
    assert await idempotency_status(redis_client, path, key) is None


@pytest.mark.parametrize("key", INVALID_KEYS)
async def test_an_empty_or_oversized_idempotency_key_is_rejected_before_any_paid_call(
    client_factory, auth_headers, stub_llm, db_session, key
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        await _seed_indexed_chunk(db_session, project_id)
        path = f"/v1/projects/{project_id}/claim-verifications"

        # Built by hand: `_verify_headers` swaps an empty key for a fresh UUID.
        response = await client.post(
            path,
            json={"claim_text": "The sky is blue."},
            headers={**headers, "Idempotency-Key": key},
        )

        assert response.status_code == 422
        [error] = response.json()["error"]["details"]["field_errors"]
        assert error["path"] == "header.idempotency-key"
        assert stub_llm.calls == []  # no paid call was made for a request that was refused
        listed = await client.get(path, headers=headers)
        assert listed.json()["items"] == []


async def test_an_idempotency_key_of_the_maximum_length_is_accepted(client_factory, auth_headers):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        response = await client.post(
            f"/v1/projects/{project_id}/claim-verifications",
            json={"claim_text": "No material yet."},
            headers=_verify_headers(headers, "k" * 255),
        )

    assert response.status_code == 201, response.text


# --- topic_id on the response (Phase 6) -------------------------------------------


async def test_a_manually_created_claim_has_no_topic_id(client_factory, auth_headers):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        response = await client.post(
            f"/v1/projects/{project_id}/claim-verifications",
            json={"claim_text": "Typed in by hand."},
            headers=_verify_headers(headers),
        )

    assert response.status_code == 201
    assert response.json()["topic_id"] is None


# --- POST .../extraction (Phase 6: candidate claim extraction) -----------------------


async def test_extraction_returns_202_with_a_queued_job(client_factory, auth_headers, db_session):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        await _seed_accepted_topic(db_session, project_id)
        response = await client.post(
            f"/v1/projects/{project_id}/claim-verifications/extraction",
            headers=_verify_headers(headers),
        )

    assert response.status_code == 202
    body = response.json()
    assert body["kind"] == "extract_claims"
    assert body["status"] == "queued"
    assert body["project_id"] == project_id
    assert body["payload"] == {"project_id": project_id, "replace": False}
    assert body["result"] is None


async def test_extraction_passes_replace_through_to_the_job(
    client_factory, auth_headers, db_session
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        await _seed_accepted_topic(db_session, project_id)
        response = await client.post(
            f"/v1/projects/{project_id}/claim-verifications/extraction",
            json={"replace": True},
            headers=_verify_headers(headers),
        )

    assert response.status_code == 202
    assert response.json()["payload"]["replace"] is True


@pytest.mark.parametrize("body", [{"replace": "maybe"}, {"replace": False, "extra": "x"}, [1]])
async def test_extraction_rejects_a_malformed_body_with_422(
    client_factory, auth_headers, db_session, body
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        await _seed_accepted_topic(db_session, project_id)
        response = await client.post(
            f"/v1/projects/{project_id}/claim-verifications/extraction",
            json=body,
            headers=_verify_headers(headers),
        )
        assert response.status_code == 422
        jobs = await client.get(f"/v1/projects/{project_id}/jobs", headers=headers)
    assert jobs.json()["items"] == []


async def test_extraction_requires_an_idempotency_key(client_factory, auth_headers, db_session):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        await _seed_accepted_topic(db_session, project_id)
        response = await client.post(
            f"/v1/projects/{project_id}/claim-verifications/extraction", headers=headers
        )
    assert response.status_code == 422


async def test_extraction_with_no_accepted_topics_is_400(client_factory, auth_headers):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        response = await client.post(
            f"/v1/projects/{project_id}/claim-verifications/extraction",
            headers=_verify_headers(headers),
        )
        assert response.status_code == 400
        jobs = await client.get(f"/v1/projects/{project_id}/jobs", headers=headers)
    assert jobs.json()["items"] == []


async def test_extraction_replays_the_same_job_for_the_same_key(
    client_factory, auth_headers, db_session
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        await _seed_accepted_topic(db_session, project_id)
        key = str(uuid4())
        first = await client.post(
            f"/v1/projects/{project_id}/claim-verifications/extraction",
            headers=_verify_headers(headers, key),
        )
        second = await client.post(
            f"/v1/projects/{project_id}/claim-verifications/extraction",
            headers=_verify_headers(headers, key),
        )
        jobs = await client.get(f"/v1/projects/{project_id}/jobs", headers=headers)

    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    assert len(jobs.json()["items"]) == 1


async def test_a_second_extraction_while_one_is_queued_is_409(
    client_factory, auth_headers, db_session
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        await _seed_accepted_topic(db_session, project_id)
        first = await client.post(
            f"/v1/projects/{project_id}/claim-verifications/extraction",
            headers=_verify_headers(headers),
        )
        second = await client.post(
            f"/v1/projects/{project_id}/claim-verifications/extraction",
            headers=_verify_headers(headers),
        )
        jobs = await client.get(f"/v1/projects/{project_id}/jobs", headers=headers)

    assert first.status_code == 202
    assert second.status_code == 409
    assert len(jobs.json()["items"]) == 1


async def test_extraction_for_another_users_project_is_404(
    client_factory, auth_headers, db_session
):
    owner, intruder = auth_headers(), auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, owner)
        await _seed_accepted_topic(db_session, project_id)
        response = await client.post(
            f"/v1/projects/{project_id}/claim-verifications/extraction",
            headers=_verify_headers(intruder),
        )
        jobs = await client.get(f"/v1/projects/{project_id}/jobs", headers=owner)
    assert response.status_code == 404
    assert jobs.json()["items"] == []


async def test_extraction_for_nonexistent_project_is_404(client_factory, auth_headers):
    async with client_factory() as client:
        response = await client.post(
            f"/v1/projects/{uuid4()}/claim-verifications/extraction",
            headers=_verify_headers(auth_headers()),
        )
    assert response.status_code == 404


# --- GET list filtered by topic_id (Phase 6) ------------------------------------------


async def test_list_filters_by_topic_id(client_factory, auth_headers, db_session):
    """Claims are only ever extracted asynchronously (via the job), so this
    seeds them the way ``_seed_indexed_chunk`` seeds a chunk: straight through
    the repository, exactly what a finished ``extract_claims`` job would have
    written.
    """
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        topic_a = await _seed_accepted_topic(db_session, project_id, "Topic A")
        topic_b = await project_topics_repo.create(
            db_session, project_id=project_id, title="Topic B", status="accepted", position=1
        )
        for_a = await claim_verifications_repo.create(
            db_session,
            project_id=project_id,
            claim_text="Claim for A.",
            status="unsupported",
            embedding_model=EMBEDDING_MODEL,
            llm_model=LLM_MODEL,
            topic_id=topic_a.id,
        )
        await claim_verifications_repo.create(
            db_session,
            project_id=project_id,
            claim_text="Claim for B.",
            status="unsupported",
            embedding_model=EMBEDDING_MODEL,
            llm_model=LLM_MODEL,
            topic_id=topic_b.id,
        )

        response = await client.get(
            f"/v1/projects/{project_id}/claim-verifications",
            params={"topic_id": str(topic_a.id)},
            headers=headers,
        )

    assert response.status_code == 200
    [item] = response.json()["items"]
    assert item["id"] == str(for_a.id)
    assert item["topic_id"] == str(topic_a.id)

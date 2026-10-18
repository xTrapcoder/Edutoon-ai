"""Topic-only generation is removed (it has no switch), and PDF + keywords is not.

The evidence pipeline verifies claims against the project's own indexed material. With keywords and
no material there is nothing to verify against, so a keyword-only project could never produce a
verified script - the product's promise. It is refused where it starts (creating the project) and
again where it would otherwise proceed (a topic extraction with keywords and no parsed material),
with the same message, and a PDF project that also has keywords works exactly as before.

The mode was removed on 2026-10-18; no setting brings it back.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from edutoon.core.config import get_settings
from edutoon.core.errors import ValidationError
from edutoon.db.session import get_session
from edutoon.domain.topic import KEYWORD_ONLY_UNSUPPORTED
from edutoon.main import create_app
from edutoon.providers import clerk
from edutoon.providers.cache import get_redis_client
from edutoon.repositories import jobs as jobs_repo
from edutoon.repositories import projects as projects_repo
from edutoon.services import projects as projects_service
from edutoon.services import topic_extraction as service
from tests.test_services_topic_extraction import (  # noqa: F401 - shared helpers
    _add_chunks,
    _add_seed,
    _enqueue,
    _fake_llm,
    _project,
    _topic,
)


@pytest.fixture
def keyword_only_off():
    """Kept as a named fixture so the tests read as before: the mode is always off now."""
    yield


# --- the message -----------------------------------------------------------------------------


def test_the_message_says_what_is_unsupported_why_and_what_to_do_instead() -> None:
    text = KEYWORD_ONLY_UNSUPPORTED
    assert "Topic-only projects are not offered" in text
    assert "checked against your own source material" in text
    assert "Upload a PDF" in text and "paste your text" in text and "add keywords" in text


def test_there_is_no_setting_that_turns_the_mode_back_on() -> None:
    assert not hasattr(get_settings(), "KEYWORD_ONLY_PROJECTS_ENABLED")


# --- creating a keyword-only project ---------------------------------------------------------


async def test_a_keyword_only_project_cannot_be_created(db_session, keyword_only_off):
    from edutoon.repositories import users as users_repo

    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")

    with pytest.raises(ValidationError) as caught:
        await projects_service.create_project(
            db_session, owner_id=owner.id, title="Only keywords", source_type="topic"
        )

    assert caught.value.message == KEYWORD_ONLY_UNSUPPORTED
    assert (await projects_repo.list_by_owner(db_session, owner.id)).items == []


async def test_a_pdf_project_is_created_as_before(db_session, keyword_only_off):
    from edutoon.repositories import users as users_repo

    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    project = await projects_service.create_project(
        db_session, owner_id=owner.id, title="A PDF", source_type="pdf"
    )
    assert project.source_type == "pdf"


# --- extracting topics from keywords alone ---------------------------------------------------


async def test_topic_extraction_from_keywords_alone_is_refused(db_session, keyword_only_off):
    owner, project = await _project(db_session, source_type="pdf")
    await _add_seed(db_session, project, "Photosynthesis", position=0)

    with pytest.raises(ValidationError) as caught:
        await _enqueue(db_session, owner, project)

    assert caught.value.message == KEYWORD_ONLY_UNSUPPORTED
    assert (await jobs_repo.list_by_project(db_session, project.id)).items == []  # no job, no spend


async def test_a_pdf_alone_can_still_be_extracted(db_session, keyword_only_off):
    owner, project = await _project(db_session)
    await _add_chunks(db_session, project, ["Plants make food from light."])

    enqueued = await _enqueue(db_session, owner, project)

    assert enqueued.job.status == "queued"


async def test_a_pdf_with_keywords_can_still_be_extracted(db_session, keyword_only_off):
    owner, project = await _project(db_session)
    await _add_chunks(db_session, project, ["Plants make food from light."])
    await _add_seed(db_session, project, "Photosynthesis", position=0)

    enqueued = await _enqueue(db_session, owner, project)

    assert enqueued.job.status == "queued"


async def test_a_job_queued_while_the_project_had_material_is_refused_when_the_material_goes(
    db_session, monkeypatch
):
    """The project can change while a job waits: the run checks again."""
    owner, project = await _project(db_session, source_type="pdf")
    await _add_chunks(db_session, project, ["Plants make food from light."])
    await _add_seed(db_session, project, "Photosynthesis", position=0)
    enqueued = await _enqueue(db_session, owner, project)  # material and keywords: accepted
    from sqlalchemy import text

    await db_session.execute(
        text("DELETE FROM source_chunks WHERE project_id = :project_id"),
        {"project_id": project.id},
    )
    llm = _fake_llm(monkeypatch, [_topic("t1", "Photosynthesis")])[0]

    job = await service.run_extract_topics_job(db_session, llm, job_id=enqueued.job.id)

    assert job.status == "failed" and job.error == KEYWORD_ONLY_UNSUPPORTED


# --- through the API -------------------------------------------------------------------------

TEST_ISSUER = "https://test.clerk.accounts.dev"


@pytest.fixture(scope="module")
def rsa_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem, private_key.public_key()


@pytest.fixture
def auth_headers(rsa_keypair, monkeypatch) -> Callable[[], dict[str, str]]:
    private_pem, public_key = rsa_keypair
    monkeypatch.setattr(clerk, "get_signing_key", lambda token: public_key)

    def _make() -> dict[str, str]:
        now = int(time.time())
        token = jwt.encode(
            {
                "sub": f"clerk_{uuid4()}",
                "email": f"{uuid4()}@example.com",
                "iat": now,
                "exp": now + 3600,
                "iss": TEST_ISSUER,
            },
            private_pem,
            algorithm="RS256",
        )
        return {"Authorization": f"Bearer {token}"}

    return _make


@pytest.fixture
async def client_factory(db_session) -> AsyncIterator[Callable[[], httpx.AsyncClient]]:
    app = create_app()

    async def _session_override():
        yield db_session

    app.dependency_overrides[get_session] = _session_override
    app.state.redis = get_redis_client(get_settings().REDIS_URL)

    def _make() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    yield _make
    await app.state.redis.aclose()


async def test_the_api_refuses_a_keyword_only_project_with_the_message(
    client_factory, auth_headers, keyword_only_off
):
    headers = auth_headers()
    async with client_factory() as client:
        refused = await client.post(
            "/v1/projects",
            json={"title": "Keywords", "source_type": "topic"},
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )
        allowed = await client.post(
            "/v1/projects",
            json={"title": "A PDF project", "source_type": "pdf"},
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )
        listing = await client.get("/v1/projects", headers=headers)

    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "validation_error"
    assert refused.json()["error"]["message"] == KEYWORD_ONLY_UNSUPPORTED
    assert allowed.status_code == 201
    assert [p["source_type"] for p in listing.json()["items"]] == ["pdf"]  # nothing was created

"""``/v1/projects/{project_id}/sources`` - PDF upload.

Same no-real-Clerk pattern as ``test_projects_api.py``: tokens are signed
locally and ``providers.clerk.get_signing_key`` is monkeypatched.
``get_session`` is overridden to the transactional ``db_session`` fixture,
so Postgres writes never persist past a test. The storage side (MinIO) is
real and not transactional - uploaded objects persist - but keys are
content-addressed (``projects/{project_id}/{checksum}.pdf``), so repeat
runs simply overwrite the same handful of objects rather than accumulating.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator, Callable
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from fpdf import FPDF

from edutoon.api.dependencies import get_current_user
from edutoon.core.config import get_settings
from edutoon.db.session import get_session
from edutoon.main import create_app
from edutoon.providers import clerk
from edutoon.providers.cache import get_redis_client
from edutoon.providers.storage import get_storage_client
from edutoon.repositories import users as users_repo

TEST_ISSUER = "https://test.clerk.accounts.dev"
IDEMPOTENCY_HEADERS = {"Idempotency-Key": "test-key-1"}


def _make_pdf(pages: list[str] | None = None) -> bytes:
    """A real, pypdf-parseable PDF with one page per string in ``pages``
    (default: a single page of sample text). An empty string produces a
    genuinely blank page (no extractable text) - used to test that the
    parsing pipeline skips blank pages when chunking.
    """
    texts = pages if pages is not None else ["Test content for parsing."]
    doc = FPDF()
    for text in texts:
        doc.add_page()
        if text:
            doc.set_font("Helvetica", size=12)
            doc.cell(text=text)
    return bytes(doc.output())


_DEFAULT_PDF = _make_pdf()

# Starts with the `%PDF` magic bytes (so it passes that check) but is
# otherwise garbage - pypdf can't parse it, unlike `_DEFAULT_PDF` above.
_CORRUPT_PDF = b"%PDF-1.4\n" + os.urandom(200)


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
        # Mirrors `db/session.py::get_session`'s own rollback-on-error
        # behaviour: unlike production (a fresh session per request), every
        # request in a test shares this one `db_session`, so a request that
        # raises past a failed statement (e.g. a unique-constraint conflict)
        # must roll back to its savepoint here, or Postgres leaves the
        # underlying transaction aborted for every later request in the
        # same test.
        try:
            yield db_session
            await db_session.commit()
        except Exception:
            await db_session.rollback()
            raise

    app.dependency_overrides[get_session] = _session_override
    # ASGITransport never runs the app's lifespan, so app.state.redis/storage
    # - which the idempotency and upload dependencies read - are set here
    # exactly as lifespan() would have set them.
    settings = get_settings()
    app.state.redis = get_redis_client(settings.REDIS_URL)
    app.state.storage = get_storage_client(
        endpoint_url=settings.STORAGE_ENDPOINT_URL,
        access_key_id=settings.STORAGE_ACCESS_KEY_ID,
        secret_access_key=settings.STORAGE_SECRET_ACCESS_KEY,
    )

    def _make() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    yield _make

    await app.state.redis.aclose()


async def _create_project(client: httpx.AsyncClient, headers: dict[str, str]) -> str:
    response = await client.post(
        "/v1/projects",
        json={"title": "Source material", "source_type": "pdf"},
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 201
    return response.json()["id"]  # type: ignore[no-any-return]


def _pdf_file(content: bytes = _DEFAULT_PDF, filename: str = "paper.pdf") -> dict[str, object]:
    return {"file": (filename, content, "application/pdf")}


# --- happy path --------------------------------------------------------------------


async def test_upload_and_list_round_trip(client_factory, auth_headers):
    headers = auth_headers()

    async with client_factory() as client:
        project_id = await _create_project(client, headers)

        upload_resp = await client.post(
            f"/v1/projects/{project_id}/sources",
            files=_pdf_file(),
            headers={**headers, **IDEMPOTENCY_HEADERS},
        )
        assert upload_resp.status_code == 201
        uploaded = upload_resp.json()
        assert uploaded["original_filename"] == "paper.pdf"
        assert uploaded["content_type"] == "application/pdf"
        assert uploaded["kind"] == "pdf"
        assert uploaded["status"] == "parsed"
        assert uploaded["page_count"] == 1
        assert uploaded["byte_size"] == len(_DEFAULT_PDF)
        assert len(uploaded["checksum_sha256"]) == 64
        assert "storage_bucket" not in uploaded  # internal storage layout is not public

        list_resp = await client.get(f"/v1/projects/{project_id}/sources", headers=headers)

    assert list_resp.status_code == 200
    body = list_resp.json()
    assert [item["id"] for item in body["items"]] == [uploaded["id"]]
    assert body["next_cursor"] is None


# --- ownership isolation (rule 9: not-owned -> 404, never 403) -------------------


async def test_upload_to_another_users_project_returns_404_not_403(client_factory, auth_headers):
    owner_headers = auth_headers()
    other_headers = auth_headers()

    async with client_factory() as client:
        project_id = await _create_project(client, owner_headers)

        upload_resp = await client.post(
            f"/v1/projects/{project_id}/sources",
            files=_pdf_file(),
            headers={**other_headers, **IDEMPOTENCY_HEADERS},
        )
        list_resp = await client.get(f"/v1/projects/{project_id}/sources", headers=other_headers)

    assert upload_resp.status_code == 404
    assert upload_resp.json()["error"]["code"] == "not_found"
    assert list_resp.status_code == 404


async def test_upload_to_nonexistent_project_returns_404(client_factory, auth_headers):
    async with client_factory() as client:
        response = await client.post(
            f"/v1/projects/{uuid4()}/sources",
            files=_pdf_file(),
            headers={**auth_headers(), **IDEMPOTENCY_HEADERS},
        )

    assert response.status_code == 404


# --- validation failures (rule 10 + upload-specific checks) -----------------------


async def test_upload_without_idempotency_key_is_rejected(client_factory, auth_headers):
    headers = auth_headers()

    async with client_factory() as client:
        project_id = await _create_project(client, headers)

        response = await client.post(
            f"/v1/projects/{project_id}/sources", files=_pdf_file(), headers=headers
        )

    assert response.status_code == 422


async def test_upload_rejects_wrong_content_type(client_factory, auth_headers):
    headers = auth_headers()

    async with client_factory() as client:
        project_id = await _create_project(client, headers)

        response = await client.post(
            f"/v1/projects/{project_id}/sources",
            files={"file": ("notes.txt", b"just text", "text/plain")},
            headers={**headers, **IDEMPOTENCY_HEADERS},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_upload_rejects_content_that_is_not_actually_a_pdf(client_factory, auth_headers):
    headers = auth_headers()

    async with client_factory() as client:
        project_id = await _create_project(client, headers)

        response = await client.post(
            f"/v1/projects/{project_id}/sources",
            files={"file": ("fake.pdf", b"not a real pdf", "application/pdf")},
            headers={**headers, **IDEMPOTENCY_HEADERS},
        )

    assert response.status_code == 422


async def test_upload_rejects_unknown_form_fields(client_factory, auth_headers):
    headers = auth_headers()

    async with client_factory() as client:
        project_id = await _create_project(client, headers)

        response = await client.post(
            f"/v1/projects/{project_id}/sources",
            files=_pdf_file(),
            data={"sneaky": "field"},
            headers={**headers, **IDEMPOTENCY_HEADERS},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_upload_rejects_oversized_file(client_factory, auth_headers, monkeypatch):
    headers = auth_headers()
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "10")
    get_settings.cache_clear()

    try:
        async with client_factory() as client:
            project_id = await _create_project(client, headers)

            response = await client.post(
                f"/v1/projects/{project_id}/sources",
                files=_pdf_file(),
                headers={**headers, **IDEMPOTENCY_HEADERS},
            )
    finally:
        get_settings.cache_clear()

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


# --- idempotency-key deduplication (rule 8, real store) -----------------------------


async def test_duplicate_upload_with_same_key_replays_first_response(client_factory, auth_headers):
    headers = auth_headers()

    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        upload_headers = {**headers, "Idempotency-Key": str(uuid4())}

        first = await client.post(
            f"/v1/projects/{project_id}/sources", files=_pdf_file(), headers=upload_headers
        )
        second = await client.post(
            f"/v1/projects/{project_id}/sources", files=_pdf_file(), headers=upload_headers
        )

        list_resp = await client.get(f"/v1/projects/{project_id}/sources", headers=headers)

    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()  # replayed, not a fresh row
    assert len(list_resp.json()["items"]) == 1


# --- duplicate content / conflict handling (distinct from idempotency replay) -------


async def test_reuploading_identical_content_under_a_different_key_returns_409(
    client_factory, auth_headers
):
    """Same project, same bytes, but a *different* Idempotency-Key: this is
    a genuine second upload attempt, not a retry - it must be rejected as a
    conflict (rule: the `(project_id, checksum_sha256)` unique index), not
    silently replayed and not treated as an idempotency-in-progress clash.
    """
    headers = auth_headers()

    async with client_factory() as client:
        project_id = await _create_project(client, headers)

        first = await client.post(
            f"/v1/projects/{project_id}/sources",
            files=_pdf_file(),
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )
        second = await client.post(
            f"/v1/projects/{project_id}/sources",
            files=_pdf_file(),
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )

        list_resp = await client.get(f"/v1/projects/{project_id}/sources", headers=headers)

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "conflict"
    assert len(list_resp.json()["items"]) == 1


async def test_same_content_in_different_projects_is_not_a_conflict(client_factory, auth_headers):
    headers = auth_headers()

    async with client_factory() as client:
        project_a = await _create_project(client, headers)
        project_b = await _create_project(client, headers)

        first = await client.post(
            f"/v1/projects/{project_a}/sources",
            files=_pdf_file(),
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )
        second = await client.post(
            f"/v1/projects/{project_b}/sources",
            files=_pdf_file(),
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )

    assert first.status_code == second.status_code == 201
    assert first.json()["id"] != second.json()["id"]
    assert first.json()["checksum_sha256"] == second.json()["checksum_sha256"]


async def test_concurrent_duplicate_upload_returns_409(db_session):
    """Real concurrency, over the real route/service/repository/storage
    stack - same pattern as ``test_projects_api.py``'s equivalent test:
    ``get_current_user`` is overridden to a fixed, already-persisted user so
    neither concurrent request touches ``db_session`` during auth, keeping
    exactly one coroutine on the session at a time.
    """
    fixed_user = await users_repo.create(db_session, email=f"{uuid4()}@example.com")

    app = create_app()

    async def _session_override():
        # Deliberately the plain (no commit/rollback) form, unlike
        # `client_factory`'s override: the two requests below run truly
        # concurrently over this one shared `db_session`, and only the
        # winner ever touches it (the loser's whole path is Redis-only -
        # see the docstring above), so a commit/rollback wrapper here would
        # itself race the winner's use of the same session object.
        yield db_session

    app.dependency_overrides[get_session] = _session_override
    app.dependency_overrides[get_current_user] = lambda: fixed_user
    settings = get_settings()
    app.state.redis = get_redis_client(settings.REDIS_URL)
    app.state.storage = get_storage_client(
        endpoint_url=settings.STORAGE_ENDPOINT_URL,
        access_key_id=settings.STORAGE_ACCESS_KEY_ID,
        secret_access_key=settings.STORAGE_SECRET_ACCESS_KEY,
    )

    idempotency_key = str(uuid4())
    headers = {"Idempotency-Key": idempotency_key}

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            project_resp = await client.post(
                "/v1/projects",
                json={"title": "Source material", "source_type": "pdf"},
                headers={"Idempotency-Key": str(uuid4())},
            )
            project_id = project_resp.json()["id"]

            response_a, response_b = await asyncio.gather(
                client.post(
                    f"/v1/projects/{project_id}/sources", files=_pdf_file(), headers=headers
                ),
                client.post(
                    f"/v1/projects/{project_id}/sources", files=_pdf_file(), headers=headers
                ),
            )

            list_resp = await client.get(f"/v1/projects/{project_id}/sources", headers=headers)
    finally:
        await app.state.redis.aclose()

    statuses = sorted([response_a.status_code, response_b.status_code])
    assert statuses == [201, 409]
    conflict = response_a if response_a.status_code == 409 else response_b
    assert conflict.json()["error"]["code"] == "idempotency_in_progress"
    assert len(list_resp.json()["items"]) == 1


# --- PDF parsing pipeline (inline, synchronous - one chunk per non-blank page) ------


async def test_multi_page_pdf_is_parsed_into_one_chunk_per_page(client_factory, auth_headers):
    headers = auth_headers()
    pdf = _make_pdf(["First page.", "Second page.", "Third page."])

    async with client_factory() as client:
        project_id = await _create_project(client, headers)

        upload_resp = await client.post(
            f"/v1/projects/{project_id}/sources",
            files=_pdf_file(pdf),
            headers={**headers, **IDEMPOTENCY_HEADERS},
        )
        assert upload_resp.status_code == 201
        uploaded = upload_resp.json()
        assert uploaded["status"] == "parsed"
        assert uploaded["page_count"] == 3
        source_id = uploaded["id"]

        chunks_resp = await client.get(
            f"/v1/projects/{project_id}/sources/{source_id}/chunks", headers=headers
        )

    assert chunks_resp.status_code == 200
    items = chunks_resp.json()["items"]
    assert [item["content"] for item in items] == ["First page.", "Second page.", "Third page."]
    assert [item["chunk_index"] for item in items] == [0, 1, 2]
    assert [(item["page_from"], item["page_to"]) for item in items] == [(1, 1), (2, 2), (3, 3)]
    for item in items:
        assert item["source_id"] == source_id
        assert item["project_id"] == project_id


async def test_blank_pages_are_skipped_when_chunking(client_factory, auth_headers):
    headers = auth_headers()
    pdf = _make_pdf(["Has text.", "", "Also has text."])

    async with client_factory() as client:
        project_id = await _create_project(client, headers)

        upload_resp = await client.post(
            f"/v1/projects/{project_id}/sources",
            files=_pdf_file(pdf),
            headers={**headers, **IDEMPOTENCY_HEADERS},
        )
        source_id = upload_resp.json()["id"]
        assert upload_resp.json()["page_count"] == 3

        chunks_resp = await client.get(
            f"/v1/projects/{project_id}/sources/{source_id}/chunks", headers=headers
        )

    items = chunks_resp.json()["items"]
    assert len(items) == 2  # the blank middle page produced no chunk
    assert [item["content"] for item in items] == ["Has text.", "Also has text."]
    assert [item["page_from"] for item in items] == [1, 3]


async def test_unparseable_pdf_is_marked_failed_not_rejected(client_factory, auth_headers):
    """Passes the `%PDF` magic-byte gate (a client-side shape check) but is
    genuinely corrupt - the upload still succeeds (the bytes are stored,
    traceable), just with ``status="failed"`` and no chunks, rather than a
    4xx that would suggest the *request* was malformed.
    """
    headers = auth_headers()

    async with client_factory() as client:
        project_id = await _create_project(client, headers)

        upload_resp = await client.post(
            f"/v1/projects/{project_id}/sources",
            files=_pdf_file(_CORRUPT_PDF),
            headers={**headers, **IDEMPOTENCY_HEADERS},
        )
        source_id = upload_resp.json()["id"]

        chunks_resp = await client.get(
            f"/v1/projects/{project_id}/sources/{source_id}/chunks", headers=headers
        )

    assert upload_resp.status_code == 201
    assert upload_resp.json()["status"] == "failed"
    assert upload_resp.json()["page_count"] is None
    assert chunks_resp.json()["items"] == []


async def test_pdf_exceeding_max_pages_is_marked_failed(client_factory, auth_headers, monkeypatch):
    headers = auth_headers()
    monkeypatch.setenv("MAX_PDF_PAGES", "1")
    get_settings.cache_clear()
    pdf = _make_pdf(["Page one.", "Page two."])

    try:
        async with client_factory() as client:
            project_id = await _create_project(client, headers)

            upload_resp = await client.post(
                f"/v1/projects/{project_id}/sources",
                files=_pdf_file(pdf),
                headers={**headers, **IDEMPOTENCY_HEADERS},
            )
            source_id = upload_resp.json()["id"]

            chunks_resp = await client.get(
                f"/v1/projects/{project_id}/sources/{source_id}/chunks", headers=headers
            )
    finally:
        get_settings.cache_clear()

    assert upload_resp.status_code == 201
    assert upload_resp.json()["status"] == "failed"
    assert upload_resp.json()["page_count"] == 2  # recorded even though it's over the limit
    assert chunks_resp.json()["items"] == []


# --- chunks endpoint ownership (rule 9: not-owned -> 404, never 403) ----------------


async def test_chunks_for_another_users_project_returns_404_not_403(client_factory, auth_headers):
    owner_headers = auth_headers()
    other_headers = auth_headers()

    async with client_factory() as client:
        project_id = await _create_project(client, owner_headers)
        upload_resp = await client.post(
            f"/v1/projects/{project_id}/sources",
            files=_pdf_file(),
            headers={**owner_headers, **IDEMPOTENCY_HEADERS},
        )
        source_id = upload_resp.json()["id"]

        response = await client.get(
            f"/v1/projects/{project_id}/sources/{source_id}/chunks", headers=other_headers
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_chunks_for_a_source_in_a_different_project_returns_404(
    client_factory, auth_headers
):
    """The source exists and the caller owns *a* project - just not the one
    named in the URL - so this must 404 exactly like a nonexistent source
    would (rule 9), not leak the source's existence via some other status.
    """
    headers = auth_headers()

    async with client_factory() as client:
        project_a = await _create_project(client, headers)
        project_b = await _create_project(client, headers)

        upload_resp = await client.post(
            f"/v1/projects/{project_a}/sources",
            files=_pdf_file(),
            headers={**headers, **IDEMPOTENCY_HEADERS},
        )
        source_id = upload_resp.json()["id"]

        response = await client.get(
            f"/v1/projects/{project_b}/sources/{source_id}/chunks", headers=headers
        )

    assert response.status_code == 404


async def test_chunks_for_nonexistent_source_returns_404(client_factory, auth_headers):
    headers = auth_headers()

    async with client_factory() as client:
        project_id = await _create_project(client, headers)

        response = await client.get(
            f"/v1/projects/{project_id}/sources/{uuid4()}/chunks", headers=headers
        )

    assert response.status_code == 404

"""``/v1/projects/{project_id}/sources`` - PDF upload.

Same no-real-Clerk pattern as ``test_projects_api.py``: tokens are signed
locally and ``providers.clerk.get_signing_key`` is monkeypatched.
``get_session`` is overridden to the transactional ``db_session`` fixture,
so Postgres writes never persist past a test. The storage side (MinIO) is
real and not transactional, so nothing here can roll a ``put_object`` back -
each project id is a fresh UUIDv7 (``projects/{project_id}/{checksum}.pdf``,
see ``domain/source_storage.py``), so *no* two test runs ever address the
same key, and every real upload would otherwise be a permanent orphan in
the dev MinIO. ``_track_uploads`` below wraps the storage client so
``client_factory`` deletes everything a test actually stored, whether or
not the test itself knew to ask for it - see
``test_every_uploaded_object_is_deleted_once_the_client_is_torn_down``.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from fpdf import FPDF
from sqlalchemy import select

from edutoon.api.dependencies import get_current_user
from edutoon.api.middleware import MULTIPART_OVERHEAD_BYTES
from edutoon.core.config import get_settings
from edutoon.db.session import get_session
from edutoon.domain.source_storage import source_storage_key
from edutoon.main import create_app
from edutoon.providers import clerk
from edutoon.providers.cache import get_redis_client
from edutoon.providers.storage import Storage, get_storage_client
from edutoon.repositories import jobs as jobs_repo
from edutoon.repositories import users as users_repo
from tests.idempotency_helpers import INVALID_KEYS, idempotency_status
from tests.pdf_helpers import make_pdf

TEST_ISSUER = "https://test.clerk.accounts.dev"
IDEMPOTENCY_HEADERS = {"Idempotency-Key": "test-key-1"}


_make_pdf = make_pdf


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


@asynccontextmanager
async def _track_uploads(storage: Storage) -> AsyncIterator[Storage]:
    """Wrap ``storage.put_object`` so every object it actually writes is
    deleted again on exit - real MinIO has no transaction to roll back, so
    without this every test that uploads leaves a permanent orphan behind
    (see the module docstring). Deletes unconditionally, whether the test
    that triggered the upload passed, failed, or never asked for cleanup.
    """
    created: list[tuple[str, str]] = []
    real_put_object = storage.put_object

    async def _tracking_put_object(
        *, bucket: str, key: str, body: bytes, content_type: str
    ) -> None:
        created.append((bucket, key))
        await real_put_object(bucket=bucket, key=key, body=body, content_type=content_type)

    storage.put_object = _tracking_put_object  # type: ignore[method-assign]
    try:
        yield storage
    finally:
        storage.put_object = real_put_object  # type: ignore[method-assign]
        for bucket, key in created:
            await storage.delete_object(bucket=bucket, key=key)


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
    raw_storage = get_storage_client(
        endpoint_url=settings.STORAGE_ENDPOINT_URL,
        access_key_id=settings.STORAGE_ACCESS_KEY_ID,
        secret_access_key=settings.STORAGE_SECRET_ACCESS_KEY,
    )

    def _make() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async with _track_uploads(raw_storage) as storage:
        app.state.storage = storage
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


async def test_upload_enqueues_a_build_evidence_job_for_the_new_source(
    client_factory, auth_headers, db_session
):
    """Phase E: a successful parse must enqueue the indexing job that lets
    a worker actually embed the chunks - this is what used to be missing,
    leaving every upload's chunks permanently unindexed.
    """
    headers = auth_headers()

    async with client_factory() as client:
        project_id = await _create_project(client, headers)

        upload_resp = await client.post(
            f"/v1/projects/{project_id}/sources",
            files=_pdf_file(),
            headers={**headers, **IDEMPOTENCY_HEADERS},
        )
        source_id = upload_resp.json()["id"]

    job = await jobs_repo.get_by_idempotency_key(
        db_session,
        project_id=UUID(project_id),
        kind="build_evidence",
        idempotency_key=source_id,
    )
    assert job is not None
    assert job.status == "queued"
    assert job.payload == {"source_id": source_id}


async def test_a_failed_parse_does_not_enqueue_an_indexing_job(
    client_factory, auth_headers, db_session
):
    headers = auth_headers()

    async with client_factory() as client:
        project_id = await _create_project(client, headers)

        upload_resp = await client.post(
            f"/v1/projects/{project_id}/sources",
            files=_pdf_file(_CORRUPT_PDF),
            headers={**headers, **IDEMPOTENCY_HEADERS},
        )
        source_id = upload_resp.json()["id"]
    assert upload_resp.json()["status"] == "failed"

    job = await jobs_repo.get_by_idempotency_key(
        db_session,
        project_id=UUID(project_id),
        kind="build_evidence",
        idempotency_key=source_id,
    )
    assert job is None


async def _project_status(client, headers, project_id) -> str:
    response = await client.get(f"/v1/projects/{project_id}", headers=headers)
    assert response.status_code == 200
    return response.json()["status"]  # type: ignore[no-any-return]


async def test_a_readable_upload_makes_the_project_processing_while_it_is_indexed(
    client_factory, auth_headers
):
    """Parsing succeeded, so a ``build_evidence`` job is queued: the pipeline is running."""
    headers = auth_headers()

    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        assert await _project_status(client, headers, project_id) == "draft"

        await client.post(
            f"/v1/projects/{project_id}/sources",
            files=_pdf_file(),
            headers={**headers, **IDEMPOTENCY_HEADERS},
        )

        assert await _project_status(client, headers, project_id) == "processing"


async def test_an_unreadable_upload_fails_the_project_since_it_has_no_job_to_say_so(
    client_factory, auth_headers
):
    headers = auth_headers()

    async with client_factory() as client:
        project_id = await _create_project(client, headers)

        await client.post(
            f"/v1/projects/{project_id}/sources",
            files=_pdf_file(_CORRUPT_PDF),
            headers={**headers, **IDEMPOTENCY_HEADERS},
        )

        assert await _project_status(client, headers, project_id) == "failed"


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
    raw_storage = get_storage_client(
        endpoint_url=settings.STORAGE_ENDPOINT_URL,
        access_key_id=settings.STORAGE_ACCESS_KEY_ID,
        secret_access_key=settings.STORAGE_SECRET_ACCESS_KEY,
    )

    idempotency_key = str(uuid4())
    headers = {"Idempotency-Key": idempotency_key}

    try:
        async with (
            _track_uploads(raw_storage) as storage,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            app.state.storage = storage
            project_resp = await client.post(
                "/v1/projects",
                json={"title": "Source material", "source_type": "pdf"},
                headers={"Idempotency-Key": str(uuid4())},
            )
            project_id = project_resp.json()["id"]

            # The project route commits before it answers, which leaves the shared
            # session with no transaction open. Both requests below read the project
            # on this one session before the idempotency lock, and two coroutines
            # cannot each start a transaction on it at once - so start it here. (In
            # production every request has its own session; this is only the test's
            # deliberate sharing.)
            await db_session.execute(select(1))

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


async def test_chunks_for_a_source_in_a_different_project_returns_404(client_factory, auth_headers):
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


# --- the Idempotency-Key header, and durability before the response --------------------


async def test_an_upload_is_committed_before_its_result_is_cached_or_returned(
    client_factory, auth_headers, db_session, redis_client, monkeypatch
):
    """The route commits for itself, before anything is cached: at the moment it does,
    the dedupe record must still say "in_progress" - a cached "completed" would promise
    work that a failed commit could still lose. (This module's session override also
    commits once the response is out, so the spy sees a second call; only the first
    is the route's own.)
    """
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        path = f"/v1/projects/{project_id}/sources"
        key = str(uuid4())
        seen_at_commit: list[str | None] = []
        real_commit = db_session.commit

        async def _spying_commit() -> None:
            seen_at_commit.append(await idempotency_status(redis_client, path, key))
            await real_commit()

        monkeypatch.setattr(db_session, "commit", _spying_commit)
        response = await client.post(
            path, files=_pdf_file(), headers={**headers, "Idempotency-Key": key}
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
        path = f"/v1/projects/{project_id}/sources"
        key = str(uuid4())

        async def _failing_commit() -> None:
            raise RuntimeError("the database went away")

        monkeypatch.setattr(db_session, "commit", _failing_commit)
        with pytest.raises(RuntimeError, match="went away"):
            await client.post(path, files=_pdf_file(), headers={**headers, "Idempotency-Key": key})

    # Nothing says the upload was done, and the key is free for the client's retry.
    assert await idempotency_status(redis_client, path, key) is None


@pytest.mark.parametrize("key", INVALID_KEYS)
async def test_an_empty_or_oversized_idempotency_key_is_rejected_and_nothing_is_written(
    client_factory, auth_headers, key
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        path = f"/v1/projects/{project_id}/sources"

        response = await client.post(
            path, files=_pdf_file(), headers={**headers, "Idempotency-Key": key}
        )

        assert response.status_code == 422
        [error] = response.json()["error"]["details"]["field_errors"]
        assert error["path"] == "header.idempotency-key"
        listed = await client.get(path, headers=headers)
        assert listed.json()["items"] == []
        jobs = await client.get(f"/v1/projects/{project_id}/jobs", headers=headers)
        assert jobs.json()["items"] == []


async def test_an_idempotency_key_of_the_maximum_length_is_accepted(client_factory, auth_headers):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        response = await client.post(
            f"/v1/projects/{project_id}/sources",
            files=_pdf_file(),
            headers={**headers, "Idempotency-Key": "k" * 255},
        )

    assert response.status_code == 201, response.text


# --- storage-isolation regression (real MinIO leaves nothing behind) ---------------


async def test_every_uploaded_object_is_deleted_once_tracking_exits(auth_headers, db_session):
    """Regression test for the leak this module used to have: every real
    upload wrote to real MinIO with a project id unique to the run, so
    nothing ever deduplicated across test sessions and every ``pytest``
    invocation left new orphaned objects behind forever.

    ``client_factory`` only tears down (and so only deletes) once the whole
    *test function* returns - a normal test body can never observe its own
    fixture's post-yield cleanup. So this drives ``_track_uploads`` - the
    exact mechanism ``client_factory`` wraps its storage client in -
    directly, uploading through it via a real request and then asserting
    the object is genuinely gone from MinIO, not merely absent from the
    database, once tracking's own context exits.
    """
    headers = auth_headers()
    settings = get_settings()
    verifier = get_storage_client(
        endpoint_url=settings.STORAGE_ENDPOINT_URL,
        access_key_id=settings.STORAGE_ACCESS_KEY_ID,
        secret_access_key=settings.STORAGE_SECRET_ACCESS_KEY,
    )
    raw_storage = get_storage_client(
        endpoint_url=settings.STORAGE_ENDPOINT_URL,
        access_key_id=settings.STORAGE_ACCESS_KEY_ID,
        secret_access_key=settings.STORAGE_SECRET_ACCESS_KEY,
    )

    app = create_app()

    async def _session_override():
        yield db_session

    key: str | None = None
    async with _track_uploads(raw_storage) as tracked:
        app.dependency_overrides[get_session] = _session_override
        app.state.redis = get_redis_client(settings.REDIS_URL)
        app.state.storage = tracked

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            project_id = await _create_project(client, headers)
            upload_resp = await client.post(
                f"/v1/projects/{project_id}/sources",
                files=_pdf_file(),
                headers={**headers, **IDEMPOTENCY_HEADERS},
            )
            assert upload_resp.status_code == 201
            key = source_storage_key(UUID(project_id), hashlib.sha256(_DEFAULT_PDF).hexdigest())

            assert await verifier.object_exists(bucket=settings.BUCKET_UPLOADS, key=key)

        await app.state.redis.aclose()

    assert key is not None
    assert not await verifier.object_exists(bucket=settings.BUCKET_UPLOADS, key=key)


# --- retrying a failed indexing ---------------------------------------------------------------


async def _fail_indexing(db_session, project_id: str, source_id: str) -> None:
    job = await jobs_repo.get_by_idempotency_key(
        db_session, project_id=UUID(project_id), kind="build_evidence", idempotency_key=source_id
    )
    assert job is not None
    await jobs_repo.update(db_session, job_id=job.id, status="running", started_at=job.created_at)
    await jobs_repo.update(
        db_session, job_id=job.id, status="failed", finished_at=job.created_at, error="x"
    )


async def _uploaded(client, headers, project_id: str, content: bytes = _DEFAULT_PDF) -> str:
    response = await client.post(
        f"/v1/projects/{project_id}/sources",
        files=_pdf_file(content),
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 201
    return response.json()["id"]  # type: ignore[no-any-return]


async def test_a_failed_indexing_can_be_retried_through_the_api(
    client_factory, auth_headers, db_session
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        source_id = await _uploaded(client, headers, project_id)
        await _fail_indexing(db_session, project_id, source_id)

        response = await client.post(
            f"/v1/projects/{project_id}/sources/{source_id}/index",
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )

    assert response.status_code == 202
    body = response.json()
    assert (body["kind"], body["status"], body["attempts"], body["error"]) == (
        "build_evidence",
        "queued",
        0,
        None,
    )
    assert body["payload"] == {"source_id": source_id}


async def test_a_retry_that_repeats_its_key_replays_instead_of_running_twice(
    client_factory, auth_headers, db_session
):
    headers = {**auth_headers(), "Idempotency-Key": str(uuid4())}
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        source_id = await _uploaded(client, headers, project_id)
        await _fail_indexing(db_session, project_id, source_id)
        url = f"/v1/projects/{project_id}/sources/{source_id}/index"

        first = await client.post(url, headers=headers)
        second = await client.post(url, headers=headers)  # same key: a replay, not a conflict

    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]


async def test_retrying_an_indexing_that_is_still_queued_is_a_409(client_factory, auth_headers):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        source_id = await _uploaded(client, headers, project_id)

        response = await client.post(
            f"/v1/projects/{project_id}/sources/{source_id}/index",
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )

    assert response.status_code == 409


async def test_retrying_another_users_source_is_a_404_not_a_403(
    client_factory, auth_headers, db_session
):
    owner, other = auth_headers(), auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, owner)
        source_id = await _uploaded(client, owner, project_id)
        await _fail_indexing(db_session, project_id, source_id)

        response = await client.post(
            f"/v1/projects/{project_id}/sources/{source_id}/index",
            headers={**other, "Idempotency-Key": str(uuid4())},
        )

    assert response.status_code == 404


async def test_a_source_from_a_different_project_is_a_404(client_factory, auth_headers):
    headers = auth_headers()
    async with client_factory() as client:
        first_project = await _create_project(client, headers)
        second_project = await _create_project(client, headers)
        source_id = await _uploaded(client, headers, first_project)

        response = await client.post(
            f"/v1/projects/{second_project}/sources/{source_id}/index",
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )

    assert response.status_code == 404


async def test_retrying_without_an_idempotency_key_is_rejected(client_factory, auth_headers):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        source_id = await _uploaded(client, headers, project_id)

        response = await client.post(
            f"/v1/projects/{project_id}/sources/{source_id}/index", headers=headers
        )

    assert response.status_code == 422


# --- the hourly limit on paid work --------------------------------------------------------------


async def test_an_upload_over_the_projects_hourly_limit_is_refused_before_anything_is_stored(
    client_factory, auth_headers, monkeypatch
):
    monkeypatch.setenv("MAX_PROVIDER_JOBS_PER_PROJECT_PER_HOUR", "1")
    get_settings.cache_clear()
    settings = get_settings()
    verifier = get_storage_client(
        endpoint_url=settings.STORAGE_ENDPOINT_URL,
        access_key_id=settings.STORAGE_ACCESS_KEY_ID,
        secret_access_key=settings.STORAGE_SECRET_ACCESS_KEY,
    )
    headers = auth_headers()
    second_pdf = _make_pdf(["A second, different document."])
    try:
        async with client_factory() as client:
            project_id = await _create_project(client, headers)
            first = await client.post(
                f"/v1/projects/{project_id}/sources",
                files=_pdf_file(),
                headers={**headers, "Idempotency-Key": str(uuid4())},
            )
            second = await client.post(
                f"/v1/projects/{project_id}/sources",
                files=_pdf_file(second_pdf, "second.pdf"),
                headers={**headers, "Idempotency-Key": str(uuid4())},
            )
    finally:
        get_settings.cache_clear()

    assert first.status_code == 201
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "too_many_requests"
    key = source_storage_key(UUID(project_id), hashlib.sha256(second_pdf).hexdigest())
    assert not await verifier.object_exists(bucket=settings.BUCKET_UPLOADS, key=key)


# --- a document with no text, and the size guard, through the real app ---------------------


async def test_the_real_app_refuses_an_oversized_declared_body_before_parsing_it(
    client_factory, auth_headers
) -> None:
    headers = auth_headers()
    settings = get_settings()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        response = await client.post(
            f"/v1/projects/{project_id}/sources",
            files=_pdf_file(),
            headers={
                **headers,
                **IDEMPOTENCY_HEADERS,
                "content-length": str(settings.MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD_BYTES + 1),
            },
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


def _scan(pages: int = 2) -> bytes:
    """A PDF whose pages carry no text at all - what a scan looks like to a text extractor."""
    doc = FPDF()
    for _ in range(pages):
        doc.add_page()
    return bytes(doc.output())


async def _upload(client, headers, project_id: str, content: bytes) -> httpx.Response:
    return await client.post(
        f"/v1/projects/{project_id}/sources",
        files=_pdf_file(content),
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )


async def test_a_scanned_pdf_fails_clearly_and_is_never_queued_for_indexing(
    client_factory, auth_headers, db_session
) -> None:
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        response = await _upload(client, headers, project_id, _scan(3))
        source_id = response.json()["id"]
        chunks = await client.get(
            f"/v1/projects/{project_id}/sources/{source_id}/chunks", headers=headers
        )

    body = response.json()
    assert response.status_code == 201
    assert (body["status"], body["page_count"]) == ("failed", 3)
    assert "No text could be read from this PDF" in body["failure_reason"]
    assert "scan" in body["failure_reason"] and "OCR" in body["failure_reason"]
    assert chunks.json()["items"] == []
    job = await jobs_repo.get_by_idempotency_key(
        db_session, project_id=UUID(project_id), kind="build_evidence", idempotency_key=source_id
    )
    assert job is None  # nothing to index, so nothing is queued


async def test_a_pdf_of_nothing_but_page_numbers_is_also_no_text(
    client_factory, auth_headers
) -> None:
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        response = await _upload(client, headers, project_id, _make_pdf(["1", "2", "3", "- 4 -"]))

    assert response.json()["status"] == "failed"
    assert "No text could be read" in response.json()["failure_reason"]


async def test_a_pdf_with_some_text_and_some_blank_pages_is_still_parsed(
    client_factory, auth_headers
) -> None:
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        response = await _upload(client, headers, project_id, _make_pdf(["", "Real words.", ""]))

    assert response.json()["status"] == "parsed" and response.json()["failure_reason"] is None


async def test_every_reason_a_source_can_fail_is_told_to_its_owner_accurately(
    client_factory, auth_headers, monkeypatch
) -> None:
    headers = auth_headers()
    monkeypatch.setenv("MAX_PDF_PAGES", "2")
    get_settings.cache_clear()
    try:
        async with client_factory() as client:
            project_id = await _create_project(client, headers)
            unreadable = await _upload(client, headers, project_id, b"%PDF-1.4\n" + b"\x00garbage")
            too_long = await _upload(client, headers, project_id, _make_pdf(["a", "b", "c"]))
            listing = await client.get(f"/v1/projects/{project_id}/sources", headers=headers)
    finally:
        get_settings.cache_clear()

    assert "could not be read" in unreadable.json()["failure_reason"]
    assert unreadable.json()["page_count"] is None
    assert "has 3 pages; the limit is 2" in too_long.json()["failure_reason"]
    # The reason is the same on every read, not just at upload.
    reasons = {item["id"]: item["failure_reason"] for item in listing.json()["items"]}
    assert reasons[unreadable.json()["id"]] == unreadable.json()["failure_reason"]
    assert reasons[too_long.json()["id"]] == too_long.json()["failure_reason"]


async def test_a_source_that_did_not_fail_has_no_failure_reason(
    client_factory, auth_headers
) -> None:
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        response = await _upload(client, headers, project_id, _make_pdf(["Some real text."]))

    assert response.json()["failure_reason"] is None


async def test_a_stored_file_of_a_failed_source_is_kept_for_traceability(
    client_factory, auth_headers
) -> None:
    """A failed source still has its object and its row: the upload succeeded, parsing did not."""
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        response = await _upload(client, headers, project_id, _scan())

    assert response.status_code == 201 and response.json()["byte_size"] > 0

"""Deleting a project also removes the PDFs it uploaded to object storage.

Before this, ``DELETE /v1/projects/{id}`` removed the database rows (sources, topics, jobs,
scripts all cascade) but left every uploaded file behind in storage for ever. These tests run
against the real MinIO the upload tests already use, and prove both halves of "safe cleanup":
what is removed, and - just as important - what is not.

Same no-real-Clerk pattern as ``test_uploaded_sources_api.py``; ``get_session`` is the
transactional ``db_session`` fixture, so Postgres writes never persist past a test. Storage is
real and not transactional, so every test that puts an object removes it itself (the upload
key is content-addressed and per project, and a project id is unique to the test).
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import AsyncIterator, Callable
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from fpdf import FPDF

from edutoon.core.config import get_settings
from edutoon.db.session import get_session
from edutoon.domain.source_storage import source_storage_key
from edutoon.main import create_app
from edutoon.providers import clerk
from edutoon.providers.cache import get_redis_client
from edutoon.providers.storage import Storage, get_storage_client
from edutoon.repositories import uploaded_sources as uploaded_sources_repo
from edutoon.services import projects as projects_service

TEST_ISSUER = "https://test.clerk.accounts.dev"


def _make_pdf(text: str = "Some content.") -> bytes:
    doc = FPDF()
    doc.add_page()
    doc.set_font("Helvetica", size=12)
    doc.cell(text=text)
    return bytes(doc.output())


# Starts with %PDF (so the upload accepts it) but cannot be parsed: stored, with status "failed".
_CORRUPT_PDF = b"%PDF-1.4\n" + b"\x00\x01garbage" * 40


@pytest.fixture(scope="module")
def rsa_keypair() -> tuple[bytes, RSAPublicKey]:
    private_key: RSAPrivateKey = rsa.generate_private_key(public_exponent=65537, key_size=2048)
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
def storage() -> Storage:
    settings = get_settings()
    return get_storage_client(
        endpoint_url=settings.STORAGE_ENDPOINT_URL,
        access_key_id=settings.STORAGE_ACCESS_KEY_ID,
        secret_access_key=settings.STORAGE_SECRET_ACCESS_KEY,
    )


@pytest.fixture
async def client_factory(db_session, storage) -> AsyncIterator[Callable[[], httpx.AsyncClient]]:
    app = create_app()

    async def _session_override():
        yield db_session

    app.dependency_overrides[get_session] = _session_override
    app.state.redis = get_redis_client(get_settings().REDIS_URL)
    app.state.storage = storage

    def _make() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    yield _make

    await app.state.redis.aclose()


@pytest.fixture
async def cleanup(storage) -> AsyncIterator[list[tuple[str, str]]]:
    """Objects a test put in storage; removed afterwards whatever the test did."""
    created: list[tuple[str, str]] = []
    yield created
    for bucket, key in created:
        await storage.delete_object(bucket=bucket, key=key)


BUCKET = get_settings().BUCKET_UPLOADS


def _idempotent(headers: dict[str, str]) -> dict[str, str]:
    return {**headers, "Idempotency-Key": str(uuid4())}


async def _create_project(client: httpx.AsyncClient, headers: dict[str, str]) -> str:
    response = await client.post(
        "/v1/projects",
        json={"title": "To be deleted", "source_type": "pdf"},
        headers=_idempotent(headers),
    )
    assert response.status_code == 201
    return response.json()["id"]  # type: ignore[no-any-return]


async def _upload(
    client: httpx.AsyncClient, headers: dict[str, str], project_id: str, content: bytes
) -> str:
    """Upload a PDF; returns its storage key."""
    response = await client.post(
        f"/v1/projects/{project_id}/sources",
        files={"file": ("paper.pdf", content, "application/pdf")},
        headers=_idempotent(headers),
    )
    assert response.status_code == 201, response.text
    return source_storage_key(UUID(project_id), hashlib.sha256(content).hexdigest())


async def _delete(client: httpx.AsyncClient, headers: dict[str, str], project_id: str):
    return await client.delete(f"/v1/projects/{project_id}", headers=_idempotent(headers))


# --- what is removed ------------------------------------------------------------------


async def test_deleting_a_project_removes_its_uploaded_pdf_from_storage(
    client_factory, auth_headers, storage, cleanup
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        key = await _upload(client, headers, project_id, _make_pdf())
        cleanup.append((BUCKET, key))
        assert await storage.object_exists(bucket=BUCKET, key=key) is True

        response = await _delete(client, headers, project_id)

        assert response.status_code == 204
        assert (await client.get(f"/v1/projects/{project_id}", headers=headers)).status_code == 404

    assert await storage.object_exists(bucket=BUCKET, key=key) is False


async def test_every_file_of_the_project_is_removed_including_an_unreadable_one(
    client_factory, auth_headers, storage, cleanup
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        keys = [
            await _upload(client, headers, project_id, _make_pdf("first")),
            await _upload(client, headers, project_id, _make_pdf("second")),
            await _upload(client, headers, project_id, _CORRUPT_PDF),  # stored, status "failed"
        ]
        cleanup.extend((BUCKET, key) for key in keys)
        assert len(set(keys)) == 3

        response = await _delete(client, headers, project_id)

    assert response.status_code == 204
    for key in keys:
        assert await storage.object_exists(bucket=BUCKET, key=key) is False


async def test_a_project_with_no_uploads_deletes_cleanly(client_factory, auth_headers):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)

        response = await _delete(client, headers, project_id)

    assert response.status_code == 204


# --- what is NOT removed --------------------------------------------------------------


async def test_another_projects_file_is_left_alone_even_with_identical_bytes(
    client_factory, auth_headers, storage, cleanup
):
    """Same PDF, two projects: two objects (the key includes the project), and deleting one
    project must not touch the other's.
    """
    headers = auth_headers()
    same_bytes = _make_pdf("identical")
    async with client_factory() as client:
        doomed = await _create_project(client, headers)
        survivor = await _create_project(client, headers)
        doomed_key = await _upload(client, headers, doomed, same_bytes)
        survivor_key = await _upload(client, headers, survivor, same_bytes)
        cleanup.extend([(BUCKET, doomed_key), (BUCKET, survivor_key)])
        assert doomed_key != survivor_key

        await _delete(client, headers, doomed)

        assert (await client.get(f"/v1/projects/{survivor}", headers=headers)).status_code == 200

    assert await storage.object_exists(bucket=BUCKET, key=doomed_key) is False
    assert await storage.object_exists(bucket=BUCKET, key=survivor_key) is True


async def test_a_row_pointing_outside_the_projects_prefix_is_never_deleted(
    client_factory, auth_headers, db_session, storage, cleanup
):
    """Defence in depth: a source row whose key is not inside ``projects/{id}/`` (a bad row, a
    bug, a hand-edit) must not turn a project deletion into deleting someone else's object.
    """
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        unrelated_key = f"unrelated/{uuid4().hex}.pdf"
        await storage.put_object(
            bucket=BUCKET, key=unrelated_key, body=b"not this project's", content_type="text/plain"
        )
        cleanup.append((BUCKET, unrelated_key))
        await uploaded_sources_repo.create(
            db_session,
            project_id=UUID(project_id),
            original_filename="stray.pdf",
            storage_bucket=BUCKET,
            storage_key=unrelated_key,
            byte_size=10,
            checksum_sha256="b" * 64,
        )
        own_key = await _upload(client, headers, project_id, _make_pdf())
        cleanup.append((BUCKET, own_key))

        response = await _delete(client, headers, project_id)

    assert response.status_code == 204
    assert await storage.object_exists(bucket=BUCKET, key=own_key) is False
    assert await storage.object_exists(bucket=BUCKET, key=unrelated_key) is True


async def test_someone_elses_delete_is_a_404_and_removes_nothing(
    client_factory, auth_headers, storage, cleanup
):
    owner, intruder = auth_headers(), auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, owner)
        key = await _upload(client, owner, project_id, _make_pdf())
        cleanup.append((BUCKET, key))

        response = await _delete(client, intruder, project_id)

        assert response.status_code == 404
        assert (await client.get(f"/v1/projects/{project_id}", headers=owner)).status_code == 200

    assert await storage.object_exists(bucket=BUCKET, key=key) is True


# --- consistency: the database decides ------------------------------------------------


async def test_a_deletion_that_fails_to_commit_leaves_the_file_in_place(
    client_factory, auth_headers, db_session, storage, cleanup, monkeypatch
):
    """Files go only after the deletion is committed. If the commit fails the project still
    exists, so its PDF must too - never a project pointing at a file that is gone.
    """
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        key = await _upload(client, headers, project_id, _make_pdf())
        cleanup.append((BUCKET, key))

        async def _failing_commit() -> None:
            raise RuntimeError("the database went away")

        monkeypatch.setattr(db_session, "commit", _failing_commit)
        with pytest.raises(RuntimeError, match="went away"):
            await _delete(client, headers, project_id)

    assert await storage.object_exists(bucket=BUCKET, key=key) is True


async def test_a_failure_to_remove_a_file_does_not_undo_or_fail_the_deletion(
    client_factory, auth_headers, storage, cleanup, monkeypatch
):
    headers = auth_headers()
    real_delete = Storage.delete_object
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        key = await _upload(client, headers, project_id, _make_pdf())
        cleanup.append((BUCKET, key))

        async def _storage_is_down(self, *, bucket: str, key: str) -> None:
            raise ConnectionError("object storage is unreachable")

        monkeypatch.setattr(Storage, "delete_object", _storage_is_down)
        response = await _delete(client, headers, project_id)

        assert response.status_code == 204
        assert (await client.get(f"/v1/projects/{project_id}", headers=headers)).status_code == 404

    # The project is gone; the file is an orphan (logged for cleanup), not a reason to fail.
    monkeypatch.setattr(Storage, "delete_object", real_delete)
    assert await storage.object_exists(bucket=BUCKET, key=key) is True


async def test_replaying_a_delete_removes_files_once_not_twice(
    client_factory, auth_headers, storage, cleanup, monkeypatch
):
    headers = auth_headers()
    removed: list[str] = []
    real_delete = Storage.delete_object

    async def _recording_delete(self, *, bucket: str, key: str) -> None:
        removed.append(key)
        await real_delete(self, bucket=bucket, key=key)

    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        key = await _upload(client, headers, project_id, _make_pdf())
        cleanup.append((BUCKET, key))

        monkeypatch.setattr(Storage, "delete_object", _recording_delete)
        request_headers = {**headers, "Idempotency-Key": str(uuid4())}
        first = await client.delete(f"/v1/projects/{project_id}", headers=request_headers)
        second = await client.delete(f"/v1/projects/{project_id}", headers=request_headers)

    assert first.status_code == second.status_code == 204
    assert removed == [key]


# --- the service on its own -----------------------------------------------------------


async def test_remove_stored_files_never_raises_and_carries_on_past_a_failure(storage, cleanup):
    project_id = uuid4()
    good = source_storage_key(project_id, "c" * 64)
    bad = source_storage_key(project_id, "d" * 64)
    await storage.put_object(bucket=BUCKET, key=good, body=b"x", content_type="text/plain")
    cleanup.append((BUCKET, good))

    class _FlakyStorage(Storage):
        def __init__(self) -> None:
            super().__init__(client=None)

        async def delete_object(self, *, bucket: str, key: str) -> None:
            if key == bad:
                raise ConnectionError("boom")
            await storage.delete_object(bucket=bucket, key=key)

    await projects_service.remove_stored_files(
        _FlakyStorage(),
        project_id=project_id,
        files=[
            uploaded_sources_repo.StorageLocation(bucket=BUCKET, key=bad),
            uploaded_sources_repo.StorageLocation(bucket=BUCKET, key=good),
        ],
    )

    assert await storage.object_exists(bucket=BUCKET, key=good) is False

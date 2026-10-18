"""``/v1/projects/{project_id}/scripts/{script_id}/synthesis`` and
``.../segments/{segment_id}/audio`` - Phase 8's HTTP surface.

Same no-real-Clerk pattern as ``test_scripts_api.py`` (tokens signed
locally, ``providers.clerk.get_signing_key`` monkeypatched) and the same
``get_session`` override so nothing here persists past its own rolled-back
transaction. Storage is real (the local MinIO ``test_project_deletion_storage.py``
already uses); where a test needs a segment that has actually reached
``ready``, it drives ``services.script_audio.run_synthesise_voice_job``
directly with the real Piper voice - exactly how ``test_scripts_api.py``
drives ``run_generate_script_job`` directly rather than through the worker.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey

from edutoon.core.config import get_settings
from edutoon.db.session import get_session
from edutoon.main import create_app
from edutoon.providers import clerk
from edutoon.providers.cache import get_redis_client
from edutoon.providers.storage import Storage, get_storage_client
from edutoon.repositories import scripts as scripts_repo
from edutoon.repositories.scripts import NewScriptSegment
from edutoon.services import script_audio as script_audio_service
from tests.idempotency_helpers import INVALID_KEYS

MODEL = "claude-sonnet-5"
TEST_ISSUER = "https://test.clerk.accounts.dev"
VOICES_DIR = Path(__file__).resolve().parents[1] / "var" / "piper-voices"
VOICE_ID = "en_GB-cori-medium"

pytestmark = pytest.mark.skipif(
    not (VOICES_DIR / f"{VOICE_ID}.onnx").exists(),
    reason=f"Piper voice {VOICE_ID} not downloaded - run `make voices` first",
)


# --- auth + app plumbing (mirrors test_scripts_api.py) ---------------------------------------


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
    private_pem, public_key = rsa_keypair
    monkeypatch.setattr(clerk, "get_signing_key", lambda token: public_key)

    def _make() -> dict[str, str]:
        token = _make_token(private_pem, sub=f"clerk_{uuid4()}", email=f"{uuid4()}@example.com")
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
    created: list[tuple[str, str]] = []
    yield created
    for bucket, key in created:
        await storage.delete_object(bucket=bucket, key=key)


def _headers(base: dict[str, str], key: str | None = None) -> dict[str, str]:
    return {**base, "Idempotency-Key": key or str(uuid4())}


# --- fixtures: a project with an approved script --------------------------------------------


async def _create_project(client: httpx.AsyncClient, headers: dict[str, str], **fields) -> str:
    body = {"title": "How photosynthesis works", "source_type": "pdf", **fields}
    response = await client.post("/v1/projects", json=body, headers=_headers(headers))
    assert response.status_code == 201, response.text
    return response.json()["id"]  # type: ignore[no-any-return]


async def _approve_script(db_session, project_id: str, texts: list[str]) -> str:
    script = await scripts_repo.create(db_session, project_id=UUID(project_id), llm_model=MODEL)
    await scripts_repo.add_segments(
        db_session,
        script_id=script.id,
        segments=[
            NewScriptSegment(segment_index=i, narration_text=text, estimated_duration_ms=1000)
            for i, text in enumerate(texts)
        ],
    )
    approved = await scripts_repo.set_status(
        db_session, project_id=UUID(project_id), script_id=script.id, status="approved"
    )
    assert approved is not None
    return str(script.id)


def _synthesis_url(project_id: str, script_id: str) -> str:
    return f"/v1/projects/{project_id}/scripts/{script_id}/synthesis"


def _audio_url(project_id: str, script_id: str, segment_id: str) -> str:
    return f"/v1/projects/{project_id}/scripts/{script_id}/segments/{segment_id}/audio"


# --- enqueue -----------------------------------------------------------------------------------


async def test_enqueueing_synthesis_for_an_approved_script_returns_202(
    client_factory, auth_headers, db_session
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        script_id = await _approve_script(db_session, project_id, ["Hello world."])
        await db_session.commit()

        response = await client.post(
            _synthesis_url(project_id, script_id), headers=_headers(headers)
        )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["kind"] == "synthesise_voice"
    assert body["status"] == "queued"


async def test_enqueueing_synthesis_for_a_non_approved_script_is_a_conflict(
    client_factory, auth_headers, db_session
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        script = await scripts_repo.create(db_session, project_id=UUID(project_id), llm_model=MODEL)
        await db_session.commit()

        response = await client.post(
            _synthesis_url(project_id, str(script.id)), headers=_headers(headers)
        )

    assert response.status_code == 409, response.text


async def test_enqueueing_synthesis_without_an_idempotency_key_is_a_422(
    client_factory, auth_headers, db_session
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        script_id = await _approve_script(db_session, project_id, ["Hello world."])
        await db_session.commit()

        response = await client.post(_synthesis_url(project_id, script_id), headers=headers)

    assert response.status_code == 422


@pytest.mark.parametrize("key", INVALID_KEYS)
async def test_an_invalid_idempotency_key_is_refused(client_factory, auth_headers, db_session, key):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        script_id = await _approve_script(db_session, project_id, ["Hello world."])
        await db_session.commit()

        response = await client.post(
            _synthesis_url(project_id, script_id), headers={**headers, "Idempotency-Key": key}
        )

    assert response.status_code == 422


async def test_an_unknown_body_field_is_rejected_with_422(client_factory, auth_headers, db_session):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        script_id = await _approve_script(db_session, project_id, ["Hello world."])
        await db_session.commit()

        response = await client.post(
            _synthesis_url(project_id, script_id),
            json={"unexpected_field": "surprise"},
            headers=_headers(headers),
        )

    assert response.status_code == 422


async def test_a_repeat_idempotency_key_replays_the_original_job(
    client_factory, auth_headers, db_session
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        script_id = await _approve_script(db_session, project_id, ["Hello world."])
        await db_session.commit()
        request_headers = _headers(headers)

        first = await client.post(_synthesis_url(project_id, script_id), headers=request_headers)
        second = await client.post(_synthesis_url(project_id, script_id), headers=request_headers)

    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]


async def test_enqueueing_for_someone_elses_project_is_a_404(
    client_factory, auth_headers, db_session
):
    owner, intruder = auth_headers(), auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, owner)
        script_id = await _approve_script(db_session, project_id, ["Hello world."])
        await db_session.commit()

        response = await client.post(
            _synthesis_url(project_id, script_id), headers=_headers(intruder)
        )

    assert response.status_code == 404


async def test_enqueueing_for_a_missing_script_is_a_404(client_factory, auth_headers):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)

        response = await client.post(
            _synthesis_url(project_id, str(uuid4())), headers=_headers(headers)
        )

    assert response.status_code == 404


# --- status ------------------------------------------------------------------------------------


async def test_status_lists_every_segment_before_any_audio_exists(
    client_factory, auth_headers, db_session
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        script_id = await _approve_script(db_session, project_id, ["First.", "Second."])
        await db_session.commit()

        response = await client.get(_synthesis_url(project_id, script_id), headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["segments"]) == 2
    assert all(segment["audio"] is None for segment in body["segments"])
    assert body["script"]["measured_duration_ms"] is None


async def test_status_for_someone_elses_project_is_a_404(client_factory, auth_headers, db_session):
    owner, intruder = auth_headers(), auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, owner)
        script_id = await _approve_script(db_session, project_id, ["Hello world."])
        await db_session.commit()

        response = await client.get(_synthesis_url(project_id, script_id), headers=intruder)

    assert response.status_code == 404


async def test_status_reflects_real_synthesised_audio(
    client_factory, auth_headers, db_session, storage, cleanup
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        script_id = await _approve_script(db_session, project_id, ["Hello world."])
        await db_session.commit()

        enqueue_response = await client.post(
            _synthesis_url(project_id, script_id), headers=_headers(headers)
        )
        job_id = UUID(enqueue_response.json()["id"])
        finished = await script_audio_service.run_synthesise_voice_job(
            db_session, storage, job_id=job_id, voices_dir=VOICES_DIR
        )
        assert finished.status == "succeeded"

        response = await client.get(_synthesis_url(project_id, script_id), headers=headers)

    body = response.json()
    [segment] = body["segments"]
    assert segment["audio"]["status"] == "ready"
    assert segment["audio"]["measured_duration_ms"] > 0
    assert segment["audio"]["voice_model"] == get_settings().PIPER_VOICE_EN_GB
    assert body["script"]["measured_duration_ms"] == segment["audio"]["measured_duration_ms"]
    cleanup.append((get_settings().BUCKET_SEGMENTS, _key_for(body)))


def _key_for(status_body: dict) -> str:
    # Reconstructed only for cleanup bookkeeping; the API never exposes the
    # storage key itself (no filesystem/storage internals in responses).
    from edutoon.domain.script_audio import segment_audio_storage_key

    return segment_audio_storage_key(
        project_id=UUID(status_body["script"]["project_id"]),
        script_id=UUID(status_body["script"]["id"]),
        segment_id=UUID(status_body["segments"][0]["id"]),
    )


# --- streaming playback --------------------------------------------------------------------


async def test_playing_audio_that_does_not_exist_yet_is_a_404(
    client_factory, auth_headers, db_session
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        script_id = await _approve_script(db_session, project_id, ["Hello world."])
        await db_session.commit()
        status_response = await client.get(_synthesis_url(project_id, script_id), headers=headers)
        segment_id = status_response.json()["segments"][0]["id"]

        response = await client.get(_audio_url(project_id, script_id, segment_id), headers=headers)

    assert response.status_code == 404


async def test_playing_real_synthesised_audio_returns_wav_bytes(
    client_factory, auth_headers, db_session, storage, cleanup
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        script_id = await _approve_script(db_session, project_id, ["Playback over HTTP."])
        await db_session.commit()
        enqueue_response = await client.post(
            _synthesis_url(project_id, script_id), headers=_headers(headers)
        )
        job_id = UUID(enqueue_response.json()["id"])
        await script_audio_service.run_synthesise_voice_job(
            db_session, storage, job_id=job_id, voices_dir=VOICES_DIR
        )
        status_response = await client.get(_synthesis_url(project_id, script_id), headers=headers)
        segment_id = status_response.json()["segments"][0]["id"]
        cleanup.append((get_settings().BUCKET_SEGMENTS, _key_for(status_response.json())))

        response = await client.get(_audio_url(project_id, script_id, segment_id), headers=headers)

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.content[:4] == b"RIFF"  # a real WAV file header


async def test_playing_audio_supports_range_requests(
    client_factory, auth_headers, db_session, storage, cleanup
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        script_id = await _approve_script(
            db_session, project_id, ["A longer sentence so there is real audio to seek within."]
        )
        await db_session.commit()
        enqueue_response = await client.post(
            _synthesis_url(project_id, script_id), headers=_headers(headers)
        )
        job_id = UUID(enqueue_response.json()["id"])
        await script_audio_service.run_synthesise_voice_job(
            db_session, storage, job_id=job_id, voices_dir=VOICES_DIR
        )
        status_response = await client.get(_synthesis_url(project_id, script_id), headers=headers)
        segment_id = status_response.json()["segments"][0]["id"]
        cleanup.append((get_settings().BUCKET_SEGMENTS, _key_for(status_response.json())))

        response = await client.get(
            _audio_url(project_id, script_id, segment_id),
            headers={**headers, "Range": "bytes=0-999"},
        )

    assert response.status_code == 206
    assert "content-range" in response.headers
    assert len(response.content) == 1000


async def test_playing_audio_from_someone_elses_project_is_a_404(
    client_factory, auth_headers, db_session, storage, cleanup
):
    owner, intruder = auth_headers(), auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, owner)
        script_id = await _approve_script(db_session, project_id, ["Private narration."])
        await db_session.commit()
        enqueue_response = await client.post(
            _synthesis_url(project_id, script_id), headers=_headers(owner)
        )
        job_id = UUID(enqueue_response.json()["id"])
        await script_audio_service.run_synthesise_voice_job(
            db_session, storage, job_id=job_id, voices_dir=VOICES_DIR
        )
        status_response = await client.get(_synthesis_url(project_id, script_id), headers=owner)
        segment_id = status_response.json()["segments"][0]["id"]
        cleanup.append((get_settings().BUCKET_SEGMENTS, _key_for(status_response.json())))

        response = await client.get(_audio_url(project_id, script_id, segment_id), headers=intruder)

    assert response.status_code == 404

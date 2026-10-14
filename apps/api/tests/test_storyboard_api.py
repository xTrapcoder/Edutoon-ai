"""``/v1/projects/{project_id}/scripts/{script_id}/storyboard`` - Phase 9's
HTTP surface.

Same no-real-Clerk pattern as ``test_scripts_api.py`` / ``test_script_audio_api.py``
(tokens signed locally, ``providers.clerk.get_signing_key`` monkeypatched) and the
same ``get_session`` override so nothing here persists past its own rolled-back
transaction. The API layer never touches the LLM itself (only the worker's
``run_generate_storyboard_job`` does) - where a test needs a script that has
actually been storyboarded, it drives that function directly with a fake ``LLM``,
exactly as ``test_scripts_api.py`` drives ``run_generate_script_job``.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
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

from edutoon.core.config import get_settings
from edutoon.db.session import get_session
from edutoon.domain.script_audio import compute_content_hash
from edutoon.main import create_app
from edutoon.providers import clerk
from edutoon.providers import tts as tts_provider
from edutoon.providers.cache import get_redis_client
from edutoon.providers.llm import LLM
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import script_segment_audio as audio_repo
from edutoon.repositories import scripts as scripts_repo
from edutoon.repositories.scripts import NewScriptSegment
from edutoon.services import script_audio as script_audio_service
from edutoon.services import storyboard as storyboard_service
from tests.idempotency_helpers import INVALID_KEYS
from tests.storage_isolation import SEGMENTS_BUCKET

# Stands in for an external provider (OpenAI / Anthropic): these tests prove how our code
# handles a response, never how a real provider behaves. See "Provider-mocked tests"
# in apps/api/README.md.
pytestmark = pytest.mark.provider_mocked

MODEL = "claude-sonnet-5"
TEST_ISSUER = "https://test.clerk.accounts.dev"
BUCKET = SEGMENTS_BUCKET


# --- auth + app plumbing (mirrors test_script_audio_api.py) ----------------------------------


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


def _headers(base: dict[str, str], key: str | None = None) -> dict[str, str]:
    return {**base, "Idempotency-Key": key or str(uuid4())}


def _storyboard_llm(scenes: list[dict]) -> LLM:
    client = AsyncAnthropic(api_key="sk-ant-test-not-a-real-key")
    message = Message(
        id="msg_1",
        content=[
            ToolUseBlock(
                type="tool_use", id="toolu_1", name="submit_scenes", input={"scenes": scenes}
            )
        ],
        model=MODEL,
        role="assistant",
        type="message",
        usage=AnthropicUsage(input_tokens=5, output_tokens=5),
    )

    async def _create(**_kwargs):
        return message

    client.messages.create = _create  # type: ignore[method-assign]
    return LLM(client, model=MODEL)


# --- fixtures: a project with an approved, fully-narrated script -----------------------------


async def _create_project(client: httpx.AsyncClient, headers: dict[str, str]) -> str:
    body = {"title": "How photosynthesis works", "source_type": "topic"}
    response = await client.post("/v1/projects", json=body, headers=_headers(headers))
    assert response.status_code == 201, response.text
    return response.json()["id"]  # type: ignore[no-any-return]


async def _approved_script_with_measured_audio(
    db_session, project_id: str, texts: list[str], *, duration_ms: int = 9_000
) -> str:
    script = await scripts_repo.create(db_session, project_id=UUID(project_id), llm_model=MODEL)
    segments = await scripts_repo.add_segments(
        db_session,
        script_id=script.id,
        segments=[
            NewScriptSegment(
                segment_index=i, narration_text=text, estimated_duration_ms=duration_ms
            )
            for i, text in enumerate(texts)
        ],
    )
    approved = await scripts_repo.set_status(
        db_session, project_id=UUID(project_id), script_id=script.id, status="approved"
    )
    assert approved is not None
    for segment in segments:
        await audio_repo.upsert(
            db_session,
            script_segment_id=segment.id,
            status="ready",
            storage_bucket=BUCKET,
            storage_key=f"k/{segment.id}",
            format="wav",
            sample_rate=22_050,
            channels=1,
            measured_duration_ms=duration_ms,
            voice_model="en_GB-cori-medium",
            is_temporary_voice_substitute=False,
            content_hash=f"hash-{segment.id}",
        )
    final = await scripts_repo.set_measured_duration(
        db_session,
        project_id=UUID(project_id),
        script_id=script.id,
        measured_duration_ms=duration_ms * len(texts),
    )
    assert final is not None
    return str(script.id)


def _storyboard_url(project_id: str, script_id: str) -> str:
    return f"/v1/projects/{project_id}/scripts/{script_id}/storyboard"


# --- enqueue -----------------------------------------------------------------------------------


async def test_enqueueing_a_storyboard_for_a_ready_script_returns_202(
    client_factory, auth_headers, db_session
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        script_id = await _approved_script_with_measured_audio(db_session, project_id, ["Hi."])
        await db_session.commit()

        response = await client.post(
            _storyboard_url(project_id, script_id), headers=_headers(headers)
        )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["job"]["kind"] == "generate_storyboard"
    assert body["job"]["status"] == "queued"
    assert body["estimated_provider_calls"] == 1


async def test_enqueueing_for_a_non_approved_script_is_a_conflict(
    client_factory, auth_headers, db_session
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        script = await scripts_repo.create(db_session, project_id=UUID(project_id), llm_model=MODEL)
        await db_session.commit()

        response = await client.post(
            _storyboard_url(project_id, str(script.id)), headers=_headers(headers)
        )

    assert response.status_code == 409, response.text


async def test_enqueueing_before_narration_is_measured_is_rejected(
    client_factory, auth_headers, db_session
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        script = await scripts_repo.create(db_session, project_id=UUID(project_id), llm_model=MODEL)
        await scripts_repo.add_segments(
            db_session,
            script_id=script.id,
            segments=[
                NewScriptSegment(segment_index=0, narration_text="Hi.", estimated_duration_ms=500)
            ],
        )
        approved = await scripts_repo.set_status(
            db_session, project_id=UUID(project_id), script_id=script.id, status="approved"
        )
        assert approved is not None
        await db_session.commit()

        response = await client.post(
            _storyboard_url(project_id, str(script.id)), headers=_headers(headers)
        )

    assert response.status_code == 400, response.text


async def test_enqueueing_without_an_idempotency_key_is_a_422(
    client_factory, auth_headers, db_session
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        script_id = await _approved_script_with_measured_audio(db_session, project_id, ["Hi."])
        await db_session.commit()

        response = await client.post(_storyboard_url(project_id, script_id), headers=headers)

    assert response.status_code == 422


@pytest.mark.parametrize("key", INVALID_KEYS)
async def test_an_invalid_idempotency_key_is_refused(client_factory, auth_headers, db_session, key):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        script_id = await _approved_script_with_measured_audio(db_session, project_id, ["Hi."])
        await db_session.commit()

        response = await client.post(
            _storyboard_url(project_id, script_id), headers={**headers, "Idempotency-Key": key}
        )

    assert response.status_code == 422


async def test_an_unknown_body_field_is_rejected_with_422(client_factory, auth_headers, db_session):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        script_id = await _approved_script_with_measured_audio(db_session, project_id, ["Hi."])
        await db_session.commit()

        response = await client.post(
            _storyboard_url(project_id, script_id),
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
        script_id = await _approved_script_with_measured_audio(db_session, project_id, ["Hi."])
        await db_session.commit()
        request_headers = _headers(headers)

        first = await client.post(_storyboard_url(project_id, script_id), headers=request_headers)
        second = await client.post(_storyboard_url(project_id, script_id), headers=request_headers)

    assert first.status_code == second.status_code == 202
    assert first.json()["job"]["id"] == second.json()["job"]["id"]


async def test_enqueueing_for_someone_elses_project_is_a_404(
    client_factory, auth_headers, db_session
):
    owner, intruder = auth_headers(), auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, owner)
        script_id = await _approved_script_with_measured_audio(db_session, project_id, ["Hi."])
        await db_session.commit()

        response = await client.post(
            _storyboard_url(project_id, script_id), headers=_headers(intruder)
        )

    assert response.status_code == 404


async def test_enqueueing_for_a_missing_script_is_a_404(client_factory, auth_headers):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)

        response = await client.post(
            _storyboard_url(project_id, str(uuid4())), headers=_headers(headers)
        )

    assert response.status_code == 404


# --- status ------------------------------------------------------------------------------------


async def test_status_lists_every_segment_before_any_scene_exists(
    client_factory, auth_headers, db_session
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        script_id = await _approved_script_with_measured_audio(
            db_session, project_id, ["First.", "Second."]
        )
        await db_session.commit()

        response = await client.get(_storyboard_url(project_id, script_id), headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["segments"]) == 2
    assert all(segment["scenes"] == [] for segment in body["segments"])
    assert body["is_complete"] is False
    assert body["ready_segment_count"] == 0
    assert body["estimated_provider_calls"] == 2
    assert body["actual_provider_calls"] is None


async def test_status_for_someone_elses_project_is_a_404(client_factory, auth_headers, db_session):
    owner, intruder = auth_headers(), auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, owner)
        script_id = await _approved_script_with_measured_audio(db_session, project_id, ["Hi."])
        await db_session.commit()

        response = await client.get(_storyboard_url(project_id, script_id), headers=intruder)

    assert response.status_code == 404


async def test_status_reflects_generated_scenes(client_factory, auth_headers, db_session):
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        script_id = await _approved_script_with_measured_audio(
            db_session, project_id, ["Hi there."]
        )
        await db_session.commit()

        enqueue_response = await client.post(
            _storyboard_url(project_id, script_id), headers=_headers(headers)
        )
        job_id = UUID(enqueue_response.json()["job"]["id"])
        llm = _storyboard_llm([{"template": "title_card", "content": {"title": "Hi there"}}])
        finished = await storyboard_service.run_generate_storyboard_job(
            db_session, llm, job_id=job_id
        )
        assert finished.status == "succeeded"

        response = await client.get(_storyboard_url(project_id, script_id), headers=headers)

    body = response.json()
    assert body["is_complete"] is True
    assert body["ready_segment_count"] == 1
    assert body["actual_provider_calls"] == 1
    [segment] = body["segments"]
    [scene] = segment["scenes"]
    assert scene["template"] == "title_card"
    assert scene["content"] == {"title": "Hi there"}
    assert scene["duration_ms"] == 9_000
    assert scene["start_ms"] == 0


# --- timings (Phase 11) --------------------------------------------------------------------------


async def _narrate(db_session, project_id: str, script_id: str, durations_ms: list[int]) -> None:
    """Give each segment ready audio of the given length whose fingerprint matches today's text,
    voice and engine - a finished, up-to-date synthesis."""
    project = await projects_repo.get_by_id(db_session, UUID(project_id))
    assert project is not None
    voice = script_audio_service.voice_selection_from_settings(get_settings(), project.language)
    segments = await scripts_repo.list_segments(
        db_session, project_id=UUID(project_id), script_id=UUID(script_id)
    )
    for segment, duration in zip(segments, durations_ms, strict=True):
        await audio_repo.upsert(
            db_session,
            script_segment_id=segment.id,
            status="ready",
            storage_bucket=BUCKET,
            storage_key=f"k/{segment.id}",
            format="wav",
            sample_rate=22_050,
            channels=1,
            measured_duration_ms=duration,
            voice_model=voice.voice_model,
            is_temporary_voice_substitute=False,
            content_hash=compute_content_hash(
                narration_text=segment.narration_text,
                voice_model=voice.voice_model,
                engine_version=tts_provider.engine_version(),
                sample_rate=22_050,
            ),
        )


async def _storyboarded(client, headers, db_session, texts: list[str]) -> tuple[str, str]:
    project_id = await _create_project(client, headers)
    script_id = await _approved_script_with_measured_audio(db_session, project_id, texts)
    await _narrate(db_session, project_id, script_id, [9_000] * len(texts))
    await db_session.commit()
    enqueued = await client.post(_storyboard_url(project_id, script_id), headers=_headers(headers))
    llm = _storyboard_llm([{"template": "title_card", "content": {"title": "Hi there"}}])
    await storyboard_service.run_generate_storyboard_job(
        db_session, llm, job_id=UUID(enqueued.json()["job"]["id"])
    )
    return project_id, script_id


async def test_status_says_timings_are_authoritative_when_narration_is_current(
    client_factory, auth_headers, db_session
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id, script_id = await _storyboarded(client, headers, db_session, ["One.", "Two."])
        body = (await client.get(_storyboard_url(project_id, script_id), headers=headers)).json()

    assert body["timings_authoritative"] is True
    assert [seg["scenes"][0]["start_ms"] for seg in body["segments"]] == [0, 9_000]


async def test_status_follows_a_re_narration_and_says_timings_are_not_final(
    client_factory, auth_headers, db_session
):
    """The regression: segment 0 becomes 15 s long, so segment 1 must now start at 15 000, not at
    the 9 000 stored when its scene was made; and the response says the numbers may still move."""
    headers = auth_headers()
    async with client_factory() as client:
        project_id, script_id = await _storyboarded(client, headers, db_session, ["One.", "Two."])
        segments = await scripts_repo.list_segments(
            db_session, project_id=UUID(project_id), script_id=UUID(script_id)
        )
        await audio_repo.upsert(
            db_session,
            script_segment_id=segments[0].id,
            status="ready",
            storage_bucket=BUCKET,
            storage_key=f"k/{segments[0].id}",
            format="wav",
            sample_rate=22_050,
            channels=1,
            measured_duration_ms=15_000,
            voice_model="another-voice",
            is_temporary_voice_substitute=False,
            content_hash="made-in-another-voice",
        )
        await db_session.commit()
        body = (await client.get(_storyboard_url(project_id, script_id), headers=headers)).json()

    assert [seg["scenes"][0]["start_ms"] for seg in body["segments"]] == [0, 15_000]
    assert body["timings_authoritative"] is False
    assert body["segments"][0]["is_stale"] is True  # the existing stale badge is untouched


# --- dropped visuals (Phase 11) -----------------------------------------------------------------


async def test_status_counts_visuals_that_were_dropped_and_shows_the_scene_as_text(
    client_factory, auth_headers, db_session
):
    headers = auth_headers()
    invalid = {"type": "cycle", "nodes": [{"label": "Hi"}]}  # a cycle needs three steps
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        script_id = await _approved_script_with_measured_audio(
            db_session, project_id, ["Hi there."]
        )
        await db_session.commit()
        enqueued = await client.post(
            _storyboard_url(project_id, script_id), headers=_headers(headers)
        )
        llm = _storyboard_llm(
            [
                {
                    "template": "labelled_diagram",
                    "content": {
                        "illustration": {"description": "Hi there", "visual": invalid},
                        "labels": [{"text": "Hi", "position": "top"}],
                    },
                }
            ]
        )
        finished = await storyboard_service.run_generate_storyboard_job(
            db_session, llm, job_id=UUID(enqueued.json()["job"]["id"])
        )
        assert finished.status == "succeeded"
        body = (await client.get(_storyboard_url(project_id, script_id), headers=headers)).json()

    assert body["dropped_visual_count"] == 1
    assert body["is_complete"] is True  # the storyboard is still whole
    [scene] = body["segments"][0]["scenes"]
    assert "visual" not in scene["content"]["illustration"]
    assert scene["content"]["illustration"]["visual_dropped"] is True
    assert "Traceback" not in str(body) and "VisualSpecError" not in str(body)  # nothing raw


async def test_status_reports_no_dropped_visuals_when_none_were_dropped(
    client_factory, auth_headers, db_session
):
    headers = auth_headers()
    async with client_factory() as client:
        project_id, script_id = await _storyboarded(client, headers, db_session, ["One."])
        body = (await client.get(_storyboard_url(project_id, script_id), headers=headers)).json()
    assert body["dropped_visual_count"] == 0

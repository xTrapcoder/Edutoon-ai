"""``domain/llm_usage.py`` (the accumulator) and ``GET /v1/projects/{id}/jobs/usage`` (the
project-level read of what jobs recorded).

The accumulator's one rule is that nothing is invented: a metric the provider did not return stays
``None`` (unavailable) and is never counted as zero. The job-level recording of each kind of call is
tested with the services that make the calls (``test_services_*.py``).
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from edutoon.core.config import get_settings
from edutoon.db.session import get_session
from edutoon.domain.llm_usage import LLM_CALL_KINDS, UsageEntry, UsageReport
from edutoon.main import create_app
from edutoon.providers import clerk
from edutoon.providers.cache import get_redis_client
from edutoon.repositories import jobs as jobs_repo
from tests.usage_helpers import usage_of


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None


# --- the accumulator ----------------------------------------------------------------


def test_calls_of_one_kind_are_summed() -> None:
    report = UsageReport()
    report.add("script_section", Usage(10, 5))
    report.add("script_section", Usage(20, 7))
    entry = report.entry("script_section")
    assert (entry.calls, entry.calls_with_usage) == (2, 2)
    assert (entry.input_tokens, entry.output_tokens) == (30, 12)


def test_a_metric_the_provider_never_reported_stays_unavailable_not_zero() -> None:
    report = UsageReport()
    report.add("claim_judge", Usage(10, 5))
    entry = report.entry("claim_judge")
    assert entry.cache_creation_input_tokens is None
    assert entry.cache_read_input_tokens is None
    assert report.total.cache_read_input_tokens is None


def test_a_cache_figure_that_some_calls_reported_is_summed_over_those_calls() -> None:
    report = UsageReport()
    report.add("claim_judge", Usage(10, 5))  # no cache figures
    report.add(
        "claim_judge", Usage(10, 5, cache_creation_input_tokens=100, cache_read_input_tokens=0)
    )
    entry = report.entry("claim_judge")
    assert (entry.cache_creation_input_tokens, entry.cache_read_input_tokens) == (100, 0)


def test_an_answer_with_no_usage_is_counted_as_a_call_but_adds_no_tokens() -> None:
    report = UsageReport()
    report.add("storyboard", Usage(4, 2))
    report.add("storyboard", None)
    entry = report.entry("storyboard")
    assert (entry.calls, entry.calls_with_usage) == (
        2,
        1,
    )  # calls_with_usage < calls: a lower bound
    assert (entry.input_tokens, entry.output_tokens) == (4, 2)


def test_only_unknown_usage_leaves_every_figure_unavailable() -> None:
    report = UsageReport()
    report.add("storyboard", None)
    entry = report.entry("storyboard")
    assert entry.calls == 1 and entry.calls_with_usage == 0
    assert entry.input_tokens is None and entry.output_tokens is None


def test_the_total_spans_every_kind() -> None:
    report = UsageReport()
    report.add("script_outline", Usage(1, 2))
    report.add("script_section", Usage(10, 20))
    report.add("script_correction", Usage(100, 200))
    assert report.total.calls == 3
    assert (report.total.input_tokens, report.total.output_tokens) == (111, 222)


def test_an_unknown_kind_is_refused_so_a_typo_cannot_open_an_unreported_bucket() -> None:
    with pytest.raises(ValueError, match="Unknown language-model call kind"):
        UsageReport().add("script_sction", Usage(1, 1))


def test_every_kind_the_application_makes_is_named() -> None:
    assert set(LLM_CALL_KINDS) == {
        "topic_extraction",
        "claim_extraction",
        "claim_judge",
        "script_outline",
        "script_section",
        "script_correction",
        "storyboard",
    }


def test_the_report_lists_only_the_kinds_used_in_their_canonical_order() -> None:
    report = UsageReport()
    report.add("storyboard", Usage(1, 1))
    report.add("topic_extraction", Usage(1, 1))
    assert list(report.as_dict()["by_call"]) == ["topic_extraction", "storyboard"]


def test_a_report_survives_being_stored_and_read_back() -> None:
    report = UsageReport()
    report.add("script_section", Usage(10, 5, cache_read_input_tokens=3))
    report.add("claim_judge", None)
    again = UsageReport.from_dict(report.as_dict())
    assert again.as_dict() == report.as_dict()


def test_reports_merge_across_jobs() -> None:
    first, second = UsageReport(), UsageReport()
    first.add("script_section", Usage(10, 5))
    second.add("script_section", Usage(1, 1))
    second.add("storyboard", Usage(2, 2))
    first.merge(second)
    assert first.entry("script_section").calls == 2
    assert first.entry("script_section").input_tokens == 11
    assert first.entry("storyboard").calls == 1


@pytest.mark.parametrize(
    "junk",
    [None, "text", 5, [], {}, {"by_call": None}, {"by_call": {"nonsense": {"calls": 1}}}],
)
def test_a_malformed_stored_report_reads_as_no_usage_instead_of_crashing_a_cost_report(
    junk: object,
) -> None:
    assert UsageReport.from_dict(junk).total == UsageEntry()


def test_a_malformed_stored_entry_reads_as_the_parts_that_are_valid() -> None:
    entry = UsageEntry.from_dict({"calls": 2, "input_tokens": "lots", "output_tokens": -4})
    assert entry.calls == 2
    assert entry.input_tokens is None and entry.output_tokens is None


# --- the API ------------------------------------------------------------------------

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


async def _create_project(client: httpx.AsyncClient, headers: dict[str, str]) -> str:
    response = await client.post(
        "/v1/projects",
        json={"title": "P", "source_type": "pdf"},
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 201
    return response.json()["id"]  # type: ignore[no-any-return]


async def _finish(db_session, project_id: str, kind: str, result: dict) -> None:
    job = await jobs_repo.create(db_session, project_id=UUID(project_id), kind=kind)
    started = await jobs_repo.update(
        db_session, job_id=job.id, status="running", started_at=job.created_at
    )
    assert started is not None
    await jobs_repo.update(
        db_session, job_id=job.id, status="succeeded", finished_at=job.created_at, result=result
    )


async def test_a_project_with_no_jobs_has_used_nothing(client_factory, auth_headers) -> None:
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        response = await client.get(f"/v1/projects/{project_id}/jobs/usage", headers=headers)

    assert response.status_code == 200
    assert response.json()["by_call"] == {}
    assert response.json()["total"]["calls"] == 0
    assert response.json()["total"]["input_tokens"] is None  # unavailable, not zero


async def test_the_projects_usage_is_the_sum_of_what_its_jobs_recorded(
    client_factory, auth_headers, db_session
) -> None:
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        await _finish(
            db_session,
            project_id,
            "extract_topics",
            {"usage": usage_of("topic_extraction", 100, 50)},
        )
        await _finish(
            db_session, project_id, "generate_storyboard", {"usage": usage_of("storyboard", 7, 3)}
        )
        await _finish(
            db_session, project_id, "generate_storyboard", {"usage": usage_of("storyboard", 1, 1)}
        )
        await _finish(db_session, project_id, "render_video", {"chunks_rendered": 4})  # no usage

        response = await client.get(f"/v1/projects/{project_id}/jobs/usage", headers=headers)

    body = response.json()
    assert list(body["by_call"]) == ["topic_extraction", "storyboard"]
    assert body["by_call"]["storyboard"]["calls"] == 2
    assert (body["by_call"]["storyboard"]["input_tokens"], body["total"]["input_tokens"]) == (
        8,
        108,
    )
    assert body["total"]["output_tokens"] == 54


async def test_another_users_projects_usage_is_a_404_not_a_403(
    client_factory, auth_headers, db_session
) -> None:
    owner, other = auth_headers(), auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, owner)
        await _finish(
            db_session, project_id, "extract_topics", {"usage": usage_of("topic_extraction", 1, 1)}
        )
        response = await client.get(f"/v1/projects/{project_id}/jobs/usage", headers=other)

    assert response.status_code == 404


async def test_usage_never_mixes_two_projects(client_factory, auth_headers, db_session) -> None:
    headers = auth_headers()
    async with client_factory() as client:
        first = await _create_project(client, headers)
        second = await _create_project(client, headers)
        await _finish(
            db_session, first, "extract_topics", {"usage": usage_of("topic_extraction", 5, 5)}
        )
        response = await client.get(f"/v1/projects/{second}/jobs/usage", headers=headers)

    assert response.json()["total"]["calls"] == 0


async def test_the_usage_route_is_not_shadowed_by_the_job_list(
    client_factory, auth_headers
) -> None:
    headers = auth_headers()
    async with client_factory() as client:
        project_id = await _create_project(client, headers)
        listing = await client.get(f"/v1/projects/{project_id}/jobs", headers=headers)
        usage = await client.get(f"/v1/projects/{project_id}/jobs/usage", headers=headers)

    assert "items" in listing.json() and "total" in usage.json()

"""``services/job_errors.py`` and ``providers/errors.py`` - what a failed job may
tell its owner.

A job's ``error`` is returned by ``GET /v1/projects/{id}/jobs``. It used to be
``str(exc)``, which for a database error is the SQL statement and its parameters
and for a provider error is the provider's response body. Every test here builds
a failure whose text is *stuffed* with things that must never reach the owner - a
key-shaped string, SQL, a file path, a fragment of the user's own material - and
asserts that none of it does, and that the owner is still told something useful.

All exceptions are real SDK / SQLAlchemy objects built offline: no network.
"""

from __future__ import annotations

from uuid import uuid4

import anthropic
import openai
import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError

from edutoon.core.errors import ConflictError, JobError, NotFoundError, ValidationError
from edutoon.domain.script_provenance import UnsourcedSegmentError
from edutoon.domain.storyboard_provenance import FabricatedContentError, ProvenanceViolation
from edutoon.domain.storyboard_template import StoryboardContentError
from edutoon.domain.topic_tree import CyclicHierarchyError, TopicLimitError
from edutoon.providers import errors as provider_errors
from edutoon.providers.llm import LLMResponseError
from edutoon.services import job_errors
from tests.leaky_errors import LEAKY_BODY, assert_clean, request, response

# Stands in for an external provider (OpenAI / Anthropic): these tests prove how our code
# handles a response, never how a real provider behaves. See "Provider-mocked tests"
# in apps/api/README.md.
pytestmark = pytest.mark.provider_mocked

# --- provider errors: chosen by kind and status, never by text -------------------------


@pytest.mark.parametrize("sdk", [anthropic, openai], ids=["anthropic", "openai"])
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, provider_errors.REJECTED),
        (401, provider_errors.NOT_AUTHORISED),
        (403, provider_errors.NOT_AUTHORISED),
        (404, provider_errors.MODEL_UNAVAILABLE),
        (408, provider_errors.TIMED_OUT),
        (413, provider_errors.TOO_LARGE),
        (422, provider_errors.REJECTED),
        (429, provider_errors.RATE_LIMITED),
        (500, provider_errors.UNAVAILABLE),
        (502, provider_errors.UNAVAILABLE),
        (503, provider_errors.UNAVAILABLE),
        (504, provider_errors.TIMED_OUT),
        (529, provider_errors.UNAVAILABLE),
        (418, provider_errors.UNEXPECTED),
    ],
)
def test_a_provider_status_error_becomes_a_fixed_sentence_and_leaks_nothing(sdk, status, expected):
    exc = sdk.APIStatusError(str(LEAKY_BODY), response=response(status), body=LEAKY_BODY)

    shown = job_errors.public_job_error(exc)

    assert shown == expected
    assert_clean(shown)
    assert str(exc) != shown  # the raw text is genuinely different from what we show


@pytest.mark.parametrize("sdk", [anthropic, openai], ids=["anthropic", "openai"])
def test_timeouts_and_connection_failures_have_their_own_messages(sdk):
    assert job_errors.public_job_error(sdk.APITimeoutError(request=request())) == (
        provider_errors.TIMED_OUT
    )
    assert job_errors.public_job_error(sdk.APIConnectionError(request=request())) == (
        provider_errors.UNREACHABLE
    )


def test_a_timeout_is_not_mistaken_for_a_plain_connection_failure():
    """``APITimeoutError`` is a subclass of ``APIConnectionError``, so the order
    of the checks matters.
    """
    assert provider_errors.describe_provider_error(
        anthropic.APITimeoutError(request=request())
    ) != provider_errors.describe_provider_error(anthropic.APIConnectionError(request=request()))


def test_a_provider_error_with_no_status_is_still_described_without_its_text():
    exc = anthropic.AnthropicError("sk-ant-api03-REDACTED something odd")

    shown = job_errors.public_job_error(exc)

    assert shown == provider_errors.UNEXPECTED
    assert_clean(shown)


def test_an_exception_that_is_not_a_provider_error_is_not_claimed_by_the_provider_module():
    assert provider_errors.describe_provider_error(RuntimeError("boom")) is None


# --- database and connection errors ------------------------------------------------------


def test_a_database_error_never_shows_its_sql_or_parameters():
    exc = DBAPIError(
        "SELECT secret_column FROM users WHERE api_token = $1",
        {"api_token": "hunter2"},
        Exception('relation "users" does not exist'),
    )
    assert "SELECT" in str(exc) and "hunter2" in str(exc)  # what used to be stored

    shown = job_errors.public_job_error(exc)

    assert shown == job_errors.DATABASE_ERROR
    assert_clean(shown)


@pytest.mark.parametrize("exc_type", [IntegrityError, OperationalError])
def test_every_kind_of_database_error_is_generic(exc_type):
    exc = exc_type("INSERT INTO t VALUES ($1)", {"a": "PRIVATE-USER-MATERIAL"}, Exception("x"))

    shown = job_errors.public_job_error(exc)

    assert shown == job_errors.DATABASE_ERROR
    assert_clean(shown)


def test_timeouts_and_lost_connections_are_described_plainly():
    assert job_errors.public_job_error(TimeoutError("read timed out at /srv/app/x.py")) == (
        job_errors.TIMEOUT_ERROR
    )
    lost = ConnectionRefusedError("connect to postgresql://user:hunter2@db:5432/edutoon failed")
    shown = job_errors.public_job_error(lost)
    assert shown == job_errors.CONNECTION_ERROR
    assert_clean(shown)


# --- everything else -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("password=hunter2 at /srv/app/worker.py line 12"),
        ValueError("sk-proj-REDACTED"),
        KeyError("PRIVATE-USER-MATERIAL"),
        AssertionError("Traceback (most recent call last): /srv/app/"),
        type("InternalThingError", (Exception,), {})("postgresql://u:hunter2@h/db"),
    ],
    ids=["runtime", "value", "key", "assertion", "custom"],
)
def test_an_unexpected_exception_is_reported_generically(exc):
    shown = job_errors.public_job_error(exc)

    assert shown == job_errors.GENERIC_ERROR
    assert_clean(shown)
    assert "InternalThingError" not in shown


# --- errors that are ours: shown as written, but bounded ---------------------------------


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (NotFoundError("Project 1 not found."), "Project 1 not found."),
        (ConflictError("A topic already occupies this position."), None),
        (ValidationError("There is nothing to extract topics from."), None),
        (JobError("Nothing was changed - run the extraction again."), None),
        (TopicLimitError(31, 30), "The tree has 31 topics; the limit is 30."),
        (CyclicHierarchyError("t1"), None),
        (UnsourcedSegmentError(2), None),
        (LLMResponseError("Topic 3: title must be a string."), "Topic 3: title must be a string."),
        (StoryboardContentError("'bullets' is required."), "'bullets' is required."),
        (
            FabricatedContentError(
                [ProvenanceViolation(slot_path="title", kind="number", token="42")]
            ),
            None,
        ),
    ],
)
def test_our_own_deliberate_messages_are_shown_as_written(exc, expected):
    shown = job_errors.public_job_error(exc)

    assert shown == (expected or (exc.message if hasattr(exc, "message") else str(exc)))
    assert shown != job_errors.GENERIC_ERROR


def test_a_long_message_quoting_the_model_is_capped_and_kept_on_one_line():
    quoted = "x" * 5_000
    shown = job_errors.public_job_error(LLMResponseError(f"Unexpected verdict:\n\n{quoted}"))

    assert len(shown) <= job_errors.MAX_PUBLIC_ERROR_CHARS
    assert shown.endswith("…") and "\n" not in shown


def test_a_blank_message_falls_back_to_the_generic_one():
    assert job_errors.public_job_error(LLMResponseError("   ")) == job_errors.GENERIC_ERROR


def test_an_app_error_subclass_is_shown_by_its_message_not_its_class_name():
    shown = job_errors.public_job_error(ConflictError("Already running."))
    assert shown == "Already running."


# --- the operator still gets everything ----------------------------------------------------


class _RecordingLog:
    """Stands in for the module's structlog logger. (structlog caches a logger's
    processors on first use, so ``capture_logs`` only works if nothing earlier in
    the run has logged - this does not depend on that.)
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def warning(self, event: str, **fields) -> None:
        self.events.append(("warning", event, fields))

    def error(self, event: str, **fields) -> None:
        self.events.append(("error", event, fields))


@pytest.fixture
def recorded(monkeypatch) -> _RecordingLog:
    log = _RecordingLog()
    monkeypatch.setattr(job_errors, "log", log)
    return log


def test_the_full_exception_goes_to_the_log_not_to_the_owner(recorded):
    job_id = uuid4()
    exc = RuntimeError("password=hunter2 at /srv/app/worker.py")

    shown = job_errors.describe_job_failure(exc, job_id=job_id, kind="extract_topics")

    assert shown == job_errors.GENERIC_ERROR
    [(level, event, fields)] = recorded.events
    assert (level, event) == ("error", "job.failed_unexpectedly")
    assert fields["job_id"] == str(job_id) and fields["kind"] == "extract_topics"
    assert fields["error_type"] == "RuntimeError"
    assert "hunter2" in fields["error"]  # for the operator
    assert fields["shown_to_owner"] == shown
    assert fields["exc_info"] is exc


def test_a_failure_that_is_the_jobs_own_doing_is_a_warning_without_a_stack_trace(recorded):
    job_errors.describe_job_failure(
        LLMResponseError("Topic 1: title must be a string."), job_id=uuid4(), kind="k"
    )

    [(level, event, fields)] = recorded.events
    assert (level, event) == ("warning", "job.failed")
    assert "exc_info" not in fields and "error" not in fields


def test_describing_a_failure_returns_exactly_what_public_job_error_says(recorded):
    exc = anthropic.APIStatusError("x", response=response(429), body=LEAKY_BODY)

    assert job_errors.describe_job_failure(exc, job_id=uuid4(), kind="k") == (
        job_errors.public_job_error(exc)
    )

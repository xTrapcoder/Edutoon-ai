"""A slow or unreachable local model must end in a clear, bounded outcome - never an endless wait.

Three things are shown, none of which starts a model: a timeout that clears is retried and the
call succeeds; a timeout that never clears stops after exactly the configured number of tries;
and a topic-extraction job whose model keeps timing out ends ``failed`` with the fixed sentence
written for the owner (never the exception), so the page shows a result and a way to try again.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from anthropic import AsyncAnthropic

from edutoon.providers.errors import LOCAL_MESSAGES, LocalModelError
from edutoon.providers.llm import LLM, Passage
from edutoon.providers.ollama import LocalMessagesClient
from edutoon.providers.retry import RetryPolicy
from edutoon.services import topic_extraction as service
from tests.test_services_topic_extraction import (  # noqa: F401 - shared fixtures/helpers
    _add_chunks,
    _enqueue,
    _project,
)

pytestmark = pytest.mark.provider_mocked

VERDICT = {"status": "supported", "rationale": "It says so.", "cited_passage_ids": ["p1"]}
PASSAGES = [Passage(id="p1", content="Water boils at 100 degrees Celsius at sea level.")]


async def _no_sleep(_: float) -> None:
    return None


def _llm(handler: Any) -> LLM:
    client = LocalMessagesClient(
        base_url="http://ollama.test",
        timeout_seconds=30,
        num_ctx=8192,
        transport=httpx.MockTransport(handler),
    )
    return LLM(client, model="qwen-test", retry=RetryPolicy(max_attempts=3), sleep=_no_sleep)


async def test_a_timeout_that_clears_is_retried_and_the_call_succeeds() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise httpx.ReadTimeout("the model is slow", request=request)
        return httpx.Response(
            200,
            json={
                "message": {"role": "assistant", "content": json.dumps(VERDICT)},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 50,
                "eval_count": 20,
            },
        )

    verdict = await _llm(handler).judge_claim("Water boils at 100 degrees.", PASSAGES)

    assert verdict is not None
    assert calls == 3


async def test_a_timeout_that_never_clears_stops_after_exactly_the_configured_tries() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("still slow", request=request)

    with pytest.raises(LocalModelError) as raised:
        await _llm(handler).judge_claim("c", PASSAGES)

    assert raised.value.kind == "timeout"
    assert calls == 3  # bounded: never an endless loop of waits


async def test_a_job_whose_model_keeps_timing_out_ends_failed_with_a_plain_sentence(
    db_session, monkeypatch
) -> None:
    owner, project = await _project(db_session)
    await _add_chunks(db_session, project, ["Plants make food from light."])
    enqueued = await _enqueue(db_session, owner, project)

    client = AsyncAnthropic(api_key="sk-ant-test-not-a-real-key")
    create = AsyncMock(side_effect=LocalModelError("timeout"))
    monkeypatch.setattr(client.messages, "create", create)
    llm = LLM(client, model="qwen-test", retry=RetryPolicy(max_attempts=3), sleep=_no_sleep)

    job = await service.run_extract_topics_job(db_session, llm, job_id=enqueued.job.id)

    assert job.status == "failed"
    assert job.error == LOCAL_MESSAGES["timeout"]
    assert job.finished_at is not None  # a terminal state: the spinner has an end
    assert create.await_count == 3  # three tries, then it gave up


async def test_the_next_extraction_can_be_started_after_a_timed_out_one(
    db_session, monkeypatch
) -> None:
    """A failed job is a retry state, not a lock: the owner can run extraction again."""
    owner, project = await _project(db_session)
    await _add_chunks(db_session, project, ["Plants make food from light."])
    first = await _enqueue(db_session, owner, project)
    client = AsyncAnthropic(api_key="sk-ant-test-not-a-real-key")
    monkeypatch.setattr(
        client.messages, "create", AsyncMock(side_effect=LocalModelError("timeout"))
    )
    llm = LLM(client, model="qwen-test", retry=RetryPolicy(max_attempts=1), sleep=_no_sleep)
    failed = await service.run_extract_topics_job(db_session, llm, job_id=first.job.id)
    assert failed.status == "failed"

    second = await _enqueue(db_session, owner, project)

    assert second.job.id != first.job.id and second.job.status == "queued"

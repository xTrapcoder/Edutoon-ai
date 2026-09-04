"""``services/idempotency.py`` - CLAUDE.md rule 8.

Runs against the real Redis instance (the same one every other test in this
suite already assumes is running via `make up`). Each test uses a fresh,
random owner id and/or idempotency key so tests never collide with each
other or with leftover state from a previous run.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest

from edutoon.core.config import get_settings
from edutoon.core.errors import IdempotencyInProgressError
from edutoon.providers.cache import Redis, get_redis_client
from edutoon.services.idempotency import build_key, run_with_idempotency


@pytest.fixture
async def redis_client() -> AsyncIterator[Redis]:
    client = get_redis_client(get_settings().REDIS_URL)
    yield client
    await client.aclose()


async def test_build_key_scopes_by_owner_method_path_and_key() -> None:
    owner_id = uuid4()

    key = build_key(owner_id=owner_id, method="post", path="/v1/projects", idempotency_key="abc")

    assert key == f"idempotency:{owner_id}:POST:/v1/projects:abc"


async def test_first_call_runs_the_handler(redis_client: Redis) -> None:
    calls = 0

    async def _handler() -> tuple[int, Any]:
        nonlocal calls
        calls += 1
        return 201, {"id": "one"}

    result = await run_with_idempotency(
        redis_client,
        owner_id=uuid4(),
        method="POST",
        path="/x",
        idempotency_key=str(uuid4()),
        handler=_handler,
    )

    assert calls == 1
    assert result == (201, {"id": "one"})


async def test_replay_returns_the_identical_response_without_rerunning_handler(
    redis_client: Redis,
) -> None:
    owner_id = uuid4()
    idempotency_key = str(uuid4())
    calls = 0

    async def _handler() -> tuple[int, Any]:
        nonlocal calls
        calls += 1
        return 201, {"id": "created-once"}

    first = await run_with_idempotency(
        redis_client,
        owner_id=owner_id,
        method="POST",
        path="/x",
        idempotency_key=idempotency_key,
        handler=_handler,
    )
    second = await run_with_idempotency(
        redis_client,
        owner_id=owner_id,
        method="POST",
        path="/x",
        idempotency_key=idempotency_key,
        handler=_handler,
    )

    assert first == second == (201, {"id": "created-once"})
    assert calls == 1


async def test_same_key_with_different_payload_still_replays_the_first_response(
    redis_client: Redis,
) -> None:
    """The key does not hash the request body, so a caller that reuses an
    ``Idempotency-Key`` for a logically different request gets back the
    *first* response, not a fresh one and not an error - varying the payload
    without varying the key is the caller's mistake to avoid, per the
    contract documented on :func:`run_with_idempotency`.
    """
    owner_id = uuid4()
    idempotency_key = str(uuid4())
    seen_payloads: list[str] = []

    def _make_handler(payload: str) -> Any:
        async def _handler() -> tuple[int, Any]:
            seen_payloads.append(payload)
            return 201, {"payload": payload}

        return _handler

    first = await run_with_idempotency(
        redis_client,
        owner_id=owner_id,
        method="POST",
        path="/x",
        idempotency_key=idempotency_key,
        handler=_make_handler("first"),
    )
    second = await run_with_idempotency(
        redis_client,
        owner_id=owner_id,
        method="POST",
        path="/x",
        idempotency_key=idempotency_key,
        handler=_make_handler("second"),
    )

    assert first == second == (201, {"payload": "first"})
    assert seen_payloads == ["first"]  # the "second" handler never ran


async def test_different_owners_can_reuse_the_same_key_value(redis_client: Redis) -> None:
    idempotency_key = "shared-key-value"
    calls = 0

    async def _handler() -> tuple[int, Any]:
        nonlocal calls
        calls += 1
        return 201, {"call": calls}

    result_a = await run_with_idempotency(
        redis_client,
        owner_id=uuid4(),
        method="POST",
        path="/x",
        idempotency_key=idempotency_key,
        handler=_handler,
    )
    result_b = await run_with_idempotency(
        redis_client,
        owner_id=uuid4(),
        method="POST",
        path="/x",
        idempotency_key=idempotency_key,
        handler=_handler,
    )

    assert calls == 2
    assert result_a != result_b


async def test_concurrent_duplicate_request_returns_409(redis_client: Redis) -> None:
    owner_id = uuid4()
    idempotency_key = str(uuid4())
    calls = 0

    async def _slow_handler() -> tuple[int, Any]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.2)
        return 201, {"ok": True}

    async def _attempt() -> tuple[int, Any] | IdempotencyInProgressError:
        try:
            return await run_with_idempotency(
                redis_client,
                owner_id=owner_id,
                method="POST",
                path="/x",
                idempotency_key=idempotency_key,
                handler=_slow_handler,
            )
        except IdempotencyInProgressError as exc:
            return exc

    result_a, result_b = await asyncio.gather(_attempt(), _attempt())
    results = [result_a, result_b]

    successes = [r for r in results if not isinstance(r, IdempotencyInProgressError)]
    failures = [r for r in results if isinstance(r, IdempotencyInProgressError)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].status_code == 409
    assert calls == 1  # the handler never ran twice, regardless of which request "won"


async def test_ttl_expiry_allows_the_key_to_be_reused(redis_client: Redis) -> None:
    owner_id = uuid4()
    idempotency_key = str(uuid4())
    calls = 0

    async def _handler() -> tuple[int, Any]:
        nonlocal calls
        calls += 1
        return 201, {"call": calls}

    await run_with_idempotency(
        redis_client,
        owner_id=owner_id,
        method="POST",
        path="/x",
        idempotency_key=idempotency_key,
        handler=_handler,
        result_ttl_seconds=1,
    )
    await asyncio.sleep(1.3)
    await run_with_idempotency(
        redis_client,
        owner_id=owner_id,
        method="POST",
        path="/x",
        idempotency_key=idempotency_key,
        handler=_handler,
        result_ttl_seconds=1,
    )

    assert calls == 2


async def test_handler_error_releases_the_lock_for_an_immediate_retry(
    redis_client: Redis,
) -> None:
    owner_id = uuid4()
    idempotency_key = str(uuid4())
    attempts = 0

    async def _flaky_handler() -> tuple[int, Any]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("boom")
        return 201, {"attempt": attempts}

    with pytest.raises(RuntimeError):
        await run_with_idempotency(
            redis_client,
            owner_id=owner_id,
            method="POST",
            path="/x",
            idempotency_key=idempotency_key,
            handler=_flaky_handler,
        )

    result = await run_with_idempotency(
        redis_client,
        owner_id=owner_id,
        method="POST",
        path="/x",
        idempotency_key=idempotency_key,
        handler=_flaky_handler,
    )

    assert result == (201, {"attempt": 2})

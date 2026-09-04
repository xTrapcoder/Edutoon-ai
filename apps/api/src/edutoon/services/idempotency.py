"""Redis-backed request deduplication (CLAUDE.md rule 8).

For a given (owner, method, path, ``Idempotency-Key``):

- never seen before -> ``handler`` runs, and its result is cached
- already completed -> the cached result is replayed, ``handler`` never runs
- still in flight (a genuine concurrent duplicate) -> rejected with 409

Generic and resource-agnostic - this module has no knowledge of projects,
uploads, or any other resource. Routers call :func:`run_with_idempotency`
around their own logic.

Lives in ``services`` (not ``core``) because it does real I/O against Redis
(rule 2: routers only ever reach providers - here, ``providers.cache`` -
through a service).

The key is scoped to ``(owner_id, method, path, idempotency_key)`` only - it
does not hash the request body. Reusing a key with a different payload still
replays the first response rather than erroring: the ``Idempotency-Key`` is a
per-request identity the *caller* is responsible for varying, not a checksum
of the payload.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from edutoon.core.errors import IdempotencyInProgressError
from edutoon.providers.cache import Redis

DEFAULT_LOCK_TTL_SECONDS = 30
DEFAULT_RESULT_TTL_SECONDS = 24 * 60 * 60

_KEY_PREFIX = "idempotency"


def build_key(*, owner_id: UUID, method: str, path: str, idempotency_key: str) -> str:
    return f"{_KEY_PREFIX}:{owner_id}:{method.upper()}:{path}:{idempotency_key}"


async def run_with_idempotency(
    redis: Redis,
    *,
    owner_id: UUID,
    method: str,
    path: str,
    idempotency_key: str,
    handler: Callable[[], Awaitable[tuple[int, Any]]],
    lock_ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS,
    result_ttl_seconds: int = DEFAULT_RESULT_TTL_SECONDS,
) -> tuple[int, Any]:
    """Run ``handler`` at most once for this key.

    Returns ``(status_code, body)`` - either freshly produced by ``handler``
    or replayed from a prior call with the same key. Raises
    :class:`IdempotencyInProgressError` if a concurrent duplicate is still
    being processed.
    """
    key = build_key(owner_id=owner_id, method=method, path=path, idempotency_key=idempotency_key)

    acquired = await redis.set(
        key, json.dumps({"status": "in_progress"}), nx=True, ex=lock_ttl_seconds
    )
    if not acquired:
        raw = await redis.get(key)
        record = json.loads(raw) if raw is not None else None
        if record is not None and record.get("status") == "completed":
            return record["status_code"], record["body"]
        raise IdempotencyInProgressError(
            "A request with this Idempotency-Key is already being processed."
        )

    try:
        status_code, body = await handler()
    except Exception:
        # Free the key immediately on failure so a genuine retry after an
        # error isn't stuck waiting out the lock TTL.
        await redis.delete(key)
        raise

    await redis.set(
        key,
        json.dumps({"status": "completed", "status_code": status_code, "body": body}),
        ex=result_ttl_seconds,
    )
    return status_code, body

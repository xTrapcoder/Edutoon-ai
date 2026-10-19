"""The worker's liveness signal: a missing worker must be visible, not look like a busy one."""

from __future__ import annotations

import asyncio
from uuid import uuid4

from fastapi.testclient import TestClient

from edutoon.main import create_app
from edutoon.providers.cache import Redis
from edutoon.worker.heartbeat import (
    beat_until_stopped,
    heartbeat_key,
    worker_is_online,
)


def test_key_is_per_database() -> None:
    a = heartbeat_key("postgresql+asyncpg://u:p@localhost:5432/edutoon")
    b = heartbeat_key("postgresql+asyncpg://u:p@localhost:5432/edutoon_qa")
    assert a != b
    assert a.endswith(":edutoon") and b.endswith(":edutoon_qa")


async def test_offline_until_a_worker_beats_and_offline_again_after_it_stops(
    redis_client: Redis,
) -> None:
    key = f"test:heartbeat:{uuid4().hex}"
    assert await worker_is_online(redis_client, key) is False

    stop = asyncio.Event()
    task = asyncio.create_task(beat_until_stopped(redis_client, key, stop))
    for _ in range(50):
        if await worker_is_online(redis_client, key):
            break
        await asyncio.sleep(0.05)
    assert await worker_is_online(redis_client, key) is True

    stop.set()
    await task
    assert await worker_is_online(redis_client, key) is False


async def test_a_broken_redis_reads_as_offline_never_raises() -> None:
    class Broken:
        async def exists(self, key: str) -> int:
            raise ConnectionError("down")

    assert await worker_is_online(Broken(), "k") is False  # type: ignore[arg-type]


def test_health_reports_the_worker_without_degrading_the_api() -> None:
    with TestClient(create_app()) as client:
        body = client.get("/health").json()
    assert body["worker"] in {"ok", "offline"}
    # The API is healthy whether or not a worker runs; the field is separate on purpose.
    assert body["status"] == "ok"

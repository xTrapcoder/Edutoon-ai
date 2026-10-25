"""A liveness signal for the job worker.

The queue is the ``jobs`` table, so a queued job is only ever picked up by a running
``python -m edutoon.worker``. Without a signal, a missing worker looks exactly like a busy one.
The worker refreshes one short-lived Redis key every few seconds; the API reads it so the
health route (and so the web app) can say plainly that nothing is processing jobs.
"""

from __future__ import annotations

import asyncio
import contextlib

import structlog
from sqlalchemy.engine import make_url

from edutoon.providers.cache import Redis

log = structlog.get_logger()

WORKER_HEARTBEAT_KEY = "edutoon:worker:heartbeat"
HEARTBEAT_INTERVAL_SECONDS = 5.0
# Several missed beats before the worker is called offline, so a slow Redis is not misreported.
HEARTBEAT_TTL_SECONDS = 20


def heartbeat_key(database_url: str) -> str:
    """One key per database: a worker serves the queue in *its* database, so a worker on another
    database (UI QA runs its own) must not make this API look served."""
    return f"{WORKER_HEARTBEAT_KEY}:{make_url(database_url).database}"


async def worker_is_online(redis_client: Redis, key: str) -> bool:
    """``True`` when a worker has refreshed its heartbeat recently. Never raises."""
    try:
        return bool(await redis_client.exists(key))
    except Exception:  # noqa: BLE001 - a liveness probe must not break its caller
        return False


async def beat_until_stopped(redis_client: Redis, key: str, stop: asyncio.Event) -> None:
    """Refresh the heartbeat until ``stop`` is set, then remove it so shutdown is seen at once."""
    try:
        while not stop.is_set():
            try:
                await redis_client.set(key, "1", ex=HEARTBEAT_TTL_SECONDS)
            except Exception as exc:  # noqa: BLE001 - the worker works without Redis
                log.warning("worker.heartbeat_failed", error=str(exc))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_INTERVAL_SECONDS)
    finally:
        with contextlib.suppress(Exception):
            await redis_client.delete(key)

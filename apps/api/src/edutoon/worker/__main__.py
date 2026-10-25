"""Standalone job worker process entrypoint: ``python -m edutoon.worker``.

Builds the same provider clients ``main.py``'s lifespan builds for the API
process (same settings, same client factories - including a second LLM client
for ``SCRIPT_LLM_MODEL``), but with no HTTP server - just the poll loop. Run
alongside the API, e.g. via ``make dev`` or its own process/container.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

import structlog

from edutoon.core.config import get_settings
from edutoon.core.logging import configure_logging
from edutoon.db.database import dispose_engine, get_engine
from edutoon.db.session import get_sessionmaker
from edutoon.providers.cache import get_redis_client
from edutoon.services.render import sweep_abandoned_work_dirs
from edutoon.worker.dispatch import build_worker_providers
from edutoon.worker.heartbeat import beat_until_stopped, heartbeat_key
from edutoon.worker.runner import run_forever

log = structlog.get_logger()


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)

    providers = build_worker_providers(settings)

    # Scratch left behind by a render whose worker was killed (see the function's docstring).
    await asyncio.to_thread(sweep_abandoned_work_dirs)

    get_engine()  # initialise the process-wide engine/pool up front
    sessionmaker = get_sessionmaker()

    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Windows' event loop doesn't support add_signal_handler for SIGTERM;
        # Ctrl-C still raises KeyboardInterrupt, caught below.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, shutdown_event.set)

    redis_client = get_redis_client(settings.REDIS_URL)
    heartbeat = asyncio.create_task(
        beat_until_stopped(redis_client, heartbeat_key(settings.DATABASE_URL), shutdown_event)
    )
    try:
        await run_forever(sessionmaker, providers, shutdown_event=shutdown_event)
    finally:
        # Also covers Ctrl-C on Windows, which never sets the event: stop the beat explicitly.
        shutdown_event.set()
        await heartbeat
        await redis_client.aclose()
        await dispose_engine()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("worker.interrupted")

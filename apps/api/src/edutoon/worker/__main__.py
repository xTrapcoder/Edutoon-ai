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
from edutoon.worker.dispatch import build_worker_providers
from edutoon.worker.runner import run_forever

log = structlog.get_logger()


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)

    providers = build_worker_providers(settings)

    get_engine()  # initialise the process-wide engine/pool up front
    sessionmaker = get_sessionmaker()

    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Windows' event loop doesn't support add_signal_handler for SIGTERM;
        # Ctrl-C still raises KeyboardInterrupt, caught below.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, shutdown_event.set)

    try:
        await run_forever(sessionmaker, providers, shutdown_event=shutdown_event)
    finally:
        await dispose_engine()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("worker.interrupted")

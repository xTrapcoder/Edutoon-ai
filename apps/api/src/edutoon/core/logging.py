"""Structured logging setup, shared by every process entrypoint (the API
app in ``main.py`` and the job worker in ``worker/__main__.py``).
"""

from __future__ import annotations

import logging

import structlog


def configure_logging(log_level: str) -> None:
    """Configure structlog for JSON output with a request_id on every line."""
    level = logging.getLevelNamesMapping().get(log_level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", level=level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

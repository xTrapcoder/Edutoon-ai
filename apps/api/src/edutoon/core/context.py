"""Request-scoped context accessible outside the FastAPI request object.

Services and repositories run without a ``Request`` in scope (background
jobs, tests). :func:`bind_request_id` lets the request-id middleware publish
the current request id so that code deeper in the call stack — logging calls
in particular — can read it via :func:`get_request_id` without threading it
through every function signature.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


def get_request_id() -> str | None:
    return _request_id.get()


@contextmanager
def bind_request_id(request_id: str) -> Iterator[None]:
    token = _request_id.set(request_id)
    try:
        yield
    finally:
        _request_id.reset(token)

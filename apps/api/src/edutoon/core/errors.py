"""Typed application errors and their HTTP mapping.

Services and repositories raise :class:`AppError` subclasses; routers never
construct HTTP responses for these cases themselves. Registered on the app
via :func:`register_exception_handlers`.
"""

from __future__ import annotations

from typing import Any, NoReturn

import structlog
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

log = structlog.get_logger()


class AppError(Exception):
    """Base class for errors that carry their own HTTP status and error code."""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "internal_error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code


class UnauthorizedError(AppError):
    """The caller's identity is missing, unverifiable, or expired.

    Distinct from :class:`NotFoundError` (rule 9): this is "we don't know
    who you are", not "you aren't allowed to see this resource".
    """

    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthorized"


class NotFoundError(AppError):
    """A resource does not exist, or the caller does not own it (rule 9: never 403)."""

    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class ConflictError(AppError):
    """The request conflicts with existing state (e.g. a uniqueness constraint)."""

    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


class IdempotencyInProgressError(AppError):
    """Another request with this ``Idempotency-Key`` is still being handled.

    Distinct from :class:`ConflictError`: this isn't a data conflict, it's a
    concurrent duplicate of the same in-flight request (rule 8).
    """

    status_code = status.HTTP_409_CONFLICT
    code = "idempotency_in_progress"


class PayloadTooLargeError(AppError):
    """The request body exceeds a configured size limit (e.g. max upload size)."""

    status_code = status.HTTP_413_CONTENT_TOO_LARGE
    code = "payload_too_large"


class InvalidCursorError(AppError):
    """A pagination cursor could not be decoded."""

    status_code = status.HTTP_400_BAD_REQUEST
    code = "invalid_cursor"


class ValidationError(AppError):
    """Caller-supplied input failed a domain-level check (not a Pydantic body).

    Distinct from FastAPI's ``RequestValidationError``, which has its own
    handler below — this is for values validated deeper in the stack, e.g. a
    pagination ``limit`` a repository receives directly.
    """

    status_code = status.HTTP_400_BAD_REQUEST
    code = "validation_error"


# Postgres unique-constraint names (from the migrations) mapped to a message
# safe to return to a client. Anything not listed here still becomes a
# ConflictError, just with a generic message - the raw DB error never reaches
# the client either way.
_UNIQUE_CONSTRAINT_MESSAGES: dict[str, str] = {
    "uq_users_email": "A user with this email already exists.",
    "uq_users_clerk_user_id": "A user with this Clerk account already exists.",
    "uq_uploaded_sources_project_id_checksum_sha256": (
        "This file has already been uploaded to this project."
    ),
    "uq_uploaded_sources_storage_bucket_storage_key": (
        "This storage location is already in use."
    ),
    "uq_jobs_project_id_kind_idempotency_key": (
        "A job with this idempotency key already exists."
    ),
    "uq_project_topics_project_id_parent_id_position": (
        "A topic already occupies this position."
    ),
    "uq_source_chunks_source_id_chunk_index": (
        "A chunk with this index already exists for this source."
    ),
}


def _constraint_name(exc: IntegrityError) -> str | None:
    """Best-effort extraction of the violated constraint's name.

    SQLAlchemy's asyncpg dialect wraps the driver error in its own
    ``IntegrityError``, one level deeper than ``exc.orig`` - the asyncpg
    exception carrying ``constraint_name`` sits at ``exc.orig.__cause__``.
    Checking ``exc.orig`` too keeps this from breaking outright if that
    wrapping ever changes.
    """
    for candidate in (getattr(exc.orig, "__cause__", None), exc.orig):
        name = getattr(candidate, "constraint_name", None)
        if name:
            return str(name)
    return None


def raise_conflict_from_integrity_error(exc: IntegrityError) -> NoReturn:
    """Translate a unique-constraint ``IntegrityError`` into a ``ConflictError``.

    Repositories call this from an ``except IntegrityError`` block around
    their INSERTs so a raw asyncpg/SQLAlchemy error never reaches a client.
    """
    message = _UNIQUE_CONSTRAINT_MESSAGES.get(
        _constraint_name(exc) or "", "This request conflicts with existing data."
    )
    raise ConflictError(message) from exc


def _error_body(code: str, message: str) -> dict[str, dict[str, str]]:
    return {"error": {"code": code, "message": message}}


def _validation_error_body(exc: RequestValidationError) -> dict[str, Any]:
    field_errors = [
        {
            "path": ".".join(str(part) for part in error["loc"]),
            "message": error["msg"],
            "type": error["type"],
        }
        for error in exc.errors()
    ]
    return {
        "error": {
            "code": "VALIDATION_ERROR",
            "message": "Request validation failed.",
            "details": {"field_errors": field_errors},
        }
    }


def register_exception_handlers(app: FastAPI) -> None:
    """Wire the error envelope onto ``app``. Call once from the app factory."""

    @app.exception_handler(AppError)
    async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(exc.code, exc.message),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Structured field errors only - never the raw `exc.errors()` dump,
        # which would include the caller's submitted values verbatim (rule 10
        # still applies: unknown fields land here as `extra_forbidden`).
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=_validation_error_body(exc),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        log.error("request.unhandled_error", error=str(exc), exc_info=exc)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_error_body("internal_error", "An unexpected error occurred."),
        )

"""What a failed background job tells the person who started it.

A job's ``error`` is returned by ``GET /v1/projects/{id}/jobs``, so it is shown
to the project's owner. It used to be ``str(exc)`` - whatever the failure happened
to say - which for a database error is the SQL statement and its parameters, and
for a provider error is the provider's response body. This module is the one place
that decides what may be shown:

- **Errors written for people** - an :class:`~edutoon.core.errors.AppError`
  (including :class:`~edutoon.core.errors.JobError`), a topic or script rule
  violation, or the model returning a response that does not match the contract -
  are shown as their own message: those messages are ours, deliberate, and already
  reach clients elsewhere. They are capped in length, since some quote a
  model-supplied fragment.
- **Provider errors** become one of a few fixed sentences chosen from the kind of
  failure (``providers.errors``).
- **Database and connection errors** become a fixed sentence.
- **Anything else** - a bug, a library error - becomes a generic sentence.

Nothing else is ever shown: no SQL, no stack trace, no provider response body, no
credential, no internal name. The full exception goes to the server log instead,
with the job's id, so an operator can find it.
"""

from __future__ import annotations

from uuid import UUID

import structlog
from sqlalchemy.exc import SQLAlchemyError

from edutoon.core.errors import AppError
from edutoon.domain.llm_usage import ProviderCallLimitError
from edutoon.domain.render import RenderPlanError
from edutoon.domain.script_provenance import UnsourcedSegmentError
from edutoon.domain.storyboard_provenance import FabricatedContentError
from edutoon.domain.storyboard_template import StoryboardContentError
from edutoon.domain.topic_tree import TopicError
from edutoon.providers.errors import describe_provider_error
from edutoon.providers.llm import LLMResponseError
from edutoon.providers.renderer.base import RendererError

log = structlog.get_logger()

# Long enough for any of our own messages; short enough that a model-supplied
# fragment quoted in one cannot make a job's error arbitrarily large.
MAX_PUBLIC_ERROR_CHARS = 300

GENERIC_ERROR = (
    "An unexpected error occurred while running this job. "
    "Please try again; if it keeps happening, contact support."
)
DATABASE_ERROR = "A database error occurred while running this job. Please try again."
CONNECTION_ERROR = "A connection to a required service failed. Please try again."
TIMEOUT_ERROR = "An operation timed out while running this job. Please try again."

# Exceptions whose message was written by this codebase, for people to read.
_OWN_MESSAGE = (
    AppError,
    TopicError,
    UnsourcedSegmentError,
    LLMResponseError,
    ProviderCallLimitError,
    StoryboardContentError,
    FabricatedContentError,
    RenderPlanError,
    RendererError,
)


def public_job_error(exc: BaseException) -> str:
    """The message safe to store on a failed job and show to its owner."""
    if isinstance(exc, _OWN_MESSAGE):
        message = exc.message if isinstance(exc, AppError) else str(exc)
        return _cap(" ".join(message.split())) or GENERIC_ERROR

    provider = describe_provider_error(exc)
    if provider is not None:
        return provider
    if isinstance(exc, SQLAlchemyError):
        return DATABASE_ERROR
    if isinstance(exc, TimeoutError):
        return TIMEOUT_ERROR
    if isinstance(exc, OSError):  # ConnectionError and friends
        return CONNECTION_ERROR
    return GENERIC_ERROR


def describe_job_failure(exc: BaseException, *, job_id: UUID, kind: str) -> str:
    """Log ``exc`` in full for the operator and return what the owner may see.

    Failures that are the job's own doing (bad input, the model's response
    breaking the contract) are warnings; everything else is an error with its
    stack trace. Neither goes anywhere a client can read.
    """
    message = public_job_error(exc)
    fields = {
        "job_id": str(job_id),
        "kind": kind,
        "error_type": type(exc).__name__,
        "shown_to_owner": message,
    }
    if isinstance(exc, _OWN_MESSAGE):
        log.warning("job.failed", **fields)
    else:
        log.error("job.failed_unexpectedly", **fields, error=str(exc)[:2000], exc_info=exc)
    return message


def _cap(text: str) -> str:
    if len(text) <= MAX_PUBLIC_ERROR_CHARS:
        return text
    return text[: MAX_PUBLIC_ERROR_CHARS - 1].rstrip() + "…"

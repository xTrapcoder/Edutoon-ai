"""Failures whose own text is stuffed with things that must never reach the
owner of a job: a key-shaped string, SQL and its parameters, a file path, a
fragment of the user's own material, a request id. Shared by the tests that
prove ``job.error`` is safe (see ``services/job_errors.py``).

Every object is a real SDK / SQLAlchemy exception, built offline.
"""

from __future__ import annotations

import anthropic
import httpx
import openai
from sqlalchemy.exc import DBAPIError

# Things that must never appear in what the owner sees.
FORBIDDEN = [
    "sk-ant-api03-REDACTED",
    "sk-proj-REDACTED",
    "x-api-key",
    "req_011CSECRETREQUESTID",
    "SELECT ",
    "INSERT ",
    "UPDATE ",
    "parameters",
    "sqlalche.me",
    "Traceback",
    "/srv/app/",
    "hunter2",
    "PRIVATE-USER-MATERIAL",
    "postgresql://",
]

LEAKY_BODY = {
    "type": "error",
    "error": {
        "type": "authentication_error",
        "message": (
            "invalid x-api-key sk-ant-api03-REDACTED; echoing your prompt: "
            "PRIVATE-USER-MATERIAL; request req_011CSECRETREQUESTID"
        ),
    },
}


def assert_clean(text: str | None) -> None:
    assert text is not None
    for needle in FORBIDDEN:
        assert needle not in text, f"{needle!r} leaked into {text!r}"


def request() -> httpx.Request:
    return httpx.Request("POST", "https://api.example.invalid/v1/messages")


def response(status: int) -> httpx.Response:
    return httpx.Response(status, request=request())


def leaky_anthropic(status: int) -> anthropic.APIStatusError:
    return anthropic.APIStatusError(str(LEAKY_BODY), response=response(status), body=LEAKY_BODY)


def leaky_openai(status: int) -> openai.APIStatusError:
    return openai.APIStatusError(str(LEAKY_BODY), response=response(status), body=LEAKY_BODY)


def leaky_db_error() -> DBAPIError:
    return DBAPIError(
        "INSERT INTO project_topics (title) VALUES ($1) -- PRIVATE-USER-MATERIAL",
        {"title": "hunter2"},
        Exception("connection to postgresql://user:hunter2@db:5432/edutoon lost"),
    )

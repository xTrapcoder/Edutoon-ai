"""Helpers shared by the enrolment and employee-access API tests (Step 2, Piece 3).

They sit on top of the fixtures of ``test_admin_training_api`` (``admin``, ``source``, ``track``,
``storage``), which the test modules import; everything here is scoped to rows and objects the
calling test created.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid4

import httpx
from sqlalchemy import text

from tests.test_admin_training_api import (
    BASE,
    Source,
    Tracker,
    extra_output,
    h,
    make_course,
    publish,
)

EMPLOYEE_BASE = "/v1/training"

# The two further renders a source supports in these tests (orientation, quality) after the first.
_EXTRA_SHAPES = (("horizontal", "high"), ("vertical", "low"), ("vertical", "high"))


async def publish_modules(
    client: httpx.AsyncClient,
    admin: Any,
    source: Source,
    track: Tracker,
    db_session: Any,
    storage: Any,
    course_id: str,
    count: int,
) -> list[dict]:
    """Publish ``count`` distinct renders of the source into the course, in order (positions
    1..count). The first is the source's own render, so its bytes are ``source.body``."""
    assert 1 <= count <= 1 + len(_EXTRA_SHAPES)
    modules = []
    for index in range(count):
        extra: dict[str, Any] = {"title": f"Module {index + 1}"}
        if index:
            orientation, quality = _EXTRA_SHAPES[index - 1]
            await extra_output(db_session, storage, source, track, orientation, quality)
            extra |= {"orientation": orientation, "quality": quality}
        response = await publish(client, track, admin, course_id, source, **extra)
        assert response.status_code == 201, response.text
        modules.append(response.json())
    return modules


async def published_course(
    client: httpx.AsyncClient,
    admin: Any,
    source: Source,
    track: Tracker,
    db_session: Any,
    storage: Any,
    *,
    modules: int = 2,
    title: str = "Safety induction",
    publish_course: bool = True,
    assessed: Sequence[int] = (),
) -> tuple[dict, list[dict]]:
    """A course with ``modules`` published modules, itself published unless told otherwise.

    ``assessed`` lists the 0-based indexes of the modules that get an assessment (Step 3: only a
    module that has one blocks the modules after it)."""
    course = await make_course(client, admin, title)
    made = await publish_modules(
        client, admin, source, track, db_session, storage, course["id"], modules
    )
    for index in assessed:
        saved = await put_assessment(client, admin, made[index]["id"])
        assert saved.status_code == 200, saved.text
    if publish_course:
        response = await client.post(f"{BASE}/courses/{course['id']}/publish", headers=h(admin))
        assert response.status_code == 200, response.text
        course = response.json()
    return course, made


async def enrol(
    client: httpx.AsyncClient,
    admin: Any,
    course_id: str,
    user_id: UUID | str,
    key: str | None = None,
) -> httpx.Response:
    return await client.post(
        f"{BASE}/courses/{course_id}/enrolments",
        json={"user_id": str(user_id)},
        headers=h(admin, key),
    )


async def withdraw(
    client: httpx.AsyncClient, admin: Any, course_id: str, user_id: UUID | str
) -> httpx.Response:
    return await client.post(
        f"{BASE}/courses/{course_id}/enrolments/{user_id}/withdraw", headers=h(admin)
    )


async def enrolment_rows(db_session: Any, course_id: str, user_id: UUID | str) -> list[Any]:
    """Every enrolment row for this one (course, user) pair."""
    result = await db_session.execute(
        text(
            "SELECT id, status, assigned_by, created_at, updated_at FROM course_enrolments "
            "WHERE course_id = :c AND user_id = :u ORDER BY created_at, id"
        ),
        {"c": course_id, "u": str(user_id)},
    )
    return list(result.all())


async def audit_actions(db_session: Any, entity_type: str, entity_id: UUID | str) -> list[str]:
    """The audit actions recorded for exactly this entity, oldest first."""
    result = await db_session.execute(
        text(
            "SELECT action FROM audit_logs WHERE entity_type = :t AND entity_id = :i "
            "ORDER BY created_at, id"
        ),
        {"t": entity_type, "i": str(entity_id)},
    )
    return [row[0] for row in result.all()]


async def audit_context(db_session: Any, action: str, entity_id: UUID | str) -> dict[str, Any]:
    """The context of the one audit entry with this action for exactly this entity."""
    result = await db_session.execute(
        text("SELECT context FROM audit_logs WHERE action = :a AND entity_id = :i"),
        {"a": action, "i": str(entity_id)},
    )
    [(context,)] = result.all()
    return context


def assessment_payload(
    *, questions: int = 2, options: int = 3, threshold: int | None = None
) -> dict[str, Any]:
    """An assessment body: ``questions`` questions of ``options`` options, the first correct."""
    payload: dict[str, Any] = {
        "questions": [
            {
                "prompt": f"Question {q + 1}?",
                "options": [
                    {"label": f"Q{q + 1} option {o + 1}", "is_correct": o == 0}
                    for o in range(options)
                ],
            }
            for q in range(questions)
        ]
    }
    if threshold is not None:
        payload["threshold_percent"] = threshold
    return payload


async def put_assessment(
    client: httpx.AsyncClient,
    admin: Any,
    module_id: str,
    payload: dict[str, Any] | None = None,
    key: str | None = None,
) -> httpx.Response:
    return await client.put(
        f"{BASE}/modules/{module_id}/assessment",
        json=payload if payload is not None else assessment_payload(),
        headers=h(admin, key),
    )


async def answers_for(
    client: httpx.AsyncClient, admin: Any, module_id: str, *, right: int | None = None
) -> list[dict[str, str]]:
    """A complete set of answers read off the administrator's view: the first ``right`` questions
    answered correctly and the rest wrongly (every question right when ``right`` is ``None``)."""
    view = await client.get(f"{BASE}/modules/{module_id}/assessment", headers=admin.headers)
    assert view.status_code == 200, view.text
    questions = view.json()["questions"]
    wanted = len(questions) if right is None else right
    answers = []
    for index, question in enumerate(questions):
        options = question["options"]
        correct = next(o for o in options if o["is_correct"])
        wrong = next(o for o in options if not o["is_correct"])
        chosen = correct if index < wanted else wrong
        answers.append({"question_id": question["id"], "option_id": chosen["id"]})
    return answers


async def submit_attempt(
    client: httpx.AsyncClient,
    person: Any,
    module_id: str,
    answers: list[dict[str, str]],
    key: str | None = None,
) -> httpx.Response:
    return await client.post(
        f"{EMPLOYEE_BASE}/modules/{module_id}/assessment/attempts",
        json={"answers": answers},
        headers={**person.headers, "Idempotency-Key": key or f"k-{uuid4()}"},
    )


def comparable(response: httpx.Response) -> tuple[int, bytes, dict[str, str]]:
    """What must be identical between two answers that are meant to be indistinguishable: the
    status, the exact body bytes and every header except the ones that differ per request."""
    volatile = {"date", "x-request-id", "server"}
    headers = {k.lower(): v for k, v in response.headers.items() if k.lower() not in volatile}
    return response.status_code, response.content, headers

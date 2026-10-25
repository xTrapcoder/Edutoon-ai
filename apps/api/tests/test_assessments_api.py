# ruff: noqa: F811, E501 - pytest fixtures imported from test_admin_training_api; long SQL strings
"""Assessments, attempts and the pass gate over the HTTP surface (Step 3).

Real application, routes and dependencies, rolled-back ``db_session``, real local Redis and MinIO,
as in ``test_employee_training_api.py`` (whose fixtures these tests reuse). What is proved here:

* grading is integer-only and decided against the **threshold stored on the assessment**;
* an employee never receives the answer - checked on the generated OpenAPI schema, on the types, and
  on the serialised response *bytes*;
* every attempt keeps the exact question set, key and threshold it was graded with, so editing the
  assessment afterwards changes nothing recorded;
* passing is sticky, a module with no assessment blocks nothing, deleting or reordering a course's
  modules is refused (409) once attempts exist;
* an assessment behind a module the caller cannot reach is **byte-identical** to one that does not
  exist, and a retried submission with the same ``Idempotency-Key`` is the same attempt.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from edutoon.api.schemas import assessments as schemas
from edutoon.core.errors import ConflictError, NotFoundError
from edutoon.domain.training_assessment import (
    MODULE_DELETE_REFUSED,
    Answer,
    AuthoredOption,
    AuthoredQuestion,
)
from edutoon.main import create_app
from edutoon.repositories import assessments as assessments_repo
from edutoon.repositories import training as training_repo
from edutoon.services import assessments as assessments_service
from edutoon.services import training_access
from tests.test_admin_training_api import (  # noqa: F401 - fixtures used by name
    BASE,
    NIL_ID,
    admin,
    h,
    make_course,
    publish,
    source,
    storage,
    track,
)
from tests.training_api_helpers import (
    EMPLOYEE_BASE,
    answers_for,
    assessment_payload,
    comparable,
    enrol,
    publish_assessment,
    published_course,
    put_assessment,
    put_live,
    submit_attempt,
    unpublish_assessment,
    withdraw,
)

pytestmark = pytest.mark.real_auth

STREAM = EMPLOYEE_BASE + "/modules/{}/video"
ASSESSMENT = EMPLOYEE_BASE + "/modules/{}/assessment"
ATTEMPTS = EMPLOYEE_BASE + "/modules/{}/assessment/attempts"
ADMIN_ASSESSMENT = BASE + "/modules/{}/assessment"
FIXED_404 = b'{"error":{"code":"not_found","message":"Assessment not found."}}'


@pytest.fixture
async def employee(make_person):
    return await make_person(role="employee")


async def world(role_client, admin, employee, source, track, db_session, storage, **kwargs):
    """A published course the employee is enrolled in (module 1 has an assessment by default)."""
    kwargs.setdefault("modules", 2)
    kwargs.setdefault("assessed", (0,))
    course, modules = await published_course(
        role_client, admin, source, track, db_session, storage, **kwargs
    )
    await enrol(role_client, admin, course["id"], employee.user.id)
    return course, modules


async def attempts_of(db_session, employee, module_id):
    return await assessments_repo.list_attempts(
        db_session, user_id=employee.user.id, module_id=UUID(module_id)
    )


# --- authoring -----------------------------------------------------------------------------------


async def test_an_administrator_creates_reads_and_replaces_an_assessment(
    role_client, admin, source, track, db_session, storage
):
    course, [module] = await published_course(
        role_client, admin, source, track, db_session, storage, modules=1
    )
    missing = await role_client.get(ADMIN_ASSESSMENT.format(module["id"]), headers=admin.headers)
    assert missing.status_code == 404 and missing.content == FIXED_404

    saved = await put_assessment(
        role_client, admin, module["id"], assessment_payload(questions=3, threshold=80)
    )

    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert (body["threshold_percent"], body["question_count"]) == (80, 3)
    assert [q["position"] for q in body["questions"]] == [1, 2, 3]
    for question in body["questions"]:
        assert [o["is_correct"] for o in question["options"]] == [True, False, False]
    assert (
        await role_client.get(ADMIN_ASSESSMENT.format(module["id"]), headers=admin.headers)
    ).json() == body

    replaced = await put_assessment(
        role_client, admin, module["id"], assessment_payload(questions=1)
    )
    assert replaced.json()["threshold_percent"] == 70  # the default, when none is sent
    assert replaced.json()["id"] == body["id"]  # the same assessment, new questions
    assert replaced.json()["question_count"] == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(questions=[]),
        lambda p: p.update(threshold_percent=0),
        lambda p: p.update(threshold_percent=101),
        lambda p: p["questions"][0]["options"][0].update(is_correct=False),  # none correct
        lambda p: p["questions"][0]["options"][1].update(is_correct=True),  # two correct
        lambda p: p["questions"][0].update(options=p["questions"][0]["options"][:1]),
        lambda p: p["questions"][0]["options"][1].update(label="Q1 option 1"),  # duplicate label
        lambda p: p["questions"][0].update(prompt="   "),
        lambda p: p.update(unexpected=1),  # rule 10: unknown fields are rejected
        lambda p: p["questions"][0].update(explanation="x"),
    ],
)
async def test_an_invalid_assessment_is_a_422_and_nothing_is_saved(
    role_client, admin, source, track, db_session, storage, mutate
):
    course, [module] = await published_course(
        role_client, admin, source, track, db_session, storage, modules=1
    )
    payload = assessment_payload(questions=2)
    mutate(payload)

    response = await put_assessment(role_client, admin, module["id"], payload)

    assert response.status_code == 422
    after = await role_client.get(ADMIN_ASSESSMENT.format(module["id"]), headers=admin.headers)
    assert after.status_code == 404


async def test_deleting_an_assessment_is_refused_once_it_has_attempts(
    role_client, admin, employee, source, track, db_session, storage
):
    course, modules = await world(
        role_client, admin, employee, source, track, db_session, storage, modules=2, assessed=(0, 1)
    )
    await submit_attempt(
        role_client,
        employee,
        modules[0]["id"],
        await answers_for(role_client, admin, modules[0]["id"]),
    )

    refused = await role_client.delete(ADMIN_ASSESSMENT.format(modules[0]["id"]), headers=h(admin))
    removed = await role_client.delete(ADMIN_ASSESSMENT.format(modules[1]["id"]), headers=h(admin))

    assert (
        refused.status_code == 409 and "Replace its questions" in refused.json()["error"]["message"]
    )
    assert removed.status_code == 204
    assert (
        await role_client.get(ADMIN_ASSESSMENT.format(modules[1]["id"]), headers=admin.headers)
    ).status_code == 404
    assert (
        await role_client.get(ADMIN_ASSESSMENT.format(modules[0]["id"]), headers=admin.headers)
    ).status_code == 200


# --- no answer ever reaches an employee ------------------------------------------------------------

EMPLOYEE_SCHEMAS = (
    "EmployeeAssessmentResponse",
    "EmployeeQuestionResponse",
    "EmployeeOptionResponse",
    "AttemptResponse",
    "AttemptListResponse",
    "AttemptSubmitRequest",
    "AnswerInput",
)
LEAKING_NAMES = ("correct_option", "is_correct", "explanation", "answer_key", "key")


def _property_names(
    schema_name: str, schemas_by_name: dict, seen: set[str] | None = None
) -> set[str]:
    seen = seen if seen is not None else set()
    if schema_name in seen:
        return set()
    seen.add(schema_name)
    schema = schemas_by_name[schema_name]
    names = set(schema.get("properties", {}))
    for prop in schema.get("properties", {}).values():
        for ref in re.findall(r"#/components/schemas/(\w+)", json.dumps(prop)):
            names |= _property_names(ref, schemas_by_name, seen)
    return names


def test_no_employee_schema_in_the_openapi_document_can_carry_an_answer():
    components = create_app().openapi()["components"]["schemas"]
    for name in EMPLOYEE_SCHEMAS:
        found = _property_names(name, components)
        assert found, name
        assert not [n for n in found if any(bad in n for bad in LEAKING_NAMES)], (name, found)
    # ... while the administrator's option does carry it (the contrast that makes the check mean something)
    assert "is_correct" in _property_names("AdminAssessmentResponse", components)


def test_the_employee_types_have_no_field_for_an_answer_and_share_nothing_with_the_admin_ones():
    for model in (
        schemas.EmployeeAssessmentResponse,
        schemas.EmployeeQuestionResponse,
        schemas.EmployeeOptionResponse,
        schemas.AttemptResponse,
    ):
        assert not [f for f in model.model_fields if any(bad in f for bad in LEAKING_NAMES)]
    assert not issubclass(schemas.EmployeeOptionResponse, schemas.AdminOptionResponse)
    assert not issubclass(schemas.AdminOptionResponse, schemas.EmployeeOptionResponse)
    assert "is_correct" in schemas.AdminOptionResponse.model_fields
    employee_fields = {
        f.name for f in __import__("dataclasses").fields(assessments_repo.EmployeeOption)
    }
    employee_fields |= {
        f.name for f in __import__("dataclasses").fields(assessments_repo.EmployeeQuestion)
    }
    assert not [n for n in employee_fields if any(bad in n for bad in LEAKING_NAMES)]


def test_the_employee_read_path_never_touches_the_answer_key_table():
    """Structural: the code reachable from ``list_employee_questions`` names neither the key table
    nor the helper that reads it (docstrings aside)."""
    import ast
    import inspect

    reachable = (
        assessments_repo.list_employee_questions,
        assessments_repo._question_rows,
        assessments_repo._options_by_question,
    )
    for function in reachable:
        tree = ast.parse(inspect.getsource(function))
        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        used |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert not used & {"assessment_answer_keys", "_keys", "correct_option_id"}, (
            function.__name__
        )


FORBIDDEN_BYTES = (b'"is_correct"', b'"correct_option_id"', b'"explanation"', b'"answer_key"')


async def test_employee_responses_contain_no_answer_in_their_bytes(
    role_client, admin, employee, source, track, db_session, storage
):
    course, modules = await world(
        role_client, admin, employee, source, track, db_session, storage, modules=1
    )
    module = modules[0]
    wrong = await answers_for(role_client, admin, module["id"], right=0)
    right = await answers_for(role_client, admin, module["id"], right=2)

    shown = await role_client.get(ASSESSMENT.format(module["id"]), headers=employee.headers)
    failed = await submit_attempt(role_client, employee, module["id"], wrong)
    passed = await submit_attempt(role_client, employee, module["id"], right)
    listed = await role_client.get(ATTEMPTS.format(module["id"]), headers=employee.headers)
    mine = await role_client.get(EMPLOYEE_BASE + "/courses", headers=employee.headers)

    for response in (shown, failed, passed, listed, mine):
        assert response.status_code in (200, 201), response.text
        for token in FORBIDDEN_BYTES:
            assert token not in response.content, (token, response.request.url)
    for option in shown.json()["questions"][0]["options"]:
        assert set(option) == {"id", "position", "label"}
    assert set(failed.json()) == {
        "id",
        "module_id",
        "attempt_number",
        "correct_count",
        "question_count",
        "threshold_percent",
        "passed",
        "submitted_at",
    }


async def test_the_employee_view_is_the_same_whichever_option_is_correct(
    role_client, admin, employee, source, track, db_session, storage
):
    """Two assessments with identical wording and different keys give byte-identical employee
    views once the (random) ids are masked: nothing in the response tells the right option."""
    course, modules = await world(
        role_client, admin, employee, source, track, db_session, storage, modules=2, assessed=()
    )
    first = assessment_payload(questions=2)
    second = assessment_payload(questions=2)
    for question in second["questions"]:
        question["options"][0]["is_correct"] = False
        question["options"][2]["is_correct"] = True
    assert (await put_live(role_client, admin, modules[0]["id"], first)).status_code == 200
    assert (await put_live(role_client, admin, modules[1]["id"], second)).status_code == 200
    # module 2 is locked behind module 1 while module 1 has an assessment: pass module 1 first
    await submit_attempt(
        role_client,
        employee,
        modules[0]["id"],
        await answers_for(role_client, admin, modules[0]["id"]),
    )

    views = []
    for module in modules:
        response = await role_client.get(ASSESSMENT.format(module["id"]), headers=employee.headers)
        assert response.status_code == 200, response.text
        views.append(re.sub(rb"[0-9a-f]{8}-[0-9a-f-]{27}", b"ID", response.content))

    assert views[0] == views[1]


# --- grading: integers, and the threshold stored on the assessment -----------------------------------


@pytest.mark.parametrize(
    ("total", "right", "threshold", "passed"),
    [
        (10, 7, 70, True),
        (10, 6, 70, False),
        (3, 2, 70, False),
        (3, 3, 70, True),
        (7, 5, 70, True),
        (10, 0, 70, False),
        (10, 10, 100, True),
        (10, 9, 100, False),
        (10, 5, 50, True),
        (3, 2, 66, True),
        (3, 2, 67, False),
    ],
)
async def test_the_pass_gate_over_http_uses_the_stored_threshold_and_integers(
    role_client,
    admin,
    employee,
    source,
    track,
    db_session,
    storage,
    total,
    right,
    threshold,
    passed,
):
    course, modules = await world(
        role_client, admin, employee, source, track, db_session, storage, modules=1, assessed=()
    )
    module = modules[0]
    saved = await put_live(
        role_client, admin, module["id"], assessment_payload(questions=total, threshold=threshold)
    )
    assert saved.status_code == 200

    response = await submit_attempt(
        role_client,
        employee,
        module["id"],
        await answers_for(role_client, admin, module["id"], right=right),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["correct_count"], body["question_count"], body["threshold_percent"]) == (
        right,
        total,
        threshold,
    )
    assert body["passed"] is passed
    assert all(isinstance(body[k], int) for k in ("correct_count", "question_count"))


async def test_an_unanswerable_submission_is_a_422_and_writes_nothing(
    role_client, admin, employee, source, track, db_session, storage
):
    course, modules = await world(
        role_client, admin, employee, source, track, db_session, storage, modules=2, assessed=(0, 1)
    )
    module, other = modules
    good = await answers_for(role_client, admin, module["id"])
    other_view = (
        await role_client.get(ADMIN_ASSESSMENT.format(other["id"]), headers=admin.headers)
    ).json()
    foreign_option = other_view["questions"][0]["options"][0]["id"]
    cases = {
        "missing an answer": good[:1],
        "a question twice": [good[0], good[0]],
        "unknown question": [*good, {"question_id": NIL_ID, "option_id": NIL_ID}],
        "foreign option": [{**good[0], "option_id": foreign_option}, good[1]],
        "empty": [],
    }

    for name, answers in cases.items():
        response = await submit_attempt(role_client, employee, module["id"], answers)
        assert response.status_code == 422, (name, response.text)

    extra = await role_client.post(
        ATTEMPTS.format(module["id"]),
        json={"answers": good, "score": 100},
        headers={**employee.headers, "Idempotency-Key": f"k-{uuid4()}"},
    )
    no_key = await role_client.post(
        ATTEMPTS.format(module["id"]), json={"answers": good}, headers=employee.headers
    )
    assert extra.status_code == 422 and no_key.status_code == 422
    assert await attempts_of(db_session, employee, module["id"]) == []


async def test_rewriting_the_assessment_after_an_attempt_changes_nothing_that_was_recorded(
    role_client, admin, employee, source, track, db_session, storage
):
    """No route can edit an attempted assessment any more (it is published, and unpublishing is
    refused once it has attempts - the 409s asserted first). The data layer must still keep every
    recorded attempt independent of the authoring rows, so they are rewritten here directly."""
    course, modules = await world(
        role_client, admin, employee, source, track, db_session, storage, modules=1, assessed=()
    )
    module = modules[0]
    await put_live(role_client, admin, module["id"], assessment_payload(questions=10, threshold=70))
    original = (
        await role_client.get(ADMIN_ASSESSMENT.format(module["id"]), headers=admin.headers)
    ).json()
    failed = await submit_attempt(
        role_client,
        employee,
        module["id"],
        await answers_for(role_client, admin, module["id"], right=6),
    )
    assert failed.json()["passed"] is False
    [before] = await attempts_of(db_session, employee, module["id"])
    answers_before = await assessments_repo.list_answer_rows(db_session, before.id)

    # the routes now refuse to change it ...
    assert (
        await put_assessment(role_client, admin, module["id"], assessment_payload(questions=2))
    ).status_code == 409
    assert (await unpublish_assessment(role_client, admin, module["id"])).status_code == 409
    # ... so rewrite the authoring rows and lower the threshold underneath it, as a bug or a manual
    # change could
    assessment = await assessments_repo.get_by_module(db_session, UUID(module["id"]))
    assert assessment is not None
    await assessments_repo.replace_questions(
        db_session,
        assessment_id=assessment.id,
        questions=[
            AuthoredQuestion(
                f"Rewritten {n}?",
                (AuthoredOption("Yes", True), AuthoredOption("No", False)),
            )
            for n in (1, 2)
        ],
    )
    await db_session.execute(
        text("UPDATE assessments SET threshold_percent = 10 WHERE id = :a"), {"a": assessment.id}
    )

    [after] = await attempts_of(db_session, employee, module["id"])
    answers_after = await assessments_repo.list_answer_rows(db_session, after.id)
    assert after == before
    assert (after.correct_count, after.question_count, after.threshold_percent, after.passed) == (
        6,
        10,
        70,
        False,
    )
    assert answers_after == answers_before and len(answers_after) == 10
    assert [r["question_id"] for r in answers_after] == [
        UUID(q["id"]) for q in original["questions"]
    ]
    listed = await role_client.get(ATTEMPTS.format(module["id"]), headers=employee.headers)
    assert listed.json()["items"][0]["passed"] is False  # not re-graded under the new threshold
    assert listed.json()["items"][0]["question_count"] == 10
    # the next attempt is graded against the new set and threshold
    again = await submit_attempt(
        role_client,
        employee,
        module["id"],
        await answers_for(role_client, admin, module["id"], right=1),
    )
    assert (again.json()["question_count"], again.json()["threshold_percent"]) == (2, 10)
    assert again.json()["attempt_number"] == 2


async def test_a_submission_made_against_an_edited_assessment_is_refused_not_misgraded(
    role_client, admin, employee, source, track, db_session, storage
):
    course, modules = await world(
        role_client, admin, employee, source, track, db_session, storage, modules=1
    )
    module = modules[0]
    stale = await answers_for(role_client, admin, module["id"])
    # the administrator unpublishes (no attempts yet), rewrites and publishes again
    assert (await unpublish_assessment(role_client, admin, module["id"])).status_code == 200
    assert (
        await put_live(role_client, admin, module["id"], assessment_payload(questions=2))
    ).status_code == 200

    response = await submit_attempt(role_client, employee, module["id"], stale)

    assert response.status_code == 422 and "reload" in response.json()["error"]["message"]
    assert await attempts_of(db_session, employee, module["id"]) == []


# --- progression ---------------------------------------------------------------------------------------


async def test_passing_unlocks_the_next_module_and_failing_does_not(
    role_client, admin, employee, source, track, db_session, storage
):
    course, modules = await world(role_client, admin, employee, source, track, db_session, storage)
    first, second = modules
    locked = await role_client.get(STREAM.format(second["id"]), headers=employee.headers)
    assert locked.status_code == 403 and locked.json()["error"]["code"] == "module_locked"

    failed = await submit_attempt(
        role_client,
        employee,
        first["id"],
        await answers_for(role_client, admin, first["id"], right=0),
    )
    assert failed.json()["passed"] is False
    assert (
        await role_client.get(STREAM.format(second["id"]), headers=employee.headers)
    ).status_code == 403

    passed = await submit_attempt(
        role_client, employee, first["id"], await answers_for(role_client, admin, first["id"])
    )
    assert passed.json()["passed"] is True
    assert (
        await role_client.get(STREAM.format(second["id"]), headers=employee.headers)
    ).status_code == 200


async def test_a_pass_is_sticky_a_later_failed_retake_never_relocks_the_next_module(
    role_client, admin, employee, source, track, db_session, storage
):
    course, modules = await world(role_client, admin, employee, source, track, db_session, storage)
    first, second = modules
    await submit_attempt(
        role_client, employee, first["id"], await answers_for(role_client, admin, first["id"])
    )

    retake = await submit_attempt(
        role_client,
        employee,
        first["id"],
        await answers_for(role_client, admin, first["id"], right=0),
    )

    assert retake.status_code == 201 and retake.json()["passed"] is False
    assert retake.json()["attempt_number"] == 2
    assert (
        await role_client.get(STREAM.format(second["id"]), headers=employee.headers)
    ).status_code == 200
    [mine] = (await role_client.get(EMPLOYEE_BASE + "/courses", headers=employee.headers)).json()[
        "items"
    ]
    assert [m["locked"] for m in mine["modules"]] == [False, False]
    assert await training_access.passed_module_ids(
        db_session, user_id=employee.user.id, module_ids=[UUID(first["id"])]
    ) == frozenset({UUID(first["id"])})
    history = await role_client.get(ATTEMPTS.format(first["id"]), headers=employee.headers)
    assert [a["passed"] for a in history.json()["items"]] == [True, False]  # every attempt recorded


async def test_a_module_without_an_assessment_does_not_block_progression(
    role_client, admin, employee, source, track, db_session, storage
):
    course, modules = await world(
        role_client, admin, employee, source, track, db_session, storage, modules=3, assessed=(1,)
    )
    first, second, third = modules

    # module 1 has no assessment: module 2 is open at once; module 3 waits for module 2's pass
    assert (
        await role_client.get(STREAM.format(second["id"]), headers=employee.headers)
    ).status_code == 200
    blocked = await role_client.get(STREAM.format(third["id"]), headers=employee.headers)
    assert blocked.status_code == 403
    assert blocked.json()["error"]["details"]["blocking_module_id"] == second["id"]
    [mine] = (await role_client.get(EMPLOYEE_BASE + "/courses", headers=employee.headers)).json()[
        "items"
    ]
    assert [m["locked"] for m in mine["modules"]] == [False, False, True]

    await submit_attempt(
        role_client, employee, second["id"], await answers_for(role_client, admin, second["id"])
    )
    assert (
        await role_client.get(STREAM.format(third["id"]), headers=employee.headers)
    ).status_code == 200


async def test_a_course_with_no_assessments_at_all_is_fully_open(
    role_client, admin, employee, source, track, db_session, storage
):
    course, modules = await world(
        role_client, admin, employee, source, track, db_session, storage, modules=3, assessed=()
    )

    for module in modules:
        response = await role_client.get(STREAM.format(module["id"]), headers=employee.headers)
        assert response.status_code == 200


async def test_a_locked_module_refuses_its_assessment_and_records_nothing(
    role_client, admin, employee, source, track, db_session, storage
):
    course, modules = await world(
        role_client, admin, employee, source, track, db_session, storage, modules=2, assessed=(0, 1)
    )
    second = modules[1]

    shown = await role_client.get(ASSESSMENT.format(second["id"]), headers=employee.headers)
    sent = await submit_attempt(
        role_client, employee, second["id"], await answers_for(role_client, admin, second["id"])
    )
    history = await role_client.get(ATTEMPTS.format(second["id"]), headers=employee.headers)

    for response in (shown, sent, history):
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "module_locked"
    assert await attempts_of(db_session, employee, second["id"]) == []


async def test_one_employees_attempts_are_invisible_to_another(
    role_client, admin, employee, make_person, source, track, db_session, storage
):
    course, modules = await world(
        role_client, admin, employee, source, track, db_session, storage, modules=1
    )
    other = await make_person(role="employee")
    await enrol(role_client, admin, course["id"], other.user.id)
    module = modules[0]
    await submit_attempt(
        role_client, employee, module["id"], await answers_for(role_client, admin, module["id"])
    )

    theirs = await role_client.get(ATTEMPTS.format(module["id"]), headers=other.headers)

    assert theirs.status_code == 200 and theirs.json() == {"items": []}


# --- deleting and reordering once attempts exist -------------------------------------------------------


async def test_deleting_a_module_is_refused_once_it_has_attempts_and_works_before(
    role_client, admin, employee, source, track, db_session, storage
):
    course, modules = await world(
        role_client, admin, employee, source, track, db_session, storage, modules=3, assessed=(0,)
    )
    first, second, third = modules
    await submit_attempt(
        role_client, employee, first["id"], await answers_for(role_client, admin, first["id"])
    )

    refused = await role_client.delete(f"{BASE}/modules/{first['id']}", headers=h(admin))
    removed = await role_client.delete(f"{BASE}/modules/{second['id']}", headers=h(admin))

    assert refused.status_code == 409
    assert "Unpublish it instead" in refused.json()["error"]["message"]
    assert (
        await role_client.get(f"{BASE}/modules/{first['id']}", headers=admin.headers)
    ).status_code == 200
    assert removed.status_code == 204  # the previously untested deletion path
    remaining = await role_client.get(
        f"{BASE}/courses/{course['id']}/modules", headers=admin.headers
    )
    assert [m["id"] for m in remaining.json()["items"]] == [first["id"], third["id"]]
    assert [m["position"] for m in remaining.json()["items"]] == [1, 2]  # the gap closed


async def test_deleting_a_module_takes_its_never_attempted_assessment_with_it(
    role_client, admin, source, track, db_session, storage
):
    course, [module] = await published_course(
        role_client, admin, source, track, db_session, storage, modules=1, assessed=(0,)
    )
    assert await assessments_repo.get_by_module(db_session, UUID(module["id"])) is not None

    removed = await role_client.delete(f"{BASE}/modules/{module['id']}", headers=h(admin))

    assert removed.status_code == 204
    assert await assessments_repo.get_by_module(db_session, UUID(module["id"])) is None
    left = await db_session.execute(
        text(
            "SELECT count(*) FROM assessment_questions WHERE assessment_id NOT IN "
            "(SELECT id FROM assessments)"
        )
    )
    assert left.scalar_one() == 0


async def test_reordering_is_refused_once_any_module_of_the_course_has_attempts(
    role_client, admin, employee, source, track, db_session, storage
):
    course, modules = await world(
        role_client, admin, employee, source, track, db_session, storage, modules=2, assessed=(0,)
    )
    ids = [m["id"] for m in modules]
    order_url = f"{BASE}/courses/{course['id']}/modules/order"
    reordered = await role_client.put(order_url, json={"module_ids": ids[::-1]}, headers=h(admin))
    assert reordered.status_code == 200  # before any attempt it still works
    await role_client.put(order_url, json={"module_ids": ids}, headers=h(admin))
    await submit_attempt(
        role_client, employee, ids[0], await answers_for(role_client, admin, ids[0])
    )

    refused = await role_client.put(order_url, json={"module_ids": ids[::-1]}, headers=h(admin))

    assert refused.status_code == 409
    message = refused.json()["error"]["message"]
    assert "Create a new course" in message
    positions = await role_client.get(
        f"{BASE}/courses/{course['id']}/modules", headers=admin.headers
    )
    assert [m["id"] for m in positions.json()["items"]] == ids  # nothing moved


async def test_the_database_itself_refuses_to_delete_a_module_with_attempts(
    role_client, admin, employee, source, track, db_session, storage
):
    """The RESTRICT foreign keys (migration 0021), whatever the application does."""
    course, modules = await world(
        role_client, admin, employee, source, track, db_session, storage, modules=1
    )
    module = modules[0]
    await submit_attempt(
        role_client, employee, module["id"], await answers_for(role_client, admin, module["id"])
    )

    for statement in (
        "DELETE FROM training_modules WHERE id = :m",
        "DELETE FROM assessments WHERE module_id = :m",
        "DELETE FROM users WHERE id = (SELECT user_id FROM assessment_attempts WHERE module_id = :m)",
    ):
        with pytest.raises(IntegrityError) as caught:
            async with db_session.begin_nested():
                await db_session.execute(text(statement), {"m": module["id"]})
        assert (
            "23503" in str(getattr(caught.value.orig, "sqlstate", ""))
            or "foreign key" in str(caught.value).lower()
        )


# --- the six causes, one 404, for assessment routes -------------------------------------------------------


async def test_an_assessment_behind_an_unreachable_module_is_indistinguishable_from_none(
    role_client, admin, employee, make_person, source, track, db_session, storage
):
    course, modules = await world(
        role_client, admin, employee, source, track, db_session, storage, modules=2, assessed=(0, 1)
    )
    target, unpublished = modules
    # a reachable module that simply has no assessment
    bare_course, [bare] = await published_course(
        role_client, admin, source, track, db_session, storage, modules=1, title="Bare"
    )
    await enrol(role_client, admin, bare_course["id"], employee.user.id)

    drafted_course, [drafted] = await published_course(
        role_client, admin, source, track, db_session, storage, modules=1, title="Drafted"
    )
    await enrol(role_client, admin, drafted_course["id"], employee.user.id)
    assert (await put_assessment(role_client, admin, drafted["id"])).status_code == 200  # a draft
    stranger = await make_person(role="employee")  # (2) enrolled in nothing
    former = await make_person(role="employee")  # (3) enrolled, then withdrawn
    await enrol(role_client, admin, course["id"], former.user.id)
    await withdraw(role_client, admin, course["id"], former.user.id)
    draft = await make_course(role_client, admin, "Not yet published")  # (4) course not published
    draft_module = (await publish(role_client, track, admin, draft["id"], source, title="D")).json()
    assert (await put_assessment(role_client, admin, draft_module["id"])).status_code == 200
    await enrol(role_client, admin, draft["id"], employee.user.id)
    await role_client.post(f"{BASE}/modules/{unpublished['id']}/unpublish", headers=h(admin))  # (5)

    # sanity: the baseline really is reachable, so the others differ only by cause
    assert (
        await role_client.get(ASSESSMENT.format(target["id"]), headers=employee.headers)
    ).status_code == 200

    def cases():
        return {
            "missing module": (NIL_ID, employee),
            "not enrolled": (target["id"], stranger),
            "withdrawn": (target["id"], former),
            "course not published": (draft_module["id"], employee),
            "module not published": (unpublished["id"], employee),
            "not an employee": (target["id"], admin),
            "no assessment on a reachable module": (bare["id"], employee),
            "draft assessment on a reachable module": (drafted["id"], employee),
        }

    for verb, builder in (
        (
            "GET",
            lambda module_id, who: role_client.get(
                ASSESSMENT.format(module_id), headers=who.headers
            ),
        ),
        (
            "GET attempts",
            lambda module_id, who: role_client.get(ATTEMPTS.format(module_id), headers=who.headers),
        ),
        (
            "POST attempts",
            lambda module_id, who: role_client.post(
                ATTEMPTS.format(module_id),
                json={"answers": [{"question_id": NIL_ID, "option_id": NIL_ID}]},
                headers={**who.headers, "Idempotency-Key": f"k-{uuid4()}"},
            ),
        ),
    ):
        answers = {
            name: await builder(module_id, who) for name, (module_id, who) in cases().items()
        }
        reference = comparable(answers["missing module"])
        assert reference[0] == 404 and reference[1] == FIXED_404, verb
        for cause, response in answers.items():
            assert comparable(response) == reference, f"{verb}: {cause} is distinguishable"


# --- idempotent submission --------------------------------------------------------------------------------


async def test_retrying_a_submission_with_the_same_key_is_the_same_attempt(
    role_client, admin, employee, source, track, db_session, storage
):
    course, modules = await world(
        role_client, admin, employee, source, track, db_session, storage, modules=1
    )
    module = modules[0]
    answers = await answers_for(role_client, admin, module["id"], right=1)
    key = f"retry-{uuid4()}"

    first = await submit_attempt(role_client, employee, module["id"], answers, key=key)
    again = await submit_attempt(role_client, employee, module["id"], answers, key=key)

    assert first.status_code == again.status_code == 201
    assert again.json() == first.json()
    assert len(await attempts_of(db_session, employee, module["id"])) == 1
    other_key = await submit_attempt(role_client, employee, module["id"], answers)
    assert other_key.json()["id"] != first.json()["id"]
    assert other_key.json()["attempt_number"] == 2


async def test_the_key_is_durable_even_if_the_cache_forgets_it(
    db_session, admin, employee, role_client, source, track, storage
):
    """The service finds the attempt by ``(user, Idempotency-Key)`` in the database, so a retry that
    arrives after the Redis record expired still returns the first attempt and makes no second."""
    course, modules = await world(
        role_client, admin, employee, source, track, db_session, storage, modules=1
    )
    module_id = UUID(modules[0]["id"])
    view = await assessments_service.get_for_employee(
        db_session, employee.user, module_id=module_id
    )
    admin_view = await assessments_service.get_admin_view(db_session, module_id=module_id)
    answers = [
        Answer(q.id, next(o.id for o in q.options if o.is_correct)) for q in admin_view.questions
    ]
    assert len(view.questions) == len(answers)

    one = await assessments_service.submit_attempt(
        db_session, employee.user, module_id=module_id, answers=answers, idempotency_key="durable-1"
    )
    two = await assessments_service.submit_attempt(
        db_session, employee.user, module_id=module_id, answers=answers, idempotency_key="durable-1"
    )

    assert one == two
    assert (
        len(
            await assessments_repo.list_attempts(
                db_session, user_id=employee.user.id, module_id=module_id
            )
        )
        == 1
    )


async def test_a_key_already_used_for_another_module_is_a_conflict_not_a_replay(
    db_session, admin, employee, role_client, source, track, storage
):
    course, modules = await world(
        role_client, admin, employee, source, track, db_session, storage, modules=2, assessed=()
    )
    for module in modules:
        assert (await put_live(role_client, admin, module["id"])).status_code == 200
    first, second = (UUID(m["id"]) for m in modules)
    answers_first = [
        Answer(q.id, next(o.id for o in q.options if o.is_correct))
        for q in (await assessments_service.get_admin_view(db_session, module_id=first)).questions
    ]
    await assessments_service.submit_attempt(
        db_session, employee.user, module_id=first, answers=answers_first, idempotency_key="shared"
    )
    answers_second = [
        Answer(q.id, next(o.id for o in q.options if o.is_correct))
        for q in (await assessments_service.get_admin_view(db_session, module_id=second)).questions
    ]

    with pytest.raises(ConflictError):
        await assessments_service.submit_attempt(
            db_session,
            employee.user,
            module_id=second,
            answers=answers_second,
            idempotency_key="shared",
        )
    assert (
        await assessments_repo.list_attempts(db_session, user_id=employee.user.id, module_id=second)
        == []
    )


async def test_the_service_turns_every_unreachable_case_into_the_assessment_404(
    db_session, admin, employee, role_client, source, track, storage
):
    with pytest.raises(NotFoundError) as caught:
        await assessments_service.get_for_employee(db_session, employee.user, module_id=uuid4())
    assert caught.value.message == "Assessment not found."


# --- immutability ------------------------------------------------------------------------------------------


async def test_the_database_refuses_to_change_or_delete_a_submitted_attempt_or_its_answers(
    role_client, admin, employee, source, track, db_session, storage
):
    course, modules = await world(
        role_client, admin, employee, source, track, db_session, storage, modules=1
    )
    module = modules[0]
    submitted = await submit_attempt(
        role_client,
        employee,
        module["id"],
        await answers_for(role_client, admin, module["id"], right=0),
    )
    attempt_id = submitted.json()["id"]
    [before] = await attempts_of(db_session, employee, module["id"])
    answers_before = await assessments_repo.list_answer_rows(db_session, before.id)

    attacks = [
        "UPDATE assessment_attempts SET passed = true, correct_count = question_count WHERE id = :a",
        "UPDATE assessment_attempts SET threshold_percent = 1 WHERE id = :a",
        "DELETE FROM assessment_attempts WHERE id = :a",
        "UPDATE assessment_attempt_answers SET is_correct = true, selected_option_id = correct_option_id "
        "WHERE attempt_id = :a",
        "DELETE FROM assessment_attempt_answers WHERE attempt_id = :a",
    ]
    for statement in attacks:
        with pytest.raises(DBAPIError) as caught:
            async with db_session.begin_nested():
                await db_session.execute(text(statement), {"a": attempt_id})
        assert "append-only" in str(caught.value), statement

    [after] = await attempts_of(db_session, employee, module["id"])
    assert after == before and not after.passed
    assert await assessments_repo.list_answer_rows(db_session, before.id) == answers_before


async def test_the_database_will_not_store_a_pass_that_disagrees_with_the_score(
    role_client, admin, employee, source, track, db_session, storage
):
    course, modules = await world(
        role_client, admin, employee, source, track, db_session, storage, modules=1
    )
    module = modules[0]
    assessment = await assessments_repo.get_by_module(db_session, UUID(module["id"]))
    assert assessment is not None

    for correct, passed in ((1, True), (2, False), (3, False)):
        with pytest.raises(IntegrityError):
            async with db_session.begin_nested():
                await db_session.execute(
                    text(
                        "INSERT INTO assessment_attempts (assessment_id, module_id, user_id, "
                        "attempt_number, threshold_percent, correct_count, question_count, passed, "
                        "idempotency_key) VALUES (:a, :m, :u, 1, 70, :c, 2, :p, :k)"
                    ),
                    {
                        "a": assessment.id,
                        "m": module["id"],
                        "u": employee.user.id,
                        "c": correct,
                        "p": passed,
                        "k": f"bad-{correct}",
                    },
                )


# --- remediation after review: the history leaks nothing, the two 409s, what "available" means ---------


def key_leaks(
    content: bytes, *, option_ids: set[str], labels: set[str], prompts: set[str]
) -> list[str]:
    """Everything in ``content`` that an attempt history has no business carrying: a field name that
    names an answer, any option or question identifier, or any option label or question prompt. The
    history is a record of outcomes (score, threshold, verdict); an identifier or wording in it would
    be a channel that could tell which option is the right one."""
    found = [token.decode() for token in FORBIDDEN_BYTES if token in content]
    found += [
        name
        for name in ("correct_option", "selected_option", "answers", "options")
        if name.encode() in content
    ]
    body = content.decode()
    found += [i for i in sorted(option_ids) if i in body]
    found += [wording for wording in sorted(labels | prompts) if wording in body]
    return found


def test_the_key_scanner_would_catch_a_leaking_history():
    """Mutation check for the scanner itself (it must not pass vacuously): a history that carries a
    correct option's id, its label, a per-answer verdict or an answer-key field is flagged, a clean
    one is not."""
    clean = json.dumps({"items": [{"attempt_number": 1, "correct_count": 1, "passed": False}]})
    scan = {"option_ids": {"opt-1"}, "labels": {"Q1 option 1"}, "prompts": {"Question 1?"}}
    assert key_leaks(clean.encode(), **scan) == []
    leaking = [
        {"items": [{"correct_option_ids": ["opt-1"]}]},
        {"items": [{"selected_option_id": "opt-2"}]},
        {"items": [{"answers": [{"is_correct": True}]}]},
        {"items": [{"note": "Q1 option 1"}]},
        {"items": [{"note": "Question 1?"}]},
        {"items": [{"hint": "opt-1"}]},
    ]
    for body in leaking:
        assert key_leaks(json.dumps(body).encode(), **scan), body


async def test_an_employees_attempt_history_shows_their_outcome_and_nothing_that_reveals_the_key(
    role_client, admin, employee, source, track, db_session, storage
):
    """Real attempts (all wrong, half right, three of four right), then the exact bytes of the history.

    The history is outcome-only: per attempt the number, the integer score, the threshold it was
    graded with, the verdict and the time. It carries no question or option identifier, no wording
    and no per-answer verdict, so it cannot say which option of any question is correct - the key
    is not in the response, and nothing in it is a function of the key beyond the score itself."""
    course, modules = await world(
        role_client, admin, employee, source, track, db_session, storage, modules=1, assessed=()
    )
    module = modules[0]
    saved = await put_live(
        role_client, admin, module["id"], assessment_payload(questions=4, options=3, threshold=70)
    )
    assert saved.status_code == 200, saved.text
    admin_view = saved.json()
    options = [o for q in admin_view["questions"] for o in q["options"]]
    option_ids = {o["id"] for o in options}
    scan = {
        "option_ids": option_ids | {q["id"] for q in admin_view["questions"]},
        "labels": {o["label"] for o in options},
        "prompts": {q["prompt"] for q in admin_view["questions"]},
    }
    assert len([o for o in options if o["is_correct"]]) == 4

    submitted = [
        await submit_attempt(
            role_client,
            employee,
            module["id"],
            await answers_for(role_client, admin, module["id"], right=right),
        )
        for right in (0, 2, 3)
    ]
    history = await role_client.get(ATTEMPTS.format(module["id"]), headers=employee.headers)

    # the employee sees the outcome of every attempt ...
    assert history.status_code == 200, history.text
    body = history.json()
    assert set(body) == {"items"}
    assert [
        (
            a["attempt_number"],
            a["correct_count"],
            a["question_count"],
            a["threshold_percent"],
            a["passed"],
        )
        for a in body["items"]
    ] == [(1, 0, 4, 70, False), (2, 2, 4, 70, False), (3, 3, 4, 70, True)]
    assert [a["id"] for a in body["items"]] == [r.json()["id"] for r in submitted]
    for item in body["items"]:
        assert set(item) == {
            "id",
            "module_id",
            "attempt_number",
            "correct_count",
            "question_count",
            "threshold_percent",
            "passed",
            "submitted_at",
        }
    # ... and nothing in the bytes of the history, or of any submission response, can name the key
    for response in (history, *submitted):
        assert key_leaks(response.content, **scan) == [], response.request.url
    # the scanner is not vacuous: it does find the key in the administrator's view of the same assessment
    assert key_leaks(json.dumps(admin_view).encode(), **scan)
    # and the question read an employee does get names every option without marking one
    shown = await role_client.get(ASSESSMENT.format(module["id"]), headers=employee.headers)
    assert shown.status_code == 200
    assert {o["id"] for q in shown.json()["questions"] for o in q["options"]} == option_ids
    assert not [t for t in FORBIDDEN_BYTES if t in shown.content]


async def test_deleting_a_module_with_attempts_is_the_same_409_from_the_check_and_from_the_database(
    role_client, admin, employee, source, track, db_session, storage, monkeypatch
):
    """Evidence for the 'equivalent' mutant "deleting a module does not check for attempts first".

    First the explicit service check refuses. Then the check is made to answer "no attempts" and the
    delete goes on to the database, whose ``RESTRICT`` foreign keys refuse it inside the savepoint.
    The status, the exact body bytes and every non-volatile header must be identical, and neither
    leaves anything changed - so the explicit check is not observable and its removal is not a
    different outcome."""
    course, modules = await world(
        role_client, admin, employee, source, track, db_session, storage, modules=2, assessed=(0,)
    )
    first, second = modules
    await submit_attempt(
        role_client, employee, first["id"], await answers_for(role_client, admin, first["id"])
    )

    async def modules_and_audit():
        listed = await role_client.get(
            f"{BASE}/courses/{course['id']}/modules", headers=admin.headers
        )
        audited = await db_session.execute(
            text("SELECT count(*) FROM audit_logs WHERE action = 'training.module.deleted'")
        )
        return listed.content, audited.scalar_one()

    before = await modules_and_audit()
    by_check = await role_client.delete(f"{BASE}/modules/{first['id']}", headers=h(admin))

    async def no_attempts_seen(*_args, **_kwargs):
        return False

    refusals: list[Exception] = []  # proof that the database, not the check, did the refusing
    real_delete = training_repo.delete_module

    async def watching_delete(session, module_id):
        try:
            await real_delete(session, module_id)
        except IntegrityError as exc:
            refusals.append(exc)
            raise

    with monkeypatch.context() as patched:  # (not monkeypatch.undo(): the fixtures' patches stay)
        patched.setattr(assessments_repo, "has_attempts_for_module", no_attempts_seen)
        patched.setattr(training_repo, "delete_module", watching_delete)
        by_database = await role_client.delete(f"{BASE}/modules/{first['id']}", headers=h(admin))
    assert len(refusals) == 1 and training_repo.is_foreign_key_violation(refusals[0])

    assert by_check.status_code == 409
    assert json.loads(by_check.content) == {
        "error": {"code": "conflict", "message": MODULE_DELETE_REFUSED}
    }
    assert comparable(by_database) == comparable(by_check)  # status, exact bytes, headers
    assert await modules_and_audit() == before  # the refused delete changed nothing
    assert second["id"] in before[0].decode()
    assert (
        await role_client.get(f"{BASE}/modules/{first['id']}", headers=admin.headers)
    ).status_code == 200


async def test_an_assessment_on_an_unpublished_module_is_invisible_and_blocks_nothing(
    role_client, admin, employee, source, track, db_session, storage
):
    """Besides the assessment's own draft state (tests below), its *module* being unpublished (or
    its course not published, covered above) hides it. An unpublished module's assessment is a 404
    for the employee, is never listed, and - whether or not anyone passed it - does not hold back
    the modules after it."""
    course, modules = await world(
        role_client, admin, employee, source, track, db_session, storage, modules=3, assessed=(0, 1)
    )
    first, second, third = modules
    await role_client.post(f"{BASE}/modules/{second['id']}/unpublish", headers=h(admin))

    hidden = await role_client.get(ASSESSMENT.format(second["id"]), headers=employee.headers)
    assert hidden.status_code == 404 and hidden.content == FIXED_404
    blocked = await role_client.get(STREAM.format(third["id"]), headers=employee.headers)
    assert blocked.status_code == 403  # still waiting for module 1, which is published and assessed
    assert blocked.json()["error"]["details"]["blocking_module_id"] == first["id"]

    await submit_attempt(
        role_client, employee, first["id"], await answers_for(role_client, admin, first["id"])
    )
    opened = await role_client.get(STREAM.format(third["id"]), headers=employee.headers)
    assert opened.status_code == 200  # the unpublished module 2 and its assessment never gated it
    [mine] = (await role_client.get(EMPLOYEE_BASE + "/courses", headers=employee.headers)).json()[
        "items"
    ]
    assert [m["id"] for m in mine["modules"]] == [first["id"], third["id"]]


async def test_a_draft_does_not_block_progression_and_publishing_it_does_until_it_is_passed(
    role_client, admin, employee, source, track, db_session, storage
):
    """The pass gate counts published assessments only. Saving gives a draft: the next module stays
    open and the employee's listing is unchanged. Publishing the very same assessment locks the next
    module until it is passed."""
    course, modules = await world(
        role_client, admin, employee, source, track, db_session, storage, modules=2, assessed=()
    )
    first, second = modules
    assert (await put_assessment(role_client, admin, first["id"])).status_code == 200  # a draft

    assert (
        await role_client.get(STREAM.format(second["id"]), headers=employee.headers)
    ).status_code == 200
    [mine] = (await role_client.get(EMPLOYEE_BASE + "/courses", headers=employee.headers)).json()[
        "items"
    ]
    assert [m["locked"] for m in mine["modules"]] == [False, False]

    published = await publish_assessment(role_client, admin, first["id"])
    assert published.status_code == 200, published.text

    locked = await role_client.get(STREAM.format(second["id"]), headers=employee.headers)
    assert locked.status_code == 403 and locked.json()["error"]["code"] == "module_locked"
    [mine] = (await role_client.get(EMPLOYEE_BASE + "/courses", headers=employee.headers)).json()[
        "items"
    ]
    assert [m["locked"] for m in mine["modules"]] == [False, True]
    await submit_attempt(
        role_client, employee, first["id"], await answers_for(role_client, admin, first["id"])
    )
    assert (
        await role_client.get(STREAM.format(second["id"]), headers=employee.headers)
    ).status_code == 200
    # what an employee reads never says whether an assessment is a draft or published
    shown = await role_client.get(ASSESSMENT.format(first["id"]), headers=employee.headers)
    assert shown.status_code == 200 and b"status" not in shown.content
    assert b"published_at" not in shown.content


@pytest.mark.parametrize("flaw", ["no questions", "one option", "no correct option", "two correct"])
async def test_publishing_validates_the_whole_stored_assessment(
    role_client, admin, employee, source, track, db_session, storage, monkeypatch, flaw
):
    """Saving already refuses these, so a stored draft is made flawed underneath it (the way a bug
    or a manual change could) and publishing must still refuse it (422) and leave it a draft.

    Two correct options cannot be *stored* at all - the key table has one row per question - so
    that case is injected at the one place publishing reads the stored questions."""
    course, modules = await world(
        role_client, admin, employee, source, track, db_session, storage, modules=1, assessed=()
    )
    module = modules[0]
    assert (
        await put_assessment(role_client, admin, module["id"], assessment_payload(options=2))
    ).status_code == 200
    stored = await assessments_repo.get_by_module(db_session, UUID(module["id"]))
    assert stored is not None
    [first_question, *_] = await assessments_repo.list_admin_questions(db_session, stored.id)

    expected_message = "needs between"
    if flaw == "no questions":
        await db_session.execute(
            text("DELETE FROM assessment_questions WHERE assessment_id = :a"), {"a": stored.id}
        )
        expected_message = "between 1 and"
    elif flaw == "one option":
        await db_session.execute(
            text("DELETE FROM assessment_options WHERE question_id = :q AND position = 2"),
            {"q": first_question.id},
        )
    elif flaw == "no correct option":
        await db_session.execute(
            text("DELETE FROM assessment_answer_keys WHERE question_id = :q"),
            {"q": first_question.id},
        )
        expected_message = "exactly one correct"
    else:
        real = assessments_repo.list_admin_questions

        async def two_correct(session, assessment_id):
            questions = await real(session, assessment_id)
            doubled = replace(
                questions[0],
                options=tuple(replace(o, is_correct=True) for o in questions[0].options),
            )
            return [doubled, *questions[1:]]

        monkeypatch.setattr(assessments_repo, "list_admin_questions", two_correct)
        expected_message = "exactly one correct"

    response = await publish_assessment(role_client, admin, module["id"])

    assert response.status_code == 422, response.text
    assert expected_message in response.json()["error"]["message"]
    after = await assessments_repo.get_by_module(db_session, UUID(module["id"]))
    assert after is not None and (after.status, after.published_at) == ("draft", None)
    shown = await role_client.get(ASSESSMENT.format(module["id"]), headers=employee.headers)
    assert shown.status_code == 404 and shown.content == FIXED_404


async def test_a_published_assessment_cannot_be_saved_over_even_with_no_attempts(
    role_client, admin, employee, source, track, db_session, storage
):
    course, modules = await world(
        role_client, admin, employee, source, track, db_session, storage, modules=1
    )
    module = modules[0]
    before = (
        await role_client.get(ADMIN_ASSESSMENT.format(module["id"]), headers=admin.headers)
    ).json()
    assert (
        before["status"] == "published"
        and await attempts_of(db_session, employee, module["id"]) == []
    )

    refused = await put_assessment(
        role_client, admin, module["id"], assessment_payload(questions=1, threshold=90)
    )

    assert refused.status_code == 409
    assert "Unpublish it first" in refused.json()["error"]["message"]
    after = await role_client.get(ADMIN_ASSESSMENT.format(module["id"]), headers=admin.headers)
    assert after.json() == before  # not a question, an option or the threshold moved


async def test_the_lifecycle_unpublish_edit_publish_while_nobody_has_attempted_it(
    role_client, admin, employee, source, track, db_session, storage
):
    course, modules = await world(
        role_client, admin, employee, source, track, db_session, storage, modules=1
    )
    module = modules[0]
    live = (
        await role_client.get(ADMIN_ASSESSMENT.format(module["id"]), headers=admin.headers)
    ).json()
    assert live["status"] == "published" and live["published_at"] is not None
    assert (
        await role_client.get(ASSESSMENT.format(module["id"]), headers=employee.headers)
    ).status_code == 200

    unpublished = await unpublish_assessment(role_client, admin, module["id"])

    assert unpublished.status_code == 200, unpublished.text
    assert (unpublished.json()["status"], unpublished.json()["published_at"]) == ("draft", None)
    hidden = await role_client.get(ASSESSMENT.format(module["id"]), headers=employee.headers)
    assert hidden.status_code == 404 and hidden.content == FIXED_404
    assert (  # already a draft
        await unpublish_assessment(role_client, admin, module["id"])
    ).status_code == 409
    edited = await put_assessment(role_client, admin, module["id"], assessment_payload(questions=3))
    assert edited.status_code == 200 and edited.json()["status"] == "draft"
    assert edited.json()["question_count"] == 3

    republished = await publish_assessment(role_client, admin, module["id"])
    assert republished.status_code == 200 and republished.json()["status"] == "published"
    assert republished.json()["published_at"] is not None
    assert (  # already published
        await publish_assessment(role_client, admin, module["id"])
    ).status_code == 409
    seen = await role_client.get(ASSESSMENT.format(module["id"]), headers=employee.headers)
    assert seen.status_code == 200 and seen.json()["question_count"] == 3


async def test_once_attempted_an_assessment_cannot_be_unpublished_changed_or_deleted(
    role_client, admin, employee, source, track, db_session, storage
):
    course, modules = await world(
        role_client, admin, employee, source, track, db_session, storage, modules=1
    )
    module = modules[0]
    await submit_attempt(
        role_client, employee, module["id"], await answers_for(role_client, admin, module["id"])
    )
    before = (
        await role_client.get(ADMIN_ASSESSMENT.format(module["id"]), headers=admin.headers)
    ).json()

    unpublished = await unpublish_assessment(role_client, admin, module["id"])
    saved = await put_assessment(role_client, admin, module["id"], assessment_payload(questions=1))
    deleted = await role_client.delete(ADMIN_ASSESSMENT.format(module["id"]), headers=h(admin))

    assert (unpublished.status_code, saved.status_code, deleted.status_code) == (409, 409, 409)
    assert "has attempts" in unpublished.json()["error"]["message"]
    assert "has attempts" in deleted.json()["error"]["message"]
    after = await role_client.get(ADMIN_ASSESSMENT.format(module["id"]), headers=admin.headers)
    assert after.json() == before and after.json()["status"] == "published"
    assert len(await attempts_of(db_session, employee, module["id"])) == 1
    assert (
        await role_client.get(ASSESSMENT.format(module["id"]), headers=employee.headers)
    ).status_code == 200


@pytest.mark.parametrize("publish_first", [False, True])
async def test_a_never_attempted_assessment_can_be_deleted_draft_or_published(
    role_client, admin, employee, source, track, db_session, storage, publish_first
):
    course, modules = await world(
        role_client, admin, employee, source, track, db_session, storage, modules=1, assessed=()
    )
    module = modules[0]
    saver = put_live if publish_first else put_assessment
    assert (await saver(role_client, admin, module["id"])).status_code == 200

    deleted = await role_client.delete(ADMIN_ASSESSMENT.format(module["id"]), headers=h(admin))

    assert deleted.status_code == 204
    assert await assessments_repo.get_by_module(db_session, UUID(module["id"])) is None
    assert (
        await role_client.get(STREAM.format(module["id"]), headers=employee.headers)
    ).status_code == 200  # and nothing is gating the module any more


async def test_publishing_reports_how_many_active_enrolments_it_affects(
    role_client, admin, make_person, source, track, db_session, storage
):
    """Zero, one, several - withdrawn enrolments excluded, other courses' enrolments not counted.
    The same assessment is unpublished and published again (allowed while unattempted) so each
    count is taken at a different state of the course."""
    course, modules = await published_course(
        role_client, admin, source, track, db_session, storage, modules=1, title="Counted"
    )
    other_course, _ = await published_course(
        role_client, admin, source, track, db_session, storage, modules=1, title="Elsewhere"
    )
    module = modules[0]
    people = [await make_person(role="employee") for _ in range(4)]
    stranger = await make_person(role="employee")
    await enrol(role_client, admin, other_course["id"], stranger.user.id)  # another course
    assert (await put_assessment(role_client, admin, module["id"])).status_code == 200

    async def publish_and_count() -> int:
        response = await publish_assessment(role_client, admin, module["id"])
        assert response.status_code == 200, response.text
        assert (await unpublish_assessment(role_client, admin, module["id"])).status_code == 200
        return response.json()["active_enrolments_affected"]

    assert await publish_and_count() == 0
    await enrol(role_client, admin, course["id"], people[0].user.id)
    assert await publish_and_count() == 1
    for person in people[1:]:
        await enrol(role_client, admin, course["id"], person.user.id)
    assert await publish_and_count() == 4
    await withdraw(role_client, admin, course["id"], people[1].user.id)
    await withdraw(role_client, admin, course["id"], people[2].user.id)
    assert await publish_and_count() == 2
    await enrol(role_client, admin, course["id"], people[1].user.id)  # re-enrolled: active again
    assert await publish_and_count() == 3
    final = await publish_assessment(role_client, admin, module["id"])
    assert final.json()["active_enrolments_affected"] == 3
    assert set(final.json()) >= {
        "status",
        "published_at",
        "questions",
        "active_enrolments_affected",
    }

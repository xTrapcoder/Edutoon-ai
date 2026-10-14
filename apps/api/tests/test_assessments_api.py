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
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from edutoon.api.schemas import assessments as schemas
from edutoon.core.errors import ConflictError, NotFoundError
from edutoon.domain.training_assessment import Answer
from edutoon.main import create_app
from edutoon.repositories import assessments as assessments_repo
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
    published_course,
    put_assessment,
    submit_attempt,
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
    assert (await put_assessment(role_client, admin, modules[0]["id"], first)).status_code == 200
    assert (await put_assessment(role_client, admin, modules[1]["id"], second)).status_code == 200
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
    saved = await put_assessment(
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


async def test_editing_the_assessment_after_an_attempt_changes_nothing_that_was_recorded(
    role_client, admin, employee, source, track, db_session, storage
):
    course, modules = await world(
        role_client, admin, employee, source, track, db_session, storage, modules=1, assessed=()
    )
    module = modules[0]
    await put_assessment(
        role_client, admin, module["id"], assessment_payload(questions=10, threshold=70)
    )
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

    # an administrator now rewrites the questions and lowers the threshold
    await put_assessment(
        role_client, admin, module["id"], assessment_payload(questions=2, threshold=10)
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
    await put_assessment(role_client, admin, module["id"], assessment_payload(questions=2))

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
        assert (await put_assessment(role_client, admin, module["id"])).status_code == 200
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

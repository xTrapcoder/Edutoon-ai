# ruff: noqa: E501 - long SQL strings and section rules
"""Migration 0021 (training assessments, attempts, answers) upgraded and downgraded for real, with
Alembic, on a scratch database (mirrors ``test_migration_0020_upgrade_downgrade.py``).

0021 is purely additive - six new tables - so what matters is that the upgrade -> downgrade ->
upgrade journey is clean, that a downgrade **with attempt data present** succeeds and removes only
the assessment tables, and that the finished schema enforces what Step 3 relies on: every foreign
key from the history to modules (and users, and assessments) is ``RESTRICT``, a submitted attempt
and its answers cannot be changed or deleted, a pass cannot disagree with the score, and an
answer key names one of its own question's options.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError

from tests.migration_helpers import add_owner, run_alembic, scratch_database, sql
from tests.test_migration_0020_upgrade_downgrade import _course, _module, _tables

ASSESSMENT_TABLES = {
    "assessments",
    "assessment_questions",
    "assessment_options",
    "assessment_answer_keys",
    "assessment_attempts",
    "assessment_attempt_answers",
}
TRAINING_TABLES = {"training_courses", "training_modules", "course_enrolments"}


@pytest.fixture
def scratch_url() -> Iterator[str]:
    with scratch_database() as url:
        yield url


@pytest.fixture
def head_url(scratch_url) -> str:
    run_alembic(scratch_url, "upgrade", "head", env={"ADMIN_BOOTSTRAP_EMAILS": ""})
    return scratch_url


# --- helpers: the smallest valid rows ----------------------------------------------------------


def _assessment(url: str, module: str, owner: Any, threshold: int = 70) -> str:
    [row] = sql(
        url,
        "INSERT INTO assessments (module_id, threshold_percent, created_by) "
        "VALUES (:m, :t, :o) RETURNING id",
        m=module,
        t=threshold,
        o=owner,
    )
    return str(row["id"])


def _question(url: str, assessment: str, position: int = 1) -> tuple[str, str, str]:
    """A question with two options and its key; returns (question, right_option, wrong_option)."""
    [q] = sql(
        url,
        "INSERT INTO assessment_questions (assessment_id, position, prompt) "
        "VALUES (:a, :p, 'Which?') RETURNING id",
        a=assessment,
        p=position,
    )
    options = []
    for place, label in ((1, "Right"), (2, "Wrong")):
        [o] = sql(
            url,
            "INSERT INTO assessment_options (question_id, position, label) "
            "VALUES (:q, :p, :l) RETURNING id",
            q=q["id"],
            p=place,
            l=label,
        )
        options.append(str(o["id"]))
    sql(
        url,
        "INSERT INTO assessment_answer_keys (question_id, correct_option_id) VALUES (:q, :o)",
        q=q["id"],
        o=options[0],
    )
    return str(q["id"]), options[0], options[1]


def _attempt(url: str, assessment: str, module: str, user: Any, **overrides: Any) -> str:
    values: dict[str, Any] = {
        "a": assessment,
        "m": module,
        "u": user,
        "n": 1,
        "t": 70,
        "c": 1,
        "q": 1,
        "p": True,
        "k": str(uuid4()),
    } | overrides
    [row] = sql(
        url,
        "INSERT INTO assessment_attempts (assessment_id, module_id, user_id, attempt_number, "
        "threshold_percent, correct_count, question_count, passed, idempotency_key) "
        "VALUES (:a, :m, :u, :n, :t, :c, :q, :p, :k) RETURNING id",
        **values,
    )
    return str(row["id"])


def _answer(
    url: str, attempt: str, question: str, right: str, chosen: str, position: int = 1
) -> None:
    sql(
        url,
        "INSERT INTO assessment_attempt_answers (attempt_id, position, question_id, prompt, "
        "options, correct_option_id, selected_option_id, is_correct) VALUES "
        "(:a, :p, :q, 'Which?', '[]'::jsonb, :r, :s, :ok)",
        a=attempt,
        p=position,
        q=question,
        r=right,
        s=chosen,
        ok=right == chosen,
    )


def _history(url: str) -> dict[str, Any]:
    """A course, a module, an assessment with one question, and one submitted attempt."""
    owner = add_owner(url)
    course = _course(url, owner)
    module = _module(url, course, owner)
    assessment = _assessment(url, module, owner)
    question, right, _ = _question(url, assessment)
    attempt = _attempt(url, assessment, module, owner)
    _answer(url, attempt, question, right, right)
    return {
        "owner": owner,
        "course": course,
        "module": module,
        "assessment": assessment,
        "question": question,
        "right": right,
        "attempt": attempt,
    }


def _count(url: str, table: str) -> int:
    return int(sql(url, f"SELECT count(*) AS n FROM {table}")[0]["n"])


# --- the journey -------------------------------------------------------------------------------


def test_upgrade_creates_the_tables_and_downgrade_removes_only_them(scratch_url):
    run_alembic(scratch_url, "upgrade", "0020", env={"ADMIN_BOOTSTRAP_EMAILS": ""})
    assert not ASSESSMENT_TABLES & _tables(scratch_url)

    run_alembic(scratch_url, "upgrade", "0021")
    assert _tables(scratch_url) >= ASSESSMENT_TABLES
    assert run_alembic(scratch_url, "current").count("0021") == 1

    run_alembic(scratch_url, "downgrade", "0020")
    assert not ASSESSMENT_TABLES & _tables(scratch_url)
    assert _tables(scratch_url) >= TRAINING_TABLES  # 0020 untouched
    assert run_alembic(scratch_url, "current").count("0020") == 1

    run_alembic(scratch_url, "upgrade", "head")
    assert _tables(scratch_url) >= ASSESSMENT_TABLES


def test_the_round_trip_leaves_the_training_rows_exactly_as_they_were(scratch_url):
    run_alembic(scratch_url, "upgrade", "0020", env={"ADMIN_BOOTSTRAP_EMAILS": ""})
    owner = add_owner(scratch_url)
    course = _course(scratch_url, owner)
    _module(scratch_url, course, owner)
    queries = {
        "users": "SELECT id, email, role, status FROM users ORDER BY id",
        "courses": "SELECT * FROM training_courses ORDER BY id",
        "modules": "SELECT * FROM training_modules ORDER BY id",
    }
    before = {name: sql(scratch_url, q) for name, q in queries.items()}

    run_alembic(scratch_url, "upgrade", "0021")
    run_alembic(scratch_url, "downgrade", "0020")
    run_alembic(scratch_url, "upgrade", "0021")

    assert {name: sql(scratch_url, q) for name, q in queries.items()} == before
    assert all(_count(scratch_url, table) == 0 for table in ASSESSMENT_TABLES)


def test_a_downgrade_with_attempt_data_present_succeeds_and_drops_only_that(scratch_url):
    """Submitted attempts are immutable and their foreign keys RESTRICT - and still a downgrade
    works, because dropping a table does not fire its row triggers. The training rows and the
    people stay."""
    run_alembic(scratch_url, "upgrade", "head", env={"ADMIN_BOOTSTRAP_EMAILS": ""})
    history = _history(scratch_url)
    assert _count(scratch_url, "assessment_attempts") == 1
    assert _count(scratch_url, "assessment_attempt_answers") == 1

    run_alembic(scratch_url, "downgrade", "0020")

    assert not ASSESSMENT_TABLES & _tables(scratch_url)
    assert _count(scratch_url, "training_modules") == 1
    assert _count(scratch_url, "users") == 1
    assert sql(scratch_url, "SELECT id FROM training_modules")[0]["id"] == _uuid(history["module"])

    run_alembic(scratch_url, "upgrade", "head")  # up again: the tables come back empty
    assert _tables(scratch_url) >= ASSESSMENT_TABLES
    assert _count(scratch_url, "assessment_attempts") == 0


def _uuid(value: str) -> Any:
    from uuid import UUID

    return UUID(value)


def test_a_full_downgrade_to_0019_and_back_with_attempt_data_present(scratch_url):
    run_alembic(scratch_url, "upgrade", "head", env={"ADMIN_BOOTSTRAP_EMAILS": ""})
    _history(scratch_url)

    run_alembic(scratch_url, "downgrade", "0019")
    run_alembic(scratch_url, "upgrade", "head")

    assert _tables(scratch_url) >= ASSESSMENT_TABLES | TRAINING_TABLES


def test_no_postgres_enum_types_are_created(head_url):
    assert sql(head_url, "SELECT typname FROM pg_type WHERE typtype = 'e'") == []


# --- foreign keys: the history is RESTRICT, the authoring rows cascade ----------------------------

RESTRICT = "r"
CASCADE = "c"


def _delete_rules(url: str, table: str) -> dict[str, str]:
    """``referenced table -> delete rule`` for every foreign key of ``table``."""
    rows = sql(
        url,
        "SELECT confrelid::regclass::text AS target, confdeltype::text AS rule FROM pg_constraint "
        "WHERE contype = 'f' AND conrelid = CAST(:t AS regclass)",
        t=table,
    )
    return {row["target"]: row["rule"] for row in rows}


def test_every_foreign_key_from_an_attempt_or_an_answer_is_restrict(head_url):
    """Migration 0018 made two keys CASCADE because that data was derived; attempts are the record,
    so these are RESTRICT on purpose (module deletion must be a 409, never a silent wipe)."""
    assert _delete_rules(head_url, "assessment_attempts") == {
        "training_modules": RESTRICT,
        "users": RESTRICT,
        "assessments": RESTRICT,
    }
    assert _delete_rules(head_url, "assessment_attempt_answers") == {
        "assessment_attempts": RESTRICT
    }


def test_the_authoring_rows_cascade_from_their_parents_only(head_url):
    assert _delete_rules(head_url, "assessments") == {
        "training_modules": CASCADE,
        "users": RESTRICT,
    }
    assert _delete_rules(head_url, "assessment_questions") == {"assessments": CASCADE}
    assert _delete_rules(head_url, "assessment_options") == {"assessment_questions": CASCADE}


def test_a_module_user_or_assessment_with_attempts_cannot_be_deleted(head_url):
    history = _history(head_url)
    for statement in (
        "DELETE FROM training_modules WHERE id = :m",
        "DELETE FROM assessments WHERE id = :a",
        "DELETE FROM users WHERE id = :u",
        "DELETE FROM training_courses WHERE id = :c",  # would cascade to the module
    ):
        with pytest.raises(IntegrityError):
            sql(
                head_url,
                statement,
                m=history["module"],
                a=history["assessment"],
                u=history["owner"],
                c=history["course"],
            )
    assert _count(head_url, "assessment_attempts") == 1


def test_deleting_a_never_attempted_module_takes_its_assessment_with_it(head_url):
    owner = add_owner(head_url)
    module = _module(head_url, _course(head_url, owner), owner)
    assessment = _assessment(head_url, module, owner)
    _question(head_url, assessment)

    sql(head_url, "DELETE FROM training_modules WHERE id = :m", m=module)

    for table in (
        "assessments",
        "assessment_questions",
        "assessment_options",
        "assessment_answer_keys",
    ):
        assert _count(head_url, table) == 0


# --- immutability --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE assessment_attempts SET passed = false, correct_count = 0 WHERE id = :a",
        "UPDATE assessment_attempts SET threshold_percent = 1 WHERE id = :a",
        "DELETE FROM assessment_attempts WHERE id = :a",
        "UPDATE assessment_attempt_answers SET prompt = 'x' WHERE attempt_id = :a",
        "DELETE FROM assessment_attempt_answers WHERE attempt_id = :a",
    ],
)
def test_a_submitted_attempt_and_its_answers_cannot_be_changed_or_deleted(head_url, statement):
    history = _history(head_url)

    with pytest.raises(DBAPIError) as caught:
        sql(head_url, statement, a=history["attempt"])

    assert "append-only" in str(caught.value)
    assert _count(head_url, "assessment_attempts") == 1
    assert _count(head_url, "assessment_attempt_answers") == 1


# --- the checks -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("correct", "total", "threshold", "passed", "valid"),
    [
        (7, 10, 70, True, True),
        (7, 10, 70, False, False),  # a pass that is recorded as a fail
        (6, 10, 70, True, False),  # a fail that is recorded as a pass
        (6, 10, 70, False, True),
        (2, 3, 70, False, True),
        (2, 3, 66, True, True),
        (2, 3, 67, True, False),
        (11, 10, 70, True, False),  # more correct than questions
        (-1, 10, 70, False, False),
        (0, 0, 70, False, False),  # an empty attempt
        (5, 10, 0, True, False),  # threshold out of range
        (5, 10, 101, False, False),
    ],
)
def test_the_stored_pass_must_agree_with_the_integer_score(
    head_url, correct, total, threshold, passed, valid
):
    owner = add_owner(head_url)
    module = _module(head_url, _course(head_url, owner), owner)
    assessment = _assessment(head_url, module, owner)
    args = {"c": correct, "q": total, "t": threshold, "p": passed}

    if valid:
        assert _attempt(head_url, assessment, module, owner, **args)
    else:
        with pytest.raises(IntegrityError):
            _attempt(head_url, assessment, module, owner, **args)


def test_an_answers_correctness_must_match_the_option_chosen(head_url):
    history = _history(head_url)
    second = _attempt(
        head_url, history["assessment"], history["module"], history["owner"], n=2, c=0, p=False
    )
    with pytest.raises(IntegrityError):
        sql(
            head_url,
            "INSERT INTO assessment_attempt_answers (attempt_id, position, question_id, prompt, "
            "options, correct_option_id, selected_option_id, is_correct) VALUES "
            "(:a, 1, :q, 'x', '[]'::jsonb, :r, :r, false)",
            a=second,
            q=history["question"],
            r=history["right"],
        )


def test_attempt_numbers_and_idempotency_keys_are_unique(head_url):
    history = _history(head_url)
    args = (head_url, history["assessment"], history["module"], history["owner"])
    with pytest.raises(IntegrityError):
        _attempt(*args, n=1, k="another-key")  # attempt number 1 already exists
    with pytest.raises(IntegrityError):
        sql(
            head_url,
            "INSERT INTO assessment_attempts (assessment_id, module_id, user_id, attempt_number, "
            "threshold_percent, correct_count, question_count, passed, idempotency_key) "
            "SELECT assessment_id, module_id, user_id, 2, 70, 1, 1, true, idempotency_key "
            "FROM assessment_attempts WHERE id = :a",
            a=history["attempt"],
        )  # the same user and Idempotency-Key a second time
    assert _attempt(*args, n=2, k="a-new-key")


def test_an_assessment_is_one_per_module_with_a_threshold_between_1_and_100(head_url):
    owner = add_owner(head_url)
    module = _module(head_url, _course(head_url, owner), owner)
    _assessment(head_url, module, owner)
    with pytest.raises(IntegrityError):
        _assessment(head_url, module, owner)
    other = _module(head_url, _course(head_url, owner), owner)
    for bad in (0, 101, -5):
        with pytest.raises(IntegrityError):
            _assessment(head_url, other, owner, threshold=bad)


def test_an_answer_key_names_exactly_one_option_of_its_own_question(head_url):
    owner = add_owner(head_url)
    module = _module(head_url, _course(head_url, owner), owner)
    assessment = _assessment(head_url, module, owner)
    question, right, wrong = _question(head_url, assessment, position=1)
    other_question, other_right, _ = _question(head_url, assessment, position=2)

    with pytest.raises(IntegrityError):  # a second key for the same question
        sql(
            head_url,
            "INSERT INTO assessment_answer_keys (question_id, correct_option_id) VALUES (:q, :o)",
            q=question,
            o=wrong,
        )
    with pytest.raises(IntegrityError):  # a key that points at another question's option
        sql(
            head_url,
            "UPDATE assessment_answer_keys SET correct_option_id = :o WHERE question_id = :q",
            q=question,
            o=other_right,
        )
    assert other_question and right


def test_question_and_option_positions_are_unique_and_text_is_not_blank(head_url):
    owner = add_owner(head_url)
    module = _module(head_url, _course(head_url, owner), owner)
    assessment = _assessment(head_url, module, owner)
    question, _, _ = _question(head_url, assessment)
    with pytest.raises(IntegrityError):
        _question(head_url, assessment, position=1)
    with pytest.raises(IntegrityError):
        sql(
            head_url,
            "INSERT INTO assessment_options (question_id, position, label) VALUES (:q, 1, 'dup')",
            q=question,
        )
    with pytest.raises(IntegrityError):
        sql(
            head_url,
            "INSERT INTO assessment_options (question_id, position, label) VALUES (:q, 9, '  ')",
            q=question,
        )
    with pytest.raises(IntegrityError):
        sql(
            head_url,
            "INSERT INTO assessment_questions (assessment_id, position, prompt) VALUES (:a, 5, ' ')",
            a=assessment,
        )

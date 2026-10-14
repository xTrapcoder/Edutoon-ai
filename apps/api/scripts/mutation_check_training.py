"""Targeted mutation checks for the training enrolment, access and route-policy logic (Step 2, Piece 3).

Each mutation breaks the implementation in one specific, plausible way and runs the tests that should
notice. A mutation is

* **killed** if at least one test *fails an assertion* (``FAILED``);
* **INVALID** if the run errors without a failing assertion (the mutant broke an import, say) - that
  proves nothing about the tests, so it is reported and fails the script, never counted as a kill;
* a **survivor** if every test still passes - unless the mutation is declared ``equivalent`` (the
  changed code behaves identically for every legal input, with the reason written next to it), in
  which case it is reported as EQUIVALENT and is not a failure.

What it does *around* each mutation:

* proves the mutant was applied (the file changed and the new text is present);
* restores the original **bytes** afterwards whatever happens (``finally``, SIGINT/SIGTERM handlers)
  and verifies the restored file is byte-identical to the backup it kept;
* leaves the development database alone: the tests that commit run on a scratch database
  (``tests/conftest.py::committed_database_url``); the script checks the development
  ``audit_logs`` count is unchanged at the end;
* removes only the stored videos this mutant's run created (keys under ``training/modules/`` that did
  not exist before it started) - never anything else.

    uv run python scripts/mutation_check_training.py                # baseline, then every mutation
    uv run python scripts/mutation_check_training.py enrol          # names or areas containing "enrol"

Needs `make up` (Postgres, Redis, MinIO). Do not edit the mutated source files or the tests while it
runs. Exit status is non-zero for any survivor that is not declared equivalent, any INVALID run, a
baseline failure, a restore mismatch or a change in the development ``audit_logs`` count.
"""

# ruff: noqa: E501 - mutation source strings are quoted verbatim and read best on one line.
from __future__ import annotations

import asyncio
import hashlib
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "edutoon"
sys.path.insert(0, str(ROOT / "src"))


@dataclass(frozen=True)
class Mutation:
    name: str
    area: str
    file: str
    old: str
    new: str
    tests: tuple[str, ...]
    equivalent: str | None = None
    """If set: why this mutant cannot be told from the original. It is then expected to survive."""


DOMAIN = "domain/training.py"
ACCESS_RULE = "domain/training_access.py"
ENROLMENTS = "services/training_enrolments.py"
ACCESS = "services/training_access.py"
REPO = "repositories/training.py"
POLICIES = "api/policies.py"
ADMIN_ROUTER = "api/routers/admin_training.py"
EMPLOYEE_ROUTER = "api/routers/training.py"
VIDEO = "api/video_response.py"
ERRORS = "core/errors.py"

T_DOMAIN = "tests/test_domain_training_enrolment.py"
T_RULE = "tests/test_domain_training_access.py"
T_SEAM = "tests/test_training_access_seam.py"
T_ENROL = "tests/test_training_enrolments_api.py"
T_RACE = "tests/test_training_enrolments_committed.py"
T_EMPLOYEE = "tests/test_employee_training_api.py"
T_SECURITY = "tests/test_training_route_security.py"
T_ROUTES = "tests/test_route_policies.py"
T_ADMIN = "tests/test_admin_training_api.py"

ALL_TESTS = (
    T_DOMAIN,
    T_RULE,
    T_SEAM,
    T_ENROL,
    T_RACE,
    T_EMPLOYEE,
    T_SECURITY,
    T_ROUTES,
    T_ADMIN,
)

MUTATIONS: tuple[Mutation, ...] = (
    # --- pure enrolment rules ---------------------------------------------------------------
    Mutation(
        "can_enrol: anyone but an employee",
        "enrol rules",
        DOMAIN,
        "    return role == ENROLLABLE_ROLE\n",
        "    return role != ENROLLABLE_ROLE\n",
        (T_DOMAIN, T_ENROL),
    ),
    Mutation(
        "decide_enrolment: no row means reactivate",
        "enrol rules",
        DOMAIN,
        '    if existing_status is None:\n        return "create"\n',
        '    if existing_status is None:\n        return "reactivate"\n',
        (T_DOMAIN, T_ENROL),
    ),
    Mutation(
        "decide_enrolment: an active row is reactivated, a withdrawn one refused",
        "enrol rules",
        DOMAIN,
        '        return "reactivate"\n    return "already_active"\n',
        '        return "already_active"\n    return "reactivate"\n',
        (T_DOMAIN, T_ENROL),
    ),
    Mutation(
        "decide_withdrawal: a missing enrolment is 'already withdrawn'",
        "enrol rules",
        DOMAIN,
        '    if existing_status is None:\n        return "not_found"\n',
        '    if existing_status is None:\n        return "already_withdrawn"\n',
        (T_DOMAIN, T_ENROL),
    ),
    Mutation(
        "decide_withdrawal: only a withdrawn row can be withdrawn",
        "enrol rules",
        DOMAIN,
        '    if existing_status == ENROLMENT_ACTIVE:\n        return "withdraw"\n',
        '    if existing_status == ENROLMENT_WITHDRAWN:\n        return "withdraw"\n',
        (T_DOMAIN, T_ENROL),
    ),
    # --- enrol / withdraw service -----------------------------------------------------------
    Mutation(
        "enrol: the role check is gone (an administrator can be enrolled)",
        "enrol service",
        ENROLMENTS,
        "    if not can_enrol(user.role):\n        raise UnprocessableError(NOT_AN_EMPLOYEE)\n",
        "",
        (T_ENROL,),
    ),
    Mutation(
        "enrol: an archived course accepts enrolments",
        "enrol service",
        ENROLMENTS,
        "    if course.status == COURSE_ARCHIVED:\n        raise ConflictError(COURSE_ARCHIVED_FOR_ENROLMENT)\n",
        "",
        (T_ENROL,),
    ),
    Mutation(
        "enrol: an unknown user is not checked",
        "enrol service",
        ENROLMENTS,
        "    if user is None:\n        raise NotFoundError(USER_NOT_FOUND)\n",
        "",
        (T_ENROL,),
    ),
    Mutation(
        "enrol: losing the insert race is not noticed (two 201s)",
        "enrol race",
        ENROLMENTS,
        "        if created is None:\n            # Lost a race: another request enrolled this person between our look and our insert.\n            current = await _existing(session, course_id, user_id)\n            action = decide_enrolment(current.status)\n",
        "",
        (T_RACE,),
    ),
    Mutation(
        "enrol: re-enrolling leaves the row withdrawn",
        "enrol service",
        ENROLMENTS,
        "session, current.id, status=ENROLMENT_ACTIVE, assigned_by=actor_id",
        "session, current.id, status=ENROLMENT_WITHDRAWN, assigned_by=actor_id",
        (T_ENROL, T_EMPLOYEE),
    ),
    Mutation(
        "enrol: re-enrolling forgets who re-enrolled",
        "enrol service",
        ENROLMENTS,
        "session, current.id, status=ENROLMENT_ACTIVE, assigned_by=actor_id",
        "session, current.id, status=ENROLMENT_ACTIVE",
        (T_ENROL,),
    ),
    Mutation(
        "enrol: an already active enrolment is answered, not refused",
        "enrol service",
        ENROLMENTS,
        '    elif action == "already_active":\n        raise ConflictError(ALREADY_ENROLLED)\n',
        '    elif action == "already_active":\n        pass\n',
        (T_ENROL, T_RACE),
    ),
    Mutation(
        "enrol: creation writes the wrong audit action",
        "enrol service",
        ENROLMENTS,
        'action="training.enrolment.created"',
        'action="training.enrolment.updated"',
        (T_ENROL, T_RACE),
    ),
    Mutation(
        "enrol: re-enrolment forgets the previous assigner in the audit entry",
        "enrol service",
        ENROLMENTS,
        "            previous_assigned_by=str(current.assigned_by),\n",
        "",
        (T_ENROL,),
    ),
    Mutation(
        "withdraw: the row is set active instead of withdrawn",
        "enrol service",
        ENROLMENTS,
        "set_enrolment_status(session, current.id, status=ENROLMENT_WITHDRAWN)",
        "set_enrolment_status(session, current.id, status=ENROLMENT_ACTIVE)",
        (T_ENROL, T_EMPLOYEE, T_RACE),
    ),
    Mutation(
        "withdraw: an already withdrawn enrolment is withdrawn again",
        "enrol service",
        ENROLMENTS,
        '    if action == "already_withdrawn":\n        raise ConflictError(ALREADY_WITHDRAWN)\n',
        "",
        (T_ENROL,),
    ),
    Mutation(
        "withdraw: an unknown course is not checked",
        "enrol service",
        ENROLMENTS,
        "    if await training_repo.get_course(session, course_id) is None:\n        raise NotFoundError(COURSE_NOT_FOUND)\n    current = await training_repo.get_enrolment(\n        session, course_id=course_id, user_id=user_id, for_update=True\n    )\n    action = decide_withdrawal",
        "    current = await training_repo.get_enrolment(\n        session, course_id=course_id, user_id=user_id, for_update=True\n    )\n    action = decide_withdrawal",
        (T_ENROL,),
    ),
    Mutation(
        "list a course's enrolments: an unknown course is an empty list",
        "enrol service",
        ENROLMENTS,
        "    if await training_repo.get_course(session, course_id) is None:\n        raise NotFoundError(COURSE_NOT_FOUND)\n    return await training_repo.list_enrolments_for_course(",
        "    return await training_repo.list_enrolments_for_course(",
        (T_ENROL,),
    ),
    Mutation(
        "list a user's enrolments: an unknown user is an empty list",
        "enrol service",
        ENROLMENTS,
        "    if await users_repo.get_by_id(session, user_id) is None:\n        raise NotFoundError(USER_NOT_FOUND)\n    return await training_repo.list_enrolments_for_user(",
        "    return await training_repo.list_enrolments_for_user(",
        (T_ENROL,),
    ),
    Mutation(
        "enrol: the shared course lock is not taken (race with delete)",
        "enrol race",
        ENROLMENTS,
        "get_course(session, course_id, for_share=True)",
        "get_course(session, course_id)",
        (T_RACE,),
    ),
    # --- repository ---------------------------------------------------------------------------
    Mutation(
        "insert: a plain INSERT, no ON CONFLICT",
        "enrol repository",
        REPO,
        "            .on_conflict_do_nothing(constraint=ENROLMENT_CONSTRAINT)\n",
        "",
        (T_RACE, T_ENROL),
    ),
    Mutation(
        "insert: a new enrolment starts withdrawn",
        "enrol repository",
        REPO,
        "                status=ENROLMENT_ACTIVE,\n                assigned_by=assigned_by,\n",
        "                status=ENROLMENT_WITHDRAWN,\n                assigned_by=assigned_by,\n",
        (T_ENROL, T_EMPLOYEE),
    ),
    Mutation(
        "employee listing: a withdrawn enrolment still lists the course",
        "access repository",
        REPO,
        "            e.c.status == ENROLMENT_ACTIVE,\n            training_courses.c.status == COURSE_PUBLISHED,\n",
        "            training_courses.c.status == COURSE_PUBLISHED,\n",
        (T_EMPLOYEE,),
    ),
    Mutation(
        "employee listing: an unpublished course is listed",
        "access repository",
        REPO,
        "            e.c.status == ENROLMENT_ACTIVE,\n            training_courses.c.status == COURSE_PUBLISHED,\n",
        "            e.c.status == ENROLMENT_ACTIVE,\n",
        (T_EMPLOYEE,),
    ),
    Mutation(
        "employee listing: another user's enrolments are listed",
        "access repository",
        REPO,
        "            e.c.user_id == user_id,\n            e.c.status == ENROLMENT_ACTIVE,\n",
        "            e.c.status == ENROLMENT_ACTIVE,\n",
        (T_EMPLOYEE, T_SECURITY),
    ),
    Mutation(
        "gap helper: the occupied-position precondition is not checked",
        "gap helper (review fix C)",
        REPO,
        "    if occupied:\n",
        "    if False:\n",
        (T_ENROL,),
    ),
    Mutation(
        "gap helper: the precondition looks past the position instead of at it",
        "gap helper (review fix C)",
        REPO,
        "training_modules.c.position == after)\n        )\n    ).scalar_one()\n    if occupied:",
        "training_modules.c.position > after)\n        )\n    ).scalar_one()\n    if occupied:",
        (T_ENROL,),
    ),
    Mutation(
        "gap helper: '>' becomes '>=' (with the precondition they select the same rows)",
        "gap helper (review fix C)",
        REPO,
        "training_modules.c.position > after))\n        .values(position=training_modules.c.position - 1)",
        "training_modules.c.position >= after))\n        .values(position=training_modules.c.position - 1)",
        (T_ENROL, T_ADMIN),
        equivalent="the precondition guarantees no module is at position `after`, so `>` and `>=` match the same rows; if it did not hold the call raises before the UPDATE",
    ),
    # --- the access rule and its seam ----------------------------------------------------------
    Mutation(
        "rule: the target counts as its own predecessor",
        "access rule",
        ACCESS_RULE,
        "m.status == MODULE_PUBLISHED and m.position < target.position",
        "m.status == MODULE_PUBLISHED and m.position <= target.position",
        (T_RULE, T_EMPLOYEE),
    ),
    Mutation(
        "seam: everything is considered passed",
        "access seam",
        ACCESS,
        "    return frozenset()\n",
        "    return frozenset(module_ids)\n",
        (T_SEAM, T_EMPLOYEE),
    ),
    Mutation(
        "access: a locked module is reported as not found",
        "access service",
        ACCESS,
        "    if decision.access == ACCESS_LOCKED:\n        blocking = _blocking(siblings, decision)\n        if blocking is not None:\n            raise ModuleLockedError(",
        "    if False and decision.access == ACCESS_LOCKED:\n        blocking = _blocking(siblings, decision)\n        if blocking is not None:\n            raise ModuleLockedError(",
        (T_EMPLOYEE,),
    ),
    Mutation(
        "listing: locked modules are not listed",
        "access service",
        ACCESS,
        "            elif decision.access == ACCESS_LOCKED:\n",
        "            elif False:\n",
        (T_EMPLOYEE,),
    ),
    Mutation(
        "access: the blocking module's position is off by one",
        "access service",
        ACCESS,
        '"blocking_module_position": blocking.position,',
        '"blocking_module_position": blocking.position + 1,',
        (T_EMPLOYEE,),
    ),
    Mutation(
        "access: the withdrawn enrolment is not looked at (enrolment status ignored)",
        "access service",
        ACCESS,
        "        enrolment_status=enrolment.status if enrolment else None,\n",
        "        enrolment_status=ENROLMENT_ACTIVE if enrolment else None,\n",
        (T_EMPLOYEE,),
    ),
    # --- policies, routes, serving, errors ----------------------------------------------------
    Mutation(
        "employee stream: a non-employee gets a 403, not the module 404",
        "policies",
        POLICIES,
        "    if user.role != ROLE_EMPLOYEE:\n        raise NotFoundError(MODULE_NOT_FOUND)\n",
        '    if user.role != ROLE_EMPLOYEE:\n        raise ForbiddenError("This action is for employee accounts.")\n',
        (T_EMPLOYEE, T_SECURITY, T_ROUTES),
    ),
    Mutation(
        "nested GET /courses/{id}/modules uses the plain admin 403 policy",
        "route policy (review fix A)",
        ADMIN_ROUTER,
        "async def list_modules(course_id: UUID, admin: CourseAdmin, session: Session)",
        "async def list_modules(course_id: UUID, admin: Admin, session: Session)",
        (T_ROUTES, T_SECURITY),
    ),
    Mutation(
        "nested GET /courses/{id}/enrolments uses the plain admin 403 policy",
        "route policy (review fix A)",
        ADMIN_ROUTER,
        "async def list_course_enrolments(\n    course_id: UUID,\n    admin: CourseAdmin,",
        "async def list_course_enrolments(\n    course_id: UUID,\n    admin: Admin,",
        (T_ROUTES, T_SECURITY),
    ),
    Mutation(
        "employee stream: the access check is skipped",
        "routes",
        EMPLOYEE_ROUTER,
        "    await access_service.require_module_access(session, employee, module_id)\n",
        "",
        (T_EMPLOYEE,),
    ),
    Mutation(
        "employee stream: the file-missing 404 keeps the admin's wording",
        "routes",
        EMPLOYEE_ROUTER,
        "        not_found_message=MODULE_NOT_FOUND,\n",
        "",
        (T_EMPLOYEE,),
    ),
    Mutation(
        "serving: a range past the end is a 404, not a 416",
        "serving",
        VIDEO,
        "status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE",
        "status_code=status.HTTP_404_NOT_FOUND",
        (T_ADMIN,),
    ),
    Mutation(
        "serving: Content-Range is not sent",
        "serving",
        VIDEO,
        '        headers["Content-Range"] = stream.content_range\n',
        "        pass\n",
        (T_ADMIN, T_EMPLOYEE),
    ),
    Mutation(
        "serving: ?download=true no longer downloads",
        "serving",
        VIDEO,
        "    if download:\n",
        "    if False:\n",
        (T_ADMIN,),
    ),
    Mutation(
        "errors: the machine-readable details are dropped from the body",
        "errors",
        ERRORS,
        "content=_error_body(exc.code, exc.message, exc.details),",
        "content=_error_body(exc.code, exc.message),",
        (T_EMPLOYEE,),
    ),
    Mutation(
        "errors: a locked module has the generic forbidden code",
        "errors",
        ERRORS,
        'code = "module_locked"',
        'code = "forbidden"',
        (T_EMPLOYEE,),
    ),
)


# --- machinery ---------------------------------------------------------------------------------

BACKUPS: dict[Path, bytes] = {}
SOURCE_DIRTY: set[Path] = set()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def restore_all(*_args: object) -> None:
    for path, original in BACKUPS.items():
        if path.read_bytes() != original:
            path.write_bytes(original)


def _on_signal(signum: int, _frame: object) -> None:
    restore_all()
    raise SystemExit(130)


def run_tests(tests: tuple[str, ...]) -> tuple[str, str, list[str]]:
    """``(verdict, summary line, failing test ids)``; verdict is passed / failed / errored."""
    command = [
        "uv",
        "run",
        "--frozen",
        "pytest",
        "-q",
        "-x",
        "-p",
        "no:cacheprovider",
        "--tb=no",
        "-rfE",
        *tests,
    ]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    failing = [line for line in lines if line.startswith(("FAILED ", "ERROR "))]
    summary = (
        lines[-1]
        if lines
        else result.stderr.strip().splitlines()[-1:] and result.stderr.strip().splitlines()[-1]
    )
    if result.returncode == 0:
        return "passed", str(summary), []
    if any(line.startswith("FAILED ") for line in failing):
        return "failed", str(summary), failing
    return "errored", str(summary), failing


async def _dev_state() -> tuple[int, set[str]]:
    """Development ``audit_logs`` row count, and the stored training videos that exist now."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from edutoon.core.config import get_settings
    from edutoon.providers.storage import get_storage_client

    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_DIRECT_URL)
    try:
        async with engine.connect() as connection:
            audit = (await connection.execute(text("SELECT count(*) FROM audit_logs"))).scalar_one()
    finally:
        await engine.dispose()
    storage = get_storage_client(
        endpoint_url=settings.STORAGE_ENDPOINT_URL,
        access_key_id=settings.STORAGE_ACCESS_KEY_ID,
        secret_access_key=settings.STORAGE_SECRET_ACCESS_KEY,
    )
    keys: set[str] = set()
    paginator = storage._client.get_paginator("list_objects_v2")  # noqa: SLF001
    for page in paginator.paginate(Bucket=settings.BUCKET_OUTPUTS, Prefix="training/modules/"):
        keys.update(item["Key"] for item in page.get("Contents", []))
    return int(audit), keys


def _purge_new_videos(before: set[str]) -> int:
    """Delete the training videos created since ``before`` (and only those)."""
    from edutoon.core.config import get_settings
    from edutoon.providers.storage import get_storage_client

    settings = get_settings()
    storage = get_storage_client(
        endpoint_url=settings.STORAGE_ENDPOINT_URL,
        access_key_id=settings.STORAGE_ACCESS_KEY_ID,
        secret_access_key=settings.STORAGE_SECRET_ACCESS_KEY,
    )
    removed = 0
    paginator = storage._client.get_paginator("list_objects_v2")  # noqa: SLF001
    for page in paginator.paginate(Bucket=settings.BUCKET_OUTPUTS, Prefix="training/modules/"):
        for item in page.get("Contents", []):
            if item["Key"] not in before:
                storage._client.delete_object(Bucket=settings.BUCKET_OUTPUTS, Key=item["Key"])  # noqa: SLF001
                removed += 1
    return removed


def main(selected: str | None) -> int:
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    for mutation in MUTATIONS:
        path = SRC / mutation.file
        BACKUPS.setdefault(path, path.read_bytes())

    audit_before, videos_before = asyncio.run(_dev_state())
    print(
        f"development audit_logs before: {audit_before}; training videos stored: {len(videos_before)}"
    )

    verdict, summary, _ = run_tests(ALL_TESTS)
    print(f"baseline (no mutation): {verdict}  {summary}")
    if verdict != "passed":
        print("The unmutated tests do not pass, so no mutation result would mean anything.")
        return 2

    results: list[tuple[str, str, str]] = []
    problems: list[str] = []
    purged = 0
    for mutation in MUTATIONS:
        if selected and selected not in mutation.name and selected not in mutation.area:
            continue
        path = SRC / mutation.file
        original = BACKUPS[path]
        text = original.decode("utf-8")
        newline = "\r\n" if "\r\n" in text else "\n"
        normalised = text.replace("\r\n", "\n")
        if normalised.count(mutation.old) != 1:
            print(
                f"?? cannot apply exactly once ({normalised.count(mutation.old)}x): {mutation.name}"
            )
            results.append((mutation.name, "NOT APPLIED", ""))
            problems.append(f"{mutation.name} (NOT APPLIED)")
            continue
        mutated = normalised.replace(mutation.old, mutation.new, 1).replace("\n", newline)
        started = time.perf_counter()
        before_videos = asyncio.run(_dev_state())[1]
        try:
            path.write_bytes(mutated.encode("utf-8"))
            if path.read_bytes() == original or mutation.old in path.read_bytes().decode().replace(
                "\r\n", "\n"
            ):
                raise RuntimeError(f"mutation was not applied: {mutation.name}")
            verdict, summary, failing = run_tests(mutation.tests)
        finally:
            path.write_bytes(original)
        restored = path.read_bytes() == original
        purged += _purge_new_videos(before_videos)
        seconds = time.perf_counter() - started
        if not restored:
            problems.append(f"{mutation.name} (RESTORE MISMATCH)")
        first = re.sub(r"^(FAILED|ERROR) ", "", failing[0])[:110] if failing else ""
        if verdict == "failed":
            outcome = "killed" if not mutation.equivalent else "KILLED (declared equivalent!)"
            if mutation.equivalent:
                problems.append(f"{mutation.name} (declared equivalent but killed)")
        elif verdict == "passed" and mutation.equivalent:
            outcome = "EQUIVALENT"
        elif verdict == "passed":
            outcome = "SURVIVED"
            problems.append(f"{mutation.name} (SURVIVED)")
        else:
            outcome = "INVALID"
            problems.append(f"{mutation.name} (INVALID: errored without a failing assertion)")
        results.append((mutation.name, outcome, first))
        print(
            f"{outcome:9} [{mutation.area}] {mutation.name}  ({seconds:.0f}s)  {first or summary}"
        )
        sys.stdout.flush()

    restore_all()
    for path, original in BACKUPS.items():
        if sha(path.read_bytes()) != sha(original):
            problems.append(f"{path} is not byte-identical to its backup")
    audit_after, videos_after = asyncio.run(_dev_state())
    if audit_after != audit_before:
        problems.append(f"development audit_logs changed: {audit_before} -> {audit_after}")
    if videos_after != videos_before:
        problems.append(
            f"training videos differ from before the run: {len(videos_after - videos_before)} extra"
        )

    killed = sum(1 for _, outcome, _ in results if outcome == "killed")
    equivalent = sum(1 for _, outcome, _ in results if outcome == "EQUIVALENT")
    print(
        f"\n{len(results)} mutations: {killed} killed, {equivalent} equivalent, "
        f"{len(problems)} problems; stray videos removed: {purged}; "
        f"development audit_logs {audit_before} -> {audit_after}"
    )
    for problem in problems:
        print(f"  PROBLEM: {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else None))

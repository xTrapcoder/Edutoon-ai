# edutoon-api

FastAPI service for EduToon AI.

```bash
uv sync
uv run uvicorn edutoon.main:app --reload --port 8000
```

Requires the environment variables listed in the repository root `.env.example`.

## Running the project's `make` targets without `make`

The root `Makefile` is a thin wrapper; every target is one or two commands. **GNU Make is not
installed on the Windows development machine this was built on**, so `make migrate`, `make lint` and
the rest cannot be run there as documented. Either install GNU Make (and run from a shell that has
`uv`, `pnpm` and `docker` on its path), or run the equivalent commands from the repository root:

| `make ...` | Equivalent |
|---|---|
| `up` | `docker compose up -d --wait` |
| `migrate` | `cd apps/api && uv run alembic upgrade head` |
| `migrate-down` | `cd apps/api && uv run alembic downgrade -1` |
| `test` | `cd apps/api && uv run pytest` and `pnpm --filter @edutoon/web test` |
| `lint` | `pnpm lint`, then `cd apps/api && uv run ruff check . && uv run mypy src` |
| `format` | `pnpm format`, then `cd apps/api && uv run ruff format .` |
| `contracts` | `cd apps/api && uv run python scripts/export_openapi.py`, then `pnpm --filter @edutoon/contracts generate` |
| `contracts-check` | the `--check` form of the first command, `pnpm --filter @edutoon/contracts generate`, then `git diff --exit-code -- packages/contracts` |
| `worker` | `cd apps/api && uv run python -m edutoon.worker` |
| `dev` | the API (`uv run uvicorn edutoon.main:app --reload --port 8000` in `apps/api`), the web app (`pnpm dev` in `apps/web`) and the worker, each in its own terminal |

The `pnpm` commands need Node 22 or later with pnpm available (Corepack); the Python ones only need `uv`.

## API contract for the front end

`scripts/export_openapi.py` writes this API's OpenAPI document to
`packages/contracts/openapi.json`, and the web app generates its TypeScript types from that
file (`make contracts`; `make contracts-check` fails if it is stale). It only builds the app
object - it needs no database, Redis, Clerk or provider keys. Note that the document still
declares FastAPI's default `{"detail": ...}` body for 422s, while the API actually answers with
`{"error": {"code", "message", "details"?}}`; clients should read errors from the latter.

## Mutating routes

Every `POST` / `PATCH` / `DELETE` requires an `Idempotency-Key` header of 1 to 255
characters (missing, empty or longer is `422`) and dedupes on it: a repeat of the same
(caller, method, path, key) replays the first response instead of running again. Each
handler commits its transaction *before* its result is cached or returned, so a `2xx` -
and a replay of one - always means the change is stored, and a commit that fails leaves
nothing cached. `tests/test_idempotent_routes_contract.py` fails for any mutating route
that loses either guarantee.

## Roles, invitations and sign-in

Step 1 of the training platform (`docs/training-platform-design.md`; migration **0019**).

**Roles.** `users.role` is `admin` or `employee`, NOT NULL with no default. The database is the
only authority: a role is never read from a token claim, a header or a request body (a `role` claim
in a Clerk token is ignored, and `tests/test_provisioning_and_suspension.py` proves it).

**Who can use the API.** Nobody becomes a user by signing in. `get_current_user`
(`api/dependencies.py`) verifies the token, then `services.auth.resolve_user`:

1. an identity already linked to a user is returned as it is;
2. a new identity becomes a user **only if its email is verified** (`email_verified: true` in the
   token) and either a *pending, unexpired invitation* names that address - the user gets the
   invitation's role - or the address is on `ADMIN_BOOTSTRAP_EMAILS` **and no active administrator
   exists** (the first administrator; the list is spent once one does);
3. anything else is `403 not_invited` and **writes nothing**.

Then a user whose `status` is not `active` is `403 account_inactive` - on every request, so a
suspension takes effect at once (`PATCH /v1/admin/users/{id}` with `{"status": "suspended"}`).
Unauthenticated is still `401`. A 403 means a capability is missing (role, invitation, suspension);
a project, or any resource, the caller does not own is still `404` (rule 9).

**Every route declares a role policy** (`api/policies.py`): `require_admin`,
`require_admin_for_project`, `require_employee`, `require_authenticated`, or `public_route`
(`/health` only). All existing project-generation routes are administrator-only, and an employee is
answered like this:

* a route that **names a project** (anything under `/v1/projects/{project_id}`) is
  `require_admin_for_project`: an employee gets **`404 not_found`**, with the same body an
  administrator gets for a project that does not exist, whether or not the project exists - so
  nothing about which projects exist can be learned, and nothing the employee sends is acted on;
* a route that names **no** project (`GET` / `POST /v1/projects`, everything under `/v1/admin`) is
  `require_admin`: an employee gets **`403 forbidden`**.

`tests/test_route_policies.py` walks every route of the real application and fails if one declares
none or several, pins which policy each route has (a `{project_id}` in the path must mean
`require_admin_for_project`), then calls every administrator route as an employee, a suspended
administrator, an uninvited person and an anonymous caller, and compares an employee's responses for
an existing project and a missing one. Adding a route without a policy fails the suite.

**Invitations** (admin only, `Idempotency-Key` required): `POST /v1/admin/invitations`
(`{email, role?, display_name?}`; role defaults to `employee`), `GET /v1/admin/invitations`,
`POST /v1/admin/invitations/{id}/revoke`. Expiry is `INVITATION_TTL_DAYS` (default 7). If
`CLERK_SECRET_KEY` is set the API asks Clerk to email the invitation (stdlib HTTP, no new
dependency; only the address is sent, never a role) and the response says `delivery: "sent"`;
otherwise `"not_configured"` (pass the sign-up link on yourself) or `"failed"`. The invitation row is
the authority either way. **The Clerk call is not verified against a real Clerk instance.**

**Users** (admin only): `GET /v1/admin/users`, `PATCH /v1/admin/users/{id}` (`role`, `status`:
`active` | `suspended`). The last active administrator can be neither demoted nor suspended (409).
`GET /v1/auth/me` returns `{id, email, display_name, role}` for any active user.

**Migration 0019 backfill is least-privilege.** Every existing user becomes an `employee`; only
addresses on `ADMIN_BOOTSTRAP_EMAILS` *at migration time* become `admin`. If that list is empty the
migration says so and nobody is promoted. To promote someone afterwards, deliberately, on the
server: `uv run python scripts/grant_admin.py --email someone@company.example` (audited as a
`system` action). Downgrading drops the role column and the invitations table and loses the roles.

**Dev QA.** `scripts/dev_qa_api.py` accepts two stand-in tokens, `ui-qa-token` (administrator) and
`ui-qa-employee-token` (employee), and creates those two local users if they are missing.

**Tests written before roles existed.** `tests/conftest.py` has an autouse fixture that, for any
test not marked `@pytest.mark.real_auth`, makes `users_repo.create` default to `admin` and provisions
an unknown Clerk identity as an administrator. It exists only in tests; production has no default
role. A test of roles, invitations or suspension carries `real_auth` and must give every user an
explicit role. The suspended-account check is never shimmed.

## Training courses, modules and enrolments

Step 2 of the training platform (`docs/training-platform-design.md`; schema from migration
**0020**). Pieces 2 and 3 (below): the administrator's routes are under `/v1/admin/training`; the
employee's two routes are under `/v1/training` ("Enrolments and employee access (Piece 3)"). Every
mutating route needs an `Idempotency-Key`, request bodies reject unknown fields (422), durations are
integer milliseconds.

| Route | Does |
|---|---|
| `POST` / `GET /courses` | create (`{title, description?}`) / list (`?status=`, paginated) |
| `GET` / `PATCH /courses/{course_id}` | read / edit title and description (archived: 409) |
| `POST /courses/{course_id}/publish` | draft -> published; needs at least one published module (else 409) |
| `POST /courses/{course_id}/archive` | hide and freeze a course, keeping everything |
| `DELETE /courses/{course_id}` | 204; **409** once anyone was enrolled (any status, a withdrawn enrolment included) or, from Step 3, has other learner history (a RESTRICT foreign key): the message says to **archive it instead** |
| `GET /courses/{course_id}/modules` | every module, published and unpublished, in order |
| `POST /courses/{course_id}/modules` | **publish a render as a new module**, last in order (below) |
| `PUT /courses/{course_id}/modules/order` | `{module_ids}`: every module once, in the new order |
| `GET /modules/{module_id}` | one module (snapshot description, never the storage key) |
| `POST /modules/{module_id}/republish` | replace the module's video with a fresh snapshot (also re-publishes an unpublished one) |
| `POST /modules/{module_id}/unpublish` | employees lose it, its file is removed, its place and description stay |
| `DELETE /modules/{module_id}` | 204; closes the gap in the order; **409** if a RESTRICT foreign key (Step 3 history) refuses |
| `GET /modules/{module_id}/video` | the administrator's preview: streams the module's **own** snapshot, with `Range` and `?download=true` |

**Publish by snapshot.** `POST .../modules` takes `{project_id, script_id, orientation, quality,
title?, description?}` - the identifiers the render API already uses, so the render API is unchanged.
The project must be the caller's (404 otherwise), the script approved (409), the render present and
its file the size recorded for it. The MP4 is copied inside the storage server
(`Storage.copy_object`, added in this piece) to `training/modules/{module_id}/{publication_uuid}.mp4`
- a fresh UUID every publication, so a key is written once and never overwritten - and the module
records its own size, duration, resolution, frame rate and the render's fingerprint
(`video_output_hash`). `source_render_output_id` is provenance only: serving a module never reads the
render row or the render's key, so a re-render or deleting the project cannot change or remove a
published video (it just becomes `NULL`). The same render twice in one course is a 409, decided by the
service before anything is copied **and** enforced by `uq_training_modules_course_source_render`.
Not checked: that the render is *current* against the latest narration/storyboard (an administrator
publishing a stale render is not stopped); the workspace already shows when a video is stale.

**Files and commits.** Object storage cannot be rolled back, so the order is: write the new object,
change the rows, commit, and only then remove an object no longer needed (the replaced snapshot, an
unpublished or deleted module's video). `services.training.storage_effects` wraps each such request:
before the commit only the *new* object is removed on failure; after the commit only the *obsolete*
ones are removed; a removal that fails never fails the request and is logged
(`training.object_cleanup_failed`, `orphaned=True`, bucket and key). A key is only ever deleted if it
lies inside its own module's prefix.

**Order.** Positions are 1..n and contiguous. Appending, closing a gap and reordering all hold
`SELECT ... FOR UPDATE` on the course row. A reorder is one `UPDATE ... FROM unnest(...) WITH
ORDINALITY` that relies on the unique constraint being `DEFERRABLE INITIALLY DEFERRED` (no negative
placeholders, no delete and re-insert); `SET CONSTRAINTS ... IMMEDIATE` then checks it inside the
transaction.

**404s.** A route naming a course or a module answers a non-administrator the fixed
`Course not found.` / `Module not found.` (policies `require_admin_for_course` / `_module`) - the same
bytes an administrator gets for an id that does not exist, with no id echoed. The collection routes
(`GET`/`POST /courses`) answer an employee 403. Modules are addressed by their own id so a route never
names two resources. `tests/test_route_policies.py` pins every training route's policy and compares
the bytes. The tests that need a real `COMMIT` (re-render, source deletion, rollback, clean-up
failure, reorder, constraint, and the enrolment races) are `tests/test_training_committed.py` and
`tests/test_training_enrolments_committed.py`; they create `qa-training-*` users and projects and
remove exactly those, and they run on a **scratch database** (below).

**A nested collection is as resource-dependent as the resource.** `GET /courses/{course_id}/modules`
and `GET /courses/{course_id}/enrolments` do not end in an identifier, but what they answer depends on
whether that course exists, so they carry `require_admin_for_course` and an employee gets the same
fixed `Course not found.` 404 for a real course and a made-up one. The route-policy test decides by
the presence of `{course_id}` / `{module_id}` / `{project_id}` *anywhere* in the path, and pins every
training route by name.

### Enrolments and employee access (Piece 3)

Administrator routes (all under `/v1/admin/training`; policy in brackets - `admin_course` = an
employee gets the fixed course 404, `admin` = a collection, an employee gets 403):

| Route | Does |
|---|---|
| `POST /courses/{course_id}/enrolments` `{user_id}` (`admin_course`) | **enrol, or re-enrol**: 201 new, **200 when the user's withdrawn enrolment is made active again** (same row), 409 already enrolled / course archived, 422 not an employee, 404 unknown user or course |
| `GET /courses/{course_id}/enrolments` (`admin_course`) | a course's enrolments (`?status=active\|withdrawn`, paginated) |
| `POST /courses/{course_id}/enrolments/{user_id}/withdraw` (`admin_course`) | 200, the row is kept with status `withdrawn`; 409 already withdrawn; 404 no such enrolment |
| `GET /enrolments?user_id=` (`admin`) | one user's enrolments across courses; `user_id` is a **query** parameter so the route names no resource in its path |

The five administrator capabilities (enrol, list a course's, list a user's, withdraw, re-enrol) are four
routes: re-enrolling is enrolling someone who has a withdrawn row, so it is the same `POST` rather than a
second route with the same effect.

Employee routes (`/v1/training`, read-only, no `Idempotency-Key`):

| Route | Does |
|---|---|
| `GET /courses` (`employee`; an administrator gets 403) | the caller's own published, actively-enrolled courses, each with its **published** modules and for every module `locked` plus `lock` (`reason`, `blocking_module_id`, `blocking_module_position`, `blocking_module_title`). No user id is accepted: the caller is the token |
| `GET /modules/{module_id}/video` (`employee_module`) | streams the module's **own snapshot** with `Range`, through `api.video_response.serve_module_video` |

**Enrolment rules.** Only a user whose `role` is `employee` can be enrolled (422 otherwise, checked in
the service - a CHECK cannot see another table - and nothing is written). One row per `(course, user)`
for ever: withdrawing sets `status = 'withdrawn'` in place, re-enrolling sets it back to `active` in
place (same id, same `created_at`, `assigned_by` becomes the re-enrolling administrator; the audit
trail keeps who did what: `training.enrolment.created` / `.withdrawn` / `.reactivated`). The row is
created by `INSERT ... ON CONFLICT DO NOTHING` on `uq_course_enrolments_course_id_user_id`, so the
database decides a race, never a prior look: of N simultaneous enrolments of one person exactly one is
201 and the rest are 409 (`tests/test_training_enrolments_committed.py`, real concurrent
transactions). Enrolling holds a shared lock on the course row, so it cannot interleave with an archive
or a delete of the course. There is no deferred constraint in enrolment, and the unique conflict is
absorbed by `ON CONFLICT`, so no `IntegrityError` can surface at the commit that the idempotency
wrapper makes after the handler; a commit that fails for any other reason releases the key and writes
nothing (tested). An enrolment that is already true is a **409** under a *different* key (a replay of
the same key returns the first result).

**404 versus locked 403.** Five causes give one identical 404 (status, body bytes and headers; `Module
not found.`), tested byte for byte: no such module, not enrolled, enrolment withdrawn, course not
published (draft or archived), module not published - and an administrator calling the employee route.
None of them is listed in `GET /courses`. The one 403 about a resource is **`module_locked`**: the
employee *is* enrolled, the course and module are published, and an earlier published module has not
been passed. It is allowed to say so because the module is already in their own listing; the body is
`{"error": {"code": "module_locked", "message": ..., "details": {"blocking_module_id",
"blocking_module_position", "blocking_module_title"}}}`. Access is decided by one pure function
(`domain.training_access.decide_module_access`) applied by `services.training_access`, for both the
listing and the stream, so they cannot disagree. **`passed_module_ids` still returns the empty set**
(no assessment exists), so an enrolled employee can open module 1 and every later module is locked;
`tests/test_employee_training_api.py` pins that and fails visibly when Step 3 changes it.

**One serving function.** The preview and the employee stream both end in
`api.video_response.serve_module_video` (snapshot lookup, `Range` parsing, 206 / 416, `Accept-Ranges`,
`Content-Range`, the 404 for a module with nothing to play). The Range suite is the preview's, run
once; a structural test proves neither route serves by itself. Revocation is checked per request: a
withdrawal stops the next request, a download already in flight finishes.

**Test isolation: the scratch database.** `audit_logs` is append-only by design (a trigger rejects
`DELETE`), so any test that really commits writes audit rows that nothing can remove. Those tests grew
the development database's `audit_logs` from 301 to 1,336 rows. They now use
`conftest.committed_database_url`: a database created for the test session, migrated by the real
`alembic upgrade head`, and dropped at its end. The ordinary `db_session` tests roll back and write
nothing. The same applies to the other tests that reach the application's process-wide engine
(worker, stale-job recovery, services that really commit): module-level detection points that engine
at the scratch database. Limits: it protects only tests that reach the database through those paths
(`world` asserts it is not the development database); Redis is still shared, so tests keep to their
own keys; existing audit rows were left alone.

**Test isolation: scratch object storage.** The development buckets (`edutoon-uploads`,
`-segments`, `-outputs`, `-assets`) are never touched by a test session (`tests/storage_isolation.py`,
regression test `tests/test_storage_isolation.py`). At import of `tests/conftest.py` the four `BUCKET_*`
settings are *overwritten* (never defaulted: `.env` and the shell cannot win) with per-session scratch
buckets `edutoon-test-<run id>-<purpose>`; `pytest_sessionstart` creates them and `pytest_sessionfinish`
empties and deletes exactly those. Every `Storage` built in the process (`Storage.__init__` is wrapped)
refuses, before any request is built, an operation whose `Bucket` or `CopySource` is not one of the four
scratch buckets - protected, hard-coded or unknown alike - so a test cannot fall back to a development
bucket by any route, including a raw `storage._client` call. Dedicated buckets rather than a key prefix:
the storage layer addresses `(bucket, key)` everywhere and has no namespace, so a prefix could not be
enforced in one place. Limits: a session killed hard leaves its `edutoon-test-*` buckets (empty or not)
behind - harmless, and deliberately not swept by later sessions, because a session removes only what it
created; `scripts/verify_phase10.py`, `verify_phase11.py` and `smoke_test_phase4.py` are manual
verification scripts, not tests, and still use the development buckets. The upload path is unchanged:
write the object, then insert the row.

### Training assessments (Step 3, migration 0021)

One optional **assessment per module**: single-correct multiple choice, 1-50 questions of 2-6 options
(`domain/training_assessment.py` holds every limit and the grading). Design and the list of decisions:
`docs/training-platform-design.md`, "Step 3".

- **Authoring** (administrators): `PUT /v1/admin/training/modules/{module_id}/assessment` replaces the
  whole assessment (threshold 1-100, default 70, stored on the assessment) as a **draft**; it is a 409
  while the assessment is published, even with no attempts. `GET` returns it with `is_correct` and its
  `status` / `published_at`; `DELETE` is allowed for a draft or published assessment and is 409 once it
  has attempts. `POST .../assessment/publish` re-validates the whole assessment (422 if it has no
  questions, a question outside 2-6 options, or not exactly one correct option; 1-50 questions), makes it
  live and returns `active_enrolments_affected`; `POST .../assessment/unpublish` returns it to a draft
  and is 409 once anyone has attempted it. An attempted assessment is therefore frozen for good.
- **Taking it** (employees): `GET /v1/training/modules/{module_id}/assessment` returns the questions with
  no key; `POST .../assessment/attempts` (`Idempotency-Key`) submits **every** answer once, is graded in
  that request and recorded; `GET .../assessment/attempts` lists the caller's own attempts. The
  response is the integer score (`correct_count`, `question_count`), the `threshold_percent` it was
  graded with and `passed` - never which answers were right. The history carries no question or option
  id and no wording either, so it has nothing to tell the right option apart (`tests/test_assessments_api.py`,
  `test_an_employees_attempt_history_shows_their_outcome_and_nothing_that_reveals_the_key`). It does not
  echo the options the employee chose. The score itself is still information: with unlimited retakes an
  employee could probe the key one answer at a time - no attempt limit or cooldown exists (a product
  decision, not made).
- **Draft and published.** Only a published assessment is visible to employees (a draft is the same
  byte-identical `Assessment not found.` 404 as every other unreachable cause, on all three employee
  routes) and only a published one counts toward the pass gate, so a draft never blocks the next module.
  Publishing deliberately starts gating the enrolled employees. An unpublished module's assessment is
  hidden and blocks nothing too. (Migration 0021 carries `status` and `published_at`; it was edited in
  place before it had been applied to any persistent database.)
- **Grading** is integer-only: `passed <=> correct * 100 >= threshold_percent * question_count`
  (7/10 and 70/100 pass a 70% gate; 69/100 and 2/3 do not). A submission with a missing, repeated,
  unknown or foreign answer is a 422 and writes nothing.
- **History is immutable.** An attempt is inserted already graded together with the question set,
  options, key and threshold used (`assessment_attempt_answers` has no foreign key into the authoring
  tables), so editing the assessment never changes a recorded attempt. `reject_mutation` triggers refuse
  any UPDATE/DELETE, a CHECK ties `passed` to the integers, and no code under `src` issues an UPDATE or
  DELETE for these tables (`tests/test_architecture_rules.py`).
- **Progression.** `services.training_access.passed_module_ids` returns the modules with a passing
  attempt - sticky, a later failed retake changes nothing. A module with no assessment blocks nothing:
  module N is open once every lower-positioned module that has an assessment is passed.
- **Deleting and reordering.** `DELETE .../modules/{id}` and `PUT .../courses/{id}/modules/order` are 409
  once an attempt exists (the reorder message says to create a new course); the foreign keys from
  attempts to modules, users and assessments are `ON DELETE RESTRICT` (migration 0021's docstring
  explains why, citing 0018).
- **Known limitations (Step 3, as built).**
  1. *An attempted assessment is frozen for good.* It is published, `PUT` is a 409 while published, and
     unpublishing is a 409 once anyone has attempted it - so its questions and threshold can never be
     changed again. Fixing a wrong question means a new module (or course) with a new assessment.
  2. *The key can be inferred by repeated attempts.* A result is the integer score, and there is no
     limit on retakes and no cooldown, so an employee can vary one answer at a time and watch
     `correct_count` move. Nothing in a single response reveals the key; the score across attempts
     does. An attempt limit, a cooldown or shuffled options would be a product decision.
  3. *The history does not show an employee which options they chose.* It is outcome-only (number,
     score, threshold, verdict, time). The chosen options are recorded
     (`assessment_attempt_answers.selected_option_id`) but no route returns them, so an employee cannot
     review their own answers.
- **Two correct options cannot be stored.** `assessment_answer_keys` has one row per question (its
  primary key), so a question with two correct options is unrepresentable in the database. Saving
  refuses it (422) before anything is written, and publishing re-checks the stored questions with the
  same rule; because no real data can hold that state, the publish test for it injects the state at
  the one place publish reads the questions (`list_admin_questions`). The other publish-time
  flaws (no questions, one option, no correct option) are produced by deleting rows underneath a
  stored draft.
- **Concurrency.** The module row is the one serialisation point. Publish, unpublish, save and delete
  take the course row and then the module row `FOR UPDATE` before they read anything; a submission takes
  the module row `FOR KEY SHARE` first, then reads the assessment, and inserts its attempt under that
  lock. They conflict, so an unpublish racing a submission is either wholly after it (it waits, sees the
  attempt, 409) or wholly before it (the submission waits, then finds a draft, 404): an assessment with
  an attempt is never a draft. The *same order* matters: an earlier version had the submission lock the
  assessment row first, reaching the module only through the attempt's foreign key, and
  `test_a_submission_paused_before_its_insert_neither_deadlocks_nor_loses_to_an_unpublish` reproduced
  Postgres aborting one of the two with a deadlock error - that is why there are no row locks on the
  assessment itself any more (they were redundant once the module is the serialisation point, and an
  assessment-row lock is what created the opposite order). Proved with real overlapping transactions in
  `tests/test_training_assessments_committed.py`; the mutation runner kills a submission that does not
  lock the module, one that locks it after reading, and administrator operations that do not lock it.
- **404 for assessment routes.** Seven causes (no module, not enrolled, withdrawn, course or module not
  published, not an employee, no assessment) give the byte-identical `Assessment not found.`; a locked
  module is the 403 `module_locked`.
- **Tests**: `tests/test_assessments_api.py`,
  `test_domain_training_assessment.py`, `test_migration_0021_upgrade_downgrade.py` (up/down/up, also
  with attempt data present), and the pinned route tables in `test_route_policies.py` and
  `test_training_route_security.py`. The development database stays at 0020 until you run
  `make migrate` - and until then the assessment API tests (which use the shared database, rolled
  back) fail with `relation "assessments" does not exist`; to run the suite without touching the
  development database, point `DATABASE_URL` and `DATABASE_DIRECT_URL` at a scratch database that
  `alembic upgrade head` has migrated.

## Formatting

Python is formatted with `ruff format` (line length 100); the whole tree is formatted, and `make lint`
runs `ruff check` and `mypy` (run `uv run ruff format --check .` to see drift). The one commit that
reformatted the tree is listed in `.git-blame-ignore-revs` at the repository root so `git blame` skips it:
once per clone, run `git config blame.ignoreRevsFile .git-blame-ignore-revs` (GitHub reads the file by
itself). Any later whole-tree format should be its own commit, added to that file.

## Project generation settings

A project carries three settings that later phases (scene planning, narration,
rendering) read to decide what to produce. They are columns on `projects` (migrations
`0010` and `0011`), sent on `POST /v1/projects` and `PATCH /v1/projects/{id}` and returned by every
route that returns a project (`GET` one, `GET` the list, and both writes). Omit them on
create and a project gets the defaults, so a client written before they existed keeps
working; omit one on update and it is left as it is.

| Field                | Type    | Allowed                                            | Default              |
| -------------------- | ------- | -------------------------------------------------- | -------------------- |
| `orientation`        | string  | `horizontal` (landscape), `vertical` (portrait)    | `horizontal`         |
| `target_duration_ms` | integer | `60000` to `3600000` inclusive (1 to 60 minutes)   | `300000` (5 minutes) |
| `language`           | string  | `en-IN`, `en-GB` (an IETF BCP 47 tag)              | `en-IN`              |

The duration is in milliseconds like every duration here, so it compares directly with a
script's `estimated_duration_ms`. It must be a real integer: `"180000"`, `180000.0`,
`true`, zero, a negative, or anything outside the range is a `422`, as is an unknown
`orientation` or `language` or an explicit `null` on update. The same limits are
`CHECK` constraints in the database, so nothing that skips the API can store a value the
code does not expect.

`language` is the column that already existed, now constrained. `en-IN` is the default
(migration `0011` changes only the column default, so no existing row is rewritten);
`en-GB` stays supported, and every project created while it was the default keeps it.
Migration `0010` reset any row holding some other, never-validated value to `en-GB`;
`downgrade` cannot restore it.

**The stored language is what the model is asked to write in.** `projects.language` is
the only source (never inferred from a user's text): the script job puts it on the
`ScriptBrief`, topic extraction puts it on the `TopicBrief`, and both prompts carry a
`Language:` line and a fixed `Writing style:` line for it (`en-IN`: Indian English, with
British spelling, for Indian students; `en-GB`: British English). The system prompts name
no language. The brief types have no default language, so a caller cannot forget it.

To support another language, add it to `src/edutoon/domain/generation_settings.py`, give
it a writing style in `providers/llm.py` (`test_every_supported_language_has_a_writing_style`
fails until it has one) and widen `ck_projects_language` in a new migration.

## Project status

`projects.status` is a roll-up of where the project's pipeline has got to. Nobody sets it -
a request that sends a `status` is a `422` - it follows the work done for the project, and
`edutoon.domain.project_status` is the one place that says how:

| Status       | Meaning                                                                                         |
| ------------ | ----------------------------------------------------------------------------------------------- |
| `draft`      | Nothing has run yet. Every project starts here.                                                 |
| `processing` | A job for the project is `queued` or `running`.                                                 |
| `ready`      | Nothing is in flight and the most recent outcome was a success.                                 |
| `failed`     | Nothing is in flight and the most recent outcome was a failure: a job failed, or a PDF could not be read. |
| `archived`   | Set aside by its owner. The pipeline never moves a project out of it.                           |

It describes the **latest outcome**, so it changes with the work: a `failed` project becomes
`processing` when retried and `ready` when the retry succeeds; a `ready` one becomes
`processing` the moment more work is queued. "Latest" is the most recently *finished* thing
(a job's `finished_at`, or the time a PDF failed to parse), and a `cancelled` job is not an
outcome. The rule ignores which job it is, so a stage added later needs no change to it.

`repositories.projects.refresh_status` recomputes it, in the same transaction as the change
that caused it, whenever a job is queued or ends (`repositories.jobs`: create, a terminal
update, crash recovery) and whenever an uploaded PDF fails to parse
(`repositories.uploaded_sources`). Those are the only writers of job and source state, so the
API, the worker and recovery all move it without a call site to forget. Moving a job to
`running` does not refresh it (nothing it looks at changes), which also keeps the project row
lock out of a model call's transaction.

- A project that already existed when this was added keeps `draft` until its next job is
  queued or ends; there is no backfill.
- A PDF that could not be read makes a project `failed` until something finishes after it -
  the status describes the pipeline, not whether the project is still usable (topics from an
  earlier PDF are untouched).
- The status says nothing about what is stored: scripts and topics have their own statuses.

## Deleting a project

`DELETE /v1/projects/{id}` removes the project and, through `ON DELETE CASCADE`, its sources,
topics, jobs, claims (with their citations), scripts (with their segments, claim links, audio,
scenes and render rows). Two foreign keys used to be `RESTRICT` and made this fail for any project
with claims - `claim_citations -> source_chunks` and `script_segment_claims -> claim_verifications`;
migration 0018 made them `CASCADE`, and nothing else about deletion changed. It also removes the PDFs the project uploaded from object
storage, which the rows only pointed at. The order matters, because the database is the record
of what exists:

1. The deletion is committed first (with the idempotent response, as for every mutating route).
2. Only then are the files removed, one by one, by `services.projects.remove_stored_files`.

So a deletion that fails or rolls back leaves its files in place, and a project is never left
pointing at a file that is gone. A file that cannot be removed (storage down) is logged as
`project.file_cleanup_failed` with its bucket and key and does not fail the request - the
project is deleted either way, and the log line is what to clean up by hand. A replay of the
same request removes nothing twice.

Only files inside the project's own storage prefix (`projects/{project_id}/`, see
`domain.source_storage`) are removed, and only the exact objects its rows name - nothing is
found by listing a prefix, and a row that points anywhere else is skipped and logged
(`project.file_cleanup_skipped`). Another project's identical PDF is a different object.
Known limit: if the process dies between the commit and the cleanup, the files are orphaned; an
upload racing a deletion of the same project can leave one too.

## Job worker

Routes only ever *enqueue* a `jobs` row (indexing, topic extraction, script
generation); a separate worker process claims and executes them. Run it
alongside the API (`make dev` starts both, plus the web app):

```bash
uv run python -m edutoon.worker
```

It builds two LLM clients from the same API key, one per model: `LLM_MODEL` for
claim judging, topic extraction and - Phase 9 - storyboard scene composition, and
`SCRIPT_LLM_MODEL` for writing scripts. A `generate_script` job runs on the
second, and the script it produces records the model that actually wrote it
(a script is queued for the API's script client's model, and the job replaces
that with the one it ran with if they differ). See "Script generation" below
for what that job actually does (Phase 7).

It polls for `queued` jobs, claims one at a time with
`SELECT ... FOR UPDATE SKIP LOCKED` (safe with any number of worker
instances running concurrently), and dispatches by `jobs.kind` to whichever
existing `services.*.run_*_job` function handles it
(`src/edutoon/worker/dispatch.py`). A job whose kind has no handler yet is
marked `failed` with a clear error rather than stalling forever. Stop it
with Ctrl-C (or `SIGTERM`); it finishes whatever job it's already claimed
before exiting.

**What a failed job says.** A job's `error` is returned by
`GET /v1/projects/{id}/jobs`, so it is written for the project's owner
(`src/edutoon/services/job_errors.py`). A message this codebase wrote for people
- a validation or conflict error, a topic or script rule, the model's response
breaking the contract - is shown as written (capped at 300 characters). A
provider failure becomes a fixed sentence chosen by its kind (rate limit,
timeout, not authorised, ...); a database or connection failure, and anything
unexpected, become a generic one. Never shown: SQL or its parameters, stack
traces, a provider's response body, request ids, credentials, file paths. The
full exception - with the job's id - goes to the server log (`job.failed` /
`job.failed_unexpectedly`) so an operator can find it. Jobs that failed before
this was introduced keep whatever text they were stored with.

## Topic understanding

Before any claim is verified or script written, the project needs an outline of
what the video should cover. Topics come from the uploaded PDF, from keywords
the user types, or both, and each topic records where it came from:

| `origin`    | Meaning                                                        | Evidence            |
| ----------- | -------------------------------------------------------------- | ------------------- |
| `source`    | Extracted from the uploaded material                           | at least one page   |
| `user`      | Typed in by the user (a keyword)                               | optional            |
| `suggested` | Proposed by the model with no material behind it (keywords only) | none — unverified   |

A topic's `summary` and `keywords` are labels for review, **not verified
claims**: facts only reach a script through claim verification. Topics are a
tree (a topic, its subtopics, …); hierarchy and reading order are the only
relationships between them.

| Route                                                  | What it does |
| ------------------------------------------------------ | ------------ |
| `POST /v1/projects/{id}/topics`                        | `201`. Store the user's keywords as `accepted` topics. No model call. |
| `POST /v1/projects/{id}/topics/extraction`             | `202` with the queued `extract_topics` job. Optional body `{"replace": true}`. |
| `GET /v1/projects/{id}/topics`                         | The outline in reading order, each topic with its `evidence` (source, chunk, page range). Not paginated. |
| `PATCH /v1/projects/{id}/topics/{topic_id}`            | The review step: `status` (`proposed` / `accepted` / `rejected`), and `title` / `summary` wording. |

Every mutating route requires an `Idempotency-Key` (1 to 255 characters; an empty
or longer one is `422`), rejects unknown body fields with `422`, and returns `404`
for a project or topic the caller does not own. The write is committed *before*
the response is sent and before an idempotent replay is cached, so a `2xx` - and a
replay of one - always means the change is stored. Progress and the outcome of an
extraction are on `GET /v1/projects/{id}/jobs`.

**How an extraction runs.** The worker reads the project's parsed pages, picks a
deterministic, whole-document sample that fits `TOPIC_INPUT_BUDGET_CHARS`, makes
**one** model call, validates the result against the rules in
`domain/topic_tree.py` (only pages the model was actually shown may be cited;
model-proposed topics that cite nothing are dropped whenever there was material;
every user keyword survives verbatim; origins are decided by the rules, never by
the model), then writes the topics in one savepoint. The job result reports
`mode` (`pdf`, `keywords` or `pdf_keywords`), `topic_count`, `chunks_total`,
`chunks_used`, `truncated` (the model saw only part of the document),
`dropped_uncited`, token counts and the model used.

- **Explicit only.** Uploading a PDF never starts an extraction; the caller asks.
- **One at a time.** A second request while one is queued or running is `409`.
- **Re-running.** Once topics have been extracted, `409` unless `replace` is
  true. `replace` deletes only the *derived* topics (`source` / `suggested`); the
  user's own topics keep their rows and wording. It is refused (`409`) if any
  script already references the project's topics.
- **Failure is safe.** A failed extraction ends the job as `failed` and leaves
  the previous topics and evidence exactly as they were. Retry with a new
  `Idempotency-Key` — a repeat of the old key replays the failed job.
- **Editing during a run.** Adding keywords and changing a title or summary are
  refused (`409`) while an extraction is queued or running; accepting or
  rejecting a topic is always allowed.
- **A crashed worker cannot block a project for ever.** If a worker dies
  mid-extraction its job is left `running`. The next request that needs the
  project free (a new extraction, adding keywords, editing a title or summary)
  fails that job first - `failed`, with "run the extraction again" as its error,
  and a `topics.extraction_recovered` audit entry - and carries on. A job is only
  recovered if it is older than `TOPIC_EXTRACTION_STALE_AFTER_SECONDS` *and* no
  live worker holds it: a running handler keeps its job's row locked until it
  finishes, so a slow model call is never mistaken for a dead worker. Nothing is
  left half-written, since an extraction's writes are one transaction. Until the
  next such request the abandoned job still shows as `running` in the job list -
  and a crash holds the project's keyword and wording edits (`409`) for up to
  `TOPIC_EXTRACTION_STALE_AFTER_SECONDS` (an hour by default) before it can be
  recovered.

Settings (all optional, defaults in the repository root `.env.example`):
`TOPIC_INPUT_BUDGET_CHARS` (the cost control), `MAX_TOPICS_PER_EXTRACTION`
(also the cap on a project's own keywords), `MAX_TOPIC_DEPTH`,
`MAX_TOPIC_TITLE_CHARS`, `MAX_TOPIC_SUMMARY_CHARS`, `MAX_TOPIC_KEYWORDS`,
`MAX_TOPIC_KEYWORD_CHARS`, `MAX_TOPIC_EVIDENCE`, `TOPIC_EXTRACTION_STALE_AFTER_SECONDS`
(default 3600, minimum 60; keep it above a model call's worst case). They use `LLM_MODEL` — there is
no separate topic model. Extraction needs `ANTHROPIC_API_KEY`; the test suite
never calls the network (it replaces `messages.create` on a real client), so the
prompt in `providers/llm.py` has not yet been tried against a live model.

## Candidate claim extraction

Before Phase 6, a claim had to be typed in by hand through
`POST /v1/projects/{id}/claim-verifications`. That route is unchanged; this adds
the other way a claim comes to exist: turning a project's *accepted* topics into
candidate factual claims and verifying each one exactly the same way.

| Route                                                           | What it does |
| ---------------------------------------------------------------- | ------------ |
| `POST /v1/projects/{id}/claim-verifications/extraction`         | `202` with the queued `extract_claims` job. Optional body `{"replace": true}`. |
| `GET /v1/projects/{id}/claim-verifications?topic_id=...`         | The existing list route, narrowed to one topic's claims. |

**How it runs.** For each of the project's accepted topics (`domain.topic_tree.accepted_topics`
- a topic only counts if it and every ancestor is accepted), unless it already has extracted
claims and `replace` was not asked for:

1. Gather the topic's own cited passages, budgeted the same way topic extraction budgets a
   whole document (`CLAIM_INPUT_BUDGET_CHARS`, far smaller since it is per topic) - empty for a
   keyword-only topic with nothing indexed.
2. **One model call** (`LLM.extract_claims`) proposes candidate claim text for this topic alone.
   Candidates are structure only, exactly like `extract_topics`'s topics: a candidate is never
   itself a verified fact, and the prompt says so explicitly.
3. `domain.claim_extraction.validate_candidates` cleans the candidates and drops any that
   duplicate one this topic already has (case/whitespace-insensitive) - so a `replace` re-run is
   genuinely incremental, never re-verifying (and re-recording) the same wording.
4. Each surviving candidate is run through **`services.evidence.verify_claim`, completely
   unmodified** - the same embed → retrieve → judge → persist flow a hand-typed claim already
   gets, with the topic's id attached (`claim_verifications.topic_id`, migration 0012; `NULL`
   for a manually created claim).

**One topic's failure never destroys another topic's successful claims.** Each topic's writes
are one SAVEPOINT; a malformed model response or a database error for one topic is recorded in
the job's `failures` list and the run moves on. The job itself only ends `failed` if every
eligible topic failed and nothing was verified - otherwise it `succeeded`, and its result reports
`topics_eligible`, `topics_processed`, `topics_skipped_already_extracted`, `topics_failed`,
`candidates_generated`, `claims_verified`, `claims_by_status` and any `failures`.

- **Additive, not all-or-nothing.** Unlike topic extraction there is no "already extracted, pass
  `replace`" `409` at enqueue time: accepting more topics later and calling the route again is the
  ordinary, idempotent way to cover them. `replace=True` re-examines every accepted topic instead
  of only the new ones.
- **Keyword-only projects.** A topic with no uploaded PDF has no indexed material, so
  `verify_claim`'s retrieval finds no candidates and every claim from it is correctly
  `unsupported`, with a rationale saying so - not a bypass of verification, and no new
  "knowledge source" invented to work around it. This is expected: it becomes visible to the
  owner once a PDF is added and the topic's claims are re-run with `replace`.
- **One job at a time, independent of topic extraction.** Guarded and stale-recovered the same
  way as `extract_topics` (`CLAIM_EXTRACTION_STALE_AFTER_SECONDS`), on its own lock keyed to
  `extract_claims` - a topic extraction in flight for the same project does not block this.

Settings (all optional): `CLAIM_INPUT_BUDGET_CHARS`, `MAX_CLAIMS_PER_TOPIC`,
`MAX_CLAIM_TEXT_CHARS`, `CLAIM_EXTRACTION_STALE_AFTER_SECONDS`. They use `LLM_MODEL`, the same
model claim judging and topic extraction already use.

See "Script generation" below for how these topics and claims feed a script (Phase 7).

## Script generation

Phase 7 replaced the single whole-project `compose_script` call with a
duration-aware, topic-scoped pipeline: one lesson outline call, then one
narration call per *accepted* topic (split into several ordered sub-sections
when a topic's own duration budget is too long for one call), assembled and
validated as a whole before anything is written.

| Route                                                  | What it does |
| ------------------------------------------------------- | ------------ |
| `POST /v1/projects/{id}/scripts`                       | `202` with the queued `generate_script` job and a fresh `pending` script. |
| `GET /v1/projects/{id}/scripts`                        | Newest first, keyset-paginated. |
| `GET /v1/projects/{id}/scripts/{script_id}`            | Segments and, per segment, the verified claims it cites. |
| `POST /v1/projects/{id}/scripts/{script_id}/approval`  | `draft` → `approved`. |

**Target flow:** accepted topics → each topic's own supported claims →
server-computed duration budgets → one outline call → one section call per
topic (or sub-section) → an at-most-one duration-correction pass → structural
and provenance validation → one atomic attach.

**Enqueue-time gate, before any provider call.** `POST .../scripts` rejects
immediately (`422`→`400` `validation_error`, no job created) if: there is no
accepted topic; there are more accepted topics than `MAX_SCRIPT_SECTIONS`;
`project.target_duration_ms` cannot give every accepted topic the
`MIN_SECTION_DURATION_MS` floor; or **any** accepted topic has zero
`status='supported'` claims of its own (`claim_verifications_repo.list_supported_by_topic`) -
named in the error. A hand-typed claim (`topic_id IS NULL`) never counts
towards any topic's evidence; it belongs to no topic's section.

**Duration is a target, not a hard gate.** `project.target_duration_ms` is
split across accepted topics (`domain.script_duration.allocate_topic_budgets`):
each gets `MIN_SECTION_DURATION_MS` as a floor, then a share of the remainder
proportional to its own supported-claim count. The LLM never decides
duration. A topic's total budget over `MAX_SECTION_DURATION_MS` is split into
several ordered sub-sections (`split_topic_budget`) sharing one `topic_id` -
never one huge call. The WPM-based `estimated_duration_ms` this produces is
an estimate for a **not-yet-real TTS duration** - if it lands outside
`SCRIPT_DURATION_TOLERANCE` of the target, exactly one bounded correction
pass re-generates every section with a rescaled word budget
(`MAX_SCRIPT_CORRECTION_ATTEMPTS`), and the result is accepted afterwards
regardless of the remaining difference. Only structural/provenance failures
reject a script outright.

**Topic scoping.** Each section call sees only its own topic's supported
claims (oldest-first, capped at `MAX_CLAIMS_PER_SECTION`) - never another
topic's, never a hand-typed one. Every persisted segment's `topic_id` is set
by the service from the section it came from, never taken from the model.
Provenance validation (`domain.script_provenance.filter_and_validate`,
unchanged) still rejects any non-connective segment left with no supported
citation.

**Atomicity (migration 0013).** `uq_scripts_project_id_active` used to cover
`pending` / `draft` / `approved`, so queuing a new generation had to
supersede the previous `approved` script immediately, before anything was
validated - a failed regeneration then left the project with nothing
approved at all. The index now covers only `draft` / `approved` (two pieces
of actual content still can never coexist); a fresh `pending` placeholder
coexists with the still-`approved` script for as long as generation takes,
and `repositories.scripts.attach_draft` supersedes the previous script in the
same `SAVEPOINT` that writes the new one's segments and flips it to `draft` -
the one and only moment a previously valid script is touched, and only once
the new one is already proven ready to replace it. A failed generation always
leaves the previous `approved` script completely untouched.

**Progress.** The worker's usual pattern runs a whole job inside one
transaction, committed only once the handler returns - which would make an
interim progress write invisible until the job finished, or block behind its
lock. `run_generate_script_job` instead commits its "running" transition
immediately and reports interim progress (`preparing_outline`,
`generating_topic`, `correcting_duration`, `validating_provenance`,
`finalising`) through short, independent sessions, visible on
`GET /v1/projects/{id}/jobs` as `payload.progress` - each write touches only
the job row, never a script/segment/claim-link row.

**Provider-call budget.** Worst case: one outline call, plus every topic's
section/sub-section calls (bounded by `domain.script_duration.max_provider_calls`,
enforced before any call is made, never merely documented), plus one full
correction pass over the same set. With the defaults (`MAX_SCRIPT_SECTIONS=20`,
`MAX_SECTION_DURATION_MS=300_000`, a 60-minute maximum target), that is at
most 65 calls, against 1 for the pre-Phase-7 single call.

**Keyword-only projects without verified evidence cannot currently generate a
fact-bearing educational script**, because the enqueue-time evidence gate
above applies uniformly: a keyword-only topic's claims are correctly
`unsupported` (no indexed material to verify them against - see "Candidate
claim extraction"), so that topic can never pass the gate. This is not a
bypass or a weakening of provenance; it is the same guarantee a hand-typed
claim against an unindexed project already gets. It becomes generatable once
the topic has real, verified evidence.

Settings (all optional): `MIN_SECTION_DURATION_MS`, `MAX_SECTION_DURATION_MS`,
`MAX_SCRIPT_SECTIONS`, `MAX_CLAIMS_PER_SECTION`, `SCRIPT_DURATION_TOLERANCE`,
`MAX_SCRIPT_CORRECTION_ATTEMPTS`, `CONTINUITY_CONTEXT_CHARS` - plus the
existing `SCRIPT_LLM_MODEL`, `MAX_SCRIPT_SEGMENTS` (per section call, not the
whole script any more), `MAX_CLAIMS_PER_GENERATION` (the whole script's
claim-count ceiling, distinct from the new per-section one) and
`SCRIPT_WORDS_PER_MINUTE`.

## PowerPoint decks

A project can be built from a `.pptx` deck (`source_type: "pptx"`). It runs the same stages as a PDF project
- topics, claims, script, narration, storyboard, video - under the **same job kinds, endpoints and review
pages**, but **no model writes anything**: the lesson is planned by deterministic code
(`domain/deck.py`, `deck_text.py`, `deck_plan.py`, `deck_scenes.py`; `services/deck_lesson.py`). It needs no
provider key, makes no provider call and needs no embeddings. A PDF project never reaches this code: the worker
routes by the project's source type (`worker.dispatch`), and the PDF services are unchanged.

**Upload.** `POST /v1/projects/{id}/sources` accepts a PDF or a `.pptx` (up to `MAX_UPLOAD_BYTES`, and
`MAX_DECK_SLIDES` slides, counted before any text is read). The file's *bytes* decide what it is, not its name
or type: a `.pptx` the browser reported as `application/octet-stream` is accepted, a PDF called `.pptx` is a
`422`, and an older binary `.ppt` is a `422` that says to save it as `.pptx`. The archive is checked before it is
opened (entry count and expanded size). A deck that cannot be read, has too many slides, or has no text at all
(every slide a picture) is recorded as `failed` with a reason, exactly as a PDF is. Hidden slides are skipped;
every slide keeps its number in the deck, which is the chunk's `page_from`/`page_to`.

**Structure, not flat text** (migration 0017). Each slide is one `source_chunks` row with two renderings: the
flat `content` every chunk has always had (a citation shows it, and every claim is checked against it) and a new
`structure` (`jsonb`) holding what the slide is made of - title, paragraphs, bullets with their level, numbered
steps, tables, speaker notes. The generator reads the structure; nothing else changed about chunks.

**Topics** (`extract_topics`). The deck's own section-header slides name the topics; with none, neighbouring slides
are cut into balanced runs of three or so. No more than `MAX_SCRIPT_SECTIONS` topics and six slides to a topic.
Each topic cites the slides it covers and is saved `accepted` (it can still be rejected or renamed on the review
page before the script is written). Title, agenda, closing ("Thank you", "Questions") and empty slides shape the
lesson but are not narrated point by point.

**Claims** (`extract_claims`). Every statement a slide makes - a bullet, a numbered step, a table row, a sentence
of a paragraph or of the speaker notes - becomes a claim, `supported` because it is *quoted* from the slide
(`domain.deck.claim_is_quoted`) and citing that slide's chunk. There is no judge and no embedding, so the ledger
records the engine, not a model: `llm_model = deck-engine/1`, `embedding_model = none`. Footers repeated across
slides, URLs, e-mail addresses and presenter instructions in the notes ("click to show the chart") are not
statements.

**Script** (`generate_script`). The verified statements are fitted to the target length and written as one
continuous narration. It goes through the same rules as a model-written script
(`script_provenance.filter_and_validate`: every fact-bearing section cites a supported claim; no number, date or
name appears that the slides do not contain).

- *Choosing.* If the deck holds more than the target can carry, the lowest-ranked statements are left out first
  (`score_statement`: definitions, steps and anything with a figure rank higher; later and nested points lower),
  then whole slides, but never the last slide of a topic. If it holds less, **nothing is added**: the video is
  shorter than the target and the script page says so. (A 25-minute deck asked to fill 30 minutes is a 25-minute
  video; the same deck asked for 20 drops statements until it fits.)
- *One lesson, not a run of slides.* It opens by naming the lesson and what it covers; each topic is introduced
  and tied to the last ("Now that we have seen X, let's move on to Y"; the last is "Finally, ..."); each slide is
  introduced by its title with varying lead-ins; short fragment bullets are spoken as one sentence ("This covers
  slips, trips and falls, including wet floors and loose cables ..."), steps in order ("There are four steps.
  First, ... Finally, ..."), table rows as comparisons; connectives vary and are never put in front of a
  proper noun; and it closes with a summary. Wording is plain, short sentences - what a new employee can follow.
- *Pace.* `DECK_SPEECH_WORDS_PER_MINUTE` (160) sizes the plan. Piper was measured at 163 words a minute on a real
  deck lesson (145 to 180 by section) and 132 on a longer-worded sample, so the plan is an estimate; the measured
  narration replaces it.

**Storyboard.** `DeckStoryboardComposer` stands where the model stands in `services.storyboard` (the same
`compose_storyboard_scenes` call, the same slot and provenance checks on its output). It composes each section's
scenes **from what the narration says**: the slide is found by which of its statements are spoken, so it does not
matter if the target or the accepted topics changed since the script was written. A welcome is a title card, a
slide is its spoken statements as bullets under its own title (about three a scene), the summary is a "Recap".
Text only: a table is shown as its rows, and a diagram is never invented.

**Not done, on purpose.** Keywords for a deck (the wizard offers none; a keyword topic would have no slide behind
it); pictures, charts and SmartArt (they carry no text this library exposes, so a picture-only slide is left out);
an older `.ppt`; a second language; a table as a comparison diagram (the procedural visuals exist, nothing in the
deck engine produces one yet).

**Verified** (this machine, the real stack, the real worker, no provider key): the sample induction deck
(13 slides, one hidden) through the browser to a 2-minute MP4, and two large synthetic decks to 25 and 30
minutes; see the numbers under "Video rendering". `tests/test_deck_api.py` runs the same journey through the real
API and worker with every provider call made to fail. **Not verified:** how the narration *sounds* to a listener
(no one has listened to it), a real customer deck, Indian-English pronunciation.

## Text-to-speech

Phase 8 turns an approved script into narrated audio with
[Piper](https://github.com/OHF-voice/piper1-gpl) (`piper-tts`, currently
1.8.0) - a small, local neural TTS engine. It runs entirely on the API/worker
host, needs no company API key, and (unlike every other provider in this
project) is never mocked in the test suite: `tests/test_services_script_audio.py`
and `tests/test_worker.py` synthesise real audio with the real engine against
the real local MinIO, every run.

**API surface** (`api/routers/script_audio.py`, prefix
`/v1/projects/{project_id}/scripts/{script_id}`, an approved script only):

- `POST /synthesis` - enqueues a `synthesise_voice` job (`202`, `Idempotency-Key`
  required, same asynchronous shape as script generation).
- `GET /synthesis` - the job's status and each segment's progress.
- `GET /segments/{segment_id}/audio` - streams that segment's WAV, `Range` requests
  honoured (browser seeking).

**Storage.** One object per segment,
`projects/{project_id}/scripts/{script_id}/segments/{segment_id}/audio.wav`
in `BUCKET_SEGMENTS` (`domain.script_audio.segment_audio_storage_key`) - mono
16-bit PCM WAV at Piper's native 22050 Hz. Uncompressed PCM costs a fixed
44.1 KB per second of audio regardless of content (~2.6 MB/minute), so a full
60-minute narration is roughly **150 MB** of WAV.

**Resumability.** `script_segment_audio.content_hash`
(`domain.script_audio.compute_content_hash`, over the narration text, voice
model, engine version and sample rate) fingerprints what produced a
segment's audio. Re-running synthesis skips any segment whose stored hash
still matches - only a changed segment, voice or Piper version re-synthesises
it - so `run_synthesise_voice_job` is safe to call again after a crash or a
partial failure without redoing already-valid work.

**Deletion.** `services.projects.delete_project` collects every segment's
storage location (`script_segment_audio` cascades on project delete, same as
`uploaded_sources`) alongside uploaded PDFs, and `remove_stored_files` removes
both from object storage after the database deletion commits - Phase 8 audio
is cleaned up by the exact same path Phase-pre-8 uploaded sources already
were, not a separate one.

**The `en-IN` voice is a labelled substitute, not a genuine Indian-English
voice.** Piper's public voice catalogue has no Indian-English model as of
this writing. Rather than pick an unrelated accent and let it pass as
correct, `PIPER_VOICE_EN_IN` defaults to the identical catalogue id as
`PIPER_VOICE_EN_GB` (`en_GB-cori-medium`) and `PIPER_EN_IN_IS_TEMPORARY_SUBSTITUTE`
records that explicitly - `domain.tts_voice.select_voice` returns the flag on
every voice selection, `script_segment_audio.is_temporary_voice_substitute`
persists it per segment, and both the API and the frontend surface it, so an
`en-IN` project's narration is always labelled for what it actually is.
Swapping in a genuine Indian-English (or other commercial) voice later only
means changing this module's mapping; every caller already goes through
`select_voice`, never a hand-picked model id.

**Licensing.** `piper-tts` is distributed under **GPL-3.0-or-later**. It is
fine for local development and for the test suite, but a GPL-licensed
dependency carries copyleft obligations that depend on how (and whether) the
resulting service is distributed - **this needs a company legal/licensing
review before any production or customer-facing distribution**, independent
of whether a genuine Indian-English voice is also added. This is not yet
reviewed or approved for production use.

**Pace, measured (2026-10-07).** At its trained settings `en_GB-cori-medium` leaves about 250 ms between sentences on its own
(measured: 120 ms or longer gaps between every pair of sentences) and speaks **163 words a minute on a real deck lesson** (339 words,
145 to 180 by section) and 132 on a longer-worded sample - the pace depends on the words, which is why a plan is an estimate that the
measured narration replaces. It is not the cause of the robotic sound, so it is not slowed down by default. What *can* be tuned without
a paid service is how it speaks: `PIPER_LENGTH_SCALE`, `PIPER_NOISE_SCALE` (pitch variation) and `PIPER_NOISE_W_SCALE` (rhythm
variation) - the trained values by default, and part of the narration's fingerprint (`providers.tts.engine_version`) when changed.
Four samples of the same lesson text in different styles are written to `var/samples/voice-sample-*.wav` for someone to **listen
to and choose**; nothing in this repository has been judged by ear. Whether another free voice sounds more natural, or more Indian, needs
the same listening (and a download, which was not done).

Settings (all optional, see `core/config.py`): `PIPER_VOICES_DIR` (where
`make voices` / `scripts/download_piper_voices.py` puts the downloaded
`.onnx`/`.onnx.json` files - never committed to git), `PIPER_VOICE_EN_GB`,
`PIPER_VOICE_EN_IN`, `PIPER_EN_IN_IS_TEMPORARY_SUBSTITUTE`,
`MAX_TTS_CHARS_PER_CALL` (one internal Piper call;
`domain.script_audio.chunk_narration_for_tts` splits a segment at sentence
boundaries under this and concatenates the chunks into one file),
`MAX_TTS_CHARS_PER_SEGMENT`, `MAX_TTS_CHARS_PER_SCRIPT`, `MAX_CONCURRENT_TTS`.

## Scene / Storyboard layer

Phase 9 turns an approved script whose narration is *fully measured* -
`scripts.measured_duration_ms IS NOT NULL`, the sole narration-completeness
gate, never `estimated_duration_ms` or the project's `target_duration_ms` -
into a deterministic, timed, templated scene timeline, ready for a future
rendering phase. Exactly one LLM call per script segment (never one per
scene, and never a new provider - `LLM.compose_storyboard_scenes` runs on the
same `providers.llm` client Phases 5/6 already use); the model never decides
timing, only which of six fixed templates fits each moment of the narration
and what goes in their slots.

**API surface** (`api/routers/storyboard.py`, prefix
`/v1/projects/{project_id}/scripts/{script_id}`, an approved script only):

- `POST /storyboard` - enqueues a `generate_storyboard` job (`202`,
  `Idempotency-Key` required, same asynchronous shape as script generation
  and synthesis) and returns the worst-case provider-call estimate alongside
  it, so the frontend can show it *before* the caller decides to generate.
- `GET /storyboard` - job status and progress, per-segment scene sets, a
  freshly computed completeness verdict, and both the estimated and (once a
  job has run) actual provider calls.

**Timing (`domain.storyboard_timing`) is exact, never rounded or
accumulated.** `effective_scene_count` bounds how many scenes a segment may
have from its own `script_segment_audio.measured_duration_ms` alone
(`min(MAX_SCENES_PER_SEGMENT, measured_duration_ms // MIN_SCENE_DURATION_MS)`) -
a segment too short for even one scene at the floor gets zero, deterministically,
with no provider call, never a scene squeezed under the minimum.
`allocate_scene_durations` is an exact `divmod` split of a segment's measured
duration across the scenes the model actually returned (which may be fewer
than the bound - "prefer fewer, well-chosen scenes... over padding to the
maximum"), remainder handed to the first scenes one millisecond at a time, so
`SUM(scene.duration_ms) == measured_audio_duration_ms` holds by construction.
`segment_absolute_starts` / `scene_timings` place every segment and scene
back to back by exact integer prefix sum - there is never a gap or an
overlap, and frame numbers are never computed here (`frame = round(t_ms *
fps / 1000)` belongs to a future rendering phase, rule 6).

**Six fixed templates (`domain.storyboard_template`), never a seventh:**
`title_card`, `bullet_points`, `labelled_diagram`, `split_text_illustration`,
`full_bleed_illustration`, `formula_or_code`. There is no separate "recap"
template - a recap is an ordinary `bullet_points` scene titled "Recap".
Three (`title_card`, `bullet_points`, `formula_or_code`) carry no
illustration slot and are fully renderable from text alone. An illustration
slot is `{"description": ..., "subject_labels": [...]}` and, since Phase 11, an
optional structured `visual` that the renderer draws itself (see "Educational
visuals"). No image is ever generated: the workspace summarises a visual in
words, or says "Shown as text - no visual", never "Illustration pending".
`content` is orientation-independent;
`layout_for(template, orientation)` is a deterministic, code-only lookup -
never persisted, and a vertical layout genuinely differs, structurally, from
its horizontal one for every template (mutation-tested: swapping a
template's two layouts is caught).

**Provenance is a safety guard, not fact verification
(`domain.storyboard_provenance`).** A number, date, percentage, unit-bearing
quantity or capitalised proper noun in a scene's text that is not also
present in the segment's own narration is a fabrication, and
`require_provenance` rejects the *whole* scene set it was found in - not
just the offending scene, since a partially-trusted set can never be told
apart from a fully-trusted one once persisted. Normalisation is fixed and
deliberately conservative (see the module docstring for exactly what it
catches and does not - it does not catch a number reworded as a word, a
converted unit, or a wrong fact built only from narration-grounded numbers
and names). A separate, non-rejecting soft signal
(`has_lexical_overlap`) flags a scene whose text shares no meaningful
content word with the narration, for a reviewer to look at - visible on
every scene in the `GET /storyboard` response, recomputed fresh on every
read, never persisted.

**Cost bound, enforced before any provider call, never merely
documented.** `POST /storyboard` rejects immediately (`400` `validation_error`,
no job created) if the script has more segments than
`MAX_STORYBOARD_SEGMENTS`, or if the *exact* total scene count computed from
every segment's own measured duration would exceed `MAX_SCENES_PER_SCRIPT` -
both already known precisely at enqueue time, since Phase 8 narration must
already be complete. With the defaults (`MAX_STORYBOARD_SEGMENTS=200`), a
storyboard generation makes at most 200 provider calls.

**Resumable and partial-failure-tolerant, like Phase 8's synthesis.** Each
segment's scene set is persisted the moment it finishes, in its own short
transaction (`repositories.script_segment_scenes.replace_for_segment` - a
whole-set DELETE + INSERT inside one `SAVEPOINT`, never a per-row `UPDATE`,
since a retry may change three scenes into five); one segment's failure
never discards another's success. `run_generate_storyboard_job` ends
`succeeded` unless every segment failed, and its `result` records
`ready_segment_count` / `failed_segment_ids` / `outcome` (`"complete"` or
`"succeeded_with_failures"`) / `provider_calls`. **Completeness is a
separate, always-freshly-computed verdict**
(`services.storyboard.get_storyboard_status`), never copied from a job's own
historical result - a storyboard can be `succeeded_with_failures` at the job
level while still reporting itself incomplete, and the frontend must gate a
"finished" timeline on completeness, never merely on job status. A segment's
scenes are stale exactly when their stored `source_audio_content_hash`
no longer matches the segment's *current* `script_segment_audio.content_hash`
(a re-synthesis) - re-running the job clears and regenerates only what is
missing, failed, or stale; an already-fresh segment is never touched again.

Settings (all optional, see `core/config.py`): `MIN_SCENE_DURATION_MS`,
`MAX_SCENES_PER_SEGMENT`, `MAX_SCENES_PER_SCRIPT`, `MAX_STORYBOARD_SEGMENTS`,
`STORYBOARD_STALE_AFTER_SECONDS`. The six templates and their per-slot
character limits are a fixed contract (`domain.storyboard_template`), not
configuration.

## Video rendering

Phase 10 turns an approved script - with fully measured narration and a complete
storyboard - into a real, seekable MP4: **H.264 High + AAC-LC, 30 fps**, at most 720p: 1280×720
(horizontal) or 720×1280 (vertical) at `high` quality, and half that size at `low`
(a review render). Rendering makes no provider call and needs no key: it reads what
Phases 8 and 9 stored and produces pictures and bytes. No image is generated. Since
Phase 11 a scene's diagram is drawn procedurally from its structured `visual` (see
"Educational visuals"); a scene without a valid one is drawn as deliberate text - numbered
parts, a statement card, or the text alone - so `labelled_diagram`, `split_text_illustration`
and `full_bleed_illustration` are as watchable as the three text-only templates.

### Which renderer, and why

| | Verdict |
| --- | --- |
| **Pillow scene compositor → FFmpeg (chosen)** | Own code, no per-render licence, deterministic (a pure function of its inputs), fast for six fixed text-and-shape templates, one language, chunking and partial re-rendering are trivial because we own the pipeline. |
| Remotion | Not used. Its licence is free only for individuals, non-profits and for-profit organisations of up to three people; larger companies need a paid Company License, with automated rendering (a SaaS render pipeline) priced per render. Whether a chunk counts as "a render" is not something we could confirm from its published terms, so it is a real cost risk for a product that renders one chunk per scene. Kept as the documented fallback if the company accepts a licence. |
| Headless Chromium + FFmpeg | Free (Apache-2.0) but slow per frame and fragile to make deterministic (fonts, timing); would re-implement what the compositor does. Second fallback. |
| Motion Canvas | MIT, but built for authoring in an editor, not for server-side chunked rendering. |

The engine sits behind `providers.renderer.Renderer` (`render_chunk`, `assemble`,
`probe`): nothing outside `providers/renderer` names Pillow, FFmpeg or a command line, so a
different engine replaces one package. Pillow and NumPy are confined to it by the
architecture test (rule 3), like every other SDK.

**Fonts** are vendored (`providers/renderer/assets/fonts`: Noto Sans and Noto Sans Mono, SIL Open
Font Licence, licence text alongside) so a frame is identical on every machine.

### The hierarchy, and what invalidates what

```
project → topic → segment → scene → chunk → final video
```

A topic is the set of segments written for it, so it needs no artifact of its own; **one scene
is one render chunk**. A chunk is *video only* and is stored under a fingerprint
(`chunk_hash`, `domain.render.chunk_fingerprint`) of exactly what decides its bytes:

`renderer version · template · content · duration · orientation · output size · quality's
encoding profile · encoder · fps · how it joins its neighbours (fade in / out, 0 for a cut) ·
visual drawing version (illustrated templates only)`

and nothing else - not its position, its absolute start, or the narration. The finished video
has its own fingerprint (`output_hash`) over the ordered chunk fingerprints and the narration.

| Change | Chunks re-rendered | Narration | Audio track / final video |
| --- | --- | --- | --- |
| Edit one scene's content | that scene | untouched | rebuilt (cheap) |
| Re-narrate one segment (text or voice) | that segment's scenes (their durations change) | that segment only | rebuilt |
| Edit every scene of a topic | those scenes | untouched | rebuilt |
| Orientation changes | all (a different shape) | untouched | rebuilt, separate output |
| Quality changes | all (a different size/encoding) | untouched | rebuilt, separate output |
| Renderer / encoder / fps changes | all | untouched | rebuilt |
| Nothing | none | untouched | none - the stored video is returned |

A later segment merely *moving* (because an earlier one got longer) changes no fingerprint, so
its chunks are reused. The dependency test is always a hash comparison, never a timestamp.

### Timing

Milliseconds stay canonical (Phase 9); frames are derived and never stored as truth.
`frame = round(t_ms × fps / 1000)` (`domain.render.frame_at`, integer half-up). A chunk's
frames are the difference of two *absolute* frames, `frame_at(end_ms) − frame_at(start_ms)`,
so they telescope to exactly `frame_at(total_ms)` - no drift, however many scenes. Because that
difference depends on the sub-frame phase of the start, a chunk is always *rendered* with
`ceil(duration × fps / 1000)` frames and assembly keeps one fewer where the absolute rule needs
it, by cutting half a frame before the last one in the concat list (`outpoint`) - stream copy,
no re-encode, because chunks are encoded without B-frames. A chunk therefore does not depend on
its phase, which is what lets an upstream duration change leave later chunks reusable.

The timeline comes from the *current* measured audio durations (an exact prefix sum), not the
scenes' stored `start_ms`: re-narrating an early segment leaves later segments' stored offsets
out of date (Phase 9 does not rewrite fresh segments), and rendering must not care (the storyboard read
model now computes the same prefix sum on read - "Timings on read"). A segment
too short for a scene of its own (Phase 9's degenerate case) is covered by the previous scene,
so the picture never has a gap while that narration plays.

Narration is assembled as one mono 16-bit WAV in which each segment is fitted to the samples
between its own absolute start and the next segment's start (`sample_at`, the same half-up rule),
so audio is placed sample-exactly too and its error never accumulates.

### Glyph safety (Phase 11)

The bundled Noto Sans has no glyph for `→ ← ≤ ≥ ≈ ≠ √ ✓` and many other symbols; before Phase 11
they were drawn as the font's missing-glyph box in every template. `domain/text_glyphs.py`
now plans each line into runs (its coverage tables are the two bundled fonts' own cmap ranges,
guarded by `tests/test_text_glyphs.py`): text the template's font has is drawn exactly as before
(one `draw.text` call, pixel-identical); a symbol only the *other* bundled font has (Noto Sans
Mono has the arrows, comparison and root signs, geometric shapes) is drawn from it at the same
baseline; `✓ ✔` and `✗ ✘ ✕ ✖` are drawn as small shapes; anything nothing can draw (emoji, control,
zero-width, bidi and private-use characters) is dropped, never drawn as a box. No icon font or
other asset was added.

This changed pixels of existing text templates, so `RENDERER_VERSION` went from
`pillow-ffmpeg/2` to `pillow-ffmpeg/3`. **That intentionally invalidates every previously
cached chunk once** (text templates included): the next render re-renders them instead of
reusing chunks that may hold missing-glyph boxes. `tests/test_domain_render.py` pins the
fingerprints of the text-only templates so a later change cannot silently do it again.

### Educational visuals (Phase 11)

An illustrated scene (`labelled_diagram`, `split_text_illustration`, `full_bleed_illustration`) may carry
`content.illustration.visual`: a small, closed, structured description of a diagram that the compositor
**draws itself** with Pillow - no image is generated, nothing is stored beyond the scene's own JSON, and there is
no new table, bucket or rendering framework. Five kinds (`domain/visual.py`), each a fixed shape:

| Kind | Shape and limits |
| --- | --- |
| `process_flow` | 2 to 6 ordered `nodes` (`label`, optional `detail`), optional `focus` (an index) |
| `cycle` | 3 to 6 `nodes` drawn as a loop that returns to the first |
| `hierarchy` | 2 to 9 `nodes`, each with `parent` (an index; the first has none), at most 3 levels and 5 per level |
| `comparison` | `relation` `versus` or `before_after` (exactly 2 columns), 2 to 3 `columns` of a `heading` and 2 to 4 `points` |
| `labelled_components` | `form` `hub` or `layers`, a `subject` and 2 to 6 `parts` |

Text limits: label 28, detail 60, title 60, heading 28, point 40, subject 28 characters; 400 characters of text in
all and 2 048 serialised bytes per visual. `labelled_diagram` and `full_bleed_illustration` give a visual the whole
frame; `split_text_illustration` gives it half, so it holds fewer (at most 4 steps, cycle nodes or parts, 5 hierarchy
nodes, 2 columns of 3 points) - a bigger one is dropped at storyboard time, where it is counted, not discovered at
render time. The schema is **closed** - an unknown key is rejected, never ignored; control, format, bidi and
private-use characters are rejected. `visual_dropped` is a mark only the server sets (dropping a visual discards any
such flag the model supplied and writes its own; a visual together with the flag is rejected). There is deliberately no
`value_chart`, map, photograph, anatomy or apparatus kind: a chart would put numbers on screen, and every number on
screen must come from the narration.

**What the model is told.** `LLM.compose_storyboard_scenes` embeds a JSON schema for the visual in its tool
(`domain/visual_schema.py`), generated from the same constants the runtime validator reads, so a limit cannot be
changed in one place. `tests/test_visual_schema.py` proves *schema-valid ⇔ validator-accepts* over a corpus of
structural cases (with its own small JSON-schema checker that raises on any keyword it does not understand). Rules a
schema cannot express - a hierarchy's `parent` must point at an earlier node, a `before_after` has exactly two
columns, a scene's total text budget - are stated to the model in the schema's description and enforced only by the
validator; the test documents that gap rather than hiding it. Output that fails is dropped (below), never coerced.

**Provenance is unchanged in principle and stricter in reach** (`domain.storyboard_provenance`). Every string leaf
of a visual - however deeply nested - is checked like any other scene text; every numeric JSON leaf is checked
against the narration too (so a number cannot be smuggled in as a value rather than a string); only the structural
integers `parent` and `focus`, and only inside a `visual`, are exempt. The *raw* content is checked before an invalid
visual is dropped, so a fabricated number inside a visual that is about to be dropped is still a hard failure of that
segment's scene set.

**An invalid visual never fails a segment, and is never silent.** A visual that does not parse, or does not fit its
template's region, is removed from the scene; the scene keeps its other content, is marked
`illustration.visual_dropped`, and is drawn by the template's deterministic fallback. `GET .../storyboard` returns
`dropped_visual_count` (counted fresh from the stored scenes, so it cannot drift from what will be drawn) and the job's
result records the same figure; the workspace says "N visuals fell back to text because their visual specifications
were invalid." and shows no raw JSON. The log line `storyboard.visual_dropped` names the field and the rule, never
the text. `labelled_diagram`'s `labels` are optional when a valid visual exists (existing scenes with labels stay
valid); the template is described in the workspace as "labelled parts".

**Layout is pure and exact** (`domain/visual_layout.py`, no I/O, no Pillow): integer geometry on a `Units` scale, a
`TextMeasurer` injected by the renderer (`providers/renderer/visual_text.py`, with 6% headroom), two phases (measure
every text block, then place), and a `LayoutOverflow` when nothing fits, which the compositor answers with the
fallback rather than a clipped picture. Font size steps down a fixed ladder (`FONT_PERCENTS`) to its role's minimum
before giving up. Rings use a fixed-point table (`RING`), never `sin`/`cos`, so a cycle is identical on every machine.
**Horizontal and vertical are different designs, not one layout scaled:**

| Kind | Horizontal (16:9) | Vertical (9:16) |
| --- | --- | --- |
| `process_flow` | a row of boxes joined left to right (wraps to fewer columns when narrow) | a column joined top to bottom |
| `cycle` | a ring with arrowed arcs | a column with a return arrow up the side |
| `hierarchy` | a tree, parents above children | an indented outline with elbow connectors |
| `comparison` | columns side by side (a VS disc between two) | columns stacked as bands |
| `labelled_components` | `hub`: the subject in the middle, parts around it; `layers`: stacked bands | the subject above its parts |

`check_layout` runs on every candidate layout (at render time as well as in the tests) and asserts what a reader
relies on: nothing overlaps, everything is inside the safe area, no connector crosses a box's interior, text fits its
box, type never falls below a floor; a candidate that fails is discarded and the next size tried. Mutation-checked:
swapping either orientation of any kind is caught.

**Painting** (`providers/renderer/visual_primitives.py`, `visual_scene.py`): rounded boxes, number markers, dividers and
connectors with arrowheads, all built from integer fixed-point geometry and **tight-cropped** to their own bounding box
(a full-canvas connector layer cost 182 ms per frame in Stage A; a tight one 7.6 ms). Reveals are progressive and
deterministic - the title, then each item in reading order, a straight connector drawing as a wipe along its path and a bent one fading in - with no
bounce, pulse, zoom, rotation or blur, and **no attempt to sync to speech** (Phase 8 stores no word or sentence timings,
so a reveal is paced across the scene, never claimed to follow the voice).

**Fallbacks are deliberate, not placeholders.** With no valid visual: `labelled_diagram` draws its `labels` as a
numbered list of parts (their positions are not used, and no figure is implied), under its subject label if it has one -
and, with no labels at all, the statement card below; `full_bleed_illustration` draws a strong statement card from its
caption (else its description), the subject labels beneath; `split_text_illustration` gives the whole width to its
text. None implies artwork, and none is an empty panel. The old "ILLUSTRATION" panel is gone.

**What invalidates what.** A scene's chunk fingerprint carries `visual`: an identifier built from
`VISUAL_FRAMEWORK_VERSION`, the kind's own version in `VISUAL_TYPE_VERSIONS` (else the template's
`FALLBACK_VERSIONS`), and the fallback version - so editing one visual re-renders that scene alone, a change to how
one kind is drawn re-renders only scenes of that kind, and the three text-only templates carry none of it. A
golden-pixel test (`tests/test_renderer_visuals.py`) fails if drawing changes without a version bump. Orientation,
quality, duration, content and renderer version are in the fingerprint as before. **The Pillow and FreeType library
versions are deliberately not in it** (a documented gap, unchanged from Phase 10): a library upgrade that changes
glyph rasterisation would not, by itself, invalidate cached chunks; bump `RENDERER_VERSION` when upgrading them.

### Transitions and pacing (Phase 11)

Phase 10 faded every scene in over 450 ms and out over 300 ms, so a long video spent a noticeable share of its frames
on a blank or dim screen. Now **scenes of the same topic are joined by a straight cut, and a scene fades only where its
topic begins or ends** (and at the start and end of the video). A topic is `script_segments.topic_id`; a script with no
topics is one group. The fades are shorter (`RENDER_FADE_IN_MS` 240, `RENDER_FADE_OUT_MS` 200; each 0 to 1 500, and 0 is a
straight cut everywhere). A scene that cuts in has its progressive reveal shifted so its first item is already on screen at
its first frame - a cut must show content, not a bare title. The two fade lengths enter the fingerprint of the scenes
that use them (a cut edge counts as 0), so retuning a fade re-renders only the scenes at topic edges, and the timeline is
untouched: fades happen *inside* a scene's own frames, never lengthening or overlapping it, so no drift is possible.

### Timings on read (Phase 11)

A scene row's stored `start_ms` goes stale when an earlier segment is re-narrated (Phase 9 does not rewrite fresh segments).
`GET .../storyboard` no longer returns it: `start_ms` is an integer **computed on read** from the current ready audio
durations of the preceding segments plus the scene's offset within its segment - the same prefix sum the renderer uses
(`domain.storyboard_timing.timeline_positions`), never persisted. The response also carries `timings_authoritative`: true
when every input is final; false while narration is stale, being made or failed, in which case the workspace shows the
one banner "Timings update when narration finishes." and keeps its per-scene stale badges.

### Encoding (decided from measurements, `scripts/verify_phase10.py`)

| | `high` | `low` |
| --- | --- | --- |
| Size | 1280×720 / 720×1280 | 640×360 / 360×640 |
| H.264 | High profile, **level 3.1**, `yuv420p`, BT.709 limited range, CRF 20, preset `medium` | High, **level 3.0**, CRF 30, `veryfast` |
| GOP / B-frames | 2 seconds = 60 frames, none (a chunk's last frame must be removable without re-encoding) | same |
| x264 tuning | `scenecut=0:threads=4:rc-lookahead=10` | same |
| Audio | AAC-LC, mono, 22 050 Hz (the narration's own format - never widened to stereo / 48 kHz), 96 kb/s target (≈ 109 kb/s achieved) | 64 kb/s target |
| Container | MP4, `+faststart` (index first: plays and seeks before it has downloaded) | same |

The encoder is a setting (`RENDER_VIDEO_ENCODER`: `libx264` default, `h264_nvenc`); it is part of
every fingerprint, so switching re-renders rather than mixing two encoders in one video. NVENC
works on this machine's RTX 3050 Ti but is not faster (the compositor, not the encoder, is the
bottleneck - 190 fps vs 220 fps for libx264 on a 1080p chunk) and produced 66% larger files
(138 KB vs 83 KB for the same chunk), so `libx264` stays the default.

**720p at 30 fps** (the output specification: at most 720p; it was 1080p at 60 fps from the pre-API pass
to 2026-10-07, and 1080p at 30 before that - `RENDERER_VERSION` is `pillow-ffmpeg/5`). Level 3.1 / 3.0 are
the lowest valid levels for 720p and 360p at 30 fps, derived from the frame size rather than trusted
(`domain.render.minimum_level_idc`); the 1080p/60 figures in the tables below are history. Slides, fades and slide-ins are smoother, and web/phone playback
is frame-rate agnostic. Phase 9 stores no frame rate; only the render configuration does.

x264's default thread count (one frame-thread per logical core, 20 here) held ~2 GB of frames in
flight per render and was no faster; limiting it to four cut peak memory by about two thirds
(measured: 2 concurrent 1080p chunks, 2.1 GB → 0.7 GB) and was slightly quicker.

### Storage

Chunks live in `BUCKET_SEGMENTS` at `projects/{project}/renders/chunks/{orientation}-{quality}/{chunk_hash}.mp4`;
finished videos in `BUCKET_OUTPUTS` at `projects/{project}/renders/{script}/{orientation}-{quality}.mp4`. Keys are
built from the project id and validated values only, and every key read back from a row is checked
to lie inside the project's own prefix before it is used (`domain.source_storage.belongs_to_project`).
No video bytes are in Postgres: `render_chunks` (a content-addressed cache, unique per
`(project, chunk_hash)`), `render_outputs` (at most one current video per `(script, orientation,
quality)`) and `render_output_scenes` (one small row per scene, recording which chunk the video
used - how a single edited scene shows as *stale* without marking the whole video stale) only name
objects. A chunk row is inserted after its upload, so the row is the commit: a crash in between
leaves an orphan object at a deterministic key that the retry overwrites, never a row pointing
at nothing. After a successful render, chunks of that configuration that no video uses and the
new plan does not need are removed. Deleting a project removes every chunk and video object
(`services.projects.delete_project` gathers them from the rows before the cascade, then
`remove_stored_files` deletes them exactly as it does uploads and narration audio).

### The job

`POST /v1/projects/{id}/scripts/{script_id}/render` (`orientation`, `quality` - both required, unknown
fields are a 422) answers `202` with a `render_video` job and an **estimate** (scenes to render vs reusable,
video length, expected size). It refuses, before any work and with a message that says what to do:
a script that is not approved (409); narration missing, stale or in another format; a storyboard
that is incomplete or stale; scenes whose timings do not tile their segment; a video longer than
`MAX_RENDER_DURATION_MS` (30 minutes today - the design does not depend on the number); another render
already queued or running for the project (409); or too little free disk space in `RENDER_WORK_DIR`
(507; checked again by the worker). It needs an `Idempotency-Key` (1–255 characters).

The worker (1) plans the render and checks every stored chunk is real - its row exists, its key is in
the project's prefix, its object exists with the recorded size (a missing or damaged one is forgotten and
re-rendered); (2) renders the missing chunks (`RENDER_CONCURRENCY`, default 2 at a time), each uploaded and
recorded in its own short transaction the moment it finishes; (3) only when every chunk exists, downloads
them, assembles the narration and the video, **verifies the result with `ffprobe`** (codecs, profile and level
- including that the level is valid for the pictures it holds - size, 60 fps and constant frame rate,
exact frame count, both streams' durations within 120 ms of the narration, `+faststart`) and publishes it.
A chunk that fails does not stop the others or discard finished work: the job ends `failed` with
"*N* render section(s) need another attempt" and `result.failed_chunk_hashes`, and **Retry**
(`POST /v1/projects/{id}/render/retry`) renders only those. A failed assembly keeps every chunk and a retry
only re-assembles. A job left `running` by a dead worker is failed after `RENDER_STALE_AFTER_SECONDS` and the
next render resumes from the chunks that were committed. If nothing has changed since the stored video was
made and it is still in storage, the job returns it without doing any work.

Progress is written through independent short sessions, never the job's own transaction, and is real:
`percent` counts chunks rendered plus one unit for assembly, `eta_seconds` appears only once two chunks have
finished, and `segment_index`/`scene_index` name the chunk being drawn. `GET .../render/progress` is one cheap
query for polling; `GET .../scripts/{id}/render?orientation=&quality=` is the full per-scene status (`ready`,
`needs_render`, `stale`, `failed`), computed fresh from fingerprints - completeness is never copied from a job's
result - so a partly rendered video can never read as finished.

`GET .../render/video?orientation=&quality=[&download=true]` streams the MP4 with HTTP `Range` support
(`206`/`416`, `Accept-Ranges`) in 1 MiB reads from object storage; the video is never held in memory. A project or
script the caller does not own is a 404 on every route.

### Measured on this machine (i7-12700H, 14 cores / 20 threads, 16 GB, RTX 3050 Ti unused by libx264)

> **These Phase 10 and Phase 11 tables were measured at 30 fps**, before the pre-API pass moved production to 60 fps. They
> stay as history of how the design scales; today's numbers are in "Measured at 60 fps" below.

A real lesson - 48 segments narrated by Piper (576 s of audio), a 120-scene storyboard using all six templates
(scene text from a deterministic stand-in for the composer, through the real storyboard job) -
`uv run python scripts/verify_phase10.py --minutes 10`. **MEASURED:**

| Render | Time | Output | Bitrate | Peak RAM (worker process + FFmpeg) | Machine CPU (mean) |
| --- | --- | --- | --- | --- | --- |
| horizontal `high` 1920×1080 | 102.7 s (0.18× real time) | 30.5 MB | 423 kb/s | 1.22 GB (+0.73 GB over the worker's idle 0.49 GB) | 54% |
| vertical `high` 1080×1920 | 101.9 s | 30.3 MB | 421 kb/s | 1.30 GB | 54% |
| horizontal `low` 960×540 | 31.0 s (0.05×) | 10.7 MB | 149 kb/s | 0.94 GB | 38% |
| vertical `low` 540×960 | 30.7 s | 10.9 MB | 151 kb/s | 1.02 GB | 42% |

Assembly (stream copy + AAC + `+faststart`) took 3.8 s at `high`; GPU utilisation 0% (libx264). Peak memory is
flat in video length (2.5 min: 1.10 GB; 10 min: 1.22 GB) because chunks are rendered, uploaded and discarded
one at a time. Every output: 17 292 frames = `frame_at(576 415 ms)` exactly, packet timestamps all exactly 1/30 s apart
(no gap, no overlap), video 15 ms shorter than the narration (one frame of quantisation) and audio 0.0 ms different,
keyframes at every chunk start, decodes with no errors, horizontal and vertical audio byte-identical.

Changing one segment's narration and going through the real product path (synthesise → storyboard → render):
1 of 48 audio rows changed, 1 storyboard call, **2 of 120 scenes re-rendered in 6.8 s**, 118 chunks reused; an unchanged
re-render did no work (0.22 s). A worker killed after 6 committed chunks resumed by drawing the other 9; one failed chunk
was retried alone (1 drawn, 14 reused); range reads matched the stored bytes.

**PROJECTED** (linear in length from the rates above - 10.7 s of rendering, 3.17 MB and 12.2 chunks per video minute at
`high`; not run at these lengths): 10 min ≈ 107 s, 32 MB, 122 chunks; 30 min ≈ 5.4 min, 95 MB, 366 chunks; 60 min ≈ 10.7 min,
190 MB, 732 chunks. Scratch space during a render is about twice the video plus the WAV; peak RAM is not expected to grow.
Size depends on content: illustration placeholders and text compress far better than photographs will.

### Phase 11 measurements (`scripts/verify_phase11.py`, `scripts/mutation_check_phase11.py`)

Same machine and method as above: real Piper narration, the real storyboard job, the real compositor and FFmpeg. The
only stand-in is the storyboard *composer* (no LLM key exists): a deterministic one that emits every visual kind
(process flow, cycle, hierarchy, comparison, labelled parts as a hub and as layers), the three text-only templates,
three text fallbacks, scenes with `→ ≤ ✓ ₂`, and two deliberately invalid visuals - so validation, the drop,
provenance, timing, planning, rendering and assembly are all the real code. `uv run python scripts/verify_phase11.py`
(2.5 minutes, every check) and `--minutes 10 --perf-only` (the 10 minute cap). **MEASURED:**

| Project | Render | Time | Output | Frames (found / expected) | Peak RAM (worker + FFmpeg) |
| --- | --- | --- | --- | --- | --- |
| 2.4 min, 30 scenes, 4 topics | horizontal `high` | 20.5 s | 5.2 MB | 4 324 / 4 324 | 1.05 GB |
| | vertical `high` | 21.0 s | 5.2 MB | 4 324 / 4 324 | 1.11 GB |
| | horizontal `low` / vertical `low` | 9.0 s / 9.0 s | 2.1 MB / 2.2 MB | 4 324 / 4 324 | 0.74 / 0.78 GB |
| 9.6 min, 120 scenes (117 distinct chunks), 14 dropped visuals | horizontal `high` | 98.9 s (10.3 s per video minute) | 21.1 MB | 17 344 / 17 344 | 1.15 GB (+0.66 GB over the worker's idle) |
| | vertical `high` | 98.9 s | 21.0 MB | 17 344 / 17 344 | 1.26 GB |

Every output decodes with no errors, has packet timestamps all exactly one frame apart, a video 6 to 8 ms from the
narration (the half-up frame rounding) and audio 0.0 ms from it; horizontal and vertical audio are byte-identical; there
are no gaps or overlaps between scenes and the last one ends at the narration's end. The 10 minute run's assembly took
4.1 s, and deleting the project removed all 236 chunk and video objects.

Against the Phase 10 baseline on this machine (10 minute horizontal `high`: 102.7 s, 1.22 GB): **-3.8 s and -72 MB**. That
is *not* a controlled comparison - Phase 10's scenes were text panels with a placeholder, these are drawn diagrams, and
fewer frames are faded - so the honest reading is "no measurable regression", not "faster". Drawing cost per 6 second
scene at 1080p, one thread, no FFmpeg (`visual_costs_MEASURED` in the report): setup 26 to 207 ms; 19 to 43% of frames
are distinct (a static frame is reused); **5.5 to 7.5 ms per distinct frame for every visual kind, against 6.4 to 10.3 ms
for the text templates** - a diagram is not dearer to draw than a text panel.

| Check (2.4 min project) | Result |
| --- | --- |
| Repeat render after deleting every chunk and video | byte-identical (same SHA-256), 30 chunks redrawn |
| Nothing changed | 0 chunks drawn, the stored video returned |
| Edit one visual's title | 1 of 30 chunks re-rendered, 29 reused |
| Re-narrate one segment (real Piper) | 1 audio row changed, 1 composer call, 2 of 30 chunks re-rendered, 28 reused; the read model showed the *current* start of the next segment (88 096 ms, stored value 85 194) with `timings_authoritative: false`, then `true` after regeneration |
| Dropped visuals | 2 of 2 counted, storyboard still `complete`, both drawn as deliberate text |
| Same-topic cuts (26, measured on the encoded video) | the outgoing scene's last frame is at 0.99 to 1.03 of its steady content; the incoming scene's first frame is already at 0.17 to 1.02 (a progressive reveal, never a bare background) |
| Topic boundaries (4) | last frame of the outgoing scene at 0.14 to 0.25 of steady content (dimming out); first frame of the incoming one at 0.0 (bare background, fading in) |
| Glyphs `→ ≤ ✓ ₂` in a visual and in a formula | drawn, not boxes (frames saved as `frame-glyphs_*.png`) |

`scripts/mutation_check_phase11.py` applies 41 deliberate faults (the drawing version or visual id left out of the
fingerprint, either orientation swapped for any of the five kinds, a node/label/text limit relaxed, unknown keys or
invalid characters accepted, the LLM schema hard-coding a limit, nested or numeric text bypassing provenance, a
prefix-sum drift, the read model showing the stale `start_ms`, `timings_authoritative` or `dropped_visual_count` wrong,
a fade applied inside a topic or removed at a boundary, and more): **41 killed, 0 survived**; the Phase 10 script's 26
are killed again.

### Measured at 60 fps (pre-API pass, `scripts/verify_phase11.py`)

Same machine, method and stand-ins as the two sections above (real Piper narration, the real storyboard job, the real
compositor and FFmpeg; the storyboard *composer* is the deterministic stand-in because no LLM key exists), re-run after the
frame rate, H.264 level and GOP changed. `uv run python scripts/verify_phase11.py` (2.5 minutes, 20 checks: **20 of 20
passed**) and `--minutes 10 --perf-only` (**8 of 8 passed**). **MEASURED:**

| Project | Render | Time | Output | Frames (found / expected) | Peak RAM (worker + FFmpeg) | Machine CPU (mean) |
| --- | --- | --- | --- | --- | --- | --- |
| 2.4 min, 30 scenes, 4 topics | horizontal `high` 1920x1080 | 39.4 s | 5.60 MB | 8 656 / 8 656 | 1.07 GB | 66% |
| | vertical `high` 1080x1920 | 39.6 s | 5.57 MB | 8 656 / 8 656 | 1.10 GB | 71% |
| | horizontal `low` 960x540 | 11.7 s | 2.21 MB | 8 656 / 8 656 | 0.75 GB | 56% |
| | vertical `low` 540x960 | 11.4 s | 2.25 MB | 8 656 / 8 656 | 0.79 GB | 53% |
| 9.7 min (581.98 s), 120 scenes, 14 dropped visuals | horizontal `high` | 149.2 s (15.4 s per video minute, 0.26x real time) | 22.84 MB | 34 919 / 34 919 | 1.21 GB (+0.69 GB over the worker's idle) | 67% |
| | vertical `high` | 149.7 s | 22.62 MB | 34 919 / 34 919 | 1.26 GB | 66% |

`ffprobe` on the 9.7 minute horizontal file: H.264 **High, level 4.2**, 1920x1080, `yuv420p`, BT.709 limited range, no
B-frames, `r_frame_rate` = `avg_frame_rate` = **60/1** (constant), time base 1/60000, 34 919 frames over 581.983 s; AAC-LC
mono 22 050 Hz, 581.977 s. **All 34 918 packet timestamps, presentation and decode, are exactly 1 000 ticks (1/60 s)
apart** - no gap, no overlap - and there are 384 keyframes (a GOP is 2 seconds, and a chunk starts on one). The vertical file
is identical in every field but the size. Both decode end to end with no errors. Video is 6.3 ms longer than the narration
(the half-up frame rounding); audio is 0.0 ms different; horizontal and vertical audio are byte-identical. 34 919 is
`round(581 977 x 60 / 1000)`, as the absolute-frame rule requires.

| Check (2.4 min project) | Result |
| --- | --- |
| Every visual form (process flow, cycle, hierarchy, comparison, labelled parts as hub and layers) and all six templates | present in both orientations; the three text fallbacks and the glyph scenes (`→ ≤ ✓`) drawn, none blank |
| Repeat render after deleting every chunk and video | byte-identical (same SHA-256), 30 chunks redrawn |
| Nothing changed | 0 chunks drawn, the stored video returned |
| Edit one visual's title | 1 of 30 chunks re-rendered, 29 reused |
| Re-narrate one segment (real Piper) | 1 composer call, 2 of 30 chunks re-rendered, 28 reused; the new video is frame-exact (8 844 / 8 844); the read model showed the *current* start of the next segment with `timings_authoritative: false`, then `true` after regeneration |
| Same-topic cuts (26) and topic boundaries (4) | straight cuts inside a topic (outgoing last frame at 1.0 of steady content); fades to and from the bare background at the four boundaries and the video's ends |

What the numbers say. **Sixty frames a second costs time, not memory or much size**: the render took about 45% longer than
the 30 fps run of the same length (149 s against 103 s; not a controlled comparison - the scenes differ - but the direction is
the extra frames to encode); peak memory is flat (1.21 GB against 1.22 GB) because chunks are still rendered, uploaded and
discarded one at a time; and the file came out *smaller* (22.8 MB against 30.5 MB; again different scenes) - a static frame costs almost nothing to
encode, and most frames are static. Projected linearly, and **not run at these lengths**: 30 minutes is about 7.7 minutes of rendering
and 71 MB, 60 minutes about 15 minutes and 142 MB. The frame counts for 1, 5, 10, 30 and 60 minutes (3 600, 18 000, 36 000,
108 000, 216 000) are held by unit tests of the frame arithmetic, not by renders. The disk-space estimate
(`ESTIMATED_VIDEO_BYTES_PER_SECOND`) still sits above the measured 39 KB/s at `high` (estimate 80) and 15 KB/s at `low` (estimate 27), so it
was left as it was: over-estimating is the safe direction for a free-space check.

**Not measured:** GPU use (libx264 does not use it; it was not sampled in this run, and Phase 10 sampled 0%); a render at
exactly the 10:00 ceiling (the longest was 9.7 minutes, 581 977 ms); any render longer than that; `h264_nvenc` at 60 fps.

### Measured at 720p / 30 fps (2026-10-07, PowerPoint decks, the real stack, no provider key)

Real API, real worker, Piper, the Pillow compositor and FFmpeg on the development machine (i7-12700H, libx264). Every file
was checked by the render job's own `ffprobe` gate before it was published: H.264 **High, level 3.1**, 1280x720 `yuv420p`,
constant 30/1, `+faststart`, AAC mono 22.05 kHz, video frames exactly `round(narration_ms * 30 / 1000)`.

| Lesson | Narration (measured) | Narrating | Storyboard | Render | File | Frames |
| --- | --- | --- | --- | --- | --- | --- |
| sample induction deck, 9 sections | 125.0 s | 9.1 s | 0.2 s | 9.8 s | 4.3 MB | 3 750 / 3 750 |
| synthetic deck, 72 sections (nothing dropped) | 25.2 min (1 510 202 ms) | 98.9 s | 6.1 s | 99.0 s | 62.9 MB | 45 306 / 45 306 |
| synthetic deck asked for 30:00, 82 sections | 28.8 min (1 730 747 ms) | 82.6 s | 4.1 s | 105.0 s | 71.1 MB | 51 922 / 51 922 |

Video and audio durations agreed to within 14 ms in every file. Not measured this time: peak memory, vertical or `low` renders at length,
a render at exactly 30:00 (the ceiling check and the frame arithmetic - unit-tested to 60 minutes - allow it).

### Not decided / not verified

- **H.264 and AAC patent licensing, and distributing an FFmpeg build that contains libx264 (GPL),** are business/legal decisions.
  Choosing `h264_nvenc` does not solve them. Local development uses the GPL "full" FFmpeg build (winget `Gyan.FFmpeg`).
- The scene text used above came from a stand-in for `LLM.compose_storyboard_scenes`; real composer output is unverified (README, "Testing").
- **Real LLM visual generation is unverified** - the tool schema, the prompt and every dropped/kept decision are
  exercised only against a stand-in. How often a real model emits a valid, useful visual, and how often it falls back
  to text, is unknown until company keys exist.
- **The procedural vocabulary is small on purpose**: five shapes, at most 9 nodes, no charts, maps, photographs,
  anatomy or apparatus. A concept outside them is drawn as text, and a busy or long-worded visual can overflow its
  region and fall back. There is no speech sync: reveals are paced across the scene.
- **The Pillow and FreeType library versions are not part of the fingerprint** (intentionally left open, as in Phase 10):
  after upgrading either, bump `RENDERER_VERSION` or cached chunks may keep the old glyph rasterisation.
- The transition measurements compare frames of one encoded video with its own bare first frame; they show fades and
  cuts happen where designed, not that the pacing *feels* right to a learner - that needs people, not a script.
- The web player fetches the video into a `blob:` URL (the same way narration audio is played) so it can send the auth
  header; the API streams and honours `Range`, but the browser holds the whole file. A proxy route would give true streaming
  for much longer videos.
- Formula/code scenes draw the text as given, in a monospace font; there is no LaTeX typesetting.
- A chunk whose upload succeeds but whose row cannot be written (the database went away at that moment, which
  `scripts/verify_phase10.py --db-restart` provokes on purpose) leaves an orphan object at its deterministic key: the retry
  overwrites it if the same chunk is rendered again, but deleting the project cannot remove it, because no row names it.
  The same is true of an upload racing a project deletion.
- Rendered on Windows only; the Linux container path (FFmpeg on PATH, the vendored fonts) is unverified.

## Reliability and safety before the first real-provider run

A pass made after Phase 11 and before any real API key is used, from an audit of the code. It adds
no feature: it makes what exists fail safely, cost visibly and say honestly what it cannot do yet.
Nothing here has been run against a real provider - every provider call in its tests is a stand-in
(`pytest.mark.provider_mocked`). Each item says what is **implemented** (in the code, covered by
tests), what was **verified locally** (run for real on this machine), and what stays **unverified
against a real provider**.

**Before any real testing, set a monthly spend limit in the OpenAI and Anthropic consoles.** The
application has no billing and does not try to reproduce the providers'; the in-process guards below
stop a runaway, not a budget.

### 60 fps, and an H.264 level that is valid for it

> **Superseded 2026-10-07.** The output specification is now at most 720p at 30 fps (`pillow-ffmpeg/5`; see "Encoding"). The
> reasoning about levels below is unchanged and is what the 720p/30 levels were derived with; the 60 fps figures are history.

The production frame rate is a constant **60 fps** (`domain.render.FPS`; it was 30). The change is not
the constant: an H.264 stream signals a *level* that bounds its macroblock rate, and a decoder may
refuse a stream whose level is too low for its pictures. 1080p at 60 fps is 489 600 macroblocks a
second; level 4.0 and 4.1 allow 245 760 (enough for 30 fps, not 60), level 4.2 allows 522 240. So `high`
now signals **4.2** and `low` (960×540, 122 400 a second) **3.2**. `domain.render.minimum_level_idc`
derives the lowest valid level from ITU-T H.264 Annex A Table A-1, a test holds each shipped profile
to exactly that level for both orientations, `build_render_plan` refuses a profile whose level is too
low for the frame size at the rate, and `check_output` verifies the finished file's own profile,
level and constant frame rate - and that the level it signals is valid for the pictures it *holds*, so
a falsely signalled file is refused, not published. The keyframe interval is stated in seconds
(`GOP_SECONDS` = 2, 120 frames). `RENDERER_VERSION` is `pillow-ffmpeg/4`, a one-time intentional
invalidation of every cached chunk (fps, profile and GOP were already fingerprint inputs, so none could
have been reused). Frame positions are unchanged in kind: `frame = round(t_ms * fps / 1000)` from an
absolute time, so a duration of *any* number of milliseconds has exactly `round(ms * 60 / 1000)`
frames - 1, 5, 10, 30 and 60 minutes are 3 600 / 18 000 / 36 000 / 108 000 / 216 000 - with no
drift however many scenes it is cut into. **Verified locally:** see "Measured at 60 fps" under "Video rendering".

### Embeddings: batching and retry

Indexing used to be one embeddings call for every chunk of a source. OpenAI's
`text-embedding-3-small` allows 2 048 inputs and 300 000 tokens a request and 8 192 tokens an input, so a
dense or long PDF could fail the whole document with nothing indexed. **Implemented:**
`domain.embedding_batches` groups inputs into requests that respect all three (the real tokenizer is not
a dependency, so a text's size is estimated by its UTF-8 bytes - a strict upper bound for any script,
at the price of a few more, smaller requests); an input over the per-input limit is embedded **in windows
whose vectors are pooled into the chunk's one vector** - nothing is ever truncated; `index_source` commits
each batch as it is stored, so a failed batch costs only itself and a retry re-embeds only the chunks
still missing a vector (a vector lives on the chunk, so nothing can be duplicated); the vector width
(1 536) is checked before it is stored. Transient failures (429, 408, 409, 5xx, timeouts, dropped
connections) are retried by the one policy in `providers.retry` (`PROVIDER_RETRY_*`, exponential backoff
with jitter, `Retry-After` honoured and capped); an exhausted quota (a 429 with `insufficient_quota`)
and every other 4xx are never retried. A *failed* indexing job can be retried through
`POST /v1/projects/{id}/sources/{source_id}/index` (an `Idempotency-Key`, 404 across projects, 409 while
queued, running or done): the same job is re-queued with its attempts reset, so a source still has exactly
one indexing job. Model and dimension are unchanged. **Unverified:** the real token behaviour of the
estimate and the real windowing quality of a very long page.

### Language-model calls: fail clearly, never produce misleading content

**Implemented** (`providers/llm.py`). Every capability passes one check before any of a response is
read: a `refusal` (`LLMRefusalError`), `max_tokens`, `model_context_window_exceeded`, any other
incomplete stop reason, a missing or wrongly named tool call and a tool input that is not an object
all fail the call with a message for the owner. (The claim judge used to ignore `stop_reason`, so a
rationale cut off mid-way could parse into a verdict.) Thinking is sent explicitly as
`{"type": "disabled"}` (`LLM_THINKING`; `omit` leaves it out): a forced `tool_choice` cannot be combined
with thinking and the model thinks adaptively when it is omitted, so with it off the output ceilings are
all answer tokens. The SDK's own retries are off; `providers.retry` applies, with a per-attempt timeout
(`LLM_TIMEOUT_SECONDS`). Tool schemas and prompts are unchanged, and **the model is unchanged:
`claude-sonnet-5`** (`LLM_MODEL`, `SCRIPT_LLM_MODEL`). Models that reject a forced `tool_choice` or a
disabled `thinking` value need more than a setting; that migration is not part of this pass.
**Unverified:** that `claude-sonnet-5` accepts exactly this request shape, and its real stop reasons.

### Token usage

**Implemented.** `domain.llm_usage` records usage by kind of call - `topic_extraction`,
`claim_extraction`, `claim_judge`, `script_outline`, `script_section`, `script_correction` (the bounded
duration-correction pass, kept apart because it is the cost of missing the target), `storyboard` - on
every job's `result["usage"]`, failed jobs and responses that were paid for and then rejected
included (`LLMResponseError.usage`). A metric the provider did not return is `null` - unavailable, never
zero - and `calls_with_usage` below `calls` says the sums are a lower bound. `GET
/v1/projects/{id}/jobs/usage` sums a project's jobs. A claim typed in by hand has no job; its usage is
in the `llm.call` log line, which every call writes (kind, model, stop reason, tokens, seconds - never
its content).

### Spend safety

**Implemented** (`services/spend_guard.py`, `services/llm_usage.py`): (1) every job stops before the call
that would exceed `MAX_PROVIDER_CALLS_PER_JOB` (500) - a tripwire against a runaway loop, held by a test to
be at least five times above the proven worst case of any job (a full-length script makes at most 65),
so it never fires on legitimate work; the existing `domain.script_duration.max_provider_calls` bound is
untouched. (2) A project may start `MAX_PROVIDER_JOBS_PER_PROJECT_PER_HOUR` (30) paid jobs (indexing,
topic and claim extraction, script and storyboard generation) and have
`MAX_MANUAL_CLAIM_VERIFICATIONS_PER_PROJECT_PER_HOUR` (120) claims verified by hand; past that a request
is a **429** before anything starts (an upload is refused before its bytes are stored). Counts come from
the jobs and claims tables, a failed job counts, an idempotent replay never does. Rendering and narration
are local compute with their own one-at-a-time guards and are not counted. This is not a quota system.

### Evidence per minute

**Implemented** (`domain/evidence_sufficiency.py`). The duration allocator will share any target across
any evidence, so one supported claim and a 60-minute target still asked the model for an hour of
narration, which can only be filled by repeating the fact or inventing more. Script generation now refuses
that at enqueue time, from counts alone, before a job or any paid call exists. The rule is a
**conservative heuristic, not a measurement**: a supported claim carries at most 30 s of narration
(`EVIDENCE_SECONDS_PER_CLAIM`; about 75 words), each topic earns 10 s of connective narration, the lesson
15 s, and a topic uses at most `MAX_CLAIMS_PER_SECTION` claims. A five-minute lesson needs about a dozen
supported claims over a few topics; one claim cannot carry any target. The message says what the
evidence can carry (for example 0:55) and what would fix it. Re-tune the allowance against the first
real run; never to make an unsupported target pass. **Unverified:** how close 30 s is to what a real
model can honestly do with a real claim.

### Script grounding

**Implemented** (`domain/script_provenance.py`). A segment marked `connective` is exempt from citing
only up to 80 words. Every number, currency amount, month name and proper noun in *any* segment must
appear in the claims the writer was shown for that segment's topic, or in the project's and topics' own
titles, otherwise the generation fails (`UngroundedSegmentError`; nothing is attached). It reuses the
storyboard token detector and leaves teaching language alone: number words ("first", "one"), everyday time
words, sentence-initial capitals, possessives and the digits in chemical formulas (`CO2`, `H2O`) are free.
It is a safety guard, not fact verification: it never judges whether a sentence is *true*, only that it does
not introduce a figure or a name from nowhere, and it cannot see a fact stated in ordinary words.

### Storyboard provenance: fall back, do not fail

**Implemented** (`domain.storyboard_provenance.resolve_provenance`). Unsupported text in a scene's own
slots still fails the segment. Unsupported text confined to its *illustration* (description, labels,
diagram - where false positives live) falls back instead: the visual is dropped, or the scene becomes a
text-only bullet slide built from the narration's own sentences. No second provider call; the storyboard stays
complete; the fallback is logged, counted in the job's result (`provenance_visual_fallback_count`,
`provenance_text_fallback_count`) and flagged for a reviewer. Unsupported figures and names never reach a
stored scene.

### PDF resource safety

**Implemented.** An upload that declares more than `MAX_UPLOAD_BYTES` (plus 1 MiB of multipart framing) is
refused with a 413 before its body is read, and a chunked body is cut off once it outgrows the limit
(`api.middleware`); the route reads at most one byte past the limit. Pages are counted before any text
is extracted. Whatever the PDF parser raises on hostile input is an unreadable file. A PDF with no
extractable text (a scan, or nothing but page numbers) is recorded as `failed`, never "parsed" with zero
chunks, and is not queued for indexing; a source's `failure_reason` (derived, not stored) says whether it
could not be read, was too long, or had no text. The whole file is still held in memory while it is
stored and parsed (up to 100 MB): streaming it is future work.

### Keyword-only (topic-only) projects - superseded, now on by default

**Superseded 2026-10-17.** This section described the mode as switched off (a keyword-only project cannot
produce a verified script). It has since been reinstated as a separate, labelled mode - see "Topic-only
lessons" below: the claims are `general_knowledge`, the script opens with a spoken notice, the UI labels it, and
the mode was run end to end on the local model before the default was flipped.
`KEYWORD_ONLY_PROJECTS_ENABLED=false` still refuses it exactly as described here (creating one is a 400, a
keyword-only topic extraction is refused at enqueue and again when the job runs). PDF projects, with or
without keywords, are unaffected either way.

### Duration: any length, up to what can actually be made

The model, the database and the API schema hold milliseconds and accept 1 to 60 minutes. What is
*supported end to end* is `MAX_RENDER_DURATION_MS`, **30 minutes** (the product's maximum; it was 10 until
2026-10-07, and the scene ceiling `MAX_SCENES_PER_SCRIPT` was raised from 300 to 1200 - exactly 200 segments
of 6 scenes - when a 25-minute, 72-section deck counted 432 and was refused). The longest video actually rendered
and checked is 28.8 minutes (below). The web app offers any whole number of seconds from 1:00 to 30:00 (never presets) and says longer videos
are not supported yet; script generation, the first paid step, refuses a longer target. Raising that
setting lifts the limit in the API; the web app mirrors it in `lib/generation-settings.ts`.

### Jobs: a live job is never reclaimed, and a reclaimed worker stops

**Implemented.** Stale recovery measures a running job's silence from the later of its start and the last
progress it reported (`payload.progress.updated_at`), so a long generation or render that keeps reporting is
never failed, and one that goes quiet is reclaimed `*_STALE_AFTER_SECONDS` after its last word. Progress is
written through `jobs.record_progress`, which refuses once the job is no longer `running`; the four progress
writers then raise `JobAbandonedError`, so a worker whose job was reclaimed anyway (a machine that slept) stops
at its next checkpoint instead of spending twice. Indexing reports progress per batch and its retry reclaims a
dead indexing job. No migration.

### Cleanup: what is done, and what is deferred

Done: deleting a project removes its PDFs, narration audio and render objects (after the commit); a finished
render prunes the chunks it no longer uses; a worker sweeps the scratch directories a killed render left
behind (`sweep_abandoned_work_dirs`, narrowly matched and age-guarded). **Deferred, on purpose:** the narration audio
and rendered videos of a *superseded* script stay in storage until the project is deleted (about 26 MB of audio
and 32 MB of video per ten minutes per regeneration, at the old 1080p60 size) - a collector must be written and tested with care because it
deletes, and a bucket lifecycle rule is the simpler stop-gap; an object orphaned by a crash between an upload and its row
is overwritten by a retry or, otherwise, only by a lifecycle rule.

### Licensing and legal - not settled by any of this

H.264 and AAC patent licensing; shipping an FFmpeg build with libx264 (GPL); Piper's `piper-tts`
(GPL-3.0-or-later) for customer-facing distribution; Remotion's licence (not used). Choosing an encoder
settles none of them; they are business and legal decisions.

### Long-video work that remains (do not claim 60 minutes until it is done)

The render ceiling above; `MAX_SCENES_PER_SCRIPT` (300, a scene-count ceiling that is far from binding at ten minutes);
script and claim generation that are all-or-nothing (a failure late in a long generation discards it);
`piper-tts` synthesis time and a 60-minute script's roughly 150 MB of WAV; the whole video being fetched
into the browser before it plays (a proxy route is worth it beyond ten minutes); and a real-provider run at each
length. Expanding these is a later, targeted task after the first controlled real-provider evaluation.

### The first real-provider evaluation

Planned, not done: a controlled educational PDF, a 3-5 minute target, the current model, usage and stop-reason
capture, a provenance review, measured against target duration, and a manual review of the video.

## Local AI backend (free, no API key)

`AI_BACKEND=local` makes the language-model and embedding calls go to an [Ollama](https://ollama.com)
server on this machine instead of Anthropic and OpenAI: no key, no recurring cost, and nothing leaves
the machine. `AI_BACKEND=cloud` (the default) is unchanged. Everything else - Postgres, MinIO, Piper,
FFmpeg, the PowerPoint path - was already local.

**How it fits.** The whole backend is `providers/ollama.py`, behind the two factories
(`llm_from_settings`, `embeddings_from_settings`). It offers the one method `LLM` calls on an Anthropic
client and the one `Embeddings` calls on an OpenAI client and returns what those SDKs would, so every
prompt, response check, id filter and domain rule runs unchanged:

- **Structured output, not a forced tool call.** Ollama's Anthropic-compatible endpoint has no
  `tool_choice`, and every language task here is a forced tool call. The tool's own `input_schema` is
  sent as Ollama's `format` (grammar-constrained JSON) and the reply is wrapped as a `tool_use` block.
- **Refused rather than trusted.** A reply cut off at the output limit is the existing "cut off" error.
  A prompt that fills the context window (`OLLAMA_NUM_CTX`) is refused: Ollama would silently drop the
  start of it. An embedding input too long for the model is an error (`truncate: false`).
- **Embeddings are zero-padded to 1536.** The column is `vector(1536)`; a narrower local model's vectors
  are padded with zeros, which changes no dot product and no norm and so no cosine similarity (a test
  shows it). A wider one is an error. No migration.
- Failures are `LocalModelError` (a kind and a status, never the server's text) with fixed messages;
  unreachable, timed-out and 5xx are retried by the one existing policy, a missing model is not.

**Set up** (nothing here is automatic - install Ollama yourself):

```bash
ollama pull qwen2.5:7b-instruct   # the language model (about 4.7 GB)
ollama pull bge-m3                # the embedding model (1024 wide, about 1.2 GB)
```

```dotenv
# apps/api/.env  (keep the two placeholder API keys - a local backend never uses them)
AI_BACKEND=local
LLM_MODEL=qwen2.5:7b-instruct
SCRIPT_LLM_MODEL=qwen2.5:7b-instruct   # the same model, so Ollama does not swap models between jobs
EMBEDDING_MODEL=bge-m3
# Smaller jobs for a small machine (a 4 GB GPU runs a 7B model partly on the CPU):
TOPIC_INPUT_BUDGET_CHARS=32000
MAX_TOPICS_PER_EXTRACTION=12
MAX_CLAIMS_PER_TOPIC=8
```

`MAX_CLAIMS_PER_TOPIC` is **how much a lesson can say**: a script may state only what its verified claims
state, so a topic that has four claims can carry about four facts of narration. It used to be recommended
as `4` here to keep claim extraction fast; that capped every lesson at roughly 25 to 40 seconds of
supported narration per topic however long the source was, and was the main reason videos came out much
shorter than their target (see "Why a video can be shorter than its target"). Each extra claim costs one
more local-model call to verify. The default is 10; 8 is a reasonable compromise on a small machine.

Then restart the API **and the worker** (a worker started earlier keeps the old settings), and prove it
works with your models before trusting a project to it:

```bash
cd apps/api && uv run python scripts/check_local_ai.py
```

It checks the server and models, embeddings, the claim judge on a supported, a contradicted and an
unmentioned claim, and the three largest structured answers (topics, candidate claims, storyboard
scenes), with timings. **Nothing about a local model's quality or speed is verified until it passes
on your machine** - the unit tests (`tests/test_ollama_backend.py`) prove only how our code handles a
reply, with the HTTP call stubbed.

**Expect, compared with a cloud model:** slower calls (a job makes many, one after another); weaker
scripts and storyboards and more rejected answers; and a less reliable claim judge. The judge is the one
to watch: the code requires a "supported" verdict to cite a passage but does not itself check that the
passage supports the claim, so a weak model can wrongly approve. Chunks already indexed with another
embedding model are not comparable with new ones - use a new project after changing `EMBEDDING_MODEL`.
`OLLAMA_DISABLE_THINKING=True` is only for a model that thinks by default (such as `qwen3`).

## Input modes, OCR, instructions and the sentence-level check (2026-10-17, migration 0024)

**Scanned PDFs (OCR).** `providers/ocr.py` (the only importer of `rapidocr`/`pypdfium2`) draws a page with
pdfium and reads it with RapidOCR; the ONNX models ship inside the wheel, so nothing is downloaded and
nothing leaves the machine. `services/uploaded_sources._read_scanned_pages` decides when: a PDF in which
*every* page has next to no text needs OCR (it fails with `ocr_unavailable` / `ocr_too_many_pages`
(`OCR_MAX_PAGES`, default 30) / `ocr_no_text` / `ocr_failed` if it cannot); a PDF with text but where a fifth
or more of the pages are empty has those pages OCR'd best-effort. A page counts only if it has at least 20
letters and a mean line confidence of 0.6. `uploaded_sources` records `extraction_method` (`text`/`ocr`),
`ocr_page_count` and `failure_code`; the API response carries a plain-language `failure_reason`. OCR runs inline
in the upload request, one document at a time, about 5 s a page on the reference laptop. Limits: no layout
analysis, words can run together inside a line, handwriting and tiny print are unreliable.

**Free-text instructions** (`projects.instructions`, at most 2,000 characters). `domain/instructions.py`
understands audience, tone, definitions, simple examples, a page range, a chapter and focus terms by fixed
patterns; `services/instructions.py` applies them: a page range or chapter narrows the pages the topic finder
reads, focus terms put the pages that mention them first when the document is longer than the topic budget,
audience and tone go to the script writer, and the rest is passed to the topic, claim, script and storyboard
prompts as fenced guidance that can never add a fact. `ProjectResponse.instructions_notes` says back what was
`applied`, what is `guidance` only and what is `unsupported` (images, outside facts, language, length, quizzes).
A PowerPoint deck takes none (no model writes it).

**Keyword coverage.** `GET /v1/projects/{id}/topics/coverage` says, for each keyword the owner typed, whether the
document backs it: `covered`, `not_in_document` (statements were drafted, none supported), `not_checked`,
`left_out`. A keyword the document does not back is never taught as if it did; the owner can leave it out.

**Sentence-level support check.** Claim verification is retrieval plus a 7B judge, and a script's own checks are
citations plus a figures-and-names rule - neither stops a sentence that reuses a claim's words to say something
else (a photosynthesis script said "plants need water to open their stomata" while its claim said a plant short
of water *closes* them). Every sentence is now judged against the claims of its section
(`domain/narration_support.py`, `services/narration_support.py`, `LLM.judge_sentence_support`): one pass that
reasons about what the sentence adds, and a stricter second pass for a sentence with a cause/requirement/
importance cue. Measured on the local 7B model (`scripts/check_support_judge.py`): 21 of 23 hand-labelled
sentences right, and it flags the stomata reversal. It is a model, not a proof. A flagged sentence is rewritten
once with the reason; one still flagged is removed. A sentence that still breaks the figures-and-names rule, or
an uncited passage, is likewise removed rather than failing the job. If nothing usable is left, the topic's
verified claims are read out as they stand (`SCRIPT_FALLBACK_TO_CLAIMS`, on by default; off = the job fails).
Everything is recorded on the job's `result["support_check"]` (judged, second opinions, unchecked, removed
sentences with reasons, fallback sections) and shown on the script page. `SCRIPT_SUPPORT_CHECK_ENABLED=false`
turns the judge off; it adds one to two local-model calls per sentence (minutes per script).

**Topic-only lessons.** With `KEYWORD_ONLY_PROJECTS_ENABLED=true` (the default) a `topic` project may be built from keywords
alone. Its claims are recorded `general_knowledge` (never `supported`, never cited), the writer is told so, the
script opens with the fixed spoken notice `domain.general_knowledge.LESSON_NOTICE`, and the UI labels the lesson.
`services/general_knowledge.applies_to` decides when this applies; a topic project that has source material
verifies against it instead.

### Why a video can be shorter than its target

The target length is a guide. A script may say only what its verified claims say, so a lesson as long as
its claims can honestly carry is the most one can ask for; nothing is added to reach the target. The full
investigation, with the sentences that were read, is `docs/script-length-investigation.md`. What decides the
length, and what each setting does:

- **How many claims there are.** `MAX_CLAIMS_PER_TOPIC` (default 10) is how many facts one topic can
  carry. It used to be recommended as 4 for small machines, which capped every lesson at roughly 25 to
  40 seconds of narration per topic whatever the source; 8 is a fair compromise. The claim prompt asks for
  every distinct fact the passages state.
- **Each fact is said once.** Neighbouring topics hold the same claims (they are drawn from the same
  passages); `domain.claim_dedupe` keeps a fact under the topic whose title fits it best, never leaving a
  topic with fewer than two claims. The job result records `claims_deduplicated`.
- **No padding pass.** If a script is short *because the checks trimmed unsupported text* and it already
  cites at least 70% of its claims, it is not re-written with a larger word budget
  (`domain.script_duration.decide_length_correction`); a script that is too long is always corrected.
- **A lesson ends.** A script that does not end in a closing gets a fixed one naming the topics it covered
  (`domain.script_closing`, `SCRIPT_FIXED_CLOSING`, on by default). It carries no figure and is checked
  against the same figures-and-names rule as everything else, so it cannot fail a generation. The writer's own
  closing is often a summary that no single claim states, which the support check rightly removes.
- **Too many passages are joined, not refused.** A section reply with more passages than
  `MAX_SCRIPT_SEGMENTS` is joined down to it (`domain.script.merge_to_limit`); a reply more than four times
  over is still rejected.
- **The job says why.** `result["shortfall_reason"]` is `evidence_limited` (the claims cannot carry more),
  `writer_short` (claims went unused: write a new version) or absent (the length is as asked), and the script
  page says so in the owner's words.

What this does **not** do: it does not make the 7B writer or judge reliable. The judge still removes a whole
sentence when one clause is the problem, still over-flags an occasional fair paraphrase (21 of 23 on the
labelled set, one false negative and one over-flag), and the writer still repeats itself a little.

## Testing, and what is not yet verified against a real provider

```bash
uv run pytest                          # everything (needs `make up`: Postgres and Redis)
uv run pytest -m "not provider_mocked" # nothing that involves an AI provider
uv run pytest -m provider_mocked       # only the tests that stand in for one
```

**No test calls OpenAI or Anthropic.** A test that involves a provider replaces
its network call (`messages.create` / `embeddings.create`), hands the code a fake
client, or builds the SDK's error objects offline, and carries
`pytest.mark.provider_mocked` (`tests/test_provider_mocked_labelling.py` keeps that
label true in both directions). Such a test proves how *our* code handles a
response - parsing, validation, persistence, error messages. It says nothing about
how a real provider behaves, and none of them is real-provider verification.

**Text-to-speech (Piper) is the one exception - it is real in every test that
touches it.** Piper is local and free, so there is no key to withhold and
nothing to mock: `tests/test_services_script_audio.py`, `tests/test_worker.py`
and `tests/test_project_deletion_storage.py` run the genuine engine against
the genuine local MinIO. `pytest.mark.provider_mocked` does not apply to it.

**Pending real-provider verification when company API keys become available:**

1. **Keys and models.** That the keys authenticate and that `LLM_MODEL` and
   `EMBEDDING_MODEL` (and the `claude-sonnet-5` default) are available to the
   accounts.
2. **Keywords-only extraction** - runs on the local model (verified 2026-10-17); against a real provider it is
   untested. See "Topic-only lessons".
3. **Long-PDF extraction:** how much of a real document the
   `TOPIC_INPUT_BUDGET_CHARS` sample covers, and the quality of the citations.
4. **`scripts/smoke_test_phase4.py`**, run unmodified, against the real providers.
5. **Real behaviour:** the response shape and quality of the prompt in
   `providers/llm.py`, latency, token use against the output-token allowance
   (`_topics_max_tokens`), and that a real provider error maps to the message
   `providers/errors.py` gives it (for example, that an unavailable model is a `404`).
6. **Candidate claim extraction** (Phase 6): the quality of `LLM.extract_claims`'s
   proposals against a live model, and token use against `_claims_max_tokens`.
7. **Script generation** (Phase 7): the quality and teacher-style tone of
   `LLM.compose_script_outline` / `LLM.compose_script_section` against a live
   model, token use against `_outline_max_tokens` / `_section_max_tokens`, and
   how far the WPM-based `estimated_duration_ms` actually sits from real TTS
   audio - now measurable (Phase 8: `script.measured_duration_ms`), but not yet
   measured across a real generated corpus at scale.
8. **A genuine Indian-English Piper voice**, if and when one appears in the
   catalogue - or a different, commercially-licensed provider for it - to
   replace the labelled `en-GB` substitute (see "Text-to-speech").
9. **Storyboard scene composition** (Phase 9): the real quality of
   `LLM.compose_storyboard_scenes`'s template choices and slot content
   against a live model, how often honest output trips the provenance rule's
   documented false-positive cases, token use against `_storyboard_max_tokens`,
   and how often the soft lexical-overlap signal fires on genuinely faithful
   scenes in practice.
10. **Video rendering** (Phase 10) is verified end to end with real Piper narration, real
    FFmpeg and the real renderer (`scripts/verify_phase10.py`, "Video rendering"), but its scene
    text came from a stand-in for `LLM.compose_storyboard_scenes`.

## Known limitations

- **No way to cancel a job.** An extraction queued while no worker is running holds
  the project's keyword and wording edits (`409`) until a worker takes it.
- **A job a worker crashed on is failed by the next request that needs the project**, not by a
  timer: recovery runs when something is enqueued (or, for indexing, retried), measured from the job's
  last progress (see "Jobs: a live job is never reclaimed").
- **A failed job is run again by a new request with a new key** (a failed indexing job by its retry
  route). Transient *provider* failures inside a call are retried (`providers.retry`).
- **One model call over a budgeted sample.** A long document is sampled evenly
  (`truncated` says so), not read in windows; an outline the model returns that is
  too deep or has too many topics fails the extraction rather than being trimmed.
- **No per-user quota or billing.** There is a per-job tripwire and a per-project hourly limit on
  paid work (see "Spend safety"), not a meter: the provider consoles' spend limits are the real
  ceiling. Credits come with the ledger, in a later phase.
- **Replacing topics is guarded, not locked, against a script that references
  them.** The check is repeated immediately before the delete, which narrows the
  window to milliseconds but does not close it. Nothing links scripts to topics
  yet; whatever does must take a lock shared with topic replacement.
- **Claim extraction cost scales with topic count.** One model call per accepted
  topic plus one embed-and-judge round trip per surviving candidate, with no
  batching across topics - a project with many accepted topics means many paid
  calls. Credits come with the ledger, in a later phase.
- **Script generation cost scales with topic count and target duration.** One
  outline call plus one section call per accepted topic (more if a topic's
  budget needs splitting, and again if duration correction fires) - bounded
  (`domain.script_duration.max_provider_calls`) but not free. Credits come
  with the ledger, in a later phase.
- **`estimated_duration_ms` is still only a word-count estimate at generation
  time.** The correction pass narrows it towards the target before the script
  is approved, but a script is never rejected for still missing it - real
  measured duration (`measured_duration_ms`, from actually-synthesised audio,
  Phase 8) only exists once narration has been run, which happens after
  approval, so it can't inform generation or the correction pass itself.
- **Synthesis cost and time scale with script length.** One Piper call per
  narration chunk (`MAX_TTS_CHARS_PER_CALL`), `MAX_CONCURRENT_TTS` limiting how
  many run at once - no company API key, but still CPU time on the worker
  host, and a 60-minute script is roughly 150 MB of WAV (see "Text-to-speech").
- **A retry after a failed commit repeats the work.** Every mutating route commits
  before it caches its result, so a failed commit is never cached as a success - but
  the client's retry then runs the handler again. For claim verification that
  repeats the paid embedding and LLM call; for an upload, an object written just
  before a failed commit stays in storage, and a retry overwrites it (its key is
  derived from the file's checksum) rather than adding a second one.
- **No image is generated for a storyboard's illustration slots.** A scene's diagram is
  drawn procedurally from a small fixed vocabulary (process flow, cycle, hierarchy,
  comparison, labelled parts - see "Educational visuals"); there is no map, photograph,
  anatomy, apparatus or chart, and no AI image generation. A concept outside those five
  shapes is drawn as text.
- **Real LLM visual generation is unverified.** The model's tool schema and every prompt
  are tested only against a stand-in; how often a real model produces a valid, useful
  visual (and how often it is dropped to text) is unknown until company keys exist.
- **The provenance rule is a safety guard, not fact verification.** It only
  checks that a number, date, unit or proper noun in scene text also appears
  in the segment's own narration - it says nothing about whether a sentence
  built only from narration-grounded tokens states something true, and its
  fixed normalisation has documented gaps (a reworded number, a converted
  unit, an ALL-CAPS acronym) - see `domain.storyboard_provenance`'s module
  docstring for the exact list.
- **Storyboard generation cost scales with segment count.** One provider call
  per script segment, bounded by `MAX_STORYBOARD_SEGMENTS` and enforced
  before any call is made - no company API key cost concern until real keys
  arrive, but still not free once they do. Credits come with the ledger, in
  a later phase.

## Clerk Local Setup

Two of those variables, `CLERK_ISSUER` and `CLERK_JWKS_URL`, come from a
Clerk application rather than the local Docker stack. The API fails to
start without them (`get_settings()` raises `MissingSettingsError` naming
both), so set them up before running `make dev`.

1. **Create a Clerk application.** Sign in at
   [dashboard.clerk.com](https://dashboard.clerk.com), click **Create
   application**, and give it any name (a free "Development" instance is
   enough for local work).
2. **Include `email` (and its verification) in the session token.** By default
   Clerk's session token only carries `sub`; this API's Clerk provider
   (`src/edutoon/providers/clerk.py`) requires an `email` claim, and a *first* sign-in
   (accepting an invitation, or the first administrator) additionally requires
   `email_verified: true`, because the address must be proven to belong to the person.
   In the dashboard, go to **Sessions > Customize session token** and add:
   ```json
   {
     "email": "{{user.primary_email_address}}",
     "email_verified": "{{user.email_verified}}"
   }
   ```
   **Unverified:** the exact shortcode for the second claim and whether Clerk emits it as a
   JSON boolean (the API accepts only a literal `true`) have not been checked against a real
   Clerk instance. Users who are already linked are unaffected. For invitation-only sign-up,
   also set the instance's sign-up mode to **Restricted** in the dashboard.
3. **Find `CLERK_ISSUER`.** In **Configure > API Keys**, copy the
   **Frontend API URL** (looks like `https://your-app-name-12.clerk.accounts.dev`,
   or `https://clerk.<your-domain>` on a production instance with a custom
   domain). That URL, unchanged, is `CLERK_ISSUER`.
4. **Find `CLERK_JWKS_URL`.** Append `/.well-known/jwks.json` to the same
   Frontend API URL.
5. **Set both in `apps/api/.env`:**
   ```bash
   CLERK_ISSUER=https://your-app-name-12.clerk.accounts.dev
   CLERK_JWKS_URL=https://your-app-name-12.clerk.accounts.dev/.well-known/jwks.json
   ```

6. **Give the web app the same instance.** Copy the **Publishable key** and **Secret key**
   from the same page into `apps/web/.env.local` (`NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` and
   `CLERK_SECRET_KEY`; the file is git-ignored - copy `.env.local.example` if it is missing).
   Keep the secret key out of chat, commits and screenshots.
7. **Check it.** From `apps/api`:
   ```bash
   uv run python scripts/check_clerk.py
   ```
   It reads both env files and says in words what is wrong: a placeholder left in, keys from
   different Clerk instances, a test key paired with a live one, an unreachable JWKS URL. It
   never prints a secret. Then sign in through the web app; a dashboard that loads (rather
   than "Your session has ended") means the API accepted the token. To see *why* a token is
   rejected - expired, wrong issuer, missing `email` claim - pipe one in without putting it on
   a command line: `await window.Clerk.session.getToken()` in the signed-in app's browser
   console, then `uv run python scripts/check_clerk.py --token-stdin` and paste it.

Neither value is a secret key — they're derived from your instance's public
Frontend API URL, safe to share within the team (just not worth committing,
hence `.env` staying git-ignored). No Clerk account is needed to run the
test suite: `tests/test_auth.py` signs its own test JWTs against a local,
throwaway RSA keypair and never calls Clerk.

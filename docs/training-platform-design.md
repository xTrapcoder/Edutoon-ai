# Training platform: roles, modules and assessments — design

Status: **proposal for review. Nothing in this document is implemented.** No migration, endpoint or
dependency exists for it yet. Written 2026-10-12 against `main` at `9ec7319`, migration head `0018`.

## Step 1 status (implemented 2026-10-12, uncommitted; see `apps/api/README.md`, "Roles, invitations and sign-in")

Roles, invitations, the suspended-account fix and per-route role policies are built and tested
locally. Deviations from this design, both deliberate:

1. **Backfill is least-privilege, not "everyone is an admin".** Migration 0019 makes every existing
   user an `employee` and promotes only the addresses on `ADMIN_BOOTSTRAP_EMAILS` at migration time;
   `scripts/grant_admin.py` promotes someone deliberately afterwards. (Section 2.1 below originally
   said "backfill every existing row to admin"; that is superseded.)
2. **Employees and project routes (as first built, then corrected on 2026-10-13 to match section 4).**
   Step 1 first answered an employee `403` on every project route. It now answers `404` on any route
   that names a project (the same body an administrator gets for a missing project, whether or not the
   project exists) and `403` only on `GET` / `POST /v1/projects` and `/v1/admin`
   (`require_admin_for_project` in `api/policies.py`). Ownership checks inside the services (rule 9)
   are unchanged.

Not done in Step 1 and still unverified: a real Clerk sign-in, a real Clerk invitation email, and the
`email_verified` token claim (the Clerk keys are placeholders). Courses, modules, assessments and
video publishing are Steps 2 onward and have not been started.

## Step 2 status (started 2026-10-13)

Piece 1 (migration 0020, the access rule, storage keys, the fail-closed pass seam) is built and tested locally.
Piece 2 (2026-10-14: administrator course and module routes, publish by snapshot, `Storage.copy_object`) is built
and tested locally - see `apps/api/README.md`, "Training courses, modules and enrolments". Piece 3
(enrolments and employee access) is below. Differences from section 2.2 above: a
module is `published` | `unpublished` (unpublishing deletes its file and keeps its descriptive snapshot as
history); `published_by` / `published_at` are kept; there is no `draft` module - publishing a render *is*
creating the module. **Lifecycle once Step 3 exists:** assessment attempts reference modules with `RESTRICT`, so a
course whose modules have learner history cannot be deleted - it is **archived**. Deleting a course is for
courses nobody has taken.

### Step 2, Piece 3 (2026-10-14): enrolments and employee access

Built and tested locally; no migration (head stays 0020). Full behaviour: `apps/api/README.md`,
"Enrolments and employee access (Piece 3)".

**Route inventory (6 new routes; policy name from `api/policies.py`).** The five administrator
capabilities are four routes - re-enrolling a withdrawn user is the enrol `POST` (it reactivates the
same row), not a separate route.

| Method and path | Policy | Employee gets |
|---|---|---|
| `POST /v1/admin/training/courses/{course_id}/enrolments` | `admin_course` | fixed 404 `Course not found.` |
| `GET  /v1/admin/training/courses/{course_id}/enrolments` | `admin_course` | fixed 404 |
| `POST /v1/admin/training/courses/{course_id}/enrolments/{user_id}/withdraw` | `admin_course` | fixed 404 |
| `GET  /v1/admin/training/enrolments?user_id=` | `admin` | 403 (names no resource in its path) |
| `GET  /v1/training/courses` | `employee` | (administrator: 403) |
| `GET  /v1/training/modules/{module_id}/video` | `employee_module` | (non-employee: the module 404) |

*As built in Piece 3.* No assessment, attempt, progress or pass-gate route existed then. **Step 3
(below) adds six assessment routes**; the scope-guard test `test_training_route_security.py::
test_the_assessment_scope_routes_are_exactly_the_pinned_table_with_their_guards` is now a pinned
route table, and `test_route_policies.py` pins every route's policy.

**404 versus locked 403 (decision D4 as built).** For an employee asking for a module the rule gives
three outcomes. *Allowed*: stream it. *Not found*: one identical 404 (status, body and headers
byte for byte) for "no such module", "not enrolled", "enrolment withdrawn", "course not published",
"module not published" - and for an administrator calling the employee route - so none of them can be
told apart, and none is listed. *Locked*: the employee is enrolled, the course and module are
published, an earlier published module is not passed -> **403 `module_locked`** with
`details = {blocking_module_id, blocking_module_position, blocking_module_title}`. Locked is the only
403 about a resource, and it is safe to say because the module is already in the employee's own
`GET /v1/training/courses` listing with `locked: true` and a `lock` object. A nested collection is as
resource-dependent as the resource: `GET /courses/{course_id}/modules` answers an employee the course
404, and the route-policy test looks for the identifier *anywhere* in the path.

**Why `passed_module_ids` returned the empty set in Piece 3.** Passing is defined by an assessment
attempt (Step 3) and none existed, so nothing could honestly be called passed. The empty set was the
fail-closed answer. There is deliberately no bypass, flag or test-only unlock; the tests prove the lock
is *because of* that seam (a test substitutes the seam, nothing else). **Step 3 has replaced the body**
with a query for a submitted, passing attempt - see "Step 3" below.

**Step 3 foreign keys must be `ON DELETE RESTRICT`.** When Step 3 adds attempt, answer, progress or
assessment tables that reference `training_modules` (or `training_courses`), those foreign keys must
be `ON DELETE RESTRICT`, never `CASCADE`. Reason: deleting a module must not silently destroy an
employee's training history, and the 409 "unpublish / archive it instead" responses
(`COURSE_DELETE_REFUSED`, the module-delete refusal) only exist because the database refuses the
delete. Precedent: **migration 0018** corrected foreign keys on `claim_citations.source_chunk_id` and
`script_segment_claims.claim_verification_id` from `RESTRICT` to `CASCADE` because `RESTRICT` made
deleting any project with claims fail; the lesson is that a foreign key's delete rule is a product
decision about whether history may disappear, and must be chosen deliberately per relationship - for
learner history, it must be `RESTRICT`. (Nothing in the project-delete chain may be `RESTRICT` -
finding F9 - and the training tables are outside that chain: a module's source render link is
`SET NULL`, and a project's deletion never reaches a module.) The tables were not created in Piece 3;
Step 3's migration 0021 creates them with exactly these rules.

**Audit-log isolation (test strategy).** `audit_logs` is append-only (a trigger rejects deleting a
row), so a test that really commits writes rows nothing can remove; QA and mutation runs had grown the
development table from 301 to 1,336 rows. Decision: **a scratch database**, not QA markers. Tests that
commit run on a database created for the test session, migrated by `alembic upgrade head` and dropped
afterwards (`conftest.committed_database_url`); the ordinary tests roll back. A marker approach would
have left permanent QA rows in a table whose reports must be trustworthy. Existing rows were not
touched. Limits: only tests that use the fixture are protected (the committed-test `world` fixture
asserts it is not the development database), and Redis / MinIO remain shared, so those tests clean up
exactly what they made.

**Known limitation, deferred: a published render may not be current.** Publishing does not check that
the render matches the latest narration and storyboard (the workspace shows staleness, publishing does
not stop it). Snapshotting still guarantees the published video cannot change afterwards, and the
administrator's preview plays exactly the snapshot employees will receive. The improvement is to show
the script and storyboard version in the publish response so a stale render can be recognised. It does
not block Piece 3.

### Storage decisions (Step 3, Piece 0 - 2026-10-14)

Prompted by an investigation of the development uploads bucket: 726 objects, of which 2 are referenced
by the two live projects and 724 are not referenced by any database row and have no project row
(plus 9 unreferenced objects in the segments bucket). Their origin is *consistent with* test runs
(tiny generated PDFs, each written at the instant its project id was minted, mostly before
object removal on project deletion existed or written by tests that never removed them) but is not
proven. Decisions:

**Upload ordering stays: write the object first, then insert its row.** A failed database operation
after the write can leave an orphaned object, which is recoverable (it harms nobody and can be
reconciled later). The reverse order would risk a committed row that points at bytes that do not
exist - a row the application would trust and fail on, a more serious integrity failure. The
production upload path is unchanged.

**No bucket-listing orphan sweeper.** Any future reconciliation must start from **database rows** or a
**durable record of failed cleanup operations** (for example, an outbox of `(bucket, key)` removals that
did not succeed), never from deleting objects merely because a database query does not mention them.
A listing-based sweeper races with in-flight uploads (the object exists a moment before its row is
committed) and could delete legitimate user files; the project-deletion algorithm therefore keeps
removing exactly the keys its rows name, after the database commit, and never a project prefix.

**Cleanup is not yet observable.** `project.file_cleanup_failed` (and `training.object_cleanup_failed`)
are log lines only. They should become observable through a monitoring/alerting mechanism
(a counter with an alert, or the durable record above). Future work; no dashboard or logging redesign
is part of this piece.

**Audit backlog (small).** Project creation and project deletion are not audited today (there are no
`project.*` rows in `audit_logs`), which is also why a deleted project's leftovers cannot be traced
from the audit trail. Not changed here.

**The test suite is isolated from the development buckets.** Every test session creates four scratch
buckets (`edutoon-test-<run id>-uploads|segments|outputs|assets`), overwrites the `BUCKET_*` settings
with them before the application reads its settings, guards every storage client so that it refuses
any other bucket (protected development buckets and unknown ones alike) before a request is built,
and deletes only its own scratch buckets at the end. Dedicated buckets were chosen over a key prefix
because the storage layer addresses `(bucket, key)` everywhere and has no namespace concept, so a
prefix could not be enforced in one place. Mutation runs inherit this (each spawns pytest sessions) and
the runner snapshots the four development buckets and the audit count around every mutant.
`tests/test_storage_isolation.py` is the regression test. Manual verification scripts
(`scripts/verify_phase10.py`, `verify_phase11.py`, `smoke_test_phase4.py`) are not part of the test
suite and still use the development buckets by design. Details: `apps/api/README.md`, "Test isolation".

### Step 3 (2026-10-15): assessments, the pass gate and progression — migration 0021

Built and tested locally. Where this section and the sketch in sections 2.4 and 6 differ, **this
section is authoritative** (the sketch is kept as the history of the thinking). Full behaviour:
`apps/api/README.md`, "Training assessments".

**What was built.** One optional assessment per module (single-correct multiple choice); employee
attempts graded on the server; attempts immutable once submitted; `passed_module_ids` now reads real
attempts; module deletion and reordering refuse (409) once attempts exist.

**Decisions as built (they differ from the section 2.4 sketch in these places).**

1. *The threshold lives on the assessment* (`assessments.threshold_percent`, 1-100, default 70 on
   authoring) - not in configuration or a code constant. Every attempt **copies** the threshold and
   the exact question set, options, key and chosen option it was graded with (`assessment_attempts`,
   `assessment_attempt_answers`, which have no foreign key into the authoring tables). Editing or
   replacing an assessment later cannot change or re-grade a recorded attempt.
2. *An attempt is only ever inserted, already graded.* There is no in-progress attempt and no `status`
   column; the sketch's start/submit pair became one `POST .../assessment/attempts` carrying every
   answer. A database CHECK ties `passed` to the integers (`correct_count * 100 >= threshold_percent *
   question_count`), triggers (`reject_mutation`) refuse any UPDATE or DELETE of the two history
   tables, and `tests/test_architecture_rules.py` fails if any code under `src` issues one. Grading is
   integer-only: `domain.training_assessment.passes`, and a test that the module has no `/`, `float`,
   `round` or `Decimal`.
3. *The key has its own table* (`assessment_answer_keys`, primary key = question) as in the sketch, and
   the employee types (`EmployeeQuestionResponse`, `EmployeeOptionResponse`, `AttemptResponse`) are
   separate classes with no field that could carry the answer. The tests check the generated OpenAPI
   schema, the types, the employee read path's code, and the serialised response bytes - and that the
   employee view is identical whichever option is correct.
4. *Sticky passing.* A module is passed when a submitted attempt with `passed` exists - the only thing
   the query looks for - so a later failed retake cannot take it away. Attempts are unlimited.
5. *A module with no assessment blocks nothing.* The access rule takes `assessed_module_ids`: module N
   opens when every lower-positioned module **that has an assessment** is passed. Not being told which
   modules have one (`None`) is the fail-closed reading: every module blocks.
6. *Deletion and reordering.* `DELETE` of a module and `PUT .../modules/order` return 409 once any
   attempt exists (the reorder message tells the administrator to create a new course). The
   foreign keys from attempts to `training_modules`, `users` and `assessments` and from answers to
   attempts are all `ON DELETE RESTRICT` - the backstop, and the reason is migration 0018's lesson in
   reverse: that migration turned two `RESTRICT` keys into `CASCADE` because the rows were *derived*;
   attempts are the record, so they stay `RESTRICT`. The authoring rows cascade from their module, so
   deleting a never-attempted module takes its assessment with it. The module-deletion path, which had
   no HTTP test before, now has one. Deleting an assessment with attempts is a 409 too.
7. *Idempotent submission.* `POST` carries an `Idempotency-Key` (rule 8, Redis-deduplicated like every
   mutating route); additionally `(user_id, idempotency_key)` is unique in the database, so a retry that
   arrives after the cache expired still returns the same attempt, and the same key for a different
   module is a 409. Concurrent submissions by one employee for one module are serialised with an
   advisory lock so attempt numbers are consecutive.
8. *The assessment 404 has seven causes, one answer.* No such module, not enrolled, enrolment withdrawn,
   course not published, module not published, the caller is not an employee, **and the module has no
   assessment** all return the byte-identical `Assessment not found.` - so an assessment behind an
   unreachable module cannot be told from one that does not exist. A locked module stays the 403
   `module_locked`.
9. *Stale answers are refused, not mis-graded.* Replacing an assessment gives its questions new ids, so
   a submission made against the old set is a 422 ("reload") and writes nothing.
10. *An assessment is a `draft` or `published` (added to migration 0021 in place, before it was ever
    applied to a persistent database).* `assessments.status` (text, CHECK, **no default** - the service
    always says which) and `published_at` (set exactly when published; a CHECK ties the two). `PUT`
    always produces a draft and is a 409 while the assessment is published, even with no attempts.
    Publishing re-validates the whole assessment (422: 1-50 questions, 2-6 options, exactly one correct
    option each), stamps `published_at` and reports `active_enrolments_affected` (the course's active
    enrolments, withdrawn ones excluded). Unpublishing returns it to a draft and is a 409 once anyone
    has attempted it. Only a **published** assessment is visible to employees (a draft is the same
    byte-identical 404 as every other unreachable cause, history route included) and only a published
    one counts toward the pass gate - publishing deliberately changes progression for the enrolled
    employees. **Consequence:** an attempted assessment is frozen for good (it cannot be unpublished,
    so it cannot be edited); changing it means a new module or course. Delete is allowed for a draft or
    published assessment with no attempts. Two-correct-options cannot be *stored* (the key table has one
    row per question), so the publish check on that case is exercised by injection in the tests.

**Known limitations and the unstorable case (as built).** (1) An attempted assessment is frozen
for good - published, 409 on save, 409 on unpublish - so correcting it means a new module or course.
(2) With unlimited retakes and no cooldown an employee can infer the key from the score across
attempts; no single response reveals it. (3) The history does not return the options an employee chose,
although they are recorded. Two correct options on one question cannot be stored at all (the key table
has one row per question), so that publish-time check is exercised by injection in the tests. The
publish / unpublish / submit races are covered by real overlapping transactions
(`tests/test_training_assessments_committed.py`); details in `apps/api/README.md`.

**Route inventory (8 new).**

| Method and path | Policy | Notes |
|---|---|---|
| `PUT /v1/admin/training/modules/{module_id}/assessment` | `admin_module` | replace the whole draft assessment; 409 if published |
| `POST /v1/admin/training/modules/{module_id}/assessment/publish` | `admin_module` | validate, make live; `active_enrolments_affected` |
| `POST /v1/admin/training/modules/{module_id}/assessment/unpublish` | `admin_module` | back to draft; 409 once attempted |
| `GET /v1/admin/training/modules/{module_id}/assessment` | `admin_module` | with the key (`is_correct`) |
| `DELETE /v1/admin/training/modules/{module_id}/assessment` | `admin_module` | 409 once attempted |
| `GET /v1/training/modules/{module_id}/assessment` | `employee_assessment` | questions, never the key |
| `GET /v1/training/modules/{module_id}/assessment/attempts` | `employee_assessment` | the caller's own history |
| `POST /v1/training/modules/{module_id}/assessment/attempts` | `employee_assessment` | submit; `Idempotency-Key` |

**Not built (out of scope by decision):** certificates, progress dashboards and admin reporting,
reminders, multi-select and free-text questions, an in-progress attempt, a "watched the video" gate
(D6), a front end for any of this. The dev database is migrated to 0021 only when the owner applies
it (`make migrate`); this step was verified on scratch databases.

**Test-isolation notes (Step 3 close criteria).**

- *The flaky `test_recovery_with_nothing_to_recover`.* Its query was already scoped to the test's own
  project, so it was not a global assertion. The cause was a module-level clock: `NOW =
  datetime.now(UTC)` was read when the test module was imported, and the test created its job with
  `started_at=NOW`; on a suite that ran for longer than the one-hour recovery window (2h39 under
  contention) that job was already "stale" when the test ran, and recovery correctly returned it. The
  clock is now read when asked (`_now()`), and `tests/test_isolation_of_test_assertions.py` fails for
  any test module with a module-level wall-clock constant and for any raw SQL over `jobs`, `scripts`,
  `render_outputs`, `audit_logs` or `claim_verifications` that has no `WHERE`. The sweep found no
  unscoped count or list over those tables; the two `project_id <> :p` comparisons in the committed
  tests are before/after comparisons on a scratch database.
- **The mutation runner's scratch-bucket sweeper is unsafe under concurrent test sessions and must not
  be used concurrently as currently implemented.** After each mutant it removes every `edutoon-test-*`
  bucket that did not exist when that mutant started - it cannot tell a bucket left behind by the
  mutant's own broken teardown from one that belongs to a test session that started meanwhile. Run the
  mutation runner alone (no other pytest session, no second runner); the full suite and the runner
  must not overlap. (A fix would have each mutant's session report its run id, as
  `test_a_whole_test_session_creates_its_own_scratch_buckets_and_removes_exactly_those` does.)
- **Manifest checksums are over LF-normalised text; Windows may write CRLF.** `make_manifest.py`
  hashes the in-memory manifest (LF line endings) and records that digest in the `.sha256` file; on
  Windows the file on disk has CRLF, so `sha256sum cleanup_manifest.json` differs from the recorded
  value although the content is identical. Compare after converting CRLF to LF
  (`tr -d '\r' < file | sha256sum`) or compare two copies byte for byte. (Seen on 2026-10-14: raw
  `24ef3e56...` versus recorded `11a95125...`, equal once normalised.)
- *Step 3 tests run against a scratch database.* The development database stays at migration 0020
  until the owner migrates it; the suites for this step ran with `DATABASE_URL` /
  `DATABASE_DIRECT_URL` pointing at a freshly created and migrated scratch database that was dropped
  afterwards.

## 0. Pending quality gate (separate from this design — not done)

> **A human must watch the generated sample video and listen to the four voice samples before the
> output is judged suitable for employee training. That review has NOT happened.**

Files to review, all in `apps/api/var/samples/`: `workplace-safety-induction-720p30.mp4` (the
sample video) and `voice-sample-1-as-trained.wav`, `voice-sample-2-slower-1.1.wav`,
`voice-sample-3-livelier.wav`, `voice-sample-4-slower-and-livelier.wav` (the four voice samples).
The two other files there (`en-GB_cori-medium_sample.wav`, `en-IN_temporary-substitute_sample.wav`)
are earlier samples; `en-IN` still narrates with a labelled `en-GB` substitute voice.

Building the platform does not depend on this review, but **rolling anything out to real employees
does**. The implementation order (section 8) therefore ends with a step that is blocked on it.

## 1. Scope, decisions taken, and what was found in the code

Confirmed by the owner: one organisation (no multi-tenancy); employees are invited by an admin;
existing projects and their ownership checks (rule 9) stay untouched; module access is enforced by
the server through one central function; correct answers never reach the browser before
submission; a submission is immutable; 70% unlocks the next module.

Findings from reading the repository that the design has to respond to:

| # | Finding | Where | Consequence |
|---|---|---|---|
| F1 | `get_or_provision_user` creates a `users` row for **any** valid Clerk token. | `services/auth.py` | Invite-only is impossible until this changes. Anyone who signs up could create projects today. |
| F2 | `get_current_user` never checks `users.status`, although the column allows `suspended` / `deleted`. | `api/dependencies.py` | A deactivated employee would still be authenticated. |
| F3 | `users` has no role. Every authenticated user can call every project endpoint, including `POST /projects` and `GET /projects`. | `db/tables.py`, routers | Existing routers need an admin gate. Resource routes already return 404 for non-owners; collection routes do not. |
| F4 | A render output is **overwritten in place**: one row per `(script, orientation, quality)`, upserted by `replace_output`, stored at a deterministic key. Deleting a project cascades to it and its object is removed. | `repositories/render.py`, `services/render.py` | A module that only stores a `render_output_id` would silently change, or lose its video, after a re-render or project delete. The video must be pinned (section 2.3). |
| F5 | The render API does not expose `render_outputs.id`. Its stable identifier is `(project_id, script_id, orientation, quality)`. | `api/schemas/render.py` | Publishing takes that tuple; the server resolves the row. No render API change needed. |
| F6 | Clerk claims carry `sub` and `email` only; nothing says the email is verified. | `providers/clerk.py` | Matching an invitation on an unverified email would allow account takeover (section 3). |
| F7 | There is no `ForbiddenError` (403), no `Storage.copy_object`, and `/auth/me` returns no role. | `core/errors.py`, `providers/storage.py` | Small additive changes in step 1 and step 2. |
| F8 | 82 test call sites create users directly. | `tests/` | Making `role` mandatory will touch many fixtures. Step 1 must measure and fix that first. |
| F9 | Migration 0018 fixed a `RESTRICT` FK that broke project deletion. | `alembic/versions/0018*` | No new table may hold a `RESTRICT` FK into the project-delete cascade chain. |

## 2. Schema (additive; migrations 0019 to 0021)

Conventions followed: UUIDv7 keys, `timestamptz`, text columns with `CHECK` (rule 7), snake_case,
British English in user-facing copy. New tables reference `users` with `ON DELETE RESTRICT`: users are
retired by `status`, never hard-deleted, and no endpoint should hard-delete one (add a test).

### 2.1 Roles and invitations — migration 0019

```
users            + role text NOT NULL  CHECK (role IN ('admin','employee'))
                   -- add nullable, backfill every existing row to 'admin', then SET NOT NULL.
                   -- No default: a new user must be given a role explicitly.

invitations      id, email (lowercase CHECK, like users), display_name, role CHECK,
                 status CHECK ('pending','accepted','revoked'),
                 invited_by -> users RESTRICT, expires_at,
                 accepted_user_id -> users, accepted_at, clerk_invitation_id, created_at, updated_at
                 UNIQUE INDEX (email) WHERE status = 'pending'
```

Expiry is computed (`expires_at < now()`), not stored as a status. Backfilling existing users as
`admin` is correct: every existing user is a project creator.

### 2.2 Courses, modules and enrolment — migration 0020

A **course** is the ordered container; without one there is nothing for "next module" to mean.
(Assumption A1: this is acceptable. The alternative is one global sequence for the whole organisation.)

```
training_courses   id, title, description, status CHECK ('draft','published','archived'),
                   created_by -> users RESTRICT, created_at, updated_at

training_modules   id, course_id -> training_courses RESTRICT, position int >= 1,
                   title, description, status CHECK ('draft','published','archived'),
                   source_render_output_id -> render_outputs  ON DELETE SET NULL   (provenance only)
                   -- pinned video snapshot, written at publish:
                   video_bucket, video_key, video_byte_size, video_duration_ms,
                   video_width, video_height, video_output_hash, published_at, published_by
                   UNIQUE (course_id, position) DEFERRABLE INITIALLY DEFERRED
                   INDEX (course_id)

course_enrolments  id, course_id -> training_courses RESTRICT, user_id -> users RESTRICT,
                   status CHECK ('active','withdrawn'), assigned_by -> users RESTRICT,
                   assigned_at, updated_at
                   UNIQUE (course_id, user_id)        INDEX (user_id)
```

### 2.3 Why the video is pinned (F4)

On publish the server copies the MP4 to its own key, `training/{module_id}/{output_hash}.mp4` in the
outputs bucket, and records size, duration, dimensions and `output_hash` on the module. Employees
stream **that copy**, never the project's render. This means:

- re-rendering or deleting the source project cannot change or break a published module;
- the assessment questions always match the video the learner actually watched;
- `source_render_output_id` still ties the module to the existing render identifier, but with `SET NULL`
  so project deletion keeps working (F9).

Cost: one extra copy per published module (about 71 MB at 30 minutes). Needs `Storage.copy_object`
(S3 server-side copy) in `providers/storage.py`. The `training/` prefix is outside
`belongs_to_project`, so project cleanup never touches it. Removing a module's copy happens after the
commit, as for uploads and narration today. **Decision D1: pin by copy (recommended) versus reference
only (cheaper, unsafe).**

Publish preconditions: the caller owns the render's project (else 404, rule 9); the script is
approved; the output exists, is current and its object is present; the assessment is valid (2.4).

### 2.4 Assessments, attempts and progress — migration 0021

One assessment per module. Single-choice questions only in v1 (D2). Published content is frozen: a
module's video and questions can be edited only while it is `draft`.

```
assessment_questions   id, module_id -> training_modules RESTRICT, position, prompt, explanation
                       UNIQUE (module_id, position) DEFERRABLE

assessment_options     id, question_id -> assessment_questions RESTRICT, position, label
                       UNIQUE (question_id, position)        UNIQUE (question_id, id)

assessment_answer_keys question_id PRIMARY KEY -> assessment_questions,
                       correct_option_id,
                       FOREIGN KEY (question_id, correct_option_id) -> assessment_options (question_id, id)
```

The correct answer lives in its **own table**, not as `is_correct` on the option. A query, serialiser or
schema that returns options can then never leak it by accident, and the primary key guarantees
exactly one correct option per question. "At least two options and a key row" is checked at publish.

```
assessment_attempts    id, module_id -> RESTRICT, user_id -> users RESTRICT, attempt_number,
                       status CHECK ('in_progress','submitted'), started_at, submitted_at,
                       score_correct, score_total, pass_mark_percent, passed boolean
                       UNIQUE (module_id, user_id, attempt_number)
                       UNIQUE INDEX (module_id, user_id) WHERE status = 'in_progress'   -- one open attempt
                       INDEX (user_id, module_id) WHERE passed                          -- unlock lookups
                       CHECK: status='submitted' <=> submitted_at, score_*, passed all NOT NULL
                       TRIGGER: any UPDATE or DELETE of a row whose OLD.status = 'submitted' is rejected

assessment_attempt_answers  attempt_id -> assessment_attempts, question_id, selected_option_id,
                       is_correct boolean,
                       PRIMARY KEY (attempt_id, question_id),
                       FOREIGN KEY (question_id, selected_option_id) -> assessment_options (question_id, id)
                       TRIGGER: reject_mutation()  (existing helper, as for audit_logs)

module_progress        PRIMARY KEY (user_id, module_id), first_opened_at, max_position_ms,
                       video_completed_at, updated_at
```

Answers are written only at submission, in the same transaction as the grade. Nothing is stored for a
half-finished attempt, so there is no draft to edit afterwards.

There is deliberately **no stored "passed" flag on progress**. A module is passed if a submitted attempt
with `passed = true` exists; the partial index makes that cheap. One source of truth, no drift.

`pass_mark_percent` is copied onto each attempt (70 today) so a later change of the constant never
rewrites history. The constant lives in `domain/training.py`, not in a table.

## 3. Roles, identity and invitations

**Role storage.** `users.role` in Postgres is the only source of truth. Never read from the JWT, a
Clerk metadata field, a header or a request body. Role changes are admin-only, audited
(`audit_logs`), and the last active admin cannot be demoted or deactivated (row lock plus check).

**Identity mapping (replaces F1).** `get_current_user` becomes:
1. verify the token (unchanged);
2. find the user by `clerk_user_id`; if found, reject if `status != 'active'` (fixes F2);
3. else find a `pending`, unexpired invitation whose email equals the token's lowercased email;
   if found, create the user with the invitation's role and mark it `accepted`, in one transaction;
4. else, if the email is in a new `ADMIN_BOOTSTRAP_EMAILS` setting **and no admin exists**, create the
   first admin;
5. else raise 403 `not_invited`. **No row is created.**

**Email trust (F6).** Step 3 is only safe if the email is verified. Plan: run Clerk in *restricted*
sign-up mode (invitation only), add `email_verified` to the JWT template, and have
`ClerkClaims.email_verified` required for step 3 and 4. **Assumption A2, unverified:** that the Clerk
template can emit that claim. Clerk keys in this repository are placeholders, so none of this has been
tried. If it cannot, fall back to a one-time invitation code entered at first sign-in.

**Sending invitations.** No email provider exists and none will be added. Options: Clerk's Invitations
API (free tier; needs `CLERK_SECRET_KEY`, which must live in `providers/clerk.py`, rule 3) or the admin
passes the sign-up link on by hand. **Decision D3: recommend the Clerk API.**

**Demo seeding (proposal only).** `apps/api/scripts/seed_demo_training.py`, in the style of
`dev_qa_api.py`: refuses to run unless `ENVIRONMENT=development` and the database host is local;
creates one admin and two employees through the Clerk Backend API (via a function in
`providers/clerk.py`), writes their `users` rows with the roles pre-linked to the Clerk ids,
is idempotent on email, generates the demo passwords at random and writes them to a git-ignored file
under `var/` (never committed, never printed to chat). It can then create one course from the existing
sample deck render. **It needs real Clerk dev-instance keys, which do not exist yet.** Until then,
browser verification uses an extension of the existing QA launcher with two fixed stand-in tokens
(admin and employee) — development only, same guards as today.

## 4. Permission matrix

Role gating failures return **403** (a capability, not a resource). A resource the caller may not know
about returns **404** (rule 9). A resource the caller is assigned to but has not unlocked returns
**403 `module_locked`**, with the blocking module named, so the screen can say what to finish first (D4).

| Capability | Admin | Employee |
|---|---|---|
| Existing project-generation endpoints (`/v1/projects/...`) | yes, own projects only (rule 9 unchanged) | **403** on collection routes, **404** on any project id |
| Invite, revoke, list invitations; list users; change role / status | yes | no |
| Create and edit courses and modules (draft), author assessments | yes | no |
| Publish / unpublish a module or course | yes (publish needs ownership of the source project) | no |
| Enrol / withdraw employees | yes | no |
| View every employee's progress, attempts and answers | yes | no |
| List own assigned courses and per-module state | no (admins use admin views) | yes |
| Open a module, stream its video, post playback progress | no | yes, only if unlocked |
| Start and submit an assessment | no | yes, only if unlocked, own attempts only |
| See the answer key | yes (admin views) | after submission only, as in section 6 |

Admins do not take assessments in v1; an admin who wants to try a course uses a separate employee account (D5).

## 5. API and the central access function

All under `/v1`. Every mutating route requires `Idempotency-Key` (rule 8); request bodies use
`extra="forbid"` (rule 10); snake_case throughout. New routers: `admin_users`, `admin_training`,
`training` (employee). Existing routers get a router-level `require_admin` dependency.

**Admin** (`require_admin`)
- `POST /admin/invitations`, `GET /admin/invitations`, `POST /admin/invitations/{id}/revoke`
- `GET /admin/users`, `PATCH /admin/users/{id}` (role, status)
- `POST|GET /admin/training/courses`, `PATCH .../{id}`, `POST .../{id}/publish`
- `POST /admin/training/courses/{id}/modules` with `{project_id, script_id, orientation, quality, title, ...}`
- `PUT /admin/training/modules/{id}/assessment` (replace the whole question set atomically, draft only)
- `POST /admin/training/modules/{id}/publish`, `POST .../unpublish` (409 once any attempt exists)
- `PUT /admin/training/courses/{id}/enrolments`, `GET` the same
- `GET /admin/training/courses/{id}/progress`, `GET /admin/training/modules/{id}/attempts`,
  `GET /admin/training/attempts/{id}` (with answers and the key)

**Employee** (`require_employee`)
- `GET /training/courses` — assigned courses with per-module state: `locked`, `available`, `passed`
- `GET /training/modules/{module_id}` — metadata and the video path
- `GET /training/modules/{module_id}/video` — `Range` streaming from the pinned copy
- `POST /training/modules/{module_id}/progress` — playback position pings
- `POST /training/modules/{module_id}/attempts` — start, or return the open attempt (questions, **no key**)
- `POST /training/attempts/{attempt_id}/submit` — `{answers: [{question_id, option_id}]}`
- `GET /training/attempts/{attempt_id}` — result, after submission only

### 5.1 The one function that decides access

Two parts, so the rule is testable without a database:

- `domain/training_access.py` (pure, rule 1): `decide_module_access(facts) -> AccessDecision(allowed, reason, blocking_module_id)`.
  Allowed only when **all** hold: user is an active employee; the course is `published`; the enrolment is
  `active`; the module is `published`; and **every published module with a lower `position` in the
  course has a passing submitted attempt by this user**. The first published module is always open.
- `services/training_access.py`: `require_module_access(session, user, module_id, purpose)` loads those
  facts in one query, calls the domain function, and raises 404 (not assigned or unknown) or 403
  `module_locked`. `purpose` is `view` or `attempt`; `attempt` additionally needs the video completed
  (D6).

Every employee route that touches a module, including video, progress, start and submit, calls
`require_module_access` and nothing else decides. `submit` and `GET attempt` resolve the attempt to its
module first (attempt not owned by the caller → 404) and then call it, so an attempt cannot be used to
get around a later lock or withdrawal. A **route-introspection test** walks the app's routes and fails if
any `/training/modules/{module_id}...` or `/training/attempts/...` route lacks the dependency, so a future
endpoint cannot forget it.

## 6. Assessment and progression rules

1. **Start.** One open attempt per learner and module (partial unique index). Starting again returns it.
   No new attempt once the module is passed (409).
2. **Questions go out without any key.** The employee response models have no field for
   `correct_option_id`, `is_correct` or `explanation`; the key table is read only inside the grading call.
3. **Submit** (one transaction): lock the attempt row `FOR UPDATE`; require `status = 'in_progress'`
   and ownership; require **every** question answered exactly once with an option belonging to that
   question (else 422); read the key; write the answers and the grade; set `status = 'submitted'`.
4. **Grade with integers.** `passed = score_correct * 100 >= score_total * 70`. No floats, no rounding
   question: 7/10 passes, 2/3 fails, 5/7 passes (71.4%). The percentage shown is derived from the same
   integers.
5. **Immutable.** A second submit, with the same or a different `Idempotency-Key`, returns 409
   `attempt_already_submitted` and changes nothing. The database trigger also rejects any later
   UPDATE or DELETE, so a bug elsewhere cannot rewrite a grade.
6. **Unlock.** A passing attempt makes the next published module available on the next call to the central
   function — there is no "unlock" write to forget or forge.
7. **Failing.** A failed attempt is final for that attempt; the learner may start a new one (unlimited in
   v1, D7). After a failed submission the result shows which questions were wrong, **not** the correct
   option; the key and explanations are revealed once the module is passed. This stops a learner reading
   the answers off and retaking at once.
8. **Content changes.** Reordering or inserting modules in a course that already has attempts is refused
   (409) in v1, so nobody is retroactively locked or unlocked (D8).

## 7. Security and test plan

Threats to cover: role escalation, reading another user's data, jumping ahead, key leakage, tampering
with a submitted grade, replayed or concurrent submits, forged playback progress, stale or deleted
source video, an uninvited sign-in.

Tests, each in `apps/api/tests/`, using the existing fixture style and the Clerk-key monkeypatch from
`test_auth.py`. Names are indicative.

- **Roles** — table-driven matrix: every route × {admin, employee, suspended, anonymous, uninvited}
  against the expected status. Existing project routes return 403 / 404 for an employee.
  Role cannot be changed through a token claim or request body; last admin cannot be removed.
- **Identity** — uninvited token → 403 and **no row created**; expired / revoked / reused invitation
  rejected; case-different email matches; unverified email rejected; bootstrap admin only when none exists.
- **Cross-user** — employee A cannot read, start, submit or fetch attempts of employee B (404);
  employee cannot read a course they are not enrolled in (404); a withdrawn enrolment loses access.
- **Locked modules / bypass** — direct `GET`, `/video`, `/progress`, `/attempts` on module 2 before
  passing module 1 → 403 `module_locked`; same after a *failed* module 1; attempt created while
  unlocked, then enrolment withdrawn, then submit → refused; guessing ids of unpublished or archived
  modules → 404; the introspection test above.
- **Answer leakage** — static: the OpenAPI schemas of every employee response contain no
  `correct_option_id`, `is_correct` or `explanation`; runtime: recursively scan the JSON of start,
  attempt, course and module responses for those keys and for the key row's option id; result of a failed
  submission also contains no correct option.
- **Grading** — 69%/70% boundary using integers (7/10, 69/100 vs 70/100, 2/3, 5/7); all-wrong; all-right;
  missing answer, duplicate answer, foreign option, unknown question → 422 with nothing written.
- **Immutability** — second submit → 409 and rows unchanged; raw SQL `UPDATE`/`DELETE` on a submitted
  attempt and on its answers is rejected by the trigger; concurrent double submit leaves exactly one
  grade (run two submits at once).
- **Progression** — pass module 1 unlocks 2 immediately; fail does not; a pass is permanent; new attempt
  after pass → 409; reorder after attempts → 409.
- **Pinned video** — re-render the source, then stream the module: unchanged bytes. Delete the source
  project: module still plays and `source_render_output_id` is `NULL`. Publishing someone else's render →
  404.
- **Schema** — migrations 0019–0021 upgrade and downgrade cleanly on a database that already holds
  projects, claims and renders; existing users become admins; no new FK into the project-delete chain is
  `RESTRICT`.
- **Existing suites** — the full backend suite (4214 passed, 25 skipped today) and web suite (243) must
  still pass after step 1. Frontend: component tests for admin / employee shells, locked, empty, loading
  and error states.
- **Real browser / API verification** (not claimed until done): sign in as each role, run a full course
  with a failed then a passing attempt, try a locked URL by hand, watch network responses for the key.

Other measures: audit rows for invitations, role and status changes, publish / unpublish and every
submission; rate-limit start / submit; server clamps playback position to the video duration and keeps
it monotonic.

## 8. Implementation order (each step separately approved, tested and reported)

1. **Roles and invitations.** Migration 0019; `ForbiddenError`; new provisioning and status check;
   `require_admin` on the existing routers; `/auth/me` returns the role; admin / employee tokens in the
   QA launcher; fixture clean-up (F8) done *first*. Highest risk to existing behaviour.
2. **Courses, modules, enrolment, publish with the pinned copy.** Migration 0020; `Storage.copy_object`.
3. **Central access function and the read-only employee endpoints** (list, detail, video, progress).
4. **Assessment authoring** (admin) with the separate key table. Migration 0021.
5. **Attempts, grading, immutability, unlocking.**
6. **Admin progress and results.**
7. **Web: admin area**, then **web: employee area** (`(admin)` and `(employee)` route groups, role from
   `/auth/me`; the UI redirects by role, the API is what enforces).
8. **Demo seeding and real-browser verification** — blocked on Clerk dev keys; falls back to stand-in tokens.
9. **Rollout to real employees — blocked on the section 0 review.**

## 9. Open decisions and risks

Decisions needed (my recommendation first):
- **D1** Pin the video by copy (yes) or reference only.
- **D2** Single-choice questions only in v1 (yes). Multi-select later.
- **D3** Send invitations through Clerk's Invitations API (yes), or by hand.
- **D4** Locked module returns 403 `module_locked` with the blocker named (yes), or 404.
- **D5** Admins cannot take courses; use a separate employee account (yes).
- **D6** Require the video to be watched (client-reported, ≥ 90% of the duration) before an attempt, or
  do not gate. This is **advisory, not secure**: the browser reports it and a determined learner can forge it.
- **D7** Unlimited retakes, or a cap and a cool-down.
- **D8** Strict recomputation of locks when content changes (blocked after attempts, as above), or grandfather
  earlier passes.
- **D9** One course per employee path, or many; due dates and reminders are out of scope.
- **D10** Reveal the key and explanations after a pass (yes), or never, or immediately after every submit.

Unverified assumptions: **A1** courses are the right container; **A2** Clerk can emit a verified-email claim
and restricted sign-up works as described (never tried — keys are placeholders); **A3** S3-compatible
server-side copy works on the local MinIO exactly as on production storage.

Risks:
- Step 1 changes who can call every existing endpoint and touches many test fixtures. It carries the most risk.
- Employee playback currently has the same limit as the admin player: a `<video>` tag cannot send an
  `Authorization` header, so the front end fetches the whole file into a `blob:` URL (acceptable at ~71 MB;
  revisit with short-lived signed URLs for larger files).
- Storage grows by one video copy per published module; there is still no clean-up of superseded renders.
- Piper's GPL licence, H.264 / AAC patents and x264 remain unreviewed business decisions; they matter more
  once the output is given to staff.
- Voice and video quality is unreviewed by a human (section 0). Real-provider output for the PDF pipeline is
  unverified.
- Out of scope and not designed: multi-tenancy (an `organisation_id` can be added to `users`, courses and
  invitations later without touching the project-generation tables), certificates, notifications, SCORM
  export, reporting exports.

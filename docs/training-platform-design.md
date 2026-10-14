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
and tested locally - see `apps/api/README.md`, "Training courses and modules". Enrolments and employee access
follow (Piece 3). Differences from section 2.2 above: a
module is `published` | `unpublished` (unpublishing deletes its file and keeps its descriptive snapshot as
history); `published_by` / `published_at` are kept; there is no `draft` module - publishing a render *is*
creating the module. **Lifecycle once Step 3 exists:** assessment attempts reference modules with `RESTRICT`, so a
course whose modules have learner history cannot be deleted - it is **archived**. Deleting a course is for
courses nobody has taken.

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

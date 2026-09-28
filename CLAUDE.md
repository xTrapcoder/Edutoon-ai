# EduToon AI — project context

## Product

EduToon AI turns source material into a verified, animated, narrated explainer video.

A user uploads a PDF or enters a topic. The system extracts the topics worth
covering, verifies every factual claim against the source material, generates a
cited script that the user reviews and approves, and then produces an animated
narrated video from that approved script.

**Core promise:** every video is traceable to verified source material. No
unsupported claims ever reach the script or the screen.

## Stack

| Area           | Choice                                               |
| -------------- | ---------------------------------------------------- |
| Web            | Next.js 15 (App Router) + TypeScript + Tailwind CSS  |
| API            | FastAPI + Python 3.12 + SQLAlchemy (async) + Alembic |
| Database       | PostgreSQL 16 + pgvector                             |
| Cache / queue  | Redis                                                |
| Object storage | S3-compatible (MinIO locally)                        |
| Auth           | Clerk                                                |
| Animation      | Remotion                                             |

## Architecture rules — do not break these

1. **Layer order is routers → services → domain.** Domain modules are pure: no
   I/O, and they must not import repositories, providers or services.
2. **Routers never import repositories or providers directly.** They go through
   services.
3. **No external provider SDK is imported outside the `providers` package.**
4. **The credit ledger is append-only.** Never `UPDATE` or `DELETE` a ledger row.
5. **All money is stored as integer micro-USD. All durations as integer
   milliseconds.**
6. **Frame positions are absolute:** `frame = round(t_ms * fps / 1000)`. Never
   accumulate durations to derive a frame.
7. **Enumerations are text columns with `CHECK` constraints**, never Postgres
   enum types.
8. **Every mutating endpoint requires an `Idempotency-Key` header.**
9. **A resource the caller does not own returns 404**, never 403.
10. **Unknown request body fields are rejected with 422**, never silently
    ignored.

## Conventions

- `snake_case` in the database and across the API surface.
- UUIDv7 primary keys.
- All timestamps are `timestamptz`, stored in UTC.
- 24-hour time everywhere.
- British English in all user-facing copy and in code comments.

## Database

- Async SQLAlchemy throughout (`asyncpg`). The engine lives in
  `edutoon.db.database`; sessions come from `edutoon.db.session.get_session`.
- Schema is defined **only** by hand-written Alembic migrations in
  `apps/api/alembic/versions` (raw SQL via `op.execute`). No ORM models exist —
  `Base` in `edutoon.db.base` is an empty declarative base kept for the
  naming convention and Alembic's `target_metadata`; repositories use the Core
  `Table` mirrors in `edutoon.db.tables`.
- Migrations connect via `DATABASE_DIRECT_URL` (never the pooled URL).
- Helper functions (migration 0001, 0008): `uuid_generate_v7()` for
  time-ordered, monotonic UUID PKs, `set_updated_at()` BEFORE-UPDATE trigger,
  `reject_mutation()` append-only guard.
- Tables: `users`, `projects`, `jobs`, `uploaded_sources`, `source_chunks`
  (pgvector `vector(1536)` + HNSW cosine + trigram GIN), `audit_logs`
  (append-only, no FKs — loose UUID references), `claim_verifications` +
  `claim_citations`, `scripts` + `script_segments` + `script_segment_claims`,
  and — Phase 5 — `project_topics` (a tree; `origin` is `source` / `user` /
  `suggested`, plus `keywords`) and `topic_citations` (topic → source chunk,
  cascading, regenerable). `projects` also carries the generation settings later
  phases read - `language` (`en-IN` by default, `en-GB` also supported; it selects
  the writing style of the script and topic prompts), `orientation` and
  `target_duration_ms` (integer milliseconds, 1 to 60 minutes) - with their
  vocabulary and limits in `edutoon.domain.generation_settings` (migrations
  0010, 0011). Migration 0012 (Phase 6) adds `claim_verifications.topic_id`
  (nullable, `ON DELETE SET NULL` — which topic a claim was extracted for; `NULL`
  for one typed in by hand) and widens `ck_jobs_kind` to add `extract_claims`.
  Migration 0013 (Phase 7) narrows `uq_scripts_project_id_active` to `draft` /
  `approved` only — a `pending` generation no longer supersedes the previous
  `approved` script until it is itself proven ready to replace it (see
  `apps/api/README.md`, "Script generation", "Atomicity").
- `projects.status` is derived, never set by a client: `edutoon.domain.project_status` is the
  rule and `projects.refresh_status` applies it whenever a job is queued or ends or a PDF fails
  to parse (`apps/api/README.md`, "Project status"). Deleting a project removes its uploaded PDFs
  from object storage after the deletion commits (`services.projects.remove_stored_files`;
  "Deleting a project").

## Current phase

**Phase 7 — duration-aware, topic-scoped script generation: code-complete for
everything that can be verified without provider credentials.** Delivered on top
of Phase 6: auth, projects, PDF upload and parsing, the Evidence Engine, topic
extraction (Phase 5), candidate claim extraction (Phase 6), the background
worker, and now a script generation pipeline that reads accepted topics and
their own topic-scoped supported claims instead of the whole project's pool -
one outline call, one narration call per accepted topic (split into ordered
sub-sections when a topic's duration budget is too long for one call), a
bounded duration-correction pass, and one atomic attach (migration 0013;
API in `apps/api/README.md`, "Script generation"). `project.target_duration_ms`
now actually controls generation via `edutoon.domain.script_duration`; the LLM
never decides durations, topic order or which claims a section may cite.

**Pending real-provider verification when company API keys become available**
(nothing below is verified; see `apps/api/README.md`, "Testing, and what is not yet
verified against a real provider"): the keys and model availability
(`LLM_MODEL`, `EMBEDDING_MODEL`, `SCRIPT_LLM_MODEL`), keywords-only extraction,
long-PDF extraction, the quality of candidate claim extraction, the teacher-style
tone and quality of `compose_script_outline` / `compose_script_section` and how
far their WPM-based duration estimate sits from real TTS audio once that exists,
`apps/api/scripts/smoke_test_phase4.py` run unmodified, and the real quality,
latency and token behaviour of every prompt. No test calls a provider; those
that involve one carry `pytest.mark.provider_mocked`.

**The web front end is built** (`apps/web`, see its README): landing, Clerk sign-in,
dashboard, project list, create-project wizard, project workspace, topic review with
evidence, script review and approval. Script Studio already shows target vs.
estimated duration and each segment's topic; job progress now also carries
`payload.progress` (which topic is being written, out of how many) for a
`generate_script` job. Real Clerk sign-in is **unverified** - the Clerk keys are
placeholders - as are the real providers.

Not built yet: a front end for candidate claim extraction, scenes, voice/TTS,
animation, rendering. **Phase 8 has not started and needs the owner's explicit
approval.**

## Commands

```bash
make up              # start Postgres, Redis, MinIO (waits for healthchecks)
make dev             # run the API (:8000) and web app (:3000) together
make migrate         # alembic upgrade head
make migrate-create name="..."   # new migration
make migrate-down    # alembic downgrade -1
make history         # migration history + current revision
make test            # run the test suites (API and web)
make lint            # lint TypeScript and Python
make contracts       # regenerate packages/contracts (openapi.json + TypeScript types) after an API change
make contracts-check # fail if the committed contract is stale
```

The web app needs **Node >= 22** (`.nvmrc`, `engines`) and pnpm through Corepack. API shapes
in TypeScript are generated from the API's OpenAPI document (`@edutoon/contracts`); never
hand-write one. `apps/web/README.md` has the rules the front end holds to, and how to run
its dev-only UI QA mode (mock API and Clerk stub, never enabled in a build).

## Layout

```
apps/
  web/        Next.js 15 front end (README.md explains it)
  api/        FastAPI service (uv, src/edutoon)
    src/edutoon/          api/ (routers, schemas) · services/ · domain/ (pure) ·
                          repositories/ · providers/ · worker/ · db/ · core/
    alembic/              migration environment + versions/0001..0012
  parser/     PDF/topic parser — not yet initialised
  render/     Remotion render worker — not yet initialised
packages/
  config/     shared TS config (@edutoon/config)
  contracts/  API contract: openapi.json + generated TS types (@edutoon/contracts)
infra/
  docker/     container bootstrap (Postgres init SQL, etc.)
```

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
  `apps/api/alembic/versions` (raw SQL via `op.execute`). No ORM models exist
  yet — `Base` in `edutoon.db.base` is an empty declarative base kept for the
  naming convention and Alembic's `target_metadata`.
- Migrations connect via `DATABASE_DIRECT_URL` (never the pooled URL).
- Helper functions (migration 0001): `uuid_generate_v7()` for time-ordered
  UUID PKs, `set_updated_at()` BEFORE-UPDATE trigger, `reject_mutation()`
  append-only guard.
- Tables: `users`, `projects`, `jobs`, `project_topics`, `uploaded_sources`,
  `source_chunks` (pgvector `vector(1536)` + HNSW cosine + trigram GIN),
  `audit_logs` (append-only, no FKs — loose UUID references).

## Current phase

**Phase 2 — database foundation.** Async SQLAlchemy plumbing + Alembic +
migrations 0001–0005. Still **no** business logic: no pipeline, evidence,
script, voice, render or auth code.

## Commands

```bash
make up              # start Postgres, Redis, MinIO (waits for healthchecks)
make dev             # run the API (:8000) and web app (:3000) together
make migrate         # alembic upgrade head
make migrate-create name="..."   # new migration
make migrate-down    # alembic downgrade -1
make history         # migration history + current revision
make test            # run the test suites
make lint            # lint TypeScript and Python
```

## Layout

```
apps/
  web/        Next.js 15 front end
  api/        FastAPI service (uv, src/edutoon)
    src/edutoon/db/       engine, session factory, declarative base
    alembic/              migration environment + versions/0001..0005
  parser/     PDF/topic parser — not yet initialised
  render/     Remotion render worker — not yet initialised
packages/
  config/     shared TS config (@edutoon/config)
  contracts/  shared TS contracts/types (@edutoon/contracts)
infra/
  docker/     container bootstrap (Postgres init SQL, etc.)
```

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
| Animation      | Pillow scene compositor + FFmpeg (Remotion not used) |

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
  `apps/api/README.md`, "Script generation", "Atomicity"). Migration 0014
  (Phase 8) adds `script_segment_audio` (one current row per segment,
  `ON DELETE CASCADE` from `script_segments`, `content_hash` fingerprints
  what produced it) and `scripts.measured_duration_ms` (the sum of real
  synthesised-audio durations, kept separate from the pre-audio,
  words-per-minute `estimated_duration_ms` — neither is derived from the
  other; see `apps/api/README.md`, "Text-to-speech"). Migration 0015
  (Phase 9) adds `script_segment_scenes` — not append-only: a retry
  replaces one segment's whole scene set via DELETE + INSERT in a single
  SAVEPOINT, never a per-row `UPDATE` — and widens `ck_jobs_kind` to add
  `generate_storyboard` (see `apps/api/README.md`, "Scene / Storyboard
  layer"). Migration 0016 (Phase 10) adds `render_chunks` (a content-addressed
  cache of rendered scene videos, one row per `(project_id, chunk_hash)`, inserted
  only once the object is uploaded), `render_outputs` (the finished MP4 for one
  `(script, orientation, quality)`) and `render_output_scenes` (which chunk each
  scene used in that output) - no video bytes in the database (`apps/api/README.md`,
  "Video rendering").
- `projects.status` is derived, never set by a client: `edutoon.domain.project_status` is the
  rule and `projects.refresh_status` applies it whenever a job is queued or ends or a PDF fails
  to parse (`apps/api/README.md`, "Project status"). Deleting a project removes its uploaded PDFs
  from object storage after the deletion commits (`services.projects.remove_stored_files`;
  "Deleting a project").

## Current phase

**Phase 9 — the Scene / Storyboard layer: code-complete for everything that can be
verified without provider credentials.** Delivered on top of Phase 8: an approved
script whose narration is _fully measured_
(`scripts.measured_duration_ms IS NOT NULL` - the sole narration-completeness
gate) can now be turned into a deterministic, timed, templated scene timeline,
ready for a future rendering phase. One LLM call per script segment (never one
per scene - `LLM.compose_storyboard_scenes`, on the same Anthropic client
Phases 5/6 already use, no new provider); scene timing itself is never decided
by the model - `domain.storyboard_timing.effective_scene_count` bounds how many
scenes a segment may have from its own measured duration, and
`allocate_scene_durations`/`scene_timings`/`segment_absolute_starts` derive
every scene's exact millisecond position by integer `divmod` and prefix sums,
never rounding or accumulation. Six fixed templates
(`domain.storyboard_template`) with their own slot contracts and
orientation-aware layout (content is orientation-independent; layout is a
deterministic, never-persisted `(template, orientation)` lookup). A hard
provenance rule (`domain.storyboard_provenance`) rejects a whole scene set if
any number, date, percentage, unit or proper noun in its text is not also in
the segment's own narration; a separate soft lexical-overlap signal flags a
scene for reviewer attention without failing the job. Full detail in
`apps/api/README.md`, "Scene / Storyboard layer".

**Resumable and partial-failure-tolerant, like Phase 8's synthesis.** Each
segment's scene set is persisted the moment it finishes, in its own
transaction (whole-set DELETE + INSERT, never a per-row `UPDATE`); one
segment's failure never discards another's success. A `generate_storyboard`
job ends `succeeded` unless every segment failed, and records
`ready_segment_count` / `failed_segment_ids` / `outcome`
(`"complete"` or `"succeeded_with_failures"`) - but _completeness_ is a
separate, always-freshly-computed read-model verdict
(`services.storyboard.get_storyboard_status`), never copied from a job's own
historical result, so the UI can never show a finished-looking timeline that
is actually missing sections. A segment too short for even one scene at the
configured floor (`MIN_SCENE_DURATION_MS`) gets none, deterministically, with
no provider call - never a scene squeezed under the minimum.

**Pending real-provider verification when company API keys become available**
(nothing below is verified; see `apps/api/README.md`, "Testing, and what is not yet
verified against a real provider"): the keys and model availability
(`LLM_MODEL`, `EMBEDDING_MODEL`, `SCRIPT_LLM_MODEL`), long-PDF extraction, the quality of
candidate claim extraction, the teacher-style tone and quality of `compose_script_outline` / `compose_script_section`, the
real quality of `compose_storyboard_scenes`' template choices and provenance
compliance, `apps/api/scripts/smoke_test_phase4.py` run unmodified, and the
real quality, latency and token behaviour of every prompt. No test calls an
LLM/embeddings provider; those that involve one carry
`pytest.mark.provider_mocked` - Piper is the one provider in this project
that is never mocked (see "Text-to-speech"). `en-IN` still narrates with a
labelled `en-GB` substitute voice, not a genuine Indian-English one, and
`piper-tts`'s GPL-3.0-or-later licence is still unreviewed for
production/customer-facing distribution - both still pending real decisions,
not engineering work.

**The web front end is built** (`apps/web`, see its README): landing, Clerk sign-in,
dashboard, project list, create-project wizard, project workspace, topic review with
evidence, script review and approval, Script Studio's narration panel, and now
its storyboard panel alongside it - the worst-case AI-call estimate before
generating, live progress, a fresh complete/incomplete verdict with a retry
action, and the per-segment scene timeline (template, exact time range,
content preview, illustration slots always shown as "Illustration pending",
the provenance reviewer flag as a warning). Real Clerk sign-in is
**unverified** - the Clerk keys are placeholders - as are the real
LLM/embeddings providers.

**Phase 10 - professional video rendering: built, and verified end to end on this
machine (see `apps/api/README.md`, "Video rendering").** An approved script with fully
measured narration and a complete storyboard renders to a real MP4: H.264 High +
AAC-LC mono 22.05 kHz, **60 fps** (H.264 level 4.2 for `high`, 3.2 for `low` - the lowest
valid levels at that rate, derived not trusted; the pre-API pass below), 1920x1080 or
1080x1920 (`high`) or half-size (`low`, a review render), with `+faststart`. The engine is a
**Pillow scene compositor feeding FFmpeg** behind a `Renderer` interface (`providers/renderer`); Remotion was rejected on
licensing (a paid company licence above three people) and is not used. All six
storyboard templates render from text alone; since Phase 11 an illustrated scene's diagram
is drawn procedurally from a structured `visual` (below), and a scene without one is drawn
as deliberate text - never a placeholder panel (no image generation exists).

The design rests on one idea: **one scene is one render chunk, stored under a
fingerprint of exactly the inputs that decide its bytes** (`domain.render`: template,
content, duration, orientation, quality, encoder, fps, renderer version, encoding
profile; since Phase 11 also the scene's fade edges and, for illustrated templates, a visual
drawing version). Nothing else - not its position, its absolute start or the narration - so
editing one scene, re-narrating one segment, or changing one topic re-renders only the
scenes it touches, and everything else is found already rendered. Timing is
absolute: `frame = round(t_ms * fps / 1000)` (`domain.render.frame_at`, integer
half-up), frame counts telescope to exactly `frame_at(total_ms)`, and a chunk is always
rendered with `ceil` frames and trimmed by at most one at assembly without re-encoding
(no B-frames). Narration is assembled sample-exactly at each segment's absolute start.
The finished video is verified with `ffprobe` before it is published.

`render_video` jobs (`services.render`) follow the storyboard job's conventions:
enqueue behind a prerequisite gate (approved script, ready and current narration, complete
storyboard, valid timings, `MAX_RENDER_DURATION_MS` = 10 minutes, free disk space), one
active render per project (409), an `Idempotency-Key`, independent progress commits, stale
recovery, sanitised errors. Each chunk is uploaded and recorded in its own transaction the
moment it finishes, so a crash or a failed chunk loses nothing already made and a retry
renders only what is left. `GET .../render/video` streams with HTTP `Range` support.

The front end adds a Video card to Script Studio (shape and quality, estimate, real
progress, failure and retry, a player and download) and a per-scene render-state badge
(ready / needs render / stale / failed) on the storyboard's scene cards. The current
maximum is 10 minutes; nothing in the chunked design depends on that number.

**Not decided / not verified (Phase 10):** H.264 and AAC patent licensing, and shipping an
FFmpeg build with libx264 (GPL), are business/legal decisions that choosing an encoder does
not settle; the front end plays the video from a `blob:` URL (the whole file is fetched before
it plays - fine at 10 minutes, worth a proxy route for much longer videos); real Clerk
sign-in is still unverified.

**Phase 11 - educational visuals (Phase 11B): built and verified locally; not pushed (see
`apps/api/README.md`, "Educational visuals", "Transitions and pacing", "Timings on read",
"Glyph safety").** A scene's diagram is drawn by the compositor from a closed `visual` spec in
its `illustration` - five kinds only (`process_flow`, `cycle`, `hierarchy`, `comparison`,
`labelled_components`), validated by `domain/visual.py`, described to the LLM by a JSON schema
generated from the same constants (`domain/visual_schema.py`), laid out purely with integer
geometry and an injected text measurer (`domain/visual_layout.py` - a genuinely different design
per orientation) and painted by `providers/renderer/visual_*.py`. No image generation, no
chart / map / photo kinds, no new table, bucket, dependency or migration (head is still 0016).
A structurally invalid visual is **dropped, not fatal**: the scene stays, is drawn by its
template's deterministic fallback, and is counted in `dropped_visual_count` (shown in the
workspace); a provenance violation - including nested text and numeric leaves - is still a hard
failure. The fingerprint gains a visual drawing version and the fade lengths;
`RENDERER_VERSION` is `pillow-ffmpeg/3` (a glyph-safety fix for `→ ≤ ✓` and friends - a one-time,
intentional invalidation of cached chunks). Scenes of one topic are joined by a straight cut and
fade only at topic boundaries and the video's ends (`RENDER_FADE_IN_MS` / `RENDER_FADE_OUT_MS`,
240 / 200 by default). `GET .../storyboard` now computes `start_ms` on read from the current
narration and reports `timings_authoritative`, so a re-narration can no longer leave later
scenes showing stale times.

**Not verified (Phase 11):** real LLM visual generation (the tool schema and prompts are tested
against a stand-in only); the procedural vocabulary is deliberately small (a concept outside the
five shapes is drawn as text); the Pillow / FreeType library versions are deliberately **not** in
the fingerprint (a documented gap - bump `RENDERER_VERSION` when upgrading them).

**Pre-API reliability pass (after Phase 11, before any real key is used).** Migration head is
still **0016**; no phase was started; nothing was run against a real provider (see
`apps/api/README.md`, "Reliability and safety before the first real-provider run", which
separates implemented / verified locally / unverified / future work). In short: production video
is **60 fps** with a valid H.264 level and `RENDERER_VERSION` `pillow-ffmpeg/4`; embeddings are
batched to the provider's limits (never truncating) and transient provider failures are retried by
one policy; every language-model response is checked for refusal, truncation and malformed tool
calls before it is read, thinking is explicitly disabled, and the model is unchanged
(`claude-sonnet-5`); token usage is recorded by kind of call on every job's `result["usage"]`
(`GET /v1/projects/{id}/jobs/usage`); a per-job provider-call tripwire and a per-project hourly limit
guard spend (the provider consoles' spend limits must still be set); a target the verified claims
cannot honestly fill is refused before any paid call (`domain/evidence_sufficiency.py`), a script's
figures and names must come from its claims, and a storyboard illustration that fails provenance falls
back instead of failing the segment; oversized uploads are refused before parsing, pages are counted
before extraction and a PDF with no text is a clear failure; **keyword-only projects are switched off**
(`KEYWORD_ONLY_PROJECTS_ENABLED`); the UI offers any length to the second from 1:00 to **10:00** (`MAX_RENDER_DURATION_MS`; the longest
render verified at 60 fps was 9.7 minutes) while the model keeps 1-60 minutes; a live
job is never reclaimed as stale and a reclaimed worker stops. Not settled: H.264/AAC/x264/Piper licensing,
long-video (over ten minutes) work, cleanup of superseded scripts' audio and videos.

**Phase 12 has not started and needs the owner's explicit approval.**

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
    alembic/              migration environment + versions/0001..0016
  parser/     PDF/topic parser — not yet initialised
  render/     placeholder - the renderer lives in apps/api/src/edutoon/providers/renderer
packages/
  config/     shared TS config (@edutoon/config)
  contracts/  API contract: openapi.json + generated TS types (@edutoon/contracts)
infra/
  docker/     container bootstrap (Postgres init SQL, etc.)
```

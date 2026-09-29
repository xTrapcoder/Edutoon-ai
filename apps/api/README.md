# edutoon-api

FastAPI service for EduToon AI.

```bash
uv sync
uv run uvicorn edutoon.main:app --reload --port 8000
```

Requires the environment variables listed in the repository root `.env.example`.

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
topics, jobs, claims and scripts. It also removes the PDFs the project uploaded from object
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
slot is only ever `{"description": ..., "subject_labels": [...]}` - no image
is generated in this phase, and the frontend always shows "Illustration
pending" for one, never a real picture. `content` is orientation-independent;
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
2. **Keywords-only extraction** against a live model.
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

## Known limitations

- **No way to cancel a job.** An extraction queued while no worker is running holds
  the project's keyword and wording edits (`409`) until a worker takes it.
- **Only `extract_topics` and `extract_claims` jobs are recovered after a worker
  crash.** An abandoned `build_evidence` or `generate_script` job stays `running`.
- **No retries.** A failed job is run again by a new request with a new key.
- **One model call over a budgeted sample.** A long document is sampled evenly
  (`truncated` says so), not read in windows; an outline the model returns that is
  too deep or has too many topics fails the extraction rather than being trimmed.
- **No per-user rate limit or quota** on extraction: each `replace` run costs a
  model call. Credits come with the ledger, in a later phase.
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
- **No image is generated for a storyboard's illustration slots.** An
  illustration is a description and up to three subject labels only, shown as
  "Illustration pending" everywhere - generating the actual image is a future
  phase's work, not part of Phase 9.
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
2. **Include `email` in the session token.** By default Clerk's session
   token only carries `sub`; this API's Clerk provider
   (`src/edutoon/providers/clerk.py`) also requires an `email` claim to
   provision a `users` row. In the dashboard, go to **Sessions > Customize
   session token** and add:
   ```json
   { "email": "{{user.primary_email_address}}" }
   ```
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

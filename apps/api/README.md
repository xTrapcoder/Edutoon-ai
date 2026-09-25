# edutoon-api

FastAPI service for EduToon AI.

```bash
uv sync
uv run uvicorn edutoon.main:app --reload --port 8000
```

Requires the environment variables listed in the repository root `.env.example`.

## Mutating routes

Every `POST` / `PATCH` / `DELETE` requires an `Idempotency-Key` header of 1 to 255
characters (missing, empty or longer is `422`) and dedupes on it: a repeat of the same
(caller, method, path, key) replays the first response instead of running again. Each
handler commits its transaction *before* its result is cached or returned, so a `2xx` -
and a replay of one - always means the change is stored, and a commit that fails leaves
nothing cached. `tests/test_idempotent_routes_contract.py` fails for any mutating route
that loses either guarantee.

## Job worker

Routes only ever *enqueue* a `jobs` row (indexing, topic extraction, script
generation); a separate worker process claims and executes them. Run it
alongside the API (`make dev` starts both, plus the web app):

```bash
uv run python -m edutoon.worker
```

It builds two LLM clients from the same API key, one per model: `LLM_MODEL` for
claim judging and topic extraction, and `SCRIPT_LLM_MODEL` for writing scripts. A
`generate_script` job runs on the second, and the script it produces records the
model that actually wrote it (a script is queued for the API's script client's model,
and the job replaces that with the one it ran with if they differ).

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

**Not built yet:** turning topics into candidate claims. Claims are still typed
in by hand, so a script's facts do not yet flow from the topic outline.

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

## Known limitations

- **No way to cancel a job.** An extraction queued while no worker is running holds
  the project's keyword and wording edits (`409`) until a worker takes it.
- **Only extraction jobs are recovered after a worker crash.** An abandoned
  `build_evidence` or `generate_script` job stays `running`.
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
- **A retry after a failed commit repeats the work.** Every mutating route commits
  before it caches its result, so a failed commit is never cached as a success - but
  the client's retry then runs the handler again. For claim verification that
  repeats the paid embedding and LLM call; for an upload, an object written just
  before a failed commit stays in storage, and a retry overwrites it (its key is
  derived from the file's checksum) rather than adding a second one.

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

Neither value is a secret key — they're derived from your instance's public
Frontend API URL, safe to share within the team (just not worth committing,
hence `.env` staying git-ignored). No Clerk account is needed to run the
test suite: `tests/test_auth.py` signs its own test JWTs against a local,
throwaway RSA keypair and never calls Clerk.

# @edutoon/web

The EduToon AI front end: Next.js 15 (App Router), TypeScript, Tailwind CSS 4, TanStack Query, and
Clerk for sign-in. It is a client of the API in `apps/api` and of nothing else.

It covers everything the API can do today: sign in, projects, PDF and keyword sources, generation
settings, topic extraction and review (with evidence), script generation, review and approval.
It does **not** cover scenes, narration, animation or rendering - those stages do not exist yet, and
the UI describes them as "coming soon" rather than offering them.

## Run it

Needs Node **22 or newer** (the repository's `engines`), and pnpm via Corepack.

```bash
pnpm install
cp apps/web/.env.local.example apps/web/.env.local   # then fill in the Clerk keys
make dev                                             # API :8000, web :3000, worker
```

`apps/web/.env.local.example` explains every variable. The Clerk keys must belong to the **same Clerk
instance** the API trusts (`CLERK_ISSUER` / `CLERK_JWKS_URL` in `apps/api/.env`), and the session token
needs an `email` claim - both are described in "Clerk Local Setup" in `apps/api/README.md`.

## API contracts (generated, never hand-written)

The API is the single source of truth for every request and response shape.

```
FastAPI app ──(apps/api/scripts/export_openapi.py)──▶ packages/contracts/openapi.json
                                                      │  openapi-typescript
                                                      ▼
                                       packages/contracts/src/schema.d.ts ──▶ @edutoon/contracts
```

```bash
make contracts         # regenerate both files after changing an API route or schema
make contracts-check   # fails if either is stale (use in CI)
```

`packages/contracts/index.ts` only _aliases_ generated types (`Project`, `Topic`, `Script`, ...).
`lib/api/endpoints.ts` calls the API through `openapi-fetch`, so a route or field the API does not
have is a compile error. Never re-declare an API shape in the web app.

One thing the generated types cannot say: the API's real error body is
`{ "error": { "code", "message", "details"? } }`, but its OpenAPI document declares FastAPI's default
`{ "detail" }` for 422s. `lib/api/errors.ts` therefore reads error bodies defensively.

## How it is organised

```
app/                      routes: landing, sign-in/up, and (app)/ for everything behind sign-in
  (app)/dashboard         recent projects
  (app)/projects          list, /new (the wizard), /[projectId] (overview), /topics, /script
components/
  ui/                     button, card, badge, field, modal (<dialog>), feedback (alert, empty, error, progress)
  app-shell/              sidebar, top bar, mobile drawer
  wizard/                 create-project steps, PDF dropzone, keyword input, creation orchestration
  workspace/              the project frame, overview, activity, job progress, add source, edit settings
  topics/  script/        the Topic Understanding and Script Studio screens
lib/
  api/                    client, endpoints, TanStack Query hooks, error handling
  workflow.ts             a project's stage, derived from real API data
  jobs.ts                 job helpers, wait assessment, safe failure messages
test/                     Vitest setup, MSW server, typed fixtures
test-support/             UI QA tooling (mock API, Clerk stub) - never part of a build
```

### Rules the code holds to

- **Nothing is faked.** Every number, status and list on screen comes from the API. Where the API
  cannot say something (a percentage, a stage), the UI does not either.
- **Every async operation has loading, error and, where it applies, empty and success states**, and no
  request can spin for ever: reads time out after 20 s, and a timed-out read is retried once.
- **Errors are for people.** `describeError` turns any failure into plain words. Text from the API is
  shown only if it is short and non-technical (`safeServerMessage`); stack traces, SQL, provider
  responses, paths and keys are never shown, whatever the server sends.
- **Every write sends an `Idempotency-Key`.** A retry after an _unknown_ outcome (timeout, dropped
  connection, server fault) reuses the key so the API deduplicates it; a new request, a definite
  rejection or a success starts with a fresh one (`useIdempotentMutation`).
- **A stage is only shown as reachable when it is.** Later stages are visible but not links.
- British English and 24-hour time in all copy (`lib/format.ts`).

### Project stage

`projects.status` never changes from `draft` in the API today, so the stage is derived
(`lib/workflow.ts`) from the project's sources, topics, jobs and scripts. A project _list_ row fetches
two small lists (jobs and scripts) per row shown. If the list grows, a server-side stage on
`GET /v1/projects` would remove those requests; that is a backend change and has not been made.

## The script step, and what the API does not do yet

`POST /v1/projects/{id}/scripts` refuses (400) until the project has at least one **supported claim**,
and nothing yet turns topics into claims to verify. So for a normal project the Script step shows
**"Verification required"** and offers nothing that would fail. It checks `GET .../claim-verifications`
first, and treats the API's refusal the same way if it arrives anyway. As soon as a project has a
supported claim, "Generate script" appears and the rest (progress, Script Studio, approval, "What's
next?") is live. Nothing here fakes a script.

## Tests

```bash
pnpm --filter @edutoon/web test        # Vitest: unit, page-level (MSW) and axe accessibility checks
pnpm typecheck && pnpm lint
```

- **Unit tests** cover the logic the product's promises rest on: workflow stages, error screening,
  idempotency-key reuse, job wait assessment, wizard validation, topic tree.
- **Page-level tests** mount the real screens against MSW-served API responses (`test/render.tsx`):
  the wizard, verification-required, generation and approval, the topic states, error recovery.
  MSW is set to fail on any request a test did not describe.
- **`test/a11y.test.tsx`** runs axe-core over the key screens. Colour contrast cannot be measured in
  jsdom; the palette was checked separately and every text pair used clears WCAG AA (4.5:1).

### What is _not_ verified

- **Real Clerk sign-in.** The Clerk keys in `apps/api/.env` are placeholders, so sign-in has never been
  exercised end to end. The code uses the real `@clerk/nextjs`; a real development Clerk instance is
  needed to verify sign-in, the `email` claim, and the API accepting the token.
- **Any real provider.** As in the API, topic extraction and script writing have not run against a real
  model (see the API README). The UI has only ever seen those jobs through the API's contract.
- Layout was checked in a browser at desktop and phone widths against the mock API below, not on real
  devices.

## UI QA mode (development only)

To look at the signed-in screens without a Clerk instance, run the mock API and the app in QA mode:

```bash
pnpm --filter @edutoon/web mock-api    # :8787 - sample projects in different stages
pnpm --filter @edutoon/web dev:qa      # :3000 - real pages, a stubbed Clerk, the mock API
```

`MOCK_SCENARIO=empty` (a new account) and `MOCK_SCENARIO=errors` (every call fails) exercise the empty
and error states; `MOCK_WITH_CLAIMS=1` pretends a project has a verified claim, so script generation
can be tried. Timings are tunable (`MOCK_QUEUE_S`, `MOCK_RUN_S`, `MOCK_LATENCY_MS`).

**This is not authentication.** `dev:qa` swaps `@clerk/nextjs` for `test-support/qa` through a
webpack alias that `next.config.ts` refuses to enable during `next build` or `next start`, and shows
a banner on every signed-in page. The mock API accepts any bearer token. Neither is imported by application
code, and the real Clerk integration and the API's authentication are untouched.

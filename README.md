# EduToon AI

EduToon AI turns source material into a **verified, narrated, animated explainer video**, and can
publish finished videos as training modules with assessments. It runs entirely on your own machine:
local language model (Ollama), local text-to-speech (Piper), local OCR (RapidOCR) and FFmpeg. There
is no recurring paid API.

The promise: every claim in a document-based video is checked against your source, and nothing
unsupported is meant to reach the script or the screen. Read [What is and is not checked](#what-is-and-is-not-checked)
before relying on that.

## What you can make

| Input | How | Notes |
| --- | --- | --- |
| **PDF** | Upload | Text PDFs are read directly. **Scanned or image-only PDFs are read with local OCR** (see below). |
| **Pasted text / notes** | Paste (300 to 200,000 characters) | Topics are found from about 40,000 characters, sampled evenly; claims are checked against all of it. |
| **PDF + keywords** | Upload and type topics | Keywords become your own topics. A keyword the PDF does not back is reported as *not in your document* and is never taught as if the PDF said it. |
| **PDF (or text) + instructions** | Free-text box | "Make it suitable for a beginner", "Focus on chapter 2", "Use simple examples". See [Instructions](#instructions). |
| **Topic only** | Name a topic | Written from the model's general knowledge. Nothing is checked against a document; the claims are marked `general_knowledge`, the script says so in its first spoken sentence and the UI labels it. Needs `KEYWORD_ONLY_PROJECTS_ENABLED=true`. |
| **PowerPoint** | Upload `.pptx` | A deterministic deck-to-video path; no model writes it, so instructions do not apply. |

The pipeline for every mode: source, topics (you accept or reject), claims, script (you review and
approve), narration, storyboard, video (MP4, at most 720p, 30 fps, 1 to 30 minutes).

## Requirements

- Windows, macOS or Linux; **Docker** (Postgres + pgvector, Redis, MinIO), **Node 22+** with Corepack
  pnpm, **Python 3.12** with [uv](https://docs.astral.sh/uv/), **FFmpeg** on `PATH` (with libx264).
- **[Ollama](https://ollama.com)** with two models: `ollama pull qwen2.5:7b-instruct` and
  `ollama pull bge-m3`.
- About 16 GB RAM. A GPU helps but is not required; the reference machine is an i7-12700H with an
  RTX 3050 Ti (4 GB), where the 7B model runs partly on the CPU.
- **OCR needs nothing extra to install**: `uv sync` installs `rapidocr`, `onnxruntime` and
  `pypdfium2` (all free; the OCR models ship inside the wheel; nothing is downloaded or sent
  anywhere). If they are missing, uploading a scanned PDF fails with a message that says exactly
  this.

## Set up and run

```bash
make install            # pnpm install + uv sync
cp .env.example apps/api/.env              # then see the settings below
cp apps/web/.env.local.example apps/web/.env.local
make up                 # Postgres, Redis, MinIO (waits for health checks)
make migrate            # alembic upgrade head (currently 0024)
make voices             # download the Piper voice files
make dev                # API :8000, web :3000 and the job worker together
```

Settings for the free local path (`apps/api/.env`): `AI_BACKEND=local`, `LLM_MODEL=qwen2.5:7b-instruct`,
`SCRIPT_LLM_MODEL=qwen2.5:7b-instruct`, `EMBEDDING_MODEL=bge-m3`. The two cloud API keys must still be
set to *something* but a local backend never uses them. Useful switches:

| Setting | Default | Meaning |
| --- | --- | --- |
| `OCR_ENABLED` | `true` | Read scanned PDF pages with local OCR. |
| `OCR_MAX_PAGES` | `30` | Most scanned pages read in one document (about 5 s a page). Longer scans are refused with a message, never half-read. |
| `SCRIPT_SUPPORT_CHECK_ENABLED` | `true` | Judge every script sentence against the verified claims (see below). Costs minutes per script. |
| `KEYWORD_ONLY_PROJECTS_ENABLED` | see `config.py` | Topic-only lessons. |

Sign-in uses Clerk (`apps/web/.env.local`). To try the product without Clerk, development only:

```bash
cd apps/api && uv run python scripts/dev_qa_api.py          # API that accepts the UI QA stand-in token
pnpm --filter @edutoon/web dev:qa:real                       # web against it
cd apps/api && uv run python -m edutoon.worker               # the worker
cd apps/api && uv run python scripts/make_qa_inputs.py       # sample PDF, scanned PDF, text in var/qa_inputs
```

In that mode the banner has a switch between the administrator and the employee.

## The training platform

Administrators (`/admin/training`) create courses, publish a rendered video as a module, order the
modules, write single-answer assessments (pass mark stored on the assessment, 70% by default),
enrol employees and invite people. Employees (`/learn`) see only what they are enrolled in:
modules open in order, a module unlocks when the one before is passed, the pass is permanent, and a
submitted attempt is graded and fixed. Roles come from the database, never a token; an employee
gets a 404 for every project route.

## What is and is not checked

Be exact about this when you demo it:

1. **Claim verification** retrieves the closest passages by embedding and asks the local 7B model
   whether they support the claim. A "supported" claim must cite a passage. This is model judgement,
   not proof.
2. **Script citations and names**: each passage must cite supported claims, and every figure, date and
   capitalised name in it must appear in them (`Earth`, `Sun`, `Moon` are exempt).
3. **Sentence-level support check** (new): each sentence is judged against the claims its section may
   rest on; one unsupported is rewritten once, then removed, and what was removed is listed on the script
   page. It catches reversed causes and invented requirements that word overlap cannot (it removes
   "plants need water to open their stomata" from the photosynthesis example). It is a 7B model, so it
   can still miss an error or remove a fair sentence.
4. **Nothing here is a human review.** Narration quality has not been listened to by a person.

## Instructions

Free text, up to 2,000 characters, kept on the project. Audience, tone, a page range or chapter and
focus terms are understood by fixed rules and applied in code; definitions and simple examples are passed
to the writers as guidance. Requests the product cannot meet (images, another language, facts from outside
the document, a different length, quiz questions) are named back to you, never silently dropped. Instructions
can never add a fact.

## Scanned PDFs (OCR)

A page whose text layer is empty is drawn locally and read by RapidOCR. Pages OCR cannot read
confidently stay empty; a document nothing can be read from fails with a specific reason (not installed,
too many pages, nothing readable). Limits: no layout analysis (multi-column pages may interleave), words
can run together inside a line, handwriting and tiny print are unreliable. Rescan at 300 dpi, upright,
dark text on a light page for best results.

## Hardware and timing (this laptop)

Local generation is slow; do not promise instant video. Measured on the reference machine:
topic extraction about 2 minutes, claim verification about 10 minutes for 9 topics, script about 8 to 20
minutes (more with the support check), narration about 20 seconds, storyboard 10 to 20 minutes, render under
2 minutes for a 5-minute video. Run one job at a time. See `apps/api/README.md` for the measurements.

## Tests and checks

```bash
make test               # backend (pytest) and web (vitest)
make lint               # eslint + ruff + mypy
make contracts-check    # the committed API contract is current
```

## Troubleshooting

- **A scanned PDF fails "text recognition is not available"**: run `uv sync` in `apps/api` and restart the API.
- **Script fails or is cut short**: the failure text on the script page says which rule it broke. Sentences that
  add a name or figure are removed; a section with nothing usable left fails.
- **"9 accepted topics cannot each receive a section"**: shorten the list or raise the target duration.
- **The UI looks frozen in a background tab**: polling pauses when a tab is hidden; reload it.
- **Ollama is slow or out of memory**: close other GPU programs; `OLLAMA_NUM_CTX` can be lowered.
- **A worker started before a code change keeps the old code**: restart `make dev` / `make worker`.

## Known limitations

See `apps/api/README.md` ("Known limitations", "Testing, and what is not yet verified against a real
provider"). Not settled: H.264/AAC/x264/Piper licensing, `en-IN` narrates with an `en-GB` substitute voice,
real Clerk sign-in and invitation email delivery are unverified, and no person has listened to the narration.

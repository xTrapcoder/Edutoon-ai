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
| **Topic only** | Name a topic | Written from the model's general knowledge. Nothing is checked against a document; the claims are marked `general_knowledge`, the script says so in its first spoken sentence and the UI labels it. On by default (`KEYWORD_ONLY_PROJECTS_ENABLED=false` refuses the mode). |
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
| `MAX_CLAIMS_PER_TOPIC` | `10` | How many facts one topic can carry; this is what limits a lesson's length. `4`, once recommended for small machines, caps every lesson hard: use `8` there. Set it in the process that runs the **worker**. |
| `SCRIPT_FIXED_CLOSING` | `true` | A script that does not end in a closing gets a fixed one naming the topics it covered. |
| `SCRIPT_FALLBACK_TO_CLAIMS` | `true` | A topic whose wording cannot be supported is read out as its verified claims instead of failing the script. |
| `STORYBOARD_FALLBACK_TO_TEXT` | `true` | A segment whose scene text cannot be grounded gets one bullet scene made of its own narration instead of failing. |
| `KEYWORD_ONLY_PROJECTS_ENABLED` | `true` | Topic-only lessons. |

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

Local generation is slow; do not promise instant video. Run one job at a time. Measured on the reference
machine (i7-12700H, 16 GB, RTX 3050 Ti 4 GB; `qwen2.5:7b-instruct` through Ollama) through the real UI on
2026-10-18, one run each. Finished videos are all 1280x720, 30 fps, H.264 High + AAC mono 22.05 kHz, decode
cleanly and play in the app after a reload. Stage times, seconds:

| Run | Topics | Claims | Script | Narration | Storyboard | Render | Video |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| A. PDF (3 pages) | 110 | 1,108 | 684 | 11 | 508 | 16 | 2:16, 4.4 MB |
| B. Pasted notes | 78 | 1,162 | 1,106 | 8 | 323 | 14 | 1:44, 3.3 MB |
| C. PDF + keywords | 215 | 1,698 | 1,864 | 9 | 624 | 14 | 2:19, 4.3 MB |
| D1. Same PDF, beginner | 171 | 1,219 | 960 | 11 | 458 | 17 | 2:57, 5.4 MB |
| D2. Same PDF, advanced, pages 3-4 | 103 | 1,276 | 1,034 | 7 | 286 | 9 | 1:51, 3.4 MB |
| E. Scanned PDF (OCR in the upload) | 173 | 698 | 371 | 8 | 416 | 9 | 1:34, 2.8 MB |
| F. Topic only | 104 | 125 | 1,087 | 14 | 565 | 25 | 4:08, 7.2 MB |

Claims and the script are the slow stages (the sentence check judges every sentence; claims are verified
one by one, up to `MAX_CLAIMS_PER_TOPIC` per topic). Expect roughly 40 to 60 minutes of waiting for a short
lesson. Two of the scripts above are the second attempt; the first attempt of one failed after 6 minutes and
of another after 17, both for reasons that have since been fixed (see `docs/script-length-investigation.md`).

**A video is usually shorter than its target, and that is correct.** A lesson may only say what its source
says, so it runs about as long as the source takes to read aloud (66 to 103% of it in the runs above). The
sources here were small (228 to 646 words), so a 3 to 5 minute target was out of reach. The script page says
how long the script really is and why. To get a longer lesson, give it more material, add keywords for more
detail, or choose a shorter target.

## Tests and checks

```bash
make test               # backend (pytest) and web (vitest)
make lint               # eslint + ruff + mypy
make contracts-check    # the committed API contract is current
```

Last full run (2026-10-18, worker stopped, machine otherwise idle): backend **5608 passed, 25 skipped**
(24 are parametrised cases that are impossible by design, one needs symlink permission this Windows account
lacks); web **300 passed** in 28 files; ruff, mypy, eslint, tsc and prettier clean; the API contract is
current; migration head 0024, applied. The sentence judge scores 21 of 23 on a small hand-labelled set
(`scripts/check_support_judge.py --eval --verbose`; one false negative, one over-flag; both stomata reversals
are caught). No test calls a model: real-model behaviour is only what the end-to-end runs above show.

Run the suites with the worker **stopped**: a few backend tests claim jobs from the same development queue,
and the web tests time out at their 5-second default while Ollama is saturating the CPU.

## Troubleshooting

- **A scanned PDF fails "text recognition is not available"**: run `uv sync` in `apps/api` and restart the API.
- **Script fails or is cut short**: the failure text on the script page says which rule it broke. Sentences that
  add a name or figure are removed; a section with nothing usable left fails.
- **"9 accepted topics cannot each receive a section"**: shorten the list or raise the target duration.
- **The UI looks frozen in a background tab**: polling pauses when a tab is hidden; reload it.
- **Ollama is slow or out of memory**: close other GPU programs; `OLLAMA_NUM_CTX` can be lowered.
- **A worker or API started before a code change keeps the old code** (an old error wording, an old rule):
  restart `make dev` / `make worker`.
- **A job stays "running" after the worker was stopped.** Stale-job recovery runs only when someone
  queues the same kind of job again (after one hour by default). For a job you know is dead, put it back:
  `update jobs set status='queued', started_at=null where id = '...';` and restart the worker. Restart a
  worker between jobs, not during one, or the job starts again from the beginning.
- **A script fails after many minutes with "mentions '8'" or "Section has N segments"**: both were bugs
  in this version's closing and in the section parser, since fixed; if you see them, the worker is running
  old code.
- **A few backend tests fail while a worker is running**: they claim jobs from the same development queue.
  Stop the worker before `make test`.

## Known limitations

See `apps/api/README.md` ("Known limitations", "Testing, and what is not yet verified against a real
provider"). Not settled: H.264/AAC/x264/Piper licensing, `en-IN` narrates with an `en-GB` substitute voice,
real Clerk sign-in and invitation email delivery are unverified, and no person has listened to the narration.

# Pasted text as a source

Status: built, applied to the development database (head 0023) and run end to end with the local
models on 2026-10-16: a 7,476-character sorting-algorithms passage went through the real UI, API,
worker, Ollama (`qwen2.5:7b-instruct`, `bge-m3`), Piper and FFmpeg to a 4:44, 1280x720, 30 fps H.264/AAC
MP4 (it decodes cleanly; nobody has listened to the narration).

## What it is

A project of `source_type: text` is built from text the owner pastes in, instead of an uploaded file.
It exists so a person with no PDF can still get a *verified* video: the text is the source, and every
claim is checked against it exactly as it is against a PDF's pages. It is **not** a way to get a
video from a bare topic name - with nothing to check against, nothing is verified. A bare topic is a
separate, labelled "topic only" mode (`KEYWORD_ONLY_PROJECTS_ENABLED`, on by default; see the API README,
"Topic-only lessons"), whose claims are never marked `supported`.

## How it works

- `POST /v1/projects/{id}/sources/text` with `{title?, text}` (unknown fields 422, `Idempotency-Key`
  required, administrator-only like every project route, someone else's project 404).
- `domain/pasted_text.py` (pure): `clean` (one newline convention, no control characters, at most one
  blank line in a row), `validate` (300 to 200,000 characters after cleaning, and at least one
  letter), `split_chunks` (whole paragraphs packed up to 2,500 characters, then sentences, then
  spaces; deterministic, nothing lost).
- `services.uploaded_sources.upload_pasted_text`: refuses bad input **before** anything is stored or
  spent (400, unlike a PDF, which records a `failed` row); runs the spend guard; stores the cleaned
  text as `projects/{id}/{sha256}.txt`; records an `uploaded_sources` row (`kind='text'`,
  `page_count` NULL); writes the chunks with NULL `page_from` / `page_to`; marks it `parsed`; queues
  the usual `build_evidence` job. Identical text sent twice to a project is a 409 (the existing
  checksum constraint; CRLF and LF versions of the same text are the same text after cleaning).
- Only a `text` project accepts it (409 otherwise). A `text` project can also have keywords added,
  like a PDF project (they become user topics); the wizard does not offer that yet.
- Everything after indexing - topics, claim extraction and verification, script, narration,
  storyboard, render - reads `source_chunks` and is unchanged. Citations from a pasted text carry no
  page label.

## Migration 0023

Widens `ck_projects_source_type` and `ck_uploaded_sources_kind` to allow `text`. No table or column.
`downgrade` refuses while any text project or source exists.

## Limits and what is not verified

- Topic finding reads at most `TOPIC_INPUT_BUDGET_CHARS` characters (40,000 in the development
  `.env`; 48,000 by default). Text within the budget is read whole; longer text is sampled evenly from
  start to end (`domain.topic_tree.select_passages`), not cut at the first 40,000. Claim evidence
  retrieval is project-wide over every indexed chunk, so claims are verified against all of the text.
- 200,000 characters is at most about 160 chunks, under `MAX_PDF_PAGES`' 300 that the chunk-list
  endpoint assumes.
- The wizard was driven in a real browser against the real API (project, `/sources/text` and the
  indexing job all succeeded). The automated wizard tests still use a mock API.
- The dev-QA mock API (`apps/web/test-support/mock-api`) does not know the `text` type.
- A 3-minute target allows at most six topics (30 s each, `MIN_SECTION_DURATION_MS`); the script step
  refuses more with a clear message, so reject topics first. The 7B model's script ran well over the
  target (4:44 narrated for 3:00 asked): duration is a target, not a gate.

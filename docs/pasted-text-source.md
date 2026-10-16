# Pasted text as a source

Status: built and tested locally (2026-10-16, branch `feat/pasted-text-source`); **migration 0023 is
not applied to the development database** and nothing has been run against a real model.

## What it is

A project of `source_type: text` is built from text the owner pastes in, instead of an uploaded file.
It exists so a person with no PDF can still get a *verified* video: the text is the source, and every
claim is checked against it exactly as it is against a PDF's pages. It is **not** a way to get a
video from a bare topic name - with nothing to check against, there is nothing to verify, and
keyword-only projects stay switched off (`KEYWORD_ONLY_PROJECTS_ENABLED=false`).

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

- Topic extraction still samples at most `TOPIC_INPUT_BUDGET_CHARS` of the text.
- 200,000 characters is at most about 160 chunks, under `MAX_PDF_PAGES`' 300 that the chunk-list
  endpoint assumes.
- The wizard and the workspace paste form are tested only against a mock API; a real browser run of
  the full flow has not been done.
- Not run: a real pasted-text project through the local models (topics, claims, script, render).
- The dev-QA mock API (`apps/web/test-support/mock-api`) does not know the `text` type.

# Final verification, 2026-10-18 (release checkpoint)

Status words: PASS = directly tested and verified; PARTIAL = some criteria pass, gaps named; BLOCKED = needs a
credential, a human or an unavailable dependency; NOT RUN = no execution evidence.

## 1. Scope: exactly five modes (a deliberate product decision)

1. PDF -> video. 2. Pasted text -> video. 3. PDF + keywords -> focused video.
2. PDF + natural-language instructions -> customised video (`INSTRUCTIONS_ENABLED`, now `true` by default).
3. Scanned/image PDF -> local OCR -> video.

The wizard has one card for each (`apps/web/lib/scope.ts`; `scope.test.tsx` asserts exactly
`pdf, text, both, instructions, scan`). Cards 3 to 5 create PDF projects (`source_type: pdf`).

**Topic-only (a typed topic with no document) was removed on purpose.** With no user-supplied source there is
nothing to check a claim against, and the product's guarantee is that generated claims trace back to the user's own
material. There is no setting any more: the API answers `source_type: topic` and a keyword-only topic extraction
with a 400 (`domain.topic.KEYWORD_ONLY_UNSUPPORTED`; tests `tests/test_topic_only_removed.py`,
`tests/test_offered_scope.py`), and the general-knowledge code path (`services/general_knowledge`,
`domain/general_knowledge`, the spoken notice, `include_general_knowledge`) was deleted with its tests. Old
`general_knowledge` claim rows stay readable. PowerPoint decks stay dormant (`PPTX_PROJECTS_ENABLED=false`).
The three historical topic-only demo files were **not touched** and exist today:
`accept_topic_machine_learning.mp4` (7,240,366 bytes), `qa_topic_only_machine_learning.mp4` (4,529,326),
`v2_dormant_F_topic_only_newton.mp4` (3,769,385), all in `apps/api/var/review/`. They are evidence of the former
mode, not of any retained one.

## 2. Historical end-to-end videos (valid outputs of their original run)

Rendered on 2026-10-18 between 07:18 and 07:25 IST through the real UI (`render_outputs.created_at`), renderer
`pillow-ffmpeg/5`, `quality=high`. They are valid historical outputs from that run and are not downgraded because
code changed afterwards. **The pipeline containing the later grounding fixes (section 3) has not been
re-demonstrated end to end with fresh videos.**

Re-checked in this pass: file present; `ffprobe` codecs; full decode `ffmpeg -v error -i F -f null -` (error lines
counted); silence check `ffmpeg -af volumedetect` (mean level; any value near -15 dB is plainly not silence);
streaming `GET /v1/projects/{id}/scripts/{script_id}/render/video?orientation=horizontal&quality=high` with a
`Range: bytes=0-1023` header on the running API (HTTP 206, 1,024 bytes returned).

| File (`apps/api/var/review/`)         | Mode                                 | Subject          | Run date             | Duration | Size (bytes) | Video / audio                       | Decode errors | Mean level | Stream |
| ------------------------------------- | ------------------------------------ | ---------------- | -------------------- | -------- | ------------ | ----------------------------------- | ------------- | ---------- | ------ |
| `accept_pdf_human_heart.mp4`          | 1 PDF                                | Human heart      | 2026-10-18 07:18 IST | 136.2 s  | 4,442,507    | H.264 High 1280x720 30 fps / AAC-LC | 0             | -15.6 dB   | 206    |
| `accept_text_python_basics.mp4`       | 2 pasted text                        | Python basics    | 2026-10-18 07:25 IST | 104.0 s  | 3,255,116    | same                                | 0             | -15.4 dB   | 206    |
| `accept_keywords_human_heart.mp4`     | 3 PDF + keywords                     | Human heart      | 2026-10-18 07:18 IST | 138.6 s  | 4,267,546    | same                                | 0             | -15.6 dB   | 206    |
| `accept_instructions_D1_beginner.mp4` | 4 instructions (beginner)            | Plate tectonics  | 2026-10-18 07:24 IST | 176.8 s  | 5,439,232    | same                                | 0             | -15.2 dB   | 206    |
| `accept_instructions_D2_advanced.mp4` | 4 instructions (advanced, pages 3-4) | Plate tectonics  | 2026-10-18 07:18 IST | 111.4 s  | 3,415,890    | same                                | 0             | -15.7 dB   | 206    |
| `accept_scanned_solar_system.mp4`     | 5 scanned PDF (OCR)                  | Solar system     | 2026-10-18 07:18 IST | 94.3 s   | 2,808,279    | same                                | 0             | -15.0 dB   | 206    |
| `accept_topic_machine_learning.mp4`   | former topic-only (removed)          | Machine learning | 2026-10-18 07:19 IST | 248.3 s  | 7,240,366    | same                                | 0             | -14.8 dB   | 206    |

File modification times (07:19 to 07:26) are the export copies, not the render. **Not recorded / unavailable:** the
Git commit the pipeline was at when each ran (nothing stores it; the last commit before 07:18 was `eabb8bd`, 05:55,
and uncommitted work may have existed); any verification log from the original run other than the database rows.
Mode 4 evidence that the two instruction sets differ (10.1 vs 14.0 words per sentence, Flesch-Kincaid grade 7.0 vs
9.2, pages 3-4 topics only) comes from the earlier report (`docs/script-length-investigation.md` and the session
notes), not from a measurement repeated here.

Not done by anyone: a human watching and listening to these videos.

## 3. Later grounding fixes (committed after those videos, all 2026-10-18)

None of these was re-demonstrated with a fresh video. Each has regression tests; the files below were rerun in this
pass together with the scope tests: **292 passed** (`pytest` on `test_claim_backing`, `test_domain_claim_grounding`,
`test_evidence_service`, `test_domain_keyword_coverage`, `test_domain_claim_dedupe`, `test_narration_support`,
`test_script_support_check`, `test_domain_storyboard_provenance`, `test_services_storyboard`,
`test_storyboard_provenance_fallback`, `test_domain_script_openers`, `test_offered_scope`,
`test_topic_only_removed`, `test_clerk_invitations_provider`).

| Defect                                                                                   | Commit / files                                                                                                     | Regression tests                                                             | Known limitation                                                             |
| ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| A claim marked supported though the passage it cites does not hold its wording           | `2ff23d0`: `domain/claim_grounding.py`, `services/claim_backing.py`, `services/evidence.py`, `services/scripts.py` | `test_claim_backing`, `test_domain_claim_grounding`, `test_evidence_service` | Word-overlap coverage: it can accept a wrong relation between real words     |
| A keyword the document never mentions reported as covered                                | `00d4c36`: `domain/keyword_coverage.py`, `services/keyword_coverage.py`                                            | `test_domain_keyword_coverage`, `test_topics_api`                            | Judged on the keyword's words appearing in the document, not on meaning      |
| A fact written under the wrong topic                                                     | `2912777`, `6d7ccad`: `domain/claim_dedupe.py`                                                                     | `test_domain_claim_dedupe`, `test_services_scripts`, `test_scripts_api`      | A heuristic (fit to title, keywords, summary, word rarity)                   |
| A rejected sentence that restated a claim lost the claim; a section with only a signpost | `b1f8bb1`: `domain/narration_support.py`                                                                           | `test_narration_support`, `test_script_support_check`                        | Depends on the 7B sentence judge                                             |
| An invented example ("like tigers") kept or whole sentence lost                          | `62c6650`, `eabb8bd` (dangling lead-in)                                                                            | `test_narration_support`, `test_script_support_check`                        | The trim is lexical                                                          |
| A slide said what the narration does not                                                 | `fb5ad2d`: `domain/storyboard_provenance.py`, `services/storyboard.py`                                             | `test_domain_storyboard_provenance`, `test_services_storyboard`              | Scenes may still introduce terms the narration does not say; this narrows it |
| Narration-only fallback slide split mid-phrase                                           | `363cf5d`                                                                                                          | `test_storyboard_provenance_fallback`, `test_services_storyboard`            | Text-only cards                                                              |
| A judge citing the same passage twice failed a topic's whole claim extraction            | `526dd99`: `services/evidence.py`                                                                                  | `test_evidence_service`                                                      | none known                                                                   |
| Repeated "Today, we will learn"                                                          | `8b142fe`, `eee57a8`: `domain/script_openers.py`                                                                   | `test_domain_script_openers`                                                 | Rule-based; other repeated phrasings are not caught                          |
| Instructions: "pages 3 and 4" read as a topic                                            | `bb64ab2`                                                                                                          | instruction tests in the full suite                                          | Only the documented patterns are understood                                  |

Numbers reported earlier, with their evidence: the sentence-judge labelled evaluation **21/23** (one false
negative, one over-flag; a prompt change scored 20/23 and was reverted) is recorded in
`docs/script-length-investigation.md`; it is a **historical measurement, not rerun**. "1 of 223 existing claims
flagged by an entity guard" and "removals 19 -> 4 on the same PDF" were **not found in any repository file or test
log** and are therefore omitted rather than repeated.

## 4. Tests run in this pass (idle queue, worker stopped)

| Command (from the repository root unless stated)                                                                                                                                                           | Result                                                                                                                                                                                                             |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `cd apps/api && uv run pytest -q -p no:cacheprovider` (first run, before the test fixes)                                                                                                                   | 172 failed, 5,496 passed, 25 skipped (996 s). Cause: tests that built topic-only projects, which the scope change refuses                                                                                          |
| same, after the fixes below                                                                                                                                                                                | **5,665 passed, 25 skipped, 0 failed** (817 s)                                                                                                                                                                     |
| `uv run pytest` on the 13 learning-platform and access files (route policies, route security, architecture rules, admin/employee/enrolment/assessment APIs and committed tests, access seam, domain tests) | **436 passed**                                                                                                                                                                                                     |
| `uv run ruff check src tests scripts`                                                                                                                                                                      | exit 0                                                                                                                                                                                                             |
| `uv run ruff format --check src tests scripts`                                                                                                                                                             | exit 0 (353 files)                                                                                                                                                                                                 |
| `uv run mypy src`                                                                                                                                                                                          | exit 0 (161 files)                                                                                                                                                                                                 |
| `pnpm --filter @edutoon/web typecheck` (`tsc --noEmit`)                                                                                                                                                    | exit 0                                                                                                                                                                                                             |
| `pnpm --filter @edutoon/web lint` (ESLint)                                                                                                                                                                 | exit 0                                                                                                                                                                                                             |
| `pnpm --filter @edutoon/web test` (Vitest)                                                                                                                                                                 | **306 passed** (28 files), exit 0                                                                                                                                                                                  |
| `cd apps/api && uv run python scripts/export_openapi.py --check` (what `make contracts-check` runs; `make` is not installed on this machine)                                                               | exit 0, contract up to date                                                                                                                                                                                        |
| `pnpm format:check` (Prettier)                                                                                                                                                                             | exit 1: 14 files. **13 are documentation or `.claude/launch.json` that this pass did not reformat** (the pre-existing docs and the unstaged launch file); the one code file, `wizard-model.test.ts`, was formatted |

Test changes made for the scope decision (assertions were kept, not weakened): project fixtures that used
`source_type="topic"` as a convenience became `pdf`; extraction tests that relied on keywords alone now give the
project a parsed page and, where a topic needs evidence, a real chunk id; three tests of the removed mode
(`test_keywords_only_keeps_the_users_topics_and_adds_unverified_suggestions`, `test_keywords_only_needs_no_pdf_chunks`,
`test_keywords_only_flow_end_to_end`) and the general-knowledge test files were deleted with the mode.

The earlier "46 queue/worker failures" were jobs competing for the queue; the earlier run in this session showed 18
failures in the stale-recovery and worker tests that were a fixture problem (a topic without `origin="source"`),
committed by the previous session, and pass now.

## 5. Learning platform and access control

PASS by automated tests (436 passed, above): course and module publishing, enrolment, withdrawal and re-enrolment,
the single identical 404 for unauthorised access, 403 `module_locked`, the 70% pass mark stored on the assessment,
answer locking, sticky pass, and the one-policy-per-route rule. The in-browser walkthrough (fail at 0%, locked,
retry and pass, persistence after reload, module 2 opening only after module 1) was done on 2026-10-18 earlier and is
recorded in `docs/verification-report-2026-10-18.md`; it was **not repeated by hand in this pass**.

## 6. Fresh-input UI checks made in this pass (no new videos)

Through the wizard in the browser (dev stand-in sign-in): five mode cards shown; seven new projects created on
subjects not used before (vaccines PDF, renewable-energy text, coffee PDF + four keywords, bridges PDF with two
instruction sets, a readable and a poor scan). Verified: the missing keywords "Decaffeination" and "Fair trade
certification" are shown as _Not in your document_ with a "Leave it out" action; "Focus only on pages 3-4" gave
only suspension and cable-stayed topics; the readable scan was read by OCR (`extraction_method: ocr`, 2 pages); the
poor scan was refused with "No words could be read ... rescan it at 300 dpi, upright, dark text on a light page",
shown on the workspace page. Generation was then frozen by the release-first decision: the worker was stopped and six
queued or running jobs cancelled (status `cancelled`, queue empty). No claim, script or video exists for those
projects.

## 7. Configuration, authentication, licences, data

- `MAX_CLAIMS_PER_TOPIC=8` in `apps/api/.env`, no override. After the restart in this session the worker started with
  that file. A comparison of 4/8/10 was not rerun. `apps/api/.env`, `var/` and the `.env` backup
  (`C:\Users\durge\edutoon-backups\api.env.2026-10-18.bak`) are outside Git (`git check-ignore` confirms `.env` and
  `var`).
- Clerk: `scripts/check_clerk.py` PASS for configuration. **Real sign-in: not done (BLOCKED, needs a person's
  credentials).** **Invitation email: not delivered (BLOCKED)**: `apps/api/.env` still has no `CLERK_SECRET_KEY`
  (rechecked, count 0); no invitation was sent. Human review of audio and visuals: not done.
- Licences (re-read from package metadata and `ollama show --license`): Piper GPL-3.0-or-later; the FFmpeg build is
  `--enable-gpl --enable-version3` with libx264; `qwen2.5:7b-instruct` Apache-2.0; `bge-m3` MIT; RapidOCR
  Apache-2.0; ONNX Runtime MIT; pypdfium2 BSD-3/Apache-2. Unresolved: GPL implications of Piper and this FFmpeg
  before redistribution, H.264/AAC patent licensing, the voice's own terms. See `docs/licensing-and-credentials.md`.
  No legal conclusion is drawn.
- QA data: nothing deleted. The development database holds 35 projects owned by the QA account (seven added today,
  "Final M..." titles, with cancelled jobs) and 9 owned by the owner, left alone (`docs/qa-data.md`).

## 8. Known limitations (supported by the evidence above)

- Grounding checks vocabulary and source overlap and a 7B judge; it can allow an invented relationship between real
  words, and the sentence judge has both false positives and false negatives (21/23 on its labelled set).
- A lexical backstop was tried and did not reliably separate leaks from legitimate sentences (earlier session
  notes); no figure is claimed here.
- Storyboard scenes may introduce terms the narration does not say; the fix above narrows, not removes, this.
- `en-IN` projects are written in Indian English but narrated by the British voice `en_GB-cori-medium`.
- Visuals are title cards, bullet cards and simple procedural diagrams.
- A short source cannot honestly give a long lesson: a faithful lesson is roughly 66 to 103% of the source's read-aloud
  time (`docs/script-length-investigation.md`).
- Nothing here establishes a hallucination rate; the samples are a handful of short documents.

## 9. Remaining unverified

Real Clerk sign-in; invitation-email delivery; human review of audio and visuals; a fresh end-to-end video from the
current pipeline for each mode; the 4/8/10 `MAX_CLAIMS_PER_TOPIC` comparison; GPL/patent/licensing decisions.

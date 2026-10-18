# Verification and completion report - 2026-10-18

One consolidated, evidence-based record of the final verification pass. It supersedes the status paragraphs
scattered through earlier notes where they differ, and links the detailed documents instead of repeating them:
[`script-length-investigation.md`](script-length-investigation.md) (why lessons are short),
[`licensing-and-credentials.md`](licensing-and-credentials.md), [`human-review-checklist.md`](human-review-checklist.md),
[`qa-data.md`](qa-data.md), [`training-platform-design.md`](training-platform-design.md) and `apps/api/README.md`.

**Status words:** **PASS** - directly executed and verified in this pass. **PARTIAL** - some criteria verified,
a named gap remains. **BLOCKED** - needs credentials, a human or an unavailable dependency. **NOT RUN** - no
execution evidence exists. Anything from an earlier report that was not re-executed is labelled *previously
reported, not re-run*.

**Nobody has listened to or watched these videos as a person.** Every quality statement below that is not an
automated check is the author's reading of frames and scripts, never of sound. See
[Human review still owed](#9-human-review-still-owed).

## 0. Scope decision (owner, 2026-10-18): exactly three modes

The product now offers **PDF -> video**, **pasted text -> video** and **PDF + keywords -> focused video**.
*Topic only*, *PowerPoint decks* and *free-text instructions* are switched **off, not deleted**
(`KEYWORD_ONLY_PROJECTS_ENABLED`, `PPTX_PROJECTS_ENABLED`, `INSTRUCTIONS_ENABLED`, all `false` by default; the
API refuses each with a message that says what to do instead; the web app offers three cards and no
instructions box, `apps/web/lib/scope.ts`). Their code and tests remain: the test environment turns the
switches on so that code stays tested, and two new test files hold the shipped defaults
(`tests/test_offered_scope.py`, `components/wizard/scope.test.tsx`). OCR stays: it is how a scanned PDF is read,
not a separate mode.

**Verified live (PASS):** through the running API a topic-only project, a PowerPoint project and a PDF with
instructions are each refused with HTTP 400 and a stated reason; a PDF project and a pasted-text project are
created normally (the two probe projects were deleted). In the running web app the wizard shows exactly
`pdf`, `text`, `both` as source cards and no PowerPoint card, no topic-only card and no instructions box.

## 1. How a video is made

```
source -> topics (you accept) -> claims (each checked against the source) -> script (you approve)
       -> narration -> storyboard -> video (MP4, 720p, 30 fps)
```

Local language model (Ollama `qwen2.5:7b-instruct`) and embeddings (`bge-m3`), local speech (Piper
`en_GB-cori-medium`), local OCR (RapidOCR), FFmpeg. API FastAPI + PostgreSQL/pgvector + Redis + MinIO; web Next.js.

- **Extraction.** Text PDFs are read directly; a page with no text layer is drawn and read by OCR; pasted text is
  chunked. A PDF with nothing readable fails with a stated reason.
- **Claims** are drafted per topic, retrieved against the document and judged by the model. A supported claim's
  wording must now be in the passage it cites (section 5).
- **Script**: each topic's section is written from its claims; every sentence is judged against them, rewritten
  once, then removed or replaced by the claim it was a wording of; figures and names must come from the claims.
  The script page states its length and why.
- **Storyboard**: a slide may not say what the narration does not (section 5). **Video**: Pillow compositor + FFmpeg,
  verified with `ffprobe` before it is published.

## 2. The three offered modes - fresh inputs, real browser UI (PASS for the pipeline; quality PARTIAL)

Inputs were written for this pass (subjects no earlier run used): `electric_circuits.pdf` (4 pages, 514 words),
`bridges.txt` (431 words), `ecosystems.pdf` (4 pages, 548 words). Every project was created, every topic accepted,
every script approved, narrated, storyboarded and rendered through the browser UI of the running application (the
dev stand-in administrator; real Clerk sign-in is separate, section 7). Target length was the default 5:00; none
reached it, correctly (section 6).

| | A. PDF | B. Pasted text | C. PDF + keywords (focused) |
| --- | --- | --- | --- |
| Input | `electric_circuits.pdf` | `bridges.txt` | `ecosystems.pdf` + 5 keywords |
| Topics / claims (supported) | 10 / 66 | 8 / 61 | 3 taught of 5 / 22 of 34 |
| Final video | **2:35** (155.1 s), 4.7 MB | **1:56** (116.5 s), 3.6 MB | **1:36** (95.5 s), 2.9 MB |
| Narration measured | 155 s | 116 s | 95 s |
| Source read aloud at 150 wpm | 3:26 | 2:52 | 3:39 (whole chapter; 3 of its topics taught) |
| Script's own account | evidence-limited | evidence-limited | evidence-limited |
| Topics 2.. claims 3.. script 4.. narration 5.. storyboard 6.. (s) | 157 / 1,469 / 837 / 10 / 538 | 117 / 1,351 / 521 / 10 / 536 | 151 / 750 / 595 / 8 / 319 |
| First render (s) | 18 | 14 | 9 |

Technical checks on all three (executed, PASS): H.264 High level 3.1, 1280x720, 30 fps, yuv420p; AAC-LC mono
22.05 kHz; the whole file decodes with no errors (`ffmpeg -v error -f null`); no silence over 1.5 s; audio and
video end within 10 ms of each other; mean level about -15 dB with a single full-scale sample (peak-normalised,
flat factor 0, so not clipped). Mode C was also played in the application after a fresh page load: duration
95.50 s, 1280x720, ready state 4, no error, time advancing, audio decoded.

**Mode A.** All 10 topics follow the document's own order and cite the right pages; every claim read against the
source matched it. The final script has one "Today, we will learn" (the writer's four became one after the
cleanup in section 5), defines resistance, conductors and insulators, states V = I x R with Ohm's year, and ends on
a fixed closing naming the topics. Residual: a water-pipe analogy the writer added (analogies are exempt from the
sentence check), "three bulbs" as an illustrative figure, and a "previous lesson" phrasing that is not true.
**Mode B.** Order follows the source (loads, compression and tension, beam, arch, suspension, cable-stayed,
temperature, aeroelastic flutter). One claim marked "supported" contradicted the source ("steel is strong in
compression but weak in tension"; the source says both) - it did not reach the script. Of the first script's
10 removed sentences two were fair and are now kept (section 5). **Mode C.** "ten percent rule", "decomposers",
"keystone species" were covered and taught; "blockchain mining" and "coral bleaching" are reported *not in your
document* and left out; neither word appears in the narration or on a slide.

## 3. Modes kept but outside the offered scope (results retained, not part of the three)

These ran end to end through the UI before the scope decision; their videos are kept as artifacts and are
**not** claimed as offered features.

| Mode (now dormant) | Final video | Note |
| --- | --- | --- |
| D1 PDF + instructions, beginner | 2:51, 5.0 MB, 9 topics, 69 claims, reading grade 7.0, 10.5 words/sentence | all four pages |
| D2 PDF + instructions, advanced, pages 3-4 | 2:32, 4.6 MB, 6 topics, 42 claims, grade 11.9, 16.8 words/sentence | page range respected; terms defined |
| E scanned PDF (OCR) | 0:45, 1.2 MB, 3 topics, 16 claims | OCR read all 3 pages exactly; an unreadable scan fails with `ocr_no_text` and a specific message; a mildly degraded scan misread a heading ("Tiddes") and the topic came out as "Tides" |
| F topic only | 2:03, 3.8 MB, 4 topics, 32 general-knowledge statements | first sentence says it is general knowledge; survived a worker kill and restart mid-job |

Instruction steering (D1 vs D2, same PDF) measured on the final scripts: 10.5 vs 16.8 words per sentence and
Flesch-Kincaid grade 7.0 vs 11.9; 9 topics over four pages vs 6 topics from pages 3-4 only. The earlier reference
(grade 7.0 vs 9.2) pointed the same way. One run each; the direction is clear, the size of the gap is not a
constant.

## 4. Learning platform and access control (PASS, executed in this pass)

Administrator in the UI: created a course; published two modules from finished renders (each gets its own copy
of the MP4); wrote a three-question assessment (pass mark 70% by default) and a two-question one; published the
course; enrolled a learner. Learner in the UI: the course lists module 1 open and module 2 locked, naming the
blocking module; the video plays (136.2 s, 1280x720, ready, no error, audio decoded). Attempt 1 answered 2 of 3
(66%) -> "Not passed"; the answers lock on submit (the question group is disabled); attempt 2 answered 3 of 3 ->
"Passed". Persisted in `assessment_attempts` (threshold 70, 2/3 false, 3/3 true). Module 2 unlocked only after the
pass; the state survived a page reload. API (dev stand-in tokens), all as expected:

| Request | Result |
| --- | --- |
| employee, module 2 video before passing module 1 | 403 `module_locked`, blocking module named |
| same, after passing | 206 `video/mp4` |
| employee, module 1 video | 206 |
| employee, a module that does not exist | 404 `Module not found.` |
| employee, an existing project / a nonexistent project | 404 / 404 (same shape) |
| employee `GET /v1/projects`, `GET /v1/admin/training/courses` | 403, 403 |
| no token | 401 |
| administrator, project read | 200 |
| administrator withdraws the learner; employee requests module 1 | 404, course disappears from the list |
| administrator re-enrols | same enrolment row reactivated (one row, `active`), history kept, module 2 reachable |

## 5. Defects found by reading real runs, and their fixes (all with regression tests built from the run's own data)

| # | Found | Fix |
| --- | --- | --- |
| 1 | A short source makes every topic hold claims about the whole document; the de-duplication scored title words only, so the subject word ("Python") won every tie. Real run: "Functions" taught variables and dictionaries, "Lists and Dictionaries" got one generic sentence, "Errors" opened with the release year. A second run put decomposers and pyramids of biomass under "What is an ecosystem?" | Facts are placed under the best-fitting topic among all topics, scored by title/keyword words (double) and summary words, each weighted by rarity among the lesson's facts; the lesson's own subject word is ignored. After: topics hold their own facts (replayed on real claims, and in the regenerated D1 script) |
| 2 | A keyword the document never mentions ("coral bleaching") was reported "covered by 4 statements": claims are judged against the nearest passages, so true statements about neighbours were supported, and one was worded as a link to the keyword that no passage makes | A keyword is covered only if the document contains its words; the report names the missing words; a script cannot be written over such a keyword until it is left out. Verified in the UI: "Not in your document" + "Leave it out" |
| 3 | The judge approved claims citing passages that do not contain them ("land clearing ... to make way for agriculture or development" cited a page on energy flow; invented elaborations; a true claim cited to the wrong page) | A supported claim's wording must be in its cited passages (floor 50%); below it the citation is re-pointed to the retrieved passage that states it (>= 60%), else `partially_supported`. Scripts apply the rule to claims already stored. On 276 supported claims: 9 elaborations flagged, 8 true claims re-pointed, none of 66 / 61 / 16 in three other runs |
| 4 | The judge removes a whole sentence for one flourish and the lesson loses the only statement of a verified fact, leaving a dangling "these temperature changes" | A rejected sentence that is mostly one verified claim (>= 60% of its words, >= 3) is replaced by that claim's own text, cited, unless already said |
| 5 | A section left as "Today, we will learn about resistance." alone (a signpost can share a word with a claim) | After removal, a signpost-only section reads its verified claims out through the existing fallback |
| 6 | "Today, we will learn ..." opened 4 of 10 sections; a premature wrap-up before the closing; the prompt rule did not stop a 7B writer | The first announcement stays, repeats and premature wrap-ups are dropped; every section keeps a passage |
| 7 | Invented ", like tigers," examples | A comma-delimited like / such as clause whose words no claim uses is removed, the sentence stays |
| 8 | A slide said "This pattern is known as semidiurnal tides." and "The dark side": no number, date or name, so the hard rule could not see them | A bullet or subtitle using two or more words (or over a third of its words) the narration lacks is removed; a bullet slide left with under two bullets is replaced by the narration's own words; decks exempt. Applied to 4-12 scenes per video |
| 9 | The narration-only fallback slide split at every "and" ("After animals" / "plants die") and cut a line after "of" | Whole phrases, never ending on a dangling word |
| 10 | A judge that cited a passage twice failed every claim of that topic ("This source chunk is already cited") | Citations de-duplicated before storing (the new test fails without the fix) |
| 11 | The Clerk invitation request sent Python's default User-Agent; the same valid key got **403** with it and **200** with an ordinary one, so every invitation would have failed | Explicit `User-Agent`; regression test |
| 12 | The topic-only pipeline panel read "32 claims checked, none supported yet" | "32 statements from general knowledge, not checked against a document" (dormant mode, verified in the UI before the scope change) |

**Residual risk, measured and not fixed.** The claim and sentence judges are a 7B model. In these runs: one claim
contradicting the source was marked supported (did not reach the script); one sentence about the Moon's rotation
reached a lesson though no claim or passage says it; invented everyday examples survive when they are not
comma-delimited ("cows eat grass", "Rabbits eat the plants"); and an embellishment inside a claim ("indicating the
presence of more organisms at lower trophic levels") survives when its words overlap the cited passage. A lexical
backstop on each final sentence was measured and rejected: the scores of leaks (0.11 to 0.29 of a sentence's words
in the section's claims) interleave with those of fair sentences (0.14 to 0.33). Reviewing each script before
approval remains the real gate.

## 6. Script length and `MAX_CLAIMS_PER_TOPIC`

A faithful lesson is about 66 to 85% of its source's read-aloud time, so a 5:00 target from a 180 to 550 word
source is unreachable honestly; the script page says so and nothing is padded (details in
`script-length-investigation.md`). **Controlled comparison** - the same 561-word notes, the same code, the worker
started with each value, one run each:

| Limit | Claims verified (supported) | Topics at the limit | Script | Words | Claims cited | Sentences judged / removed | Claims job | Script job | Script's own account |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 4 | 28 (26) | 7 / 7 | 1:54 | 285 | 20 | 29 / 0 | 11 min | 6 min | evidence-limited |
| **8** | 54 (52) | 6 / 7 | **2:34** | 385 | 29 | 35 / 2 | 23 min | 7 min | evidence-limited |
| 10 | 67 (66) | 5 / 7 | 1:58 | 295 | 23 | 87 / 7 | 26 min | 16 min | writer short (claims unused) |

**Decision: 8, kept.** At 4 every topic is capped (the old cause of short videos). At 8 the cap still binds on
6 of 7 topics but gave the longest, fullest script at about twice the claims cost of 4. At 10 there were 24% more claims
but a shorter script (the writer left claims unused), over twice the sentence-judging and script time, and the
highest removal rate (8%). No job failed at any value. Honest limits: one run per cell on a model with run-to-run
variance, so 8 versus 10 is within noise on length; the evidence for 8 is cost and fewer judged-and-removed
sentences, not a measured quality win. Persistence verified: `apps/api/.env` was backed up outside the repository
(`C:\Users\durge\edutoon-backups\api.env.2026-10-18.bak`), only the one line changed from 4 to 8, the settings
loader reads 8 with no environment override, and the worker was restarted the documented way
(`cd apps/api && uv run python -m edutoon.worker`). Previously-reported figures for 4 and 10 on older code
(25 and 79 claims on the same notes) are consistent with this run but are not comparable on length.

## 7. Authentication and invitations

- **Clerk configuration: PASS** (`scripts/check_clerk.py`, executed): issuer https, JWKS reachable with 1 signing
  key, web and API trust the same instance, secret and publishable keys both in test mode.
- **Secret key valid: PASS** (read-only `GET /v1/jwks`, status 200, 1 key; nothing printed from the key).
- **Real interactive sign-in: BLOCKED** - needs a person's credentials; not claimed.
- **Invitation email: BLOCKED on one decision.** The 403 bug (row 11) is fixed, so the code path can now work.
  To finish, the API process needs `CLERK_SECRET_KEY` (the same test key that is in `apps/web/.env.local`), and an
  invitation sends a real email from the owner's Clerk instance. That was not done without an explicit yes. Steps
  are in [`licensing-and-credentials.md`](licensing-and-credentials.md).

## 8. Dependencies, licences, QA data

Licences were read from the installed packages and model manifests, not inferred from runtimes: see
[`licensing-and-credentials.md`](licensing-and-credentials.md) (Piper GPL-3.0-or-later; FFmpeg GPL-3.0 build with
x264; `qwen2.5:7b-instruct` Apache-2.0 and `bge-m3` MIT as packaged by Ollama; RapidOCR code and bundled models
Apache-2.0; Noto fonts OFL; plus the open commercial-distribution questions). Using them locally is not
distributing them; nothing here is legal advice. **QA data: no cleanup performed**; the inventory in `qa-data.md`
was re-verified against the database (the 9 projects under the owner's account were not touched; this pass added
12 verification projects under the QA account, including the ones for the claim-limit comparison). The garbled
module title cannot be renamed by design and is left.

## 9. Human review still owed

Open the videos in `apps/api/var/review/v2_*` and follow `human-review-checklist.md`: listen for pronunciation of
the subject's own words and for pace; confirm the British voice is acceptable for `en-IN` projects; check each
slide matches what is being said (the slides are plain text cards; a few procedural diagrams are meaningless, for
example a two-item list "circuit / circuit breaker"; some closing slides paraphrase loosely); read the script
page's removed-sentence list for the lesson you review. Timestamps worth checking: A 0:00-0:20 (introduction), B
0:35-0:55 (compression and tension), C 0:10-0:25 (ten percent rule). Nobody has done this.

## 10. Tests and checks

PENDING_TESTS

## 11. Operation

```bash
make up                                   # Postgres, Redis, MinIO
make migrate                              # head is 0024
cd apps/api && uv run python -m edutoon.worker       # reads apps/api/.env: MAX_CLAIMS_PER_TOPIC=8
cd apps/api && uv run python scripts/dev_qa_api.py   # API with the dev stand-in token (no Clerk)
pnpm --filter @edutoon/web dev:qa:real               # web against it
```

A worker started before a code change keeps the old code: restart it at the start of a job, never in the middle
of one (stale-job recovery runs only on the API's enqueue path; a killed worker leaves a `running` row to set
back to `queued`).

## 12. Git

PENDING_GIT

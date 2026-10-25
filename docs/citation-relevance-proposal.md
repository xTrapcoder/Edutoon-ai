# Citation relevance in generated scripts - audit and proposal

Status: **items 1-3 implemented 2026-10-16 on the owner's go-ahead (uncommitted); not yet run
against a live generation.** Item 3's flag is recorded in the job result as
`weak_citation_segments`; showing it on the script review page is **not built**. Written after the
attempt-6 script (job `01a14430`, script `01a14430-ad29-7097-8004-07f4bcf20275`).
Pinned by `apps/api/tests/test_script_citation_relevance.py`.

## What the code actually does

- **Rewrite prompt** (`providers/llm.py`, `_format_section_request`): a rewrite re-sends the whole
  section with *every* verified claim of the topic plus a list of what was wrong. For an uncited
  passage the correction says "lists no claim ids and is not marked connective. List the claims it
  draws on, or ... mark it connective." It never says which claim should support the passage, so
  the model may choose any claim id of the topic.
- **Validator** (`domain/script_provenance.filter_and_validate`) checks that (1) a cited id is a
  supported claim, (2) the segment keeps at least one citation or is short and marked connective,
  and (3) with grounding, every number, currency, month name and proper noun in the narration
  appears somewhere in the topic's claims and titles. It does **not** check that a cited claim is
  about the sentence. Check (3) is topic-wide, not per cited claim.
- `_parse_section_draft` only confirms the id was one of the claims supplied for the call.

## Classification

| Item | Class |
| --- | --- |
| Citation relevance is unchecked | **Pre-existing gap**, not introduced by the rewrite |
| A rewrite told "cite or mark connective" can satisfy the check with any claim | **Made more likely by the rewrite loop**; observed |
| Invented figures and names in a rewrite | Still caught (unchanged) |
| A true-looking but unsupported sentence with no new figure or name | Not caught, before or after |

Observed in attempt 6: after the "Channels for Making Investments" rewrite, segment 29 (hand-off
to fixed income securities) cites claims about regulators and advisers, and segment 27 ("direct
investments are like managing your own garden") cites the managed-portfolio claim. Both were
passages flagged "lists no claim ids". The script is mechanically valid and not citation-clean.

## Why a plain lexical gate is not enough

Measured on the saved scripts of attempts 5 and 6 (cited segments only): the share of a segment's
content words found in its cited claims ranges from 0.06 to 0.57 for correct citations, and the
wrong citation of segment 29 scores 0.15. A threshold that keeps every correct citation also keeps
that one, and "shares at least one content word" passes it too. Lexical overlap can find a
zero-overlap citation (segment 27 scored 0.00) and nothing subtler.

## Proposed minimal safe fix (not applied)

All of it only ever **removes** or **flags** citations; none of it adds or loosens anything.

1. **Per-claim zero-overlap drop** in the domain (pure): a cited claim sharing no content word with
   its segment is removed from that segment's citations. A segment left uncited and not connective
   then fails `UnsourcedSegmentError`, which already sends it to a rewrite. Catches segment 27,
   not 29.
2. **Name the claim in the rewrite correction.** For an uncited passage, tell the writer the claims
   whose wording overlaps it most (computed, ranked) and say that if none fits it must be
   connective or rewritten without the fact. Removes the "pick anything" freedom that produced 29.
3. **Reviewer flag, not a gate**: record `weak_citation_segments` (cited segments whose best cited
   claim covers under a stated fraction of the narration) in the job result and show it in the
   script review page, like the storyboard's lexical-overlap flag. Catches 29 for a human without
   rejecting good analogy segments.
4. **Human approval stays the real gate**: a script is never rendered unapproved, and approval
   should show the flag from (3).

Not proposed: an LLM judge of support (a second unverified model call inside the provenance path),
and any relaxation of the figure / name check.

## Decisions needed from the owner

- Whether to implement 1-3 (small: one domain function, one correction-text change, one result
  field) before the next run, or accept the limitation for the first proof run.
- Whether the first video may be rendered from a script with known weak citations at all, as a
  pipeline proof only.

## What was implemented

- `domain/storyboard_provenance.content_words`, `domain/script_provenance.relevant_claim_ids`,
  `weak_citation_coverage`, `WEAK_CITATION_COVERAGE = 0.2`, and an optional `claim_texts` argument
  to `filter_and_validate` (omitted = old behaviour).
- `services/scripts.py`: the section check and the final validation pass the claim texts; a section
  whose only citations are irrelevant counts as hollow; the rewrite correction says whether the
  passage cited nothing or cited claims that do not state it, and names the closest claims by
  overlap (or says none is close).
- Replayed read-only over the saved scripts: attempt 7 segment 27 (garden) loses its only citation
  and is rewritten; segment 29 and attempt 8's segment 32 lose their regulator claim and are
  flagged. Five of attempt 8's 35 segments are flagged, one of them truly mis-cited - expect the
  flag to be noisy on analogy-heavy passages.
- Test fixtures that padded narration with "word" now pad with the words of their claims.

# Human review checklist

Automated checks prove that a video is a valid MP4, that it decodes, that its length and size are what
the job recorded and that every sentence passed the project's grounding rules. They cannot show that a
voice sounds natural, that a diagram helps a learner, or that a lesson teaches well. **Nobody has
done this review yet.** Until a person has, report "automated checks passed; human review pending",
never "reviewed".

## What to review, and which videos

The product offers three modes since 2026-10-18. Review at least one video per offered mode (all in
`apps/api/var/review/`, newest first; a 2-3 minute video takes about 10 minutes to review properly):

| Mode | Video | Also look for |
| --- | --- | --- |
| A. PDF | `v2_A_pdf_electric_circuits.mp4` | Facts match the PDF; resistance, V = I x R and the series/parallel rules are right; no fact the PDF does not state |
| B. Pasted text | `v2_B_pasted_text_bridges.mp4` | Nothing added beyond the notes; the order follows the notes |
| C. PDF + keywords (focused) | `v2_C_pdf_keywords_ecosystems.mp4` | The three covered keywords are taught; "blockchain mining" and "coral bleaching" are absent (the topics page says they are not in the document) |

Kept but no longer offered (`v2_dormant_*`): instructions beginner/advanced, scanned PDF, topic only. Review them
only if those modes come back.

## Listen (narration)

- [ ] Pronunciation of the subject's own words (names, terms, numbers, units).
- [ ] Pace: neither rushed nor dragging. About 150 words a minute is the planning rate.
- [ ] No cut-off words at the start or end of a section; no odd long silences between sections.
- [ ] Sentence rhythm: does a run of short sentences sound like a list read aloud?
- [ ] Accent: the voice is `en_GB-cori-medium`, a British English female voice. **It is not an
  Indian-English voice.** A project whose language is `en-IN` is written in Indian English but
  narrated in this British voice, and the UI says so. Decide whether that is acceptable for your
  learners before an employee rollout.

## Watch (visuals)

- [ ] Text is readable at 720p on the screen your learners will use, and does not run off a card.
- [ ] Each scene matches what is being said at that moment (no card for the previous section).
- [ ] Diagrams (flow, cycle, hierarchy, comparison, labelled parts) are correct and uncluttered.
- [ ] Scene changes are not too fast to read; the last card does not vanish before the narration ends.
- [ ] Variety: the renderer draws text cards, bullet cards and five simple diagram shapes only. It has
  no photographs or illustrations. Is that enough for the subject?

## Read (teaching and accuracy)

- [ ] There is an introduction, a logical order and a conclusion.
- [ ] Every fact can be found in the source (for A-E) or is textbook-level (for F).
- [ ] Nothing important in the source was left out because a sentence was removed. The script page lists
  every removed sentence and the reason; read that list for the lesson you are reviewing.
- [ ] If the video is shorter than the target, the script page says why. Is the shorter lesson still
  complete enough to teach the topic?
- [ ] The assessment questions (if any) test what the video actually says.

## Record

For each video note the reviewer, the date, the problems found and a verdict (fit to use / fix first).
Put the notes beside this file (for example `docs/human-review-notes-2026-10.md`), not in the commit
message.

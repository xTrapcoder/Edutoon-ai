# Why videos came out shorter than their target (investigation, 2026-10-17/18)

**Question.** After the sentence-level support check was added, finished videos were much shorter than
the length asked for: 144 s of 300 (PDF), 143 s of 240 (topic only), 214 s of 240 (pasted text), 240 s of
300 (PDF + keywords). Is the checker removing legitimate explanations, or is the writer producing
unsupported filler?

**Answer.** Both, and a third thing nobody had looked at. The checker is mostly right but removes a real
share of fair sentences; the writer produced a lot of unsupported flourish; and the largest single
cause was configuration: the evidence itself was capped at four claims per topic. None of it was fixed
by loosening the judge.

Method: the final scripts, the job results (`support_check.removed_sentences` with reasons) and the
claims on file for the four runs were read directly from the development database
(`analyze_filter.py` style queries; the sentences below are quoted from there). Every removed sentence
of three runs (64 in all) was read against its claims **and** the source text. The classification is a
manual judgement, not a measurement of truth; where the source states a fact that the claims do not,
it is called "supported by the source".

## 1. What was removed

Two runs, classified by hand. U = clearly unsupported (adds a cause, an importance, a place, a reason
or an effect the source does not state, or is filler). S = supported by the source or reasonably
inferable from the claims (the judge or a rule over-removed it). A = ambiguous: a supported fact with a
flourish added, or an inference the source neither states nor contradicts.

| Run | Removed | U | S | A |
| --- | ---: | ---: | ---: | ---: |
| Pasted text (Industrial Revolution, 7 topics, 25 claims) | 25 | 9 (36%) | 6 (24%) | 10 (40%) |
| PDF (plate tectonics, 9 topics, 23 claims) | 19 | 4 (21%) | 10 (53%) | 5 (26%) |

Representative sentences (all quoted from the runs):

- **Clearly unsupported (kept out correctly).** "A stable government ensured that the laws and
  regulations were in place to support new industries, while a growing banking system provided the
  necessary capital" (the source says only "a stable government, a growing banking system");
  "coal became the new fuel ... because it was more readily available and could be transported more
  easily" (the reason is invented); "Thanks to these reforms, people's lives improved" (the source
  credits the Industrial Revolution, not the reforms); "One of the most famous examples of a divergent
  boundary is the Mid-Atlantic Ridge"; "This is an important point to remember."; "Claim [c2] tells us
  that ..." (the writer quoted an internal claim handle).
- **Supported, removed wrongly.** "Factories could now be built anywhere, not just near rivers"
  (removed three times in one section; two claims say exactly that); "Some volcanoes, like those in
  Hawaii, form over hotspots, which are columns of hot rock rising from the mantle" (a claim says
  this word for word; flagged for the word "like"); "At these boundaries, magma rises ... and cools to
  form new crust"; "Earlier, factories had to be built near rivers because they used water wheels" (the
  source says so; the claims were too thin to).
- **Cross-topic facts.** Of the ten PDF-run removals classed S, five were true facts that belong to
  another topic's claims (Wegener and Pangaea, Mesosaurus, the San Andreas Fault, Hawaii, the Ring of
  Fire) which the writer reached back for in the wrong section; the names rule, which compares a
  section with its own topic's claims, refused them. Two were closings (section 2) and three were
  restatements of a claim that the judge over-flagged.
- **A supported fact lost with its flourish.** "It was the first public railway to use steam
  locomotives, revolutionizing transport further." and "Iceland is also on the Mid-Atlantic Ridge,
  showing how these boundaries can affect land formations." Each carried a supported fact and one
  unsupported clause; the whole sentence went, and in the PDF run the Mid-Atlantic Ridge example
  disappeared from the lesson altogether.

## 2. Does filtering remove the introduction, a definition, an example, a transition or a conclusion?

It removed **conclusions**, and it did so because of a rule rather than the judge. Three of the four
finished scripts examined ended abruptly: the plate-tectonics scripts end on "A famous example of a
transform boundary is the San Andreas Fault" and "This means they're older and further away", the
pasted-text script on a fact. The closing passages ("This concludes our lesson on plate tectonics. We've
learned about different types of boundaries ...") came back from the writer with no cited claim and
without the `connective` flag, so `filter_and_validate` treated them as uncited statements and dropped
them whole ("the passage cites no verified claim"). They assert nothing about the subject.

Definitions and examples were removed only when they added something the claims did not state (see the
table); the introductions survived.

## 3. The causes, in order of size

1. **The evidence was capped.** `apps/api/.env` and the README's "small machine" recommendation set
   `MAX_CLAIMS_PER_TOPIC=4`, and the claim prompt said "prefer fewer ... claims". Every topic therefore
   had 2 to 4 claims, so a script could say perhaps 25 to 40 seconds of supported narration per topic
   however long the source was. With the cap at 10, the same pasted notes gave 79 supported claims
   instead of 25.
2. **The length-correction pass asked for padding.** When a script came out short, `run_generate_script_job`
   re-wrote every section with a *rescaled-up* word budget. After the support check had just removed
   unsupported text, that asked the writer for roughly twice as many words from the same claims: more
   flourish for the judge to remove again, and a second full generation's time.
3. **The writer's own instructions invited the flourish.** "Define any term a student would not already
   know", "explain the concept" and "use a simple example" all push beyond the claims, and nothing told the
   writer what the judge removes (causes, reasons, importance words, superlatives, quantities, places).
4. **The checker's strictness.** It flagged fair paraphrases (the "built anywhere" sentences) and a
   sentence is removed whole when one clause is the problem. The labelled evaluation scores 21/23 (one
   false negative, one over-flag); a prompt change meant to reduce over-flagging scored 20/23 and was
   reverted, so the judge itself was left alone.
5. **Cross-topic repetition (found while fixing 1).** More claims exposed that neighbouring topics hold
   the same fact, so a longer script repeated itself ("began in Britain in the late 18th century" four
   times, the Stockton and Darlington Railway three times).

## 4. What was changed (smallest change for each cause)

| Cause | Change | Test |
| --- | --- | --- |
| 1 | Claim prompt asks for every distinct fact the passages state; default `MAX_CLAIMS_PER_TOPIC` 10; README recommends 8 on a small machine and explains the effect | existing prompt tests; measured below |
| 2 | `decide_length_correction`: a *short* script is not stretched when the checks changed it **and** it already cites at least 70% of its claims; a long one is always corrected; the job records `shortfall_reason: "evidence_limited"` | `test_domain_script_duration`, `test_script_support_check` (three service tests) |
| 3 | Writer prompt: explain only by restating, ordering and linking the claims; never add a cause, importance, quantity, place or analogy with facts; fill the budget by covering more claims; do not mention another topic's facts | none can test a model; measured below |
| 2b | A passage whose every sentence is a question, signpost or short lesson recap (no cause/importance cue, no clause about the subject, at most 20 words) needs no citation, so a closing is kept | `test_narration_support`, `test_domain_script_provenance` |
| 5 | `domain.claim_dedupe`: each distinct fact is written once, under the topic whose title fits best; no topic keeps fewer than two claims; the job records `claims_deduplicated` | `test_domain_claim_dedupe`, `test_services_scripts` |
| honesty | The script page says when a script is shorter than its target because the verified claims cannot carry more, and that nothing was added to reach it | `support-check-notice.test.tsx`, `jobs.test.ts` |

Also fixed because the investigation found it: a claim naming a place the source never mentions was
stored as "supported" (the India claim; `domain.claim_grounding`, see its docstring).

Not changed, deliberately: the sentence judge's prompt, its strictness, and the rule that figures and
names must come from the topic's own claims. Removing a sentence whole when one clause is the problem
remains; a clause-level rewrite would need a model call per sentence and was not attempted.

## 5. Measured effect

Everything below is one run per cell on the local 7B model, so treat a difference of a few percent as
noise. The source of every number is the job rows and the finished files in the development database.

### 5a. Same pasted notes (`industrial_revolution.txt`, 7 topics, 4:00 target), step by step

| | Before | Claims 10/topic, prompt v1 | + coverage rule | + dedupe, prompt v2 |
| --- | ---: | ---: | ---: | ---: |
| Supported claims available | 25 | 79 | 79 | 79 (37 folded away) |
| Claims cited by the script | 18 | 32 | 49 | 31 |
| Script length (estimated) | 220 s | 179 s | 226 s | 189 s |
| Sentences judged / removed / rewritten | 162 / 25 / 14 | 106 / 7 / 11 | 110 / 7 / 9 | 145 / 14 / 13 |
| Script generation time | 1,914 s | 1,225 s | 1,264 s | 1,596 s |
| Repeated facts in the text | some | none seen | **many** (1825 railway three times) | few |
| Ends with a conclusion | no | no | no | yes |

The third column is the cautionary one: more claims and a prompt that fills the budget got the length to
94% of the target by saying the same facts again. De-duplication takes that padding out, and the lesson
is shorter (79%) and honest. Length is not the goal; the right length for what the source says is.

### 5b. The same plate-tectonics PDF, before and after (old run vs acceptance run D1)

Not a controlled comparison (different target, topic count and instructions), but the only like-for-like
source: both read `plate_tectonics.pdf`.

| | Old run (4 claims per topic) | D1 (all fixes) |
| --- | ---: | ---: |
| Topics | 9 | 8 |
| Supported claims cited | 19 | 43 |
| Sentences judged / removed | 119 / 19 | 97 / 4 |
| Script generation time | 1,629 s | 960 s |
| Finished video | 143.8 s | 176.8 s |

### 5c. The acceptance runs: how long a lesson can be

Seven fresh runs through the wizard (2026-10-18). The decisive column is the last one: **a lesson that may
only say what its source says cannot honestly be much longer than the source takes to read aloud.**

| Run | Target | Video | % of target | Source read aloud (150 wpm) | Video as % of that | Judged / removed | Script said |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| A. PDF, 3 pages | 4:00 | 2:16 | 57% | 2:42 | 84% | 65 / 5 | evidence limited |
| B. Pasted notes | 4:00 | 1:44 | 43% | 2:38 | 66% | 76 / 25 | writer short |
| C. PDF + keywords | 5:00 | 2:19 | 46% | 2:42 | 85% | 171 / 28 | writer short |
| D1. Same PDF, beginner | 4:00 | 2:57 | 74% | 4:18 | 68% | 97 / 4 | evidence limited |
| D2. Same PDF, advanced, pages 3-4 | 4:00 | 1:51 | 46% | (half the PDF) | | 73 / 10 | none |
| E. Scanned PDF, 3 pages | 3:00 | 1:34 | 52% | 1:31 | 103% | 30 / 1 | evidence limited |
| F. Topic only | 4:00 | 4:08 | 103% | none | | 108 / 3 | none |

The fresh sources are small (228 to 646 words), so a 3 to 5 minute target was never reachable honestly
from them, and the script page now says so in words. The sentence check removed little here (1 to 28
sentences per script, against 19 to 25 before) and, apart from B and C, did not decide the length.

### 5d. What is still imperfect

- B and C are `writer_short`: verified claims went unused, so a new version may be longer. The 7B writer is
  the limit there, not the evidence.
- The judge still removes a whole sentence for one flourish, still over-flags a fair paraphrase now and
  then (21 of 23 on the labelled set), and in C it let through "the mitral valve is between the **left**
  atrium and ventricle", which the source states only as "between the atria and the ventricles".
- A lesson can still repeat itself a little (F says what supervised learning is three times); the
  de-duplication is by word overlap, not by meaning.
- Nobody has listened to or watched these lessons (`docs/human-review-checklist.md`).

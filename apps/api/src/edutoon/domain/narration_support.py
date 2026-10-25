"""Sentence-level support for narration - pure domain logic (rule 1: no I/O, no configuration).

A script segment cites the verified claims it draws on, and its numbers and names are checked
against them (``script_provenance``). That still lets a sentence through whose *wording* comes from
the claims but whose *meaning* does not: "plants need water to open their stomata" shares every
word with "a plant short of water closes its stomata", and says something the source never did.

So the sentences of a segment are also judged, one at a time, against the verified claims the
section may rest on, and a sentence the judge says is unsupported is not kept. The judge is a
language model, not a proof: it is stricter about causes, requirements and the direction of an
effect than a word-overlap check can be, and it can still be wrong in either direction (measured on
a local 7B model: 25 of 28 labelled sentences right with one pass, and the first misses were
"importance" and "reason" additions - see ``needs_second_opinion``). This module only splits
narration into sentences, decides which need judging and takes sentences out; it never decides what
is true.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from edutoon.domain.claim_dedupe import content_words

SentenceVerdict = Literal["supported", "unsupported"]

MIN_WORDS_TO_JUDGE: Final = 4
"""A sentence shorter than this ("Let's begin.") asserts nothing worth judging."""

MIN_WORDS_TO_KEEP_A_SEGMENT: Final = 6
"""After unsupported sentences are taken out, a segment with fewer words than this is dropped
rather than read out as a fragment."""

# Sentence-final punctuation followed by space and a capital letter, digit or opening quote.
_BOUNDARY = re.compile(r"(?<=[.!?])[\"')\]]?\s+(?=[A-Z0-9\"'(\[])")
_ABBREVIATIONS: Final = frozenset(
    {"e.g", "i.e", "etc", "vs", "dr", "mr", "mrs", "ms", "prof", "fig", "eq", "no", "approx", "cf"}
)

# A sentence that only introduces, transitions, asks or illustrates. Its facts, if any, are still
# held to the numbers-and-names check; judging it as a claim would only remove teaching.
_SIGNPOST = re.compile(
    r"^(?:let'?s|let us|now,? (?:that|let|we)|next,?|first,? let|so,? (?:what|how|why|let)|"
    r"in this (?:section|lesson|video|part)|welcome|today,? we|before we|to begin|to start|"
    r"imagine|think of|picture|consider|you might wonder|have you ever|you can (?:think|picture))",
    re.IGNORECASE,
)

# A sentence about the lesson itself - closing it, recapping it, pointing back at an earlier part -
# that says nothing about the subject. Only a short one counts, and never one with a cue for a
# cause, a reason or an importance (``_RISK_CUES``): that is a statement about the subject in
# recap's clothes and is judged like any other. Figures and names are still held to the
# numbers-and-names check.
_LESSON_REFERENCE = re.compile(
    r"^(?:this (?:concludes|brings us|wraps up)|"
    r"that (?:concludes|brings us|wraps up|'?s (?:all|it))|"
    r"thanks? (?:you )?for|thank you|"
    r"we(?:'ve| have) (?:learned|covered|seen|looked at|explored|talked about|discussed)|"
    r"in (?:our|the) (?:last|previous|first|next|earlier) "
    r"(?:segment|section|part|video|lesson)|"
    r"in this (?:lesson|video)|"
    r"(?:to|so,? to) (?:sum up|recap|summari[sz]e|conclude|wrap up)|"
    r"in (?:summary|conclusion)|"
    r"as we (?:saw|learned|discussed|covered))\b",
    re.IGNORECASE,
)

# A clause that says something about the subject (a definition, a reason, a "that ..."): a recap
# with one is a statement and is judged like any other.
_SUBJECT_CLAUSE = re.compile(
    r"\b(?:which|where|who|whose|that|when|how|what|because)\b", re.IGNORECASE
)

MAX_RECAP_WORDS: Final = 20
"""The longest sentence that can count as a recap or closing rather than as a statement."""

# An analogy that opens a sentence ("Just like a story has an end, photosynthesis has two parts").
# The comparison is illustration, not a claim; the clause after its comma is the claim.
_ANALOGY = re.compile(
    r"^(?:just like|much like|like a|like an|like the|similar to|it'?s like|it is like|as if|"
    r"in the same way(?: that)?|the same way)\b",
    re.IGNORECASE,
)

# Words that mark a cause, a reason, a requirement, a purpose, an importance, a comparison or a
# scope - the additions a single pass over a sentence was measured to miss. A sentence with one is
# judged a second time with a differently worded, stricter question.
_RISK_CUES = re.compile(
    r"\b(?:because|why|so that|in order to|therefore|thus|hence|since|due to|as a result|"
    r"results? in|leads? to|causes?|caused|need(?:s|ed)?|require[sd]?|requires|must|essential|"
    r"crucial|vital|important|key to|necessary|only|always|never|every|all|most|best|more|less|"
    r"than|allows?|enables?|ensures?|helps?|prevents?|if|unless|means? that|which is)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class SentenceJudgement:
    """The judge's answer for one sentence: ``supported`` or ``unsupported``, and its reason."""

    verdict: SentenceVerdict
    reason: str


@dataclass(frozen=True, slots=True)
class UnsupportedSentence:
    """A sentence the judge found no claim to support: its 0-based position in the segment, its
    text and the judge's reason."""

    position: int
    text: str
    reason: str


def split_sentences(text: str) -> list[str]:
    """Cut ``text`` into sentences, keeping each one's own punctuation. A full stop after a known
    abbreviation (``e.g.``, ``Dr.``) or inside a number (``3.5``) does not end a sentence."""
    pieces = _BOUNDARY.split(" ".join(text.split()))
    sentences: list[str] = []
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        if sentences and _ends_with_abbreviation(sentences[-1]):
            sentences[-1] = f"{sentences[-1]} {piece}"
        else:
            sentences.append(piece)
    return sentences


def _ends_with_abbreviation(sentence: str) -> bool:
    last = sentence.rstrip().rsplit(" ", 1)[-1].rstrip(".").lower().strip("\"')([")
    return sentence.rstrip().endswith(".") and last in _ABBREVIATIONS


def needs_judging(sentence: str) -> bool:
    """Whether ``sentence`` asserts something to judge: not a question, not a signpost or an
    illustration, and long enough to say anything."""
    stripped = sentence.strip()
    if stripped.endswith("?") or len(stripped.split()) < MIN_WORDS_TO_JUDGE:
        return False
    return _SIGNPOST.match(stripped) is None


def is_recap_or_signpost(sentence: str) -> bool:
    """Whether ``sentence`` only moves the lesson along: a question, a signpost, or a short closing
    or recap of the lesson itself that adds no clause about the subject and no cue for a cause,
    reason or importance. A short *statement* ("Plants make food.") is not one: it is too short
    for the judge, but it still says something and needs its citation."""
    stripped = sentence.strip()
    if stripped.endswith("?") or _SIGNPOST.match(stripped) is not None:
        return True
    return (
        _LESSON_REFERENCE.match(stripped) is not None
        and len(stripped.split()) <= MAX_RECAP_WORDS
        and _RISK_CUES.search(stripped) is None
        and _SUBJECT_CLAUSE.search(stripped) is None
    )


def is_signpost_only(text: str) -> bool:
    """Whether every sentence of ``text`` only moves the lesson along
    (:func:`is_recap_or_signpost`): a passage like that asserts nothing about the subject, so it
    needs no citation. ``False`` for empty text."""
    sentences = split_sentences(text)
    return bool(sentences) and all(is_recap_or_signpost(sentence) for sentence in sentences)


def factual_part(sentence: str) -> str | None:
    """The part of ``sentence`` to judge as a claim. An opening analogy is illustration: only the
    clause after its first comma is judged (``None`` when there is none, or nothing left)."""
    stripped = sentence.strip()
    if _ANALOGY.match(stripped) is None:
        return stripped
    _, comma, rest = stripped.partition(",")
    rest = rest.strip()
    return rest if comma and len(rest.split()) >= MIN_WORDS_TO_JUDGE else None


def needs_second_opinion(sentence: str) -> bool:
    """Whether ``sentence`` carries a cue for a cause, reason, requirement, purpose, importance,
    comparison or scope, and so is worth a second, stricter look even if the first pass said yes."""
    return _RISK_CUES.search(sentence) is not None


def without_sentences(sentences: Sequence[str], positions: Sequence[int]) -> str | None:
    """``sentences`` with those at ``positions`` (0-based) taken out, joined by single spaces, or
    ``None`` if what is left is too short to read out (:data:`MIN_WORDS_TO_KEEP_A_SEGMENT`)."""
    drop = set(positions)
    remaining = [sentence for index, sentence in enumerate(sentences) if index not in drop]
    # A lead-in ("For example:") whose example was taken out must not be left hanging.
    while remaining and remaining[-1].rstrip().endswith(":"):
        remaining.pop()
    kept = " ".join(remaining)
    return kept if len(kept.split()) >= MIN_WORDS_TO_KEEP_A_SEGMENT else None


RESTATE_MIN_COVERAGE: Final = 0.6
"""A sentence counts as a (flourished) restatement of a claim when it contains at least this
share of the claim's content words."""

RESTATE_MIN_SHARED_WORDS: Final = 3
"""...and at least this many of them, so a two-word claim is never "restated" by a sentence that
happens to mention both."""

RESTATE_ALREADY_SAID: Final = 0.8
"""A claim is not restated when this share of its content words is already in the section's text."""


def restating_claim_index(
    sentence: str, claim_texts: Sequence[str], said: Sequence[str] = ()
) -> int | None:
    """The claim a rejected ``sentence`` is mostly a wording of, as an index into ``claim_texts``,
    or ``None``.

    The judge removes a sentence whole when one clause of it is the problem ("Because of this
    design, suspension bridges can span very long distances", where a claim says they can span
    very long distances), and a lesson that loses its only statement of a fact has a gap, not a
    safer text.
    When the rejected sentence contains most of one *verified* claim's content words, that claim -
    which a cited passage backs - can be read out instead. Nothing the judge rejected is kept: what
    is read out is the claim's own text. A claim already said in ``said`` is not said again, and a
    sentence that is only loosely about a claim (:data:`RESTATE_MIN_COVERAGE`) is simply removed.
    """
    sentence_words = content_words(sentence)
    best: int | None = None
    best_coverage = 0.0
    for index, text in enumerate(claim_texts):
        claim_words = content_words(text)
        shared = len(sentence_words & claim_words)
        if not claim_words or shared < RESTATE_MIN_SHARED_WORDS:
            continue
        coverage = shared / len(claim_words)
        if coverage >= RESTATE_MIN_COVERAGE and coverage > best_coverage:
            best, best_coverage = index, coverage
    if best is None:
        return None
    claim_words = content_words(claim_texts[best])
    for text in said:
        if len(claim_words & content_words(text)) / len(claim_words) >= RESTATE_ALREADY_SAID:
            return None
    return best


# --- an example clause no claim names -------------------------------------------------------------

_EXAMPLE_PARENTHETICAL: Final = re.compile(
    r",\s*(?:like|such as)\s+(?P<items>[^,.;!?]{1,60}),", re.IGNORECASE
)


def without_unnamed_examples(sentence: str, claim_texts: Sequence[str]) -> tuple[str, list[str]]:
    """``sentence`` with each comma-delimited ", like ..., " / ", such as ..., " clause taken out
    whose words no claim uses, and the clauses that were taken out.

    A beginner-level writer illustrates a fact with an everyday example ("Carnivores, like tigers,
    eat other animals and are called secondary consumers"). The fact is a claim; the tiger is not,
    and the sentence judge lets a clause like that through often enough to matter. The rest of the
    sentence is untouched, so a single invented example never costs a supported sentence, and a
    clause whose every content word occurs in the claims (``like cows`` when a claim says so) stays.
    Only a clause delimited by commas on both sides is handled: "plants like grass are ..." is part
    of the subject and is left to the judge. Only text is ever removed.
    """
    claim_words = {word for text in claim_texts for word in content_words(text)}
    claim_words |= {word[:5] for word in claim_words}
    removed: list[str] = []

    def _drop(match: re.Match[str]) -> str:
        items = content_words(match.group("items"))
        if not items or all(word in claim_words or word[:5] in claim_words for word in items):
            return match.group(0)
        removed.append(match.group(0).strip(" ,"))
        return ""

    return _EXAMPLE_PARENTHETICAL.sub(_drop, sentence), removed

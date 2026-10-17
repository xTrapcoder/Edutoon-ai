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
    kept = " ".join(sentence for index, sentence in enumerate(sentences) if index not in drop)
    return kept if len(kept.split()) >= MIN_WORDS_TO_KEEP_A_SEGMENT else None

"""Say each fact once across a lesson - pure domain logic (rule 1: no I/O, no imports of
repositories / providers / services).

Claims are extracted per topic, but each topic's claims are drawn from the passages it cites, and
neighbouring topics often cite the same ones - and, in a short pasted text, the very same two
chunks. The result is the same fact verified once for several topics: "the Stockton and Darlington
Railway opened in 1825" as a claim of three topics, "the Industrial Revolution began in Britain in
the late eighteenth century" of four. Written out section by section, that is a lesson that says the
same thing again and again, which is padding by another route even though every sentence is
supported.

:func:`dedupe_claims_across_topics` keeps each distinct fact once. When several topics hold the same
fact it goes to the topic whose title fits it best (a fact about steam engines belongs under "Steam
power", not under whichever topic happens to come first), and a topic is never left with fewer than
:data:`MIN_CLAIMS_PER_TOPIC` claims: the user accepted it, and a hollow section is worse than a
repeated fact. Two claims are the same fact when their content words overlap by at least
:data:`SAME_FACT_OVERLAP` (a Jaccard ratio), a deliberately simple and explainable rule rather than
a model's judgement.
"""

from __future__ import annotations

import re
from collections.abc import Hashable, Sequence
from dataclasses import dataclass
from typing import Final

SAME_FACT_OVERLAP: Final = 0.5
"""The share of two claims' combined content words that both must contain for them to count as one
fact. Reworded claims ("allowed" for "enabled") are well above it; claims that merely share a
subject word are far below."""

MIN_CLAIMS_PER_TOPIC: Final = 2
"""A topic keeps at least this many claims even when all of them repeat another topic's."""

_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'-]*")
_STOPWORDS: Final = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "from",
        "with",
        "by",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "which",
        "who",
        "whom",
        "whose",
        "into",
        "onto",
        "over",
        "under",
        "than",
        "then",
        "there",
        "their",
        "they",
        "them",
        "he",
        "she",
        "his",
        "her",
        "we",
        "our",
        "you",
        "your",
        "not",
        "no",
        "can",
        "could",
        "may",
        "might",
        "will",
        "would",
        "should",
        "also",
        "very",
        "more",
        "most",
        "some",
        "any",
        "each",
        "both",
        "one",
        "two",
        "such",
    ]
)


@dataclass(frozen=True, slots=True)
class ClaimRef:
    """A claim as the de-duplication sees it: an id of any hashable kind and its text."""

    id: Hashable
    text: str


@dataclass(frozen=True, slots=True)
class TopicClaims:
    """One topic, in lesson order, with its title and its claims in the order to keep them."""

    topic_id: Hashable
    title: str
    claims: Sequence[ClaimRef]


def content_words(text: str) -> frozenset[str]:
    """The words that carry a claim's meaning: lower-case, without stopwords or one-letter words;
    plural ``-s`` is folded so "factories" and "factory" differ but "rivers" and "river" do not."""
    words: set[str] = set()
    for match in _WORD.finditer(text.lower()):
        word = match.group().strip("'-")
        if len(word) < 2 or word in _STOPWORDS:
            continue
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        words.add(word)
    return frozenset(words)


def same_fact(a: frozenset[str], b: frozenset[str]) -> bool:
    """Whether two claims' content-word sets overlap enough to be one fact."""
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= SAME_FACT_OVERLAP


def dedupe_claims_across_topics(topics: Sequence[TopicClaims]) -> dict[Hashable, list[ClaimRef]]:
    """Each topic's claims with repeated facts removed, as ``{topic_id: claims}`` (every topic is
    present). Order within a topic is preserved. Deterministic: the same input gives the same
    answer."""
    words_of = {claim.id: content_words(claim.text) for topic in topics for claim in topic.claims}
    title_words = {topic.topic_id: content_words(topic.title) for topic in topics}
    topic_order = {topic.topic_id: index for index, topic in enumerate(topics)}

    # Group the claims into facts, in lesson order: a claim joins the first fact it repeats.
    facts: list[list[tuple[Hashable, ClaimRef]]] = []
    for topic in topics:
        for claim in topic.claims:
            for fact in facts:
                if same_fact(words_of[claim.id], words_of[fact[0][1].id]):
                    fact.append((topic.topic_id, claim))
                    break
            else:
                facts.append([(topic.topic_id, claim)])

    # Each fact goes to the topic whose title shares the most words with it (earliest on a tie),
    # and within that topic to its first wording.
    owned: set[Hashable] = set()
    for fact in facts:
        _, owner = min(
            fact,
            key=lambda member: (
                -len(title_words[member[0]] & words_of[member[1].id]),
                topic_order[member[0]],
            ),
        )
        owned.add(owner.id)

    kept: dict[Hashable, list[ClaimRef]] = {}
    for topic in topics:
        chosen = {claim.id for claim in topic.claims if claim.id in owned}
        if len(chosen) < MIN_CLAIMS_PER_TOPIC:
            # Never leave an accepted topic hollow: restore its own claims, in order.
            for claim in topic.claims:
                if len(chosen) >= MIN_CLAIMS_PER_TOPIC:
                    break
                chosen.add(claim.id)
        kept[topic.topic_id] = [claim for claim in topic.claims if claim.id in chosen]
    return kept

"""Say each fact once across a lesson - pure domain logic (rule 1: no I/O, no imports of
repositories / providers / services).

Claims are extracted per topic, but each topic's claims are drawn from the passages it cites, and
neighbouring topics often cite the same ones - and, in a short pasted text, the very same two
chunks. The result is the same fact verified once for several topics: "the Stockton and Darlington
Railway opened in 1825" as a claim of three topics, "the Industrial Revolution began in Britain in
the late eighteenth century" of four. Written out section by section, that is a lesson that says the
same thing again and again, which is padding by another route even though every sentence is
supported.

Placement matters as much as repetition. In a short source every topic is extracted from the same
one or two passages, so a topic's own claim list holds facts about the whole document (in one
pasted-notes run "Errors" held the facts about variables, lists and Python's release year). Written
section by section, that puts a fact in the wrong part of the lesson. So
:func:`dedupe_claims_across_topics` keeps each distinct fact once and gives it to the topic - *among
all topics, not only those that happen to hold it* - whose title and keywords it fits best: a fact
about steam engines belongs under "Steam power", not under whichever topic happens to come first.
Words that almost every fact contains (the lesson's own subject, "Python") and filler title words
("Introduction to ...") are not evidence of fit, or the topic named after the subject would win
everything. A fact that fits no topic stays with the earliest topic that holds it, and a topic is
never left with fewer than :data:`MIN_CLAIMS_PER_TOPIC` claims: the user accepted it, and a hollow
section is worse than a repeated fact. Two claims are the same fact when their content words overlap
by at least :data:`SAME_FACT_OVERLAP` (a Jaccard ratio), a deliberately simple and explainable rule
rather than a model's judgement.
"""

from __future__ import annotations

import math
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

SUBJECT_WORD_SHARE: Final = 0.5
"""A word contained in at least this share of a lesson's distinct facts is the lesson's own subject
("Python" in a Python lesson), not evidence that a fact belongs to one topic. Applied only once a
lesson has :data:`MIN_FACTS_FOR_SUBJECT_WORDS` distinct facts, so a tiny lesson is not stripped of
every word."""

MIN_FACTS_FOR_SUBJECT_WORDS: Final = 4

TITLE_WEIGHT: Final = 2.0
"""A word of a topic's title or keywords counts this many times a word of its summary when a fact is
matched to a topic."""

MIN_PLACEMENT_SCORE: Final = 2.0
"""A fact moves to a topic that does not hold it, or leaves the topic that does, only when the best
topic's match is at least this strong: one reasonably rare title word, or two summary words."""

_FILLER_TITLE_WORDS: Final = frozenset(
    [
        "introduction",
        "intro",
        "overview",
        "basic",
        "basics",
        "summary",
        "conclusion",
        "understanding",
    ]
)
"""Title words that name a kind of section, not its subject."""

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
    keywords: Sequence[str] = ()
    summary: str = ""


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


def _fit_stem(word: str) -> str:
    """A slightly stronger fold than :func:`content_words`, used only to match a title to a claim:
    "dictionaries" and "dictionary", "boxes" and "box" are the same subject."""
    if len(word) > 4 and word.endswith("ie"):  # content_words has already dropped the plural s
        return word[:-2] + "y"
    if len(word) > 4 and word.endswith(("sse", "xe", "ze", "che", "she")):
        return word[:-1]
    return word


def dedupe_claims_across_topics(topics: Sequence[TopicClaims]) -> dict[Hashable, list[ClaimRef]]:
    """Each topic's claims with repeated facts removed and each fact placed under the topic it fits
    best, as ``{topic_id: claims}`` (every topic is present). A claim may be placed under a topic
    that did not list it when that topic fits it strictly better than any topic that did. Claims
    come out in the order they went in. Deterministic: the same input gives the same answer."""
    claim_order: dict[Hashable, int] = {}
    words_of: dict[Hashable, frozenset[str]] = {}
    for topic in topics:
        for claim in topic.claims:
            claim_order.setdefault(claim.id, len(claim_order))
            words_of.setdefault(claim.id, content_words(claim.text))
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

    fact_stems = [
        frozenset(
            _fit_stem(word)
            for claim_words in (words_of[c.id] for _, c in fact)
            for word in claim_words
        )
        for fact in facts
    ]

    # How common a word is among the lesson's facts: a word in nearly every fact is the lesson's own
    # subject ("Python") and says nothing about which topic a fact belongs to, and a word in few
    # facts is strong evidence.
    document_frequency: dict[str, int] = {}
    for stems in fact_stems:
        for stem in stems:
            document_frequency[stem] = document_frequency.get(stem, 0) + 1
    subject_words: set[str] = set()
    if len(facts) >= MIN_FACTS_FOR_SUBJECT_WORDS:
        subject_words = {
            word
            for word, count in document_frequency.items()
            if count / len(facts) >= SUBJECT_WORD_SHARE
        }

    def stems_of(text: str) -> set[str]:
        return {_fit_stem(word) for word in content_words(text)} - _FILLER_TITLE_WORDS

    def term_weights(topic: TopicClaims) -> dict[str, float]:
        """The words that say what a topic is about, each with how much it counts: the title's and
        keywords' twice the summary's. A topic named only by a frequent word ("Resistance", in a
        lesson about circuits) keeps it: it is about exactly that word."""
        named = stems_of(" ".join([topic.title, *topic.keywords]))
        named_specific = named - subject_words or named
        weights = {stem: TITLE_WEIGHT for stem in named_specific}
        for stem in stems_of(topic.summary) - subject_words:
            weights.setdefault(stem, 1.0)
        return weights

    weights_of = {topic.topic_id: term_weights(topic) for topic in topics}

    def score(topic_id: Hashable, stems: frozenset[str]) -> float:
        """How well a fact fits a topic: the words they share, each worth its weight times how rare
        it is among the lesson's facts."""
        return sum(
            weight * math.log(1 + len(facts) / document_frequency[stem])
            for stem, weight in weights_of[topic_id].items()
            if stem in stems
        )

    # Each fact goes to the topic - any topic - it fits best; a topic that already holds it wins a
    # tie, then the earliest. A fact that fits nowhere (:data:`MIN_PLACEMENT_SCORE`) stays with the
    # earliest topic that holds it.
    placed: dict[Hashable, list[ClaimRef]] = {topic.topic_id: [] for topic in topics}
    for fact, stems in zip(facts, fact_stems, strict=True):
        holders = {topic_id for topic_id, _ in fact}
        best = max(
            topics,
            key=lambda topic: (
                round(score(topic.topic_id, stems), 6),
                topic.topic_id in holders,
                -topic_order[topic.topic_id],
            ),
        )
        if score(best.topic_id, stems) < MIN_PLACEMENT_SCORE:
            owner_id, owner = min(fact, key=lambda member: topic_order[member[0]])
        else:
            owner_id = best.topic_id
            # the owner's own wording when it has one, else the first wording anywhere
            owner = next((c for t, c in fact if t == owner_id), fact[0][1])
        placed[owner_id].append(owner)

    kept: dict[Hashable, list[ClaimRef]] = {}
    for topic in topics:
        chosen = {claim.id: claim for claim in placed[topic.topic_id]}
        if len(chosen) < MIN_CLAIMS_PER_TOPIC:
            # Never leave an accepted topic hollow: restore its own claims, the ones that fit it
            # best first.
            own = sorted(
                topic.claims,
                key=lambda claim: (
                    -score(topic.topic_id, frozenset(_fit_stem(w) for w in words_of[claim.id])),
                    claim_order[claim.id],
                ),
            )
            for claim in own:
                if len(chosen) >= MIN_CLAIMS_PER_TOPIC:
                    break
                chosen.setdefault(claim.id, claim)
        kept[topic.topic_id] = sorted(chosen.values(), key=lambda claim: claim_order[claim.id])
    return kept

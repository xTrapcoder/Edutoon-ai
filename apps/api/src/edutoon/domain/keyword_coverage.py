"""Whether the keywords an owner asked for are covered by their document - pure domain logic
(rule 1: no I/O, no configuration).

An owner who uploads a PDF and types keywords ("Calvin cycle", "light-dependent reactions") asks
for those topics to be taught *from that document*. A keyword the document says nothing about must
not be taught as if it did, and the owner must not be left thinking it was: so for every keyword the
verdict is one of four, each with the sentence the owner reads.

``covered`` - the document contains the keyword's words **and** at least one statement about it was
checked against the document and supported.
``not_in_document`` - the document does not back this keyword: either none of the drafted
statements could be supported, or the document never mentions what the keyword names. The second
matters because a statement is judged against the *nearest* passages, so for a keyword the document
does not cover the model can still be handed true statements about neighbouring topics (invasive
species, for "coral bleaching" in a chapter on ecosystems) - and can even word one of them as a link
to the keyword that no passage makes. Support for such a statement is not coverage of the keyword.
Nothing about an uncovered keyword can reach the video as a fact from the PDF.
``not_checked`` - no statements have been drafted yet, so there is nothing to say either way.
``left_out`` - the owner rejected the topic.

Nothing here judges evidence; it reads the counts the claim pipeline already produced.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal
from uuid import UUID

from edutoon.domain.claim_dedupe import content_words

CoverageState = Literal["covered", "not_in_document", "not_checked", "left_out"]


@dataclass(frozen=True, slots=True)
class ClaimCounts:
    """How many statements were drafted for one topic, and how many a document backed."""

    supported: int = 0
    total: int = 0


@dataclass(frozen=True, slots=True)
class KeywordCoverage:
    topic_id: UUID
    title: str
    state: CoverageState
    supported_claims: int
    total_claims: int
    in_script: bool | None
    message: str


@dataclass(frozen=True, slots=True)
class TopicFacts:
    """What coverage needs to know about one topic the owner typed. ``missing_terms`` are the words
    of the keyword that the document never contains (see :func:`missing_terms`); empty when the
    document mentions all of them, or when there is no document to look in."""

    topic_id: UUID
    title: str
    status: str
    missing_terms: tuple[str, ...] = ()


_KEYWORD_FILLER: Final = frozenset(
    {
        "how", "what", "why", "when", "where", "about", "role", "importance", "concept",
        "introduction", "intro", "overview", "basic", "basics", "summary", "understanding",
    }
)  # fmt: skip
"""Words that frame a keyword ("the role of ...", "basics of ...") without naming its subject; a
document need not contain them for the keyword to be covered."""

_MIN_PREFIX_MATCH: Final = 6
_WORD: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9'-]*")


def _stem(word: str) -> str:
    if len(word) > 4 and word.endswith("ie"):  # content_words has already dropped a plural s
        return word[:-2] + "y"
    return word


def missing_terms(keyword: str, document_texts: Iterable[str]) -> tuple[str, ...]:
    """The words of ``keyword`` that appear nowhere in ``document_texts``, as the owner typed them
    and in the order typed. A keyword whose every subject word is in the document gives ``()``.

    A word matches a document word when they are the same apart from a plural, or when both have at
    least :data:`_MIN_PREFIX_MATCH` letters and one begins with the other ("decompose" and
    "decomposers"). Framing words (:data:`_KEYWORD_FILLER`) are ignored. This is a presence test,
    not a judgement of meaning: it says only that the document never uses a word the keyword
    needs, which is the case the support verdicts cannot see on their own.
    """
    document_words = {_stem(word) for text in document_texts for word in content_words(text)}
    missing: list[str] = []
    for raw in _WORD.findall(keyword):
        stems = content_words(raw)
        if not stems:
            continue  # a stopword or a one-letter word
        stem = _stem(next(iter(stems)))
        if stem in _KEYWORD_FILLER:
            continue
        found = stem in document_words or (
            len(stem) >= _MIN_PREFIX_MATCH
            and any(
                len(word) >= _MIN_PREFIX_MATCH and (word.startswith(stem) or stem.startswith(word))
                for word in document_words
            )
        )
        if not found:
            missing.append(raw)
    return tuple(missing)


def assess_keyword(
    topic: TopicFacts, counts: ClaimCounts, *, scripted_topic_ids: Collection[UUID] | None
) -> KeywordCoverage:
    """The coverage verdict for one keyword. ``scripted_topic_ids`` is the set of topics the
    project's current script teaches, or ``None`` when there is no script yet."""
    in_script = None if scripted_topic_ids is None else topic.topic_id in scripted_topic_ids

    if topic.status == "rejected":
        state: CoverageState = "left_out"
        message = "You left this keyword out of the lesson."
    elif topic.missing_terms:
        names = ", ".join(f"'{term}'" for term in topic.missing_terms)
        state = "not_in_document"
        message = (
            f"Your document never mentions {names}, so it does not cover this keyword. Statements "
            "drafted about it describe other topics in your document, not this one, so it cannot "
            "appear in the video as something your document says. Leave it out, or add material "
            "that covers it."
        )
    elif counts.total == 0:
        state = "not_checked"
        message = "Statements about this keyword have not been drafted and checked yet."
    elif counts.supported == 0:
        state = "not_in_document"
        message = (
            f"Your document does not support this keyword: none of the {counts.total} statements "
            "drafted about it could be found in the document, so it cannot appear in the video "
            "as something your document says. Leave it out, or add material that covers it."
        )
    else:
        state = "covered"
        message = (
            f"Covered by {counts.supported} statement{'' if counts.supported == 1 else 's'} "
            "checked against your document"
        )
        if counts.supported < counts.total:
            message += f" ({counts.total - counts.supported} more could not be supported)"
        message += "."
        if in_script is False:
            message += " The current script does not teach it: write the script again."
    return KeywordCoverage(
        topic_id=topic.topic_id,
        title=topic.title,
        state=state,
        supported_claims=counts.supported,
        total_claims=counts.total,
        in_script=in_script,
        message=message,
    )


def assess(
    topics: Sequence[TopicFacts],
    counts: Mapping[UUID, ClaimCounts],
    *,
    scripted_topic_ids: Collection[UUID] | None,
) -> list[KeywordCoverage]:
    """One verdict per keyword, in the order given."""
    return [
        assess_keyword(
            topic, counts.get(topic.topic_id, ClaimCounts()), scripted_topic_ids=scripted_topic_ids
        )
        for topic in topics
    ]


def is_complete(items: Sequence[KeywordCoverage]) -> bool:
    """Whether every keyword the owner kept is covered (a keyword they left out does not count
    against it). With no keywords there is nothing missing."""
    return all(item.state in ("covered", "left_out") for item in items)


def summary(items: Sequence[KeywordCoverage]) -> str | None:
    """One sentence about the whole set, or ``None`` when there are no keywords."""
    if not items:
        return None
    covered = sum(1 for item in items if item.state == "covered")
    missing = [item.title for item in items if item.state == "not_in_document"]
    pending = sum(1 for item in items if item.state == "not_checked")
    kept = sum(1 for item in items if item.state != "left_out")
    parts = [f"{covered} of {kept} keyword{'' if kept == 1 else 's'} covered by your document"]
    if missing:
        parts.append("not in your document: " + ", ".join(f"'{title}'" for title in missing))
    if pending:
        parts.append(f"{pending} not checked yet")
    return "; ".join(parts) + "."

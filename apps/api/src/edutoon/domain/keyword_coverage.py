"""Whether the keywords an owner asked for are covered by their document - pure domain logic
(rule 1: no I/O, no configuration).

An owner who uploads a PDF and types keywords ("Calvin cycle", "light-dependent reactions") asks
for those topics to be taught *from that document*. A keyword the document says nothing about must
not be taught as if it did, and the owner must not be left thinking it was: so for every keyword the
verdict is one of four, each with the sentence the owner reads.

``covered`` - at least one statement about it was checked against the document and supported.
``not_in_document`` - statements were drafted and none could be supported: the document does not
back this keyword, and nothing about it can reach the video as a fact from the PDF.
``not_checked`` - no statements have been drafted yet, so there is nothing to say either way.
``left_out`` - the owner rejected the topic.

Nothing here judges evidence; it reads the counts the claim pipeline already produced.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

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
    """What coverage needs to know about one topic the owner typed."""

    topic_id: UUID
    title: str
    status: str


def assess_keyword(
    topic: TopicFacts, counts: ClaimCounts, *, scripted_topic_ids: Collection[UUID] | None
) -> KeywordCoverage:
    """The coverage verdict for one keyword. ``scripted_topic_ids`` is the set of topics the
    project's current script teaches, or ``None`` when there is no script yet."""
    in_script = None if scripted_topic_ids is None else topic.topic_id in scripted_topic_ids

    if topic.status == "rejected":
        state: CoverageState = "left_out"
        message = "You left this keyword out of the lesson."
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

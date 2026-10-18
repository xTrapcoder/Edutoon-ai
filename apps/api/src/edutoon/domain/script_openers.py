"""Stop a lesson announcing itself in every section - pure domain logic (rule 1: no I/O).

A section writer told to "introduce the topic" opens nearly every section the same way - "Today, we
will learn about resistance." - and some add a "Today, we have learned ..." wrap-up before the
fixed closing. Each is a signpost that asserts nothing, so removing one can never remove a fact,
but heard ten times in four minutes it is the most noticeable thing about a script. Asking the
model not to did not stop a 7B writer (measured: four of ten sections still opened that way), so
this takes the repeats out deterministically: the lesson keeps its first announcement, and every
section keeps at least one passage.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import replace
from typing import Final

from edutoon.domain.narration_support import split_sentences
from edutoon.domain.script import ScriptSegmentDraft

_ANNOUNCEMENT: Final = re.compile(
    r"^(?:today|now|next|in this (?:lesson|section|video)),?\s+"
    r"(?:we|let'?s|let us|we'?ll|we will)\b[^.!?]*?"
    r"\b(?:learn|look at|talk about|discuss|explore|understand|cover)\b",
    re.IGNORECASE,
)
_PREMATURE_WRAP_UP: Final = re.compile(
    r"^(?:today|now|so),?\s+we(?:'ve| have)\s+(?:learned|learnt|covered|seen|looked at)\b",
    re.IGNORECASE,
)


def is_announcement(sentence: str) -> bool:
    """A sentence that only says what the lesson will do ("Today, we will learn about ...")."""
    return _ANNOUNCEMENT.match(sentence.strip()) is not None


def is_premature_wrap_up(sentence: str) -> bool:
    """A sentence that sums the lesson up before its end ("Today, we have learned about ...")."""
    return _PREMATURE_WRAP_UP.match(sentence.strip()) is not None


def drop_repeated_openers(
    segments: Sequence[ScriptSegmentDraft], *, words_per_minute: int
) -> tuple[list[ScriptSegmentDraft], int]:
    """``segments`` with every announcement after the first, and every premature wrap-up, taken out,
    and how many sentences that was. A passage left empty is dropped unless it is its topic's last
    one: a section is never left without a passage."""
    topic_passages: dict[object, int] = {}
    for segment in segments:
        topic_passages[segment.topic_id] = topic_passages.get(segment.topic_id, 0) + 1

    kept: list[ScriptSegmentDraft] = []
    seen_announcement = False
    removed = 0
    for segment in segments:
        sentences = split_sentences(segment.narration_text)
        remaining: list[str] = []
        for sentence in sentences:
            if is_announcement(sentence):
                if not seen_announcement:
                    seen_announcement = True
                    remaining.append(sentence)
                else:
                    removed += 1
            elif is_premature_wrap_up(sentence):
                removed += 1
            else:
                remaining.append(sentence)
        if len(remaining) == len(sentences):
            kept.append(segment)
            continue
        if not remaining:
            if topic_passages[segment.topic_id] > 1:
                topic_passages[segment.topic_id] -= 1
                continue
            # the topic's only passage: keep it rather than leave the section empty
            removed -= len(sentences)
            kept.append(segment)
            continue
        text = " ".join(remaining)
        kept.append(
            replace(
                segment,
                narration_text=text,
                estimated_duration_ms=round(len(text.split()) / words_per_minute * 60_000),
            )
        )
    return kept, removed

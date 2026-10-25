"""A lesson ends: a fixed closing, written here and not by the model - pure domain logic (rule 1:
no I/O, no imports of repositories / providers / services).

Real scripts kept ending abruptly. The writer does produce a closing ("In summary, the heart and
blood vessels work together to ..."), but a summary that *synthesises* several claims is exactly the
kind of sentence a strict support check will not find stated in any one of them, so it was removed;
the same had happened to uncited closings before they were allowed to stand (``narration_support``).
Loosening the judge to keep a summary would let in other unsupported synthesis.

So a script that does not already end in a closing gets a short fixed one that says nothing about
the subject at all: that the lesson is over, and which topics it covered, in the words of the topic
titles the owner accepted. It is code's sentence, free of figures and of anything a claim would
need to back, and the script's own grounding rule allows it because it names only the project's
and its topics' titles.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

MAX_TITLES_NAMED: Final = 6
"""More topics than this are named by their first and last, not one by one."""

# A sentence that ends a lesson - not merely one that looks back at an earlier part of it.
_CLOSING: Final = re.compile(
    r"^(?:in (?:summary|conclusion)|"
    r"to (?:sum up|conclude|wrap up|recap)|so,? to (?:sum up|conclude)|"
    r"that (?:concludes|brings us|wraps up|completes)|"
    r"this (?:concludes|brings us|wraps up)|"
    r"thanks? (?:you )?for (?:watching|listening)|"
    r"that'?s (?:all|it|everything) for)",
    re.IGNORECASE,
)

_SENTENCES: Final = re.compile(r"(?<=[.!?])\s+")


def has_closing(text: str) -> bool:
    """Whether the end of ``text`` (its last two sentences) is already a closing."""
    sentences = [s.strip() for s in _SENTENCES.split(text.strip()) if s.strip()]
    return any(_CLOSING.match(sentence) for sentence in sentences[-2:])


def _join(titles: Sequence[str]) -> str:
    if len(titles) == 1:
        return titles[0]
    return f"{', '.join(titles[:-1])} and {titles[-1]}"


def closing_text(topic_titles: Sequence[str]) -> str:
    """The fixed closing: that the lesson is over and which topics it covered. Titles are cleaned,
    and repeats removed, keeping their order."""
    seen: dict[str, None] = {}
    for title in topic_titles:
        cleaned = " ".join(title.split())
        if cleaned:
            seen.setdefault(cleaned, None)
    titles = list(seen)
    if not titles:
        return "That brings us to the end of this lesson."
    if len(titles) > MAX_TITLES_NAMED:
        # No count: a digit would be a figure no claim contains, which the script's own
        # rule rejects.
        covered = f"everything from {titles[0]} to {titles[-1]}"
    else:
        covered = _join(titles)
    return f"That brings us to the end of this lesson. We covered {covered}."

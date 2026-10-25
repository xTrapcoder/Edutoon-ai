"""Does a claim bring in a name or figure that the source passages do not contain? - pure domain
logic (rule 1: no I/O, no imports of repositories / providers / services).

A local model that judges "does this claim hold up against these passages?" can approve a claim on
the strength of its familiar part and overlook one word that is not in the source. A real case: the
passage said "Manchester, Birmingham and Leeds filled with workers"; the extracted claim said
"Factory towns in **India** grew rapidly, with the population of cities like Manchester,
Birmingham, and Leeds increasing sharply"; the judge's rationale discussed the three cities only and
the claim was stored as *supported*. Every script sentence is already held to the same
"figures and names must come from the claims" rule (:mod:`edutoon.domain.script_provenance`); this
applies it one step earlier, to the claim itself, so a claim carrying a place, person, date or
figure that no retrieved passage contains is never treated as established.

Like the script rule it is a safety guard and not fact verification: it never judges whether a claim
is *true*, only that it does not bring in a number, date or name from nowhere. Ordinary words,
number words and sentence-initial capitals are free.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

from edutoon.domain.claim_dedupe import content_words
from edutoon.domain.storyboard_provenance import unsupported_tokens

GROUNDED_KINDS: Final[frozenset[str]] = frozenset(
    {"number", "currency", "date_word", "proper_noun"}
)
"""The kinds of token that must appear in the passages: figures, dates and names."""

MAX_REPORTED: Final = 5


def ungrounded_entities(claim_text: str, passage_texts: Sequence[str]) -> tuple[str, ...]:
    """Every figure, date or name in ``claim_text`` that none of ``passage_texts`` contains, in
    the order found, at most :data:`MAX_REPORTED`. Empty when the claim is grounded, or when there
    are no passages to compare with (nothing can be said, so nothing is rejected)."""
    if not passage_texts:
        return ()
    reference = "\n".join(passage_texts)
    # An ordinal ("18th" for the passage's "eighteenth") or a formula coefficient ("6CO2") is a
    # different way of writing the same thing, not a figure from nowhere.
    checked = _ORDINAL_OR_FORMULA_RE.sub(" ", claim_text)
    reference_words = {word.lower() for word in _WORD_RE.findall(reference)}
    missing = [
        (kind, token)
        for kind, token in unsupported_tokens(checked, reference, kinds=GROUNDED_KINDS)
        if not (kind == "proper_noun" and _is_form_of_a_name_in(token, reference_words))
    ]
    # The detector gathers names in a set, so its order is not stable: report them in the order
    # they appear in the claim.
    tokens = {token for _, token in missing}
    ordered = sorted(tokens, key=lambda token: (_position_in(claim_text, token), token))
    return tuple(ordered)[:MAX_REPORTED]


def _position_in(text: str, token: str) -> int:
    position = text.find(token)
    return position if position >= 0 else len(text)


_ORDINAL_OR_FORMULA_RE: Final = re.compile(r"\b\d+(?:st|nd|rd|th)\b|\b\d+(?=[A-Za-z])")
_WORD_RE: Final = re.compile(r"[A-Za-z][A-Za-z'-]*")
MIN_NAME_PREFIX_LENGTH: Final = 5


def _is_form_of_a_name_in(name: str, reference_words: set[str]) -> bool:
    """``Hawaii`` is in a passage that says "Hawaiian": a name of five letters or more that begins
    a passage word is the same name in another form (a demonym, a derived adjective). A shorter
    name must appear as itself, so the rule never lets an unrelated short word through."""
    lowered = name.lower()
    return len(lowered) >= MIN_NAME_PREFIX_LENGTH and any(
        word.startswith(lowered) for word in reference_words
    )


def downgrade_rationale(original: str | None, entities: Sequence[str]) -> str:
    """The stored rationale for a verdict lowered from ``supported``: the judge's own words, then
    what the guard found, written for the project's owner."""
    names = ", ".join(f"'{entity}'" for entity in entities)
    found = (
        f"The claim mentions {names}, which none of the retrieved source passages contain, so it "
        "is only partly supported."
    )
    return f"{original.strip()} {found}" if original and original.strip() else found


# --- is the claim's wording in the passages it cites? -------------------------------------------

CITATION_COVERAGE_FLOOR: Final = 0.5
"""A claim marked supported must have at least this share of its content words in the passages it
cites. Measured on 276 supported claims from fresh runs: citations covering under half of a
claim's words were either true claims pointed at the wrong passage ("Animals that eat plants are
called herbivores" cited a page about energy flow) or claims dressed in words the source never
uses ("tiny living things that we can't see with our eyes", "to make way for agriculture or
development"), which the judge had approved all the same."""

CITATION_REPAIR_MINIMUM: Final = 0.6
"""A claim whose citation falls under the floor is kept, pointed at the retrieved passage that does
contain its wording, when that one passage covers at least this share of it."""

_PREFIX: Final = 5


def _stems_of(texts: Sequence[str]) -> set[str]:
    words = content_words(" ".join(texts))
    return set(words) | {word[:_PREFIX] for word in words}


def claim_coverage(claim_text: str, passage_texts: Sequence[str]) -> float:
    """The share of ``claim_text``'s content words that appear in ``passage_texts`` (a word matches
    its plural or other form sharing its first five letters). ``1.0`` for a claim with no content
    words. Not a measure of truth: it says only whether the cited material uses the claim's
    words."""
    claim_words = content_words(claim_text)
    if not claim_words:
        return 1.0
    reference = _stems_of(passage_texts)
    found = sum(1 for word in claim_words if word in reference or word[:_PREFIX] in reference)
    return found / len(claim_words)


def best_covering_passage(claim_text: str, passage_texts: Sequence[str]) -> int | None:
    """The index of the single passage that covers the most of ``claim_text`` when that is at least
    :data:`CITATION_REPAIR_MINIMUM`, else ``None``."""
    best: int | None = None
    best_coverage = 0.0
    for index, text in enumerate(passage_texts):
        coverage = claim_coverage(claim_text, [text])
        if coverage > best_coverage:
            best, best_coverage = index, coverage
    return best if best is not None and best_coverage >= CITATION_REPAIR_MINIMUM else None


def thin_citation_rationale(original: str | None, coverage: float) -> str:
    """The stored rationale for a verdict lowered because the cited passages do not hold the claim's
    wording."""
    found = (
        f"Only {coverage:.0%} of the claim's wording is in the passages it cites, and no other "
        "retrieved passage states it, so it is only partly supported."
    )
    return f"{original.strip()} {found}" if original and original.strip() else found

"""The provenance safety guard for Phase 9 scene text - pure domain logic
(rule 1: no I/O, no imports of repositories / providers / services).

**This is a safety guard, not fact verification.** It never judges whether a
claim is *true* - only whether a number, date, percentage, unit-bearing
quantity or capitalised proper noun that shows up in a scene's text slots was
also present, in some recognisable form, in the segment's own
``narration_text`` (the material the Evidence Engine already verified,
Phases 5-7). It is deliberately conservative and deliberately imperfect; see
"What this does and does not catch" below.

**The hard rule** (:func:`check_provenance`): a number, date, percentage,
unit-bearing quantity or capitalised proper noun in scene text that does not
also appear in the narration is a **fabrication** - the whole scene *set* for
that segment must be rejected (never silently dropped or trimmed; see
``services.storyboard``). A single fabricated figure in one scene invalidates
every scene generated alongside it for that call, since a partially-trusted
set can never be told apart from a fully-trusted one once persisted.

**The soft signal** (:func:`has_lexical_overlap`): separately, if a scene's
text shares no *meaningful* lexical overlap with the narration at all, that
is worth a reviewer's attention but is never itself grounds for rejection -
a well-written scene may legitimately paraphrase narration so thoroughly
that few content words are shared verbatim.

## What normalisation catches

- **Digits**: any run of digits (with optional thousands commas and a decimal
  point), matched as an exact string after commas are stripped - "1,000" in a
  scene matches "1000" or "1,000" in the narration, but not "one thousand".
- **Common number words**: a fixed list (``zero``-``billion``,
  ``first``-``tenth``), matched case-insensitively as whole words.
- **Currency**: the symbols ``$ £ € ¥ ₹`` (each checked as its own token) and
  a fixed list of currency words (``dollars``, ``rupees``, ``pounds``,
  ``euros``, ``cents``, ...).
- **Common units**: a fixed list of unit words (``km``, ``kg``, ``degrees``,
  ``hours``, ``percent``, ...), matched case-insensitively as whole words.
- **Dates**: month names/abbreviations (matched as words, so caught by the
  same mechanism as unit words) and any 4-digit year (caught by the digit
  check above; there is no separate calendar-date parser).
- **Capitalised proper nouns**: any Title-Case word (first letter upper, rest
  lower) that is not the first word of its sentence, matched
  case-insensitively against every word in the narration.

- **Numeric JSON leaves** (Phase 11): a number stored as a JSON number (``73``, ``3.5``) is checked
  exactly like a digit run in text - it must appear in the narration - so a value can never be
  smuggled into a scene as data instead of text. ``3.5`` and ``3.50`` are the same number.
  The only numbers exempt are the integer *indices* a visual uses to point at its own items
  (``parent``, ``focus`` inside ``visual``): they are structure, and are validated separately.
- **Visual text** (Phase 11): every free-text field of a scene's procedural ``visual`` is walked
  like any other slot (its labels, details, headings, points, subject, title), at any depth, in
  raw content *before* an invalid visual is dropped - so fabricated text cannot be discarded
  quietly. Its enumerated fields (``type``, ``relation``, ``form``) hold structure, not claims,
  and are skipped only when the value is one of the allowed ones.

## What normalisation does not catch

- A number spelled out in the scene that the narration also spells out but
  with different wording ("forty-two" vs "42"), or a number expressed as a
  fraction, a range, or in words not in the fixed list (e.g. "a dozen").
- A unit converted to another unit the narration used instead (narration
  says "500 metres", scene says "half a kilometre").
- An ALL-CAPS acronym (``DNA``, ``NASA``) - deliberately excluded from the
  proper-noun check, since an acronym is far more often a legitimate,
  narration-independent domain term than a fabrication.
- A proper noun that happens to be the very first word of a sentence in the
  scene text (skipped, to avoid flagging ordinary sentence-initial
  capitalisation as if it were a name).
- Any claim that is numerically or factually wrong but reuses only numbers,
  units and names that *do* appear somewhere in the narration (e.g.
  attaching a real figure to the wrong fact). This guard checks provenance
  of tokens, never the truth of the sentence they appear in.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final

from edutoon.domain.visual import ENUM_FIELDS, STRUCTURAL_INDEX_FIELDS

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]*")
# A digit run counts as a number unless a letter sits right before it: the 2 in CO2, the 12 in
# C6H12O6 are part of a chemical formula, not figures. (The lookbehind also stops a match from
# starting in the middle of a run.)
_NUMBER_TOKEN_RE = re.compile(r"(?<![A-Za-z\d])\d[\d,]*(?:\.\d+)?%?")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

NUMBER_WORDS: Final[frozenset[str]] = frozenset(
    {
        "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
        "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
        "eighteen", "nineteen", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
        "eighty", "ninety", "hundred", "thousand", "million", "billion", "trillion",
        "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth",
        "ninth", "tenth",
    }
)  # fmt: skip

MONTH_WORDS: Final[frozenset[str]] = frozenset(
    {
        "january", "february", "march", "april", "may", "june", "july", "august",
        "september", "october", "november", "december",
        "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
    }
)  # fmt: skip

UNIT_WORDS: Final[frozenset[str]] = frozenset(
    {
        "km", "kg", "m", "cm", "mm", "g", "mg", "l", "ml", "kmh", "mph",
        "degree", "degrees", "celsius", "fahrenheit",
        "year", "years", "month", "months", "week", "weeks", "day", "days",
        "hour", "hours", "hr", "hrs", "minute", "minutes", "min", "mins",
        "second", "seconds", "sec", "secs",
        "percent", "percentage",
        "dollar", "dollars", "cent", "cents", "rupee", "rupees",
        "pound", "pounds", "euro", "euros",
        "metre", "metres", "meter", "meters", "kilometre", "kilometres",
        "kilometer", "kilometers", "centimetre", "centimetres", "centimeter",
        "centimeters", "millimetre", "millimetres", "millimeter", "millimeters",
        "mile", "miles",
        "litre", "litres", "liter", "liters",
        "gram", "grams", "kilogram", "kilograms",
    }
)  # fmt: skip

CURRENCY_SYMBOLS: Final[frozenset[str]] = frozenset({"$", "£", "€", "¥", "₹"})

# Common function words, excluded from the lexical-overlap soft signal so it
# measures shared *content*, never shared grammar.
_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "at", "is", "are",
        "was", "were", "this", "that", "these", "those", "it", "its", "for", "with", "as",
        "by", "from", "be", "been", "being", "has", "have", "had", "not", "no", "so", "than",
        "then", "there", "here", "will", "would", "can", "could", "should", "may", "might",
        "into", "over", "under", "about", "above", "below", "more", "most", "each", "some",
        "any", "all", "both", "few", "other", "such", "only", "own", "same", "too", "very",
        "just", "also", "you", "your", "we", "our", "they", "their", "he", "she", "his", "her",
    }
)  # fmt: skip


class FabricatedContentError(Exception):
    """A scene set contains at least one hard provenance violation. Carries
    every violation found (not just the first), so the caller can log and
    report them all. Raised by :func:`require_provenance`.
    """

    def __init__(self, violations: list[ProvenanceViolation]) -> None:
        self.violations = violations
        joined = "; ".join(
            f"{v.slot_path!r}: {v.kind} {v.token!r} not found in narration" for v in violations
        )
        super().__init__(f"{len(violations)} provenance violation(s): {joined}")


@dataclass(frozen=True, slots=True)
class ProvenanceViolation:
    slot_path: str
    kind: str
    token: str


def _capitalised_candidates(text: str) -> set[str]:
    candidates: set[str] = set()
    for sentence in _SENTENCE_SPLIT_RE.split(text.strip()):
        words = _WORD_RE.findall(sentence)
        for word in words[1:]:  # skip the sentence's own first word
            if len(word) >= 2 and word[0].isupper() and word[1:].islower():
                candidates.add(word)
    return candidates


def _protected_tokens(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    for match in _NUMBER_TOKEN_RE.finditer(text):
        normalised = match.group().replace(",", "").rstrip("%")
        if normalised and normalised != ".":
            tokens.append((normalised, "number"))
    for symbol in CURRENCY_SYMBOLS:
        if symbol in text:
            tokens.append((symbol, "currency"))
    words_lower = {word.lower() for word in _WORD_RE.findall(text)}
    tokens.extend((word, "number_word") for word in words_lower & NUMBER_WORDS)
    tokens.extend((word, "date_word") for word in words_lower & MONTH_WORDS)
    tokens.extend((word, "unit") for word in words_lower & UNIT_WORDS)
    tokens.extend((word, "proper_noun") for word in _capitalised_candidates(text))
    return tokens


def _canonical_number(text: str) -> str:
    """A number's plain decimal form, so ``3.50`` and ``3.5``, or ``1000`` and ``1e3``, compare
    equal. A string that is not a number comes back unchanged."""
    try:
        value = abs(Decimal(text))
    except InvalidOperation:
        return text
    if not value.is_finite():
        return text
    return format(value.normalize(), "f")


def _narration_numbers(narration_text: str) -> set[str]:
    found = {
        match.group().replace(",", "").rstrip("%")
        for match in _NUMBER_TOKEN_RE.finditer(narration_text)
    }
    return found | {_canonical_number(number) for number in found}


def _strip_possessive(word: str) -> str:
    """``earth's`` -> ``earth``: a possessive is the same name, and must not read as a new one."""
    lowered = word.lower()
    return lowered[:-2] if lowered.endswith("'s") else lowered


def _narration_words(narration_text: str) -> set[str]:
    words = {word.lower() for word in _WORD_RE.findall(narration_text)}
    return words | {_strip_possessive(word) for word in words}


def check_scene_text(
    slot_path: str, scene_text: str, narration_text: str
) -> list[ProvenanceViolation]:
    """Every hard-rule violation in one text slot, checked against
    ``narration_text``. Pure and side-effect free: returns an empty list for
    honest text, never raises.
    """
    if not scene_text:
        return []

    narration_numbers = _narration_numbers(narration_text)
    narration_words = _narration_words(narration_text)

    violations: list[ProvenanceViolation] = []
    for token, kind in _protected_tokens(scene_text):
        if kind == "number":
            allowed = token in narration_numbers or _canonical_number(token) in narration_numbers
        elif kind == "currency":
            allowed = token in narration_text
        else:  # number_word, date_word, unit, proper_noun
            allowed = (
                token.lower() in narration_words or _strip_possessive(token) in narration_words
            )
        if not allowed:
            violations.append(ProvenanceViolation(slot_path=slot_path, kind=kind, token=token))
    return violations


def unsupported_tokens(
    text: str, reference_text: str, *, kinds: frozenset[str]
) -> list[tuple[str, str]]:
    """``(kind, token)`` for every protected token in ``text`` of one of ``kinds`` that
    ``reference_text`` does not contain - the same detection scene text is held to, exposed for
    any other text that must not add facts (a script segment's narration is checked against the
    claims it was written from). ``kinds`` are the violation kinds: ``number``, ``currency``,
    ``number_word``, ``date_word``, ``unit``, ``proper_noun``."""
    return [
        (violation.kind, violation.token)
        for violation in check_scene_text("", text, reference_text)
        if violation.kind in kinds
    ]


def _walk_leaves(
    value: object, *, path: str, key: str, in_visual: bool
) -> Iterator[tuple[str, str, object, bool]]:
    """Every scalar leaf (string or number; never a bool or null) of a decoded JSON value, at any
    depth and through lists of anything, as ``(path, owning key, value, inside a visual)``. The
    path style is ``illustration.description`` / ``labels[0].text`` / ``bullets[1]``."""
    if isinstance(value, Mapping):
        for child_key, child in value.items():
            yield from _walk_leaves(
                child,
                path=f"{path}.{child_key}" if path else str(child_key),
                key=str(child_key),
                in_visual=in_visual or child_key == "visual",
            )
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_leaves(item, path=f"{path}[{index}]", key=key, in_visual=in_visual)
    elif isinstance(value, bool) or value is None:
        return
    elif isinstance(value, str | int | float):
        yield path, key, value, in_visual


def _iter_text_slots(content: Mapping[str, object]) -> Iterator[tuple[str, str]]:
    """Every string leaf in a (possibly nested) scene ``content`` dict, with
    a dotted/indexed path identifying where it came from - illustration
    descriptions, label text and a visual's words included, since those are
    exactly the kind of free text a model could use to slip in a fabricated
    figure. A visual's enumerated fields (``type``, ``relation``, ``form``) are
    structure, not text, and are skipped when their value is an allowed one.
    """
    for path, key, value, in_visual in _walk_leaves(content, path="", key="", in_visual=False):
        if not isinstance(value, str):
            continue
        if in_visual and key in ENUM_FIELDS and value in ENUM_FIELDS[key]:
            continue
        yield path, value


def _iter_numeric_slots(content: Mapping[str, object]) -> Iterator[tuple[str, int | float]]:
    """Every numeric JSON leaf of ``content`` - except the integer indices a visual uses to point
    at its own items (``parent``, ``focus``), which are structure."""
    for path, key, value, in_visual in _walk_leaves(content, path="", key="", in_visual=False):
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        if in_visual and key in STRUCTURAL_INDEX_FIELDS and isinstance(value, int):
            continue
        yield path, value


def check_provenance(
    content: Mapping[str, object], narration_text: str
) -> list[ProvenanceViolation]:
    """Every hard-rule violation across all of ``content``'s text slots *and* numeric leaves."""
    violations: list[ProvenanceViolation] = []
    for slot_path, text in _iter_text_slots(content):
        violations.extend(check_scene_text(slot_path, text, narration_text))
    numbers = _narration_numbers(narration_text)
    for slot_path, number in _iter_numeric_slots(content):
        if isinstance(number, float) and not math.isfinite(number):
            violations.append(ProvenanceViolation(slot_path, "number", repr(number)))
            continue
        token = _canonical_number(repr(number))
        if token not in numbers:
            violations.append(ProvenanceViolation(slot_path, "number", token))
    return violations


def require_provenance(content: Mapping[str, object], narration_text: str) -> None:
    """Raise :class:`FabricatedContentError` if ``content`` contains any hard
    provenance violation against ``narration_text``; otherwise return
    normally. The caller (``services.storyboard``) is expected to reject the
    *whole* scene set a violation was found in, never just the offending
    scene.
    """
    violations = check_provenance(content, narration_text)
    if violations:
        raise FabricatedContentError(violations)


def has_lexical_overlap(
    content: Mapping[str, object], narration_text: str, *, min_word_length: int = 4
) -> bool:
    """The soft signal (spec section 14): ``True`` if the scene's own text
    shares at least one meaningful content word (length >= ``min_word_length``,
    not a stopword) with the narration - never a rejection, only a reviewer
    flag when it comes back ``False``. A scene with no text slots at all
    (nothing to compare) counts as having overlap - there is nothing to warn
    the reviewer about.
    """

    def _content_words(text: str) -> set[str]:
        return {
            word.lower()
            for word in _WORD_RE.findall(text)
            if len(word) >= min_word_length and word.lower() not in _STOPWORDS
        }

    scene_text = " ".join(text for _, text in _iter_text_slots(content))
    if not scene_text.strip():
        return True

    scene_words = _content_words(scene_text)
    if not scene_words:
        return True

    narration_words = _content_words(narration_text)
    return bool(scene_words & narration_words)

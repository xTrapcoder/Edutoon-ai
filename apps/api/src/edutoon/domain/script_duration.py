"""Deterministic, server-authoritative duration budgeting for Phase 7's
per-topic script sections - pure domain logic (rule 1: no I/O, no imports of
repositories / providers / services, and no configuration reads).

The LLM never decides how long a topic's section runs: :func:`allocate_topic_budgets`
turns a project's ``target_duration_ms`` and its accepted topics' evidence volume
into a fixed, reproducible list of per-topic millisecond budgets *before* any
provider call is made, and :func:`split_topic_budget` turns one such budget, when
it is larger than a single provider call should produce, into an ordered list of
smaller sub-section budgets - so a single topic filling a long target never
becomes one enormous call. :func:`max_provider_calls` is the mathematical upper
bound on how many provider calls one generation can ever make, derived from the
same limits, for the service layer to enforce (never merely document).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal
from uuid import UUID


@dataclass(frozen=True, slots=True)
class TopicDurationBudget:
    topic_id: UUID
    duration_ms: int


def allocate_topic_budgets(
    *,
    topic_ids: Sequence[UUID],
    claim_counts: Mapping[UUID, int],
    target_duration_ms: int,
    min_section_duration_ms: int,
) -> tuple[TopicDurationBudget, ...]:
    """Split ``target_duration_ms`` across ``topic_ids`` (in the order given -
    callers pass reading order, already the teaching sequence).

    Every topic first receives ``min_section_duration_ms`` as a floor; the
    remainder is shared out proportionally to each topic's entry in
    ``claim_counts`` (its evidence volume - a topic missing from the mapping,
    or with a non-positive count, is treated as having exactly one claim, so it
    still receives its fair share rather than nothing). Integer division only,
    so the result is exact and reproducible; the (at most ``len(topic_ids) - 1``
    millisecond) rounding remainder is handed out one millisecond at a time,
    starting from the first topic, so the total always equals
    ``target_duration_ms`` exactly.

    Raises :class:`ValueError` if ``topic_ids`` is empty, if
    ``min_section_duration_ms`` is not positive, or if there are more topics
    than the target duration can give each the floor - the caller is expected
    to have already turned that into a clear, user-facing rejection before
    reaching here (this is a defence-in-depth check, not the primary one).

    There is deliberately no ceiling applied here: a topic may legitimately be
    allocated more than any single provider call should produce (e.g. very few
    topics against a long target). :func:`split_topic_budget` is how that is
    turned into several calls instead of one; this function only decides *how
    much* narration time a topic gets in total.
    """
    if not topic_ids:
        raise ValueError("allocate_topic_budgets requires at least one topic.")
    if min_section_duration_ms < 1:
        raise ValueError("min_section_duration_ms must be at least 1.")

    topic_count = len(topic_ids)
    floor_total = min_section_duration_ms * topic_count
    if floor_total > target_duration_ms:
        raise ValueError(
            f"{topic_count} accepted topics cannot each receive the "
            f"{min_section_duration_ms}ms floor within a {target_duration_ms}ms "
            "target duration."
        )

    weights = [max(claim_counts.get(topic_id, 0), 1) for topic_id in topic_ids]
    total_weight = sum(weights)
    remainder = target_duration_ms - floor_total

    allocated = [
        min_section_duration_ms + (remainder * weight) // total_weight for weight in weights
    ]
    leftover = target_duration_ms - sum(allocated)
    for i in range(leftover):
        allocated[i % topic_count] += 1

    return tuple(
        TopicDurationBudget(topic_id=topic_id, duration_ms=duration_ms)
        for topic_id, duration_ms in zip(topic_ids, allocated, strict=True)
    )


def split_topic_budget(duration_ms: int, *, max_section_duration_ms: int) -> tuple[int, ...]:
    """``duration_ms`` split into the fewest chunks that each stay within
    ``max_section_duration_ms``, as evenly sized as possible (so a topic's
    sub-sections read as similar-length parts of one lesson, not one full
    chunk plus a thin leftover). The chunks sum to exactly ``duration_ms``.

    Returns a single-element tuple, unchanged, when ``duration_ms`` already
    fits in one call. Raises :class:`ValueError` if either argument is not
    positive.
    """
    if duration_ms < 1:
        raise ValueError("duration_ms must be at least 1.")
    if max_section_duration_ms < 1:
        raise ValueError("max_section_duration_ms must be at least 1.")

    chunk_count = -(-duration_ms // max_section_duration_ms)  # ceil division
    base, extra = divmod(duration_ms, chunk_count)
    return tuple(base + (1 if i < extra else 0) for i in range(chunk_count))


def max_provider_calls(
    *,
    max_target_duration_ms: int,
    max_section_duration_ms: int,
    max_script_sections: int,
) -> int:
    """The provable worst-case number of provider calls a single generation
    can ever make: one outline call, plus every topic's section/sub-section
    calls, plus one full bounded correction pass over the same set of calls.

    For ``n`` accepted topics with budgets ``b_1..b_n`` summing to at most
    ``max_target_duration_ms`` (``D``), the number of section/sub-section
    calls is ``sum(ceil(b_i / M))`` where ``M`` is ``max_section_duration_ms``.
    Since ``ceil(x / M) <= x / M + 1`` for any positive ``x``, that sum is at
    most ``D / M + n``. Rounding ``D / M`` up keeps the bound valid even
    though it is not always the tightest achievable value - callers must
    enforce this exact number before making any provider call, never merely
    report it.
    """
    max_sections = -(-max_target_duration_ms // max_section_duration_ms) + max_script_sections
    return 1 + 2 * max_sections


LengthDecision = Literal["within_tolerance", "rescale", "skip_evidence_limited"]


def decide_length_correction(
    estimated_ms: int,
    target_ms: int,
    tolerance: float,
    *,
    evidence_limited: bool,
) -> LengthDecision:
    """Whether the assembled script's length should be corrected by a second writing pass.

    ``within_tolerance``: the length is close enough to the target; nothing to do.
    ``rescale``: it is outside the tolerance and a pass with a rescaled word budget may help.
    ``skip_evidence_limited``: it is *short*, and the checks already took unsupported text out of
    it (or rewrote it, or read a section's claims out instead). More words from the same verified
    claims could only be repetition or invention - exactly what the checks removed - and the second
    pass would also cost as long as the first. The script is kept as it is and is reported as
    shorter than the target because the evidence cannot carry more; the target is a guide, not a
    reason to pad. A script that is too *long* is always corrected: that never needs new facts.
    """
    if abs(estimated_ms - target_ms) <= target_ms * tolerance:
        return "within_tolerance"
    if estimated_ms < target_ms and evidence_limited:
        return "skip_evidence_limited"
    return "rescale"

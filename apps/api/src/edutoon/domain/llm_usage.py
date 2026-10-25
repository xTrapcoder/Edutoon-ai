"""What the language model cost, by kind of call - pure domain logic (rule 1: no I/O, no imports of
repositories / providers / services, and no configuration reads).

Every provider call that returns usage is recorded against one of :data:`LLM_CALL_KINDS`, summed in
a :class:`UsageReport` while a job runs, and stored on the job's ``result["usage"]`` - so the cost
of one generation is read from that job, and the cost of a project is the sum of its jobs'
(:func:`UsageReport.from_dict` / :meth:`UsageReport.merge`).

**Nothing is invented.** A metric the provider did not return (cache figures on a model that does
not report them, or a response with no usage at all) stays ``None`` - *unavailable* - and is never
counted as zero. ``calls`` counts the calls that were answered; ``calls_with_usage`` the ones whose
usage is known. When they differ, the token sums are a lower bound, and the report says so by
carrying both.

A response that was received and then rejected (cut off, refused, malformed) is still paid for, so
its usage is recorded too; a call that failed before any response (a timeout, a rate limit) has no
usage to record and is not counted.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol

LLM_CALL_KINDS: Final[tuple[str, ...]] = (
    "topic_extraction",
    "claim_extraction",
    "claim_judge",
    "script_outline",
    "script_section",
    "script_correction",
    "storyboard",
    "narration_support",
)
"""Every kind of language-model call the application makes. ``script_correction`` is the section
calls of the one bounded duration-correction pass, kept apart from the first pass's
``script_section`` calls because the correction is the cost of missing the target duration."""

_METRICS: Final[tuple[str, ...]] = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


class ProviderCallLimitError(Exception):
    """A job reached the most language-model calls one job may make (``MAX_PROVIDER_CALLS_PER_JOB``)
    and was stopped before making another. The message is written for the project's owner."""

    def __init__(self, limit: int) -> None:
        super().__init__(
            f"This job stopped after {limit} AI calls, the most a single job may make, and asked "
            "for nothing more. If your material really needs more, contact support."
        )
        self.limit = limit


class UsageLike(Protocol):
    """What a provider's usage object offers - :class:`edutoon.providers.llm.TokenUsage` and
    anything shaped like it."""

    @property
    def input_tokens(self) -> int: ...

    @property
    def output_tokens(self) -> int: ...

    @property
    def cache_creation_input_tokens(self) -> int | None: ...

    @property
    def cache_read_input_tokens(self) -> int | None: ...


def _sum(left: int | None, right: int | None) -> int | None:
    """``left + right`` where ``None`` means "unavailable": both unavailable stays unavailable,
    one unavailable counts as the other alone."""
    if left is None and right is None:
        return None
    return (left or 0) + (right or 0)


def _as_count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


@dataclass(slots=True)
class UsageEntry:
    """The usage of one kind of call, or of all of them (the total)."""

    calls: int = 0
    calls_with_usage: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None

    def add(self, usage: UsageLike | None) -> None:
        """Count one answered call. ``usage`` is ``None`` when the response carried none."""
        self.calls += 1
        if usage is None:
            return
        self.calls_with_usage += 1
        for metric in _METRICS:
            setattr(self, metric, _sum(getattr(self, metric), getattr(usage, metric, None)))

    def merge(self, other: UsageEntry) -> None:
        self.calls += other.calls
        self.calls_with_usage += other.calls_with_usage
        for metric in _METRICS:
            setattr(self, metric, _sum(getattr(self, metric), getattr(other, metric)))

    def as_dict(self) -> dict[str, int | None]:
        return {
            "calls": self.calls,
            "calls_with_usage": self.calls_with_usage,
            **{metric: getattr(self, metric) for metric in _METRICS},
        }

    @classmethod
    def from_dict(cls, raw: object) -> UsageEntry:
        """Read back a stored entry. Anything malformed reads as no usage (a stored result is data
        from the database, not something to crash a cost report on)."""
        if not isinstance(raw, Mapping):
            return cls()
        return cls(
            calls=_as_count(raw.get("calls")) or 0,
            calls_with_usage=_as_count(raw.get("calls_with_usage")) or 0,
            **{metric: _as_count(raw.get(metric)) for metric in _METRICS},
        )


class UsageReport:
    """The usage of a job (or, merged, of a project), by kind of call."""

    def __init__(self, max_calls: int | None = None) -> None:
        self._entries: dict[str, UsageEntry] = {}
        self.max_calls = max_calls

    def ensure_capacity(self) -> None:
        """Raise :class:`ProviderCallLimitError` if the job has already made the most calls it
        may. Called before each call, so the call that would exceed the limit is never made."""
        if self.max_calls is not None and self.total.calls >= self.max_calls:
            raise ProviderCallLimitError(self.max_calls)

    def add(self, kind: str, usage: UsageLike | None) -> None:
        """Record one answered call of ``kind``. Raises :class:`ValueError` for a kind that is not
        in :data:`LLM_CALL_KINDS` - a typo must not open a new, unreported bucket."""
        if kind not in LLM_CALL_KINDS:
            raise ValueError(f"Unknown language-model call kind {kind!r}.")
        self._entries.setdefault(kind, UsageEntry()).add(usage)

    def merge(self, other: UsageReport) -> None:
        for kind, entry in other._entries.items():
            self._entries.setdefault(kind, UsageEntry()).merge(entry)

    @property
    def total(self) -> UsageEntry:
        total = UsageEntry()
        for entry in self._entries.values():
            total.merge(entry)
        return total

    def entry(self, kind: str) -> UsageEntry:
        return self._entries.get(kind, UsageEntry())

    def as_dict(self) -> dict[str, Any]:
        """``{"total": {...}, "by_call": {kind: {...}}}`` - kinds in their canonical order, only
        those that were used."""
        return {
            "total": self.total.as_dict(),
            "by_call": {
                kind: self._entries[kind].as_dict()
                for kind in LLM_CALL_KINDS
                if kind in self._entries
            },
        }

    @classmethod
    def from_dict(cls, raw: object) -> UsageReport:
        """Read back a stored report; unknown kinds and anything malformed are ignored."""
        report = cls()
        if not isinstance(raw, Mapping):
            return report
        by_call = raw.get("by_call")
        if isinstance(by_call, Mapping):
            for kind in LLM_CALL_KINDS:
                if kind in by_call:
                    report._entries[kind] = UsageEntry.from_dict(by_call[kind])
        return report

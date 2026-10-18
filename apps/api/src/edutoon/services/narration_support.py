"""Judging a script segment's sentences against the verified claims it may rest on.

``domain.narration_support`` decides which sentences are judged and how text is taken out;
``providers.llm.LLM.judge_sentence_support`` asks the model. This module is the loop between them:
one judged sentence at a time (a small local model is only dependable on one sentence), a second,
stricter opinion for a sentence that carries a cause, reason or requirement cue, one retry of a call
that failed, and an honest "unchecked" for a sentence the judge could not be got to answer.

A sentence is only ever removed on a positive ``unsupported`` verdict; a judge that fails leaves the
sentence in and says so on the job's result.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field

import structlog

from edutoon.domain import narration_support as ns
from edutoon.domain.llm_usage import UsageReport
from edutoon.providers.llm import LLM, Claim, LLMResponseError
from edutoon.services.llm_usage import record_call

log = structlog.get_logger()

KIND = "narration_support"


@dataclass(slots=True)
class SupportLog:
    """What the support check did over one script generation, for the job's result. Also a cache:
    the same sentence judged against the same claims is not judged twice (a duration-correction
    pass often rewrites a section with many of the same sentences)."""

    enabled: bool = True
    judged_sentences: int = 0
    second_opinions: int = 0
    unchecked_sentences: int = 0
    cache_hits: int = 0
    removed: list[dict[str, object]] = field(default_factory=list)
    rewrites_for_support: int = 0
    fallback_sections: list[dict[str, object]] = field(default_factory=list)
    trimmed_examples: list[dict[str, object]] = field(default_factory=list)
    _cache: dict[tuple[str, str], ns.SentenceJudgement | None] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "judged_sentences": self.judged_sentences,
            "second_opinions": self.second_opinions,
            "unchecked_sentences": self.unchecked_sentences,
            "cache_hits": self.cache_hits,
            "rewrites_for_support": self.rewrites_for_support,
            "removed_sentences": self.removed,
            "fallback_sections": self.fallback_sections,
            "trimmed_examples": self.trimmed_examples,
        }


def _claims_key(claims: Sequence[Claim]) -> str:
    digest = hashlib.sha256("\n".join(sorted(claim.text for claim in claims)).encode())
    return digest.hexdigest()


async def _ask(
    llm: LLM,
    report: UsageReport,
    sentence: str,
    claims: Sequence[Claim],
    *,
    second_opinion: bool,
) -> ns.SentenceJudgement | None:
    """One judgement, retried once if the answer was unusable; ``None`` if it still was."""
    for attempt in (1, 2):
        try:
            return await record_call(
                report,
                KIND,
                llm.judge_sentence_support(sentence, claims, second_opinion=second_opinion),
            )
        except LLMResponseError as exc:
            log.warning("narration_support.unusable_answer", attempt=attempt, reason=str(exc))
    return None


async def judge_sentence(
    llm: LLM,
    report: UsageReport,
    sentence: str,
    claims: Sequence[Claim],
    support: SupportLog,
) -> ns.SentenceJudgement | None:
    """The verdict on one sentence, or ``None`` if it could not be judged (it is then kept and
    counted as unchecked). ``unsupported`` if either opinion says so."""
    key = (sentence, _claims_key(claims))
    if key in support._cache:
        support.cache_hits += 1
        return support._cache[key]

    first = await _ask(llm, report, sentence, claims, second_opinion=False)
    support.judged_sentences += 1
    result = first
    if first is not None and first.verdict == "supported" and ns.needs_second_opinion(sentence):
        second = await _ask(llm, report, sentence, claims, second_opinion=True)
        support.second_opinions += 1
        if second is not None and second.verdict == "unsupported":
            result = second
    if result is None:
        support.unchecked_sentences += 1
    else:
        support._cache[key] = result
    return result


async def unsupported_in(
    llm: LLM,
    report: UsageReport,
    narration: str,
    claims: Sequence[Claim],
    support: SupportLog,
) -> list[ns.UnsupportedSentence]:
    """The sentences of ``narration`` that no claim in ``claims`` supports."""
    sentences = ns.split_sentences(narration)
    flagged: list[ns.UnsupportedSentence] = []
    for position, sentence in enumerate(sentences):
        if not ns.needs_judging(sentence):
            continue
        part = ns.factual_part(sentence)
        if part is None:
            continue
        judgement = await judge_sentence(llm, report, part, claims, support)
        if judgement is not None and judgement.verdict == "unsupported":
            flagged.append(
                ns.UnsupportedSentence(position=position, text=sentence, reason=judgement.reason)
            )
    return flagged

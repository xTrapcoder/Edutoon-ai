"""Anthropic-backed language tasks - the only module allowed to import
``anthropic`` (rule 3).

Deliberately narrow: a caller cannot send an arbitrary prompt. There are
exactly two capabilities, each a single typed question with a single typed
answer:

- :meth:`LLM.judge_claim` - "does this claim hold up against these specific
  passages?" Retrieval already picked the candidates; this only judges
  entailment against exactly those, and reports which ones it relied on.
- :meth:`LLM.compose_script` (SG-2) - "write the narration for this brief
  using only these verified claims." The Evidence Engine already verified
  the claims; this only arranges them into ordered narration segments and
  records, per segment, which claim ids that narration draws on. Every
  cited id is checked back against the supplied claims.

Both force the model to answer through a tool call and raise
:class:`LLMResponseError` - never a plausible-looking fabrication - when the
response does not match the expected shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

from anthropic import AsyncAnthropic
from anthropic.types import MessageParam, ToolChoiceToolParam, ToolParam, ToolUseBlock

VerdictStatus = Literal["supported", "partially_supported", "unsupported", "contradicted"]

_VALID_STATUSES: frozenset[str] = frozenset(
    {"supported", "partially_supported", "unsupported", "contradicted"}
)

_VERDICT_TOOL_NAME = "submit_verdict"

_SYSTEM_PROMPT = (
    "You verify a single factual claim against numbered source passages. "
    "Judge only whether the given passages support the claim - never rely on "
    "outside knowledge. Cite only the passage ids you actually relied on to "
    "reach the verdict; never cite a passage that is merely topically "
    "related but doesn't itself support or contradict the claim. If no "
    "passage supports the claim, cite none."
)

_VERDICT_TOOL: ToolParam = {
    "name": _VERDICT_TOOL_NAME,
    "description": "Submit the verification verdict for the claim.",
    "input_schema": {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": sorted(_VALID_STATUSES),
            },
            "rationale": {"type": "string"},
            "cited_passage_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Ids of the passages that actually support the verdict. "
                    "Empty if the claim is unsupported."
                ),
            },
        },
        "required": ["status", "rationale", "cited_passage_ids"],
    },
}


class LLMResponseError(Exception):
    """The model's response didn't match the expected verdict shape."""


@dataclass(frozen=True, slots=True)
class Passage:
    id: str
    content: str


@dataclass(frozen=True, slots=True)
class Verdict:
    status: VerdictStatus
    rationale: str
    cited_passage_ids: list[str]


def _format_passages(passages: list[Passage]) -> str:
    return "\n\n".join(f"[{passage.id}]\n{passage.content}" for passage in passages)


# --- Script composition (SG-2) -----------------------------------------------

_SCRIPT_TOOL_NAME = "submit_script"

_SCRIPT_SYSTEM_PROMPT = (
    "You write the narration for a short animated explainer video. Work only "
    "from the verified claims you are given: every factual statement in the "
    "narration must be traceable to one of them, and you must not add facts "
    "from outside knowledge. Break the narration into ordered segments, each "
    "a single spoken passage. For every segment, list the ids of the claims "
    "that segment's narration draws on. A purely connective segment - an "
    "introduction, or a hand-off between topics - that asserts no fact of its "
    "own may list no claim ids. Write in British English."
)

_SCRIPT_TOOL: ToolParam = {
    "name": _SCRIPT_TOOL_NAME,
    "description": "Submit the finished narration script.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "segments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "narration_text": {"type": "string"},
                        "claim_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Ids of the supplied claims this segment's "
                                "narration draws on; empty for a purely "
                                "connective segment."
                            ),
                        },
                    },
                    "required": ["narration_text", "claim_ids"],
                },
            },
        },
        "required": ["title", "segments"],
    },
}


@dataclass(frozen=True, slots=True)
class Claim:
    """A single verified claim offered to the script writer."""

    id: str
    text: str


@dataclass(frozen=True, slots=True)
class ScriptBrief:
    """What the script should cover and how it should sound. Only ``topic``
    is required; the optional fields are added to the prompt when present.
    """

    topic: str
    audience: str | None = None
    tone: str | None = None
    target_duration_ms: int | None = None


@dataclass(frozen=True, slots=True)
class ScriptSegmentDraft:
    segment_index: int
    narration_text: str
    claim_ids: list[str]
    estimated_duration_ms: int


@dataclass(frozen=True, slots=True)
class ScriptDraft:
    title: str
    segments: list[ScriptSegmentDraft]
    estimated_duration_ms: int


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int


def _format_claims(claims: list[Claim]) -> str:
    return "\n".join(f"[{claim.id}] {claim.text}" for claim in claims)


def _format_brief(brief: ScriptBrief) -> str:
    lines = [f"Topic: {brief.topic}"]
    if brief.audience:
        lines.append(f"Audience: {brief.audience}")
    if brief.tone:
        lines.append(f"Tone: {brief.tone}")
    if brief.target_duration_ms is not None:
        lines.append(f"Target duration (ms): {brief.target_duration_ms}")
    return "\n".join(lines)


def _estimate_segment_duration_ms(narration_text: str, words_per_minute: int) -> int:
    """Estimate a segment's spoken length from its word count. Integer
    milliseconds (rule 5); this is an independent per-segment estimate, not
    an accumulator (rule 6 concerns frame positions, derived elsewhere).
    """
    word_count = len(narration_text.split())
    return round(word_count / words_per_minute * 60_000)


def _parse_script_draft(
    raw: dict[str, object],
    *,
    known_claim_ids: set[str],
    max_segments: int,
    words_per_minute: int,
) -> ScriptDraft:
    """Validate the model's ``submit_script`` payload and turn it into a
    :class:`ScriptDraft`. Raises :class:`LLMResponseError` on any shape
    mismatch, an over-long script, or a cited claim id that was never
    supplied - the last one is a hard error here (a script traced to a
    non-existent claim is malformed), unlike :meth:`LLM.judge_claim`, which
    merely filters unknown ids.
    """
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        raise LLMResponseError("Script title must be a non-empty string.")

    raw_segments = raw.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise LLMResponseError("Script must contain at least one segment.")
    if len(raw_segments) > max_segments:
        raise LLMResponseError(
            f"Script has {len(raw_segments)} segments; the limit is {max_segments}."
        )

    segments: list[ScriptSegmentDraft] = []
    for index, raw_segment in enumerate(raw_segments):
        if not isinstance(raw_segment, dict):
            raise LLMResponseError("Each script segment must be an object.")

        narration = raw_segment.get("narration_text")
        if not isinstance(narration, str) or not narration.strip():
            raise LLMResponseError(
                "Each segment's narration_text must be a non-empty string."
            )

        raw_claim_ids = raw_segment.get("claim_ids")
        if not isinstance(raw_claim_ids, list):
            raise LLMResponseError("Each segment's claim_ids must be a list of strings.")
        claim_ids: list[str] = []
        for claim_id in raw_claim_ids:
            if not isinstance(claim_id, str):
                raise LLMResponseError(
                    "Each segment's claim_ids must be a list of strings."
                )
            claim_ids.append(claim_id)

        unknown = sorted({cid for cid in claim_ids if cid not in known_claim_ids})
        if unknown:
            raise LLMResponseError(
                f"Segment {index} cites claim ids that were not supplied: {unknown}."
            )

        segments.append(
            ScriptSegmentDraft(
                segment_index=index,
                narration_text=narration,
                # De-duplicate, keeping first-seen order.
                claim_ids=list(dict.fromkeys(claim_ids)),
                estimated_duration_ms=_estimate_segment_duration_ms(
                    narration, words_per_minute
                ),
            )
        )

    total_duration_ms = sum(segment.estimated_duration_ms for segment in segments)
    return ScriptDraft(
        title=title, segments=segments, estimated_duration_ms=total_duration_ms
    )


class LLM:
    """Thin async wrapper over an Anthropic client bound to one model."""

    def __init__(self, client: AsyncAnthropic, *, model: str) -> None:
        self._client = client
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    async def judge_claim(self, claim: str, passages: list[Passage]) -> Verdict:
        """Judge ``claim`` against exactly ``passages`` - no other source
        material is visible to the model. Raises :class:`LLMResponseError`
        if the model's tool call doesn't match the expected shape; never
        silently returns a made-up verdict.
        """
        known_ids = {passage.id for passage in passages}

        messages: list[MessageParam] = [
            {
                "role": "user",
                "content": f"Claim: {claim}\n\nPassages:\n{_format_passages(passages)}",
            }
        ]
        tool_choice: ToolChoiceToolParam = {"type": "tool", "name": _VERDICT_TOOL_NAME}

        response = await self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            system=_SYSTEM_PROMPT,
            messages=messages,
            tools=[_VERDICT_TOOL],
            tool_choice=tool_choice,
        )

        tool_use = next(
            (block for block in response.content if isinstance(block, ToolUseBlock)), None
        )
        if tool_use is None:
            raise LLMResponseError("The model did not return a verdict tool call.")

        raw = tool_use.input

        status = raw.get("status")
        if status not in _VALID_STATUSES:
            raise LLMResponseError(f"Unexpected verdict status: {status!r}")

        rationale = raw.get("rationale")
        if not isinstance(rationale, str):
            raise LLMResponseError("Verdict rationale must be a string.")

        raw_cited = raw.get("cited_passage_ids")
        if not isinstance(raw_cited, list) or not all(isinstance(p, str) for p in raw_cited):
            raise LLMResponseError("Verdict cited_passage_ids must be a list of strings.")

        # Never trust a cited id the model invented outside what it was
        # actually shown - filtered here, at the provider boundary, not
        # left to callers to remember to check.
        cited = [pid for pid in raw_cited if pid in known_ids]

        return Verdict(
            status=cast(VerdictStatus, status), rationale=rationale, cited_passage_ids=cited
        )

    async def compose_script(
        self,
        brief: ScriptBrief,
        claims: list[Claim],
        *,
        max_segments: int,
        max_claims: int,
        words_per_minute: int,
    ) -> tuple[ScriptDraft, TokenUsage]:
        """Draft a narration script for ``brief`` using only ``claims``.

        The model is forced to answer through the ``submit_script`` tool.
        Segment positions are assigned here from list order - never taken
        from the model - and each segment's duration is derived from its
        word count at ``words_per_minute`` (rule 5: integer milliseconds).
        Every claim id the model cites is checked against ``claims``; an
        unknown id, a segment count above ``max_segments``, or any other
        shape mismatch raises :class:`LLMResponseError` rather than
        returning a partial draft.

        ``max_segments`` / ``max_claims`` / ``words_per_minute`` come from
        configuration (``MAX_SCRIPT_SEGMENTS`` / ``MAX_CLAIMS_PER_GENERATION``
        / ``SCRIPT_WORDS_PER_MINUTE``). Passing an empty claim list, more
        than ``max_claims`` claims, or duplicate claim ids is a caller
        error and raises ``ValueError`` before any API call is made.
        """
        if not claims:
            raise ValueError("compose_script requires at least one claim.")
        if len(claims) > max_claims:
            raise ValueError(
                f"compose_script was given {len(claims)} claims; the limit is {max_claims}."
            )
        known_claim_ids = {claim.id for claim in claims}
        if len(known_claim_ids) != len(claims):
            raise ValueError("compose_script was given duplicate claim ids.")

        messages: list[MessageParam] = [
            {
                "role": "user",
                "content": (
                    f"{_format_brief(brief)}\n\n"
                    f"Verified claims:\n{_format_claims(claims)}"
                ),
            }
        ]
        tool_choice: ToolChoiceToolParam = {"type": "tool", "name": _SCRIPT_TOOL_NAME}

        response = await self._client.messages.create(
            model=self._model,
            max_tokens=4096,
            system=_SCRIPT_SYSTEM_PROMPT,
            messages=messages,
            tools=[_SCRIPT_TOOL],
            tool_choice=tool_choice,
        )

        tool_use = next(
            (block for block in response.content if isinstance(block, ToolUseBlock)), None
        )
        if tool_use is None:
            raise LLMResponseError("The model did not return a script tool call.")

        draft = _parse_script_draft(
            tool_use.input,
            known_claim_ids=known_claim_ids,
            max_segments=max_segments,
            words_per_minute=words_per_minute,
        )
        usage = TokenUsage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        return draft, usage


def get_llm_client(*, api_key: str, model: str) -> LLM:
    return LLM(AsyncAnthropic(api_key=api_key), model=model)

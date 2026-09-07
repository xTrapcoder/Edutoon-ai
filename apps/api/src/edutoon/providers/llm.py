"""Anthropic claim judgment - the only module allowed to import
``anthropic`` (rule 3).

Deliberately narrow: the only thing a caller can do is ask "does this claim
hold up against these specific passages" and get back a typed verdict - not
a general "send any prompt" method. Retrieval (similarity search) already
picked the candidates; this module's only job is judging entailment against
exactly those candidates, and reporting which ones it actually relied on.
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


def get_llm_client(*, api_key: str, model: str) -> LLM:
    return LLM(AsyncAnthropic(api_key=api_key), model=model)

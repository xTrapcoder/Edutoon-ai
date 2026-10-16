"""The local AI backend: an Ollama server answering the same two calls the cloud providers do, so
the project can run with no API key and no recurring cost (``AI_BACKEND=local``).

Nothing else in the application knows this exists. :class:`LocalMessagesClient` offers the one
method :class:`edutoon.providers.llm.LLM` calls on an Anthropic client (``messages.create``) and
:class:`LocalEmbeddingsClient` the one method :class:`edutoon.providers.embeddings.Embeddings`
calls on an OpenAI client (``embeddings.create``). Each returns what the SDK would have returned,
so every prompt, every response check (:func:`edutoon.providers.llm._tool_input`), every id filter
and every domain rule runs unchanged against a local model.

**Structured output instead of a forced tool call.** Ollama's Anthropic-compatible endpoint does
not support ``tool_choice``, and every language task here is a forced tool call. So the tool's own
``input_schema`` is sent as Ollama's ``format`` (grammar-constrained JSON) and the reply is wrapped
as a ``tool_use`` block. A small model cannot answer in prose or in the wrong shape when decoding
is constrained to the schema; the existing validation still runs on what it says.

**What is refused rather than trusted.** A reply cut off at ``num_predict`` is a ``max_tokens`` stop
(the caller's existing "cut off" error). A prompt that fills the context window is a
``model_context_window_exceeded`` stop, because Ollama silently drops the start of an over-long
prompt and the answer would be about something other than what was asked. An embedding input too
long for the model is an error (``truncate: false``), never silently shortened. An embedding wider
than the database column is an error; a narrower one is zero-padded, which changes no dot product
and no norm and so no cosine similarity.

Failures are :class:`~edutoon.providers.errors.LocalModelError` - a kind and a status, never the
server's text, which can echo the user's material.
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass
from typing import Any, Final, Literal

import httpx
import structlog
from anthropic.types import ContentBlock, Message, TextBlock, ToolUseBlock, Usage

from edutoon.domain.embedding_batches import EMBEDDING_DIMENSIONS
from edutoon.providers.errors import LocalFailure, LocalModelError

log = structlog.get_logger()

# Keep a model in memory between calls: a job makes many, and reloading a multi-gigabyte model for
# each would cost far more than the answer does.
_KEEP_ALIVE: Final = "30m"
_CONNECT_TIMEOUT_SECONDS: Final = 10.0

# A prompt that uses this much of the context window has almost certainly been cut at the front.
_CONTEXT_FULL_FRACTION: Final = 0.98

# Constrained decoding is most reliable at temperature 0 (Ollama's advice for structured output).
_TEMPERATURE: Final = 0

_StopReason = Literal["end_turn", "max_tokens", "tool_use", "model_context_window_exceeded"]


def _failure_for_status(status: int) -> LocalFailure:
    if status == 404:
        return "model_missing"
    if status in (408, 504):
        return "timeout"
    if status == 429 or status >= 500:
        return "unavailable"
    if status in (400, 413, 422):
        return "rejected"
    return "unexpected"


async def _post_json(
    http: httpx.AsyncClient, path: str, payload: dict[str, Any], *, what: str
) -> dict[str, Any]:
    """POST ``payload`` and return the JSON object that came back, or raise
    :class:`LocalModelError`. The response body is never logged or put in an error."""
    try:
        response = await http.post(path, json=payload)
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        raise LocalModelError("unreachable") from exc
    except httpx.TimeoutException as exc:
        raise LocalModelError("timeout") from exc
    except httpx.TransportError as exc:
        raise LocalModelError("unreachable") from exc

    if response.status_code >= 400:
        log.warning("ollama.http_error", call=what, status=response.status_code)
        raise LocalModelError(
            _failure_for_status(response.status_code), status_code=response.status_code
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise LocalModelError("unexpected") from exc
    if not isinstance(body, dict):
        raise LocalModelError("unexpected")
    return body


def _http_client(
    base_url: str, timeout_seconds: float, transport: httpx.AsyncBaseTransport | None
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url.rstrip("/"),
        timeout=httpx.Timeout(timeout_seconds, connect=_CONNECT_TIMEOUT_SECONDS),
        transport=transport,
    )


def _count(body: dict[str, Any], key: str) -> int:
    value = body.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


# --- language model -------------------------------------------------------------------------


def _text_of(content: object) -> str:
    """A message's content as plain text: a string, or a list of text blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block["text"])
            for block in content
            if isinstance(block, dict) and block.get("type") == "text" and "text" in block
        )
    raise ValueError("A local model call takes text messages only.")


def _without_string_length_limits(schema: Any) -> Any:
    """``schema`` with every ``maxLength`` removed, at any depth.

    Grammar-constrained decoding enforces ``maxLength`` by forcing the string to end, so a model
    that was writing a longer sentence is cut off mid-word ("... differ in their obj") and the
    clipped text then passes validation. The limit is still stated in the prompt and still checked
    by the domain's own validators after the reply, which reject an over-long string and trigger
    the bounded rewrite - so an over-long slot is corrected or fails visibly, never shown cut."""
    if isinstance(schema, dict):
        return {
            key: _without_string_length_limits(value)
            for key, value in schema.items()
            if key != "maxLength"
        }
    if isinstance(schema, list):
        return [_without_string_length_limits(value) for value in schema]
    return schema


def _forced_tool(tools: list[Any], tool_choice: Any) -> tuple[str, dict[str, Any]]:
    """The one tool whose input schema the answer must match: the one ``tool_choice`` names, or the
    only one offered."""
    wanted = tool_choice.get("name") if isinstance(tool_choice, dict) else None
    for tool in tools:
        if (wanted is None and len(tools) == 1) or tool.get("name") == wanted:
            return str(tool["name"]), _without_string_length_limits(dict(tool["input_schema"]))
    raise ValueError("A local model call needs the tool it is to answer through.")


class _LocalMessages:
    def __init__(self, http: httpx.AsyncClient, *, num_ctx: int, disable_thinking: bool) -> None:
        self._http = http
        self._num_ctx = num_ctx
        self._disable_thinking = disable_thinking

    async def create(
        self,
        *,
        model: str,
        max_tokens: int,
        system: str,
        messages: list[Any],
        tools: list[Any],
        tool_choice: Any = None,
        temperature: float | None = None,
        **_ignored: Any,
    ) -> Message:
        """Answer one forced-tool request. ``thinking`` and any other cloud-only field arrives in
        ``_ignored`` and is dropped: whether the model thinks is ``OLLAMA_DISABLE_THINKING``'s
        decision, not the request's."""
        tool_name, schema = _forced_tool(tools, tool_choice)
        instruction = (
            f"{system}\n\nReply with a single JSON object and nothing else. It is the input of the "
            f"`{tool_name}` function and must be valid against this JSON Schema:\n"
            f"{json.dumps(schema)}"
        )
        payload: dict[str, Any] = {
            "model": model,
            "stream": False,
            "format": schema,
            "keep_alive": _KEEP_ALIVE,
            "messages": [
                {"role": "system", "content": instruction},
                *({"role": m["role"], "content": _text_of(m["content"])} for m in messages),
            ],
            "options": {
                "num_ctx": self._num_ctx,
                "num_predict": max_tokens,
                "temperature": _TEMPERATURE if temperature is None else temperature,
            },
        }
        if self._disable_thinking:
            payload["think"] = False

        body = await _post_json(self._http, "/api/chat", payload, what="chat")
        return self._as_message(body, model=model, tool_name=tool_name, max_tokens=max_tokens)

    def _as_message(
        self, body: dict[str, Any], *, model: str, tool_name: str, max_tokens: int
    ) -> Message:
        prompt_tokens = _count(body, "prompt_eval_count")
        output_tokens = _count(body, "eval_count")
        raw = body.get("message")
        text = raw.get("content") if isinstance(raw, dict) else None
        text = text if isinstance(text, str) else ""

        stop: _StopReason
        content: list[ContentBlock]
        parsed: object = None
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None

        context_limit = math.floor(self._num_ctx * _CONTEXT_FULL_FRACTION)
        if prompt_tokens >= context_limit:
            stop, content = "model_context_window_exceeded", []
        elif body.get("done_reason") == "length":
            filled = prompt_tokens + output_tokens >= context_limit
            stop = "model_context_window_exceeded" if filled else "max_tokens"
            content = []
        elif not isinstance(parsed, dict):
            # Not a JSON object: no tool call, which the caller reports as a missing answer.
            stop, content = "end_turn", [TextBlock(type="text", text=text)]
        else:
            stop = "tool_use"
            content = [
                ToolUseBlock(
                    type="tool_use",
                    id=f"toolu_local_{uuid.uuid4().hex}",
                    name=tool_name,
                    input=parsed,
                )
            ]

        log.info(
            "ollama.chat",
            model=model,
            done_reason=body.get("done_reason"),
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
            num_ctx=self._num_ctx,
            max_tokens=max_tokens,
        )
        return Message(
            id=f"msg_local_{uuid.uuid4().hex}",
            type="message",
            role="assistant",
            model=model,
            content=content,
            stop_reason=stop,
            stop_sequence=None,
            usage=Usage(input_tokens=prompt_tokens, output_tokens=output_tokens),
        )


class LocalMessagesClient:
    """Stands where ``AsyncAnthropic`` stands: ``client.messages.create(...)`` returns a
    :class:`anthropic.types.Message`."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        num_ctx: int,
        disable_thinking: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.messages = _LocalMessages(
            _http_client(base_url, timeout_seconds, transport),
            num_ctx=num_ctx,
            disable_thinking=disable_thinking,
        )


# --- embeddings -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LocalEmbeddingItem:
    index: int
    embedding: list[float]


@dataclass(frozen=True, slots=True)
class LocalEmbeddingResponse:
    data: list[LocalEmbeddingItem]


def pad_to_width(vector: list[float], width: int = EMBEDDING_DIMENSIONS) -> list[float]:
    """``vector`` with zeros appended up to ``width``. Zeros change neither a dot product nor a
    norm, so cosine similarity between padded vectors equals that of the originals. A vector wider
    than ``width`` cannot be stored and raises :class:`LocalModelError`."""
    if len(vector) > width:
        raise LocalModelError("dimension")
    return vector + [0.0] * (width - len(vector))


class _LocalEmbeddings:
    def __init__(self, http: httpx.AsyncClient, *, num_ctx: int) -> None:
        self._http = http
        self._num_ctx = num_ctx

    async def create(self, *, input: list[str], model: str) -> LocalEmbeddingResponse:  # noqa: A002 - mirrors the SDK's argument name
        payload: dict[str, Any] = {
            "model": model,
            "input": input,
            # An input the model cannot hold is an error, never silently shortened.
            "truncate": False,
            "keep_alive": _KEEP_ALIVE,
            "options": {"num_ctx": self._num_ctx},
        }
        body = await _post_json(self._http, "/api/embed", payload, what="embed")
        vectors = body.get("embeddings")
        if not isinstance(vectors, list):
            raise LocalModelError("unexpected")

        items: list[LocalEmbeddingItem] = []
        for index, vector in enumerate(vectors):
            if not isinstance(vector, list) or not all(
                isinstance(x, int | float) and not isinstance(x, bool) for x in vector
            ):
                raise LocalModelError("unexpected")
            items.append(
                LocalEmbeddingItem(index=index, embedding=pad_to_width([float(x) for x in vector]))
            )
        return LocalEmbeddingResponse(data=items)


class LocalEmbeddingsClient:
    """Stands where ``AsyncOpenAI`` stands: ``client.embeddings.create(input=..., model=...)``
    returns an object with ``data[i].index`` and ``data[i].embedding`` - each embedding always
    exactly :data:`~edutoon.domain.embedding_batches.EMBEDDING_DIMENSIONS` wide."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        num_ctx: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.embeddings = _LocalEmbeddings(
            _http_client(base_url, timeout_seconds, transport), num_ctx=num_ctx
        )

"""``providers/embeddings.py`` - never a real call to OpenAI.

``AsyncOpenAI(api_key=...)`` makes no network call at construction time, so
these tests build a real client (satisfying the type checker and proving
``Embeddings`` is used exactly as it would be in production) and monkeypatch
only its ``embeddings.create`` method - the one thing that would otherwise
hit the network.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from openai import AsyncOpenAI
from openai.types import CreateEmbeddingResponse, Embedding
from openai.types.create_embedding_response import Usage

from edutoon.providers.embeddings import Embeddings, get_embeddings_client

MODEL = "text-embedding-3-small"


def _fake_response(vectors: list[list[float]]) -> CreateEmbeddingResponse:
    return CreateEmbeddingResponse(
        object="list",
        model=MODEL,
        usage=Usage(prompt_tokens=0, total_tokens=0),
        data=[
            Embedding(object="embedding", index=index, embedding=vector)
            for index, vector in enumerate(vectors)
        ],
    )


def _embeddings_with_fake_client(monkeypatch: Any, vectors: list[list[float]]) -> Embeddings:
    client = AsyncOpenAI(api_key="sk-test-not-a-real-key")
    monkeypatch.setattr(
        client.embeddings, "create", AsyncMock(return_value=_fake_response(vectors))
    )
    return Embeddings(client, model=MODEL)


async def test_embed_returns_vectors_in_input_order(monkeypatch):
    embeddings = _embeddings_with_fake_client(monkeypatch, [[1.0, 0.0], [0.0, 1.0]])

    result = await embeddings.embed(["first", "second"])

    assert result == [[1.0, 0.0], [0.0, 1.0]]


async def test_embed_reorders_by_response_index_not_list_position(monkeypatch):
    """The OpenAI API is documented to preserve order, but ``Embeddings``
    doesn't trust that implicitly - it re-sorts by each item's own
    ``index`` field, so a shuffled response still maps back correctly.
    """
    client = AsyncOpenAI(api_key="sk-test-not-a-real-key")
    shuffled_response = CreateEmbeddingResponse(
        object="list",
        model=MODEL,
        usage=Usage(prompt_tokens=0, total_tokens=0),
        data=[
            Embedding(object="embedding", index=1, embedding=[0.0, 1.0]),
            Embedding(object="embedding", index=0, embedding=[1.0, 0.0]),
        ],
    )
    monkeypatch.setattr(
        client.embeddings, "create", AsyncMock(return_value=shuffled_response)
    )
    embeddings = Embeddings(client, model=MODEL)

    result = await embeddings.embed(["first", "second"])

    assert result == [[1.0, 0.0], [0.0, 1.0]]


async def test_embed_with_empty_list_does_not_call_the_api(monkeypatch):
    client = AsyncOpenAI(api_key="sk-test-not-a-real-key")
    create = AsyncMock()
    monkeypatch.setattr(client.embeddings, "create", create)
    embeddings = Embeddings(client, model=MODEL)

    assert await embeddings.embed([]) == []
    create.assert_not_called()


def test_embeddings_model_property_reflects_construction():
    embeddings = get_embeddings_client(api_key="sk-test-not-a-real-key", model=MODEL)

    assert embeddings.model == MODEL

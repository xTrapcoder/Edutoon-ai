"""OpenAI embeddings - the only module allowed to import ``openai``
(rule 3).
"""

from __future__ import annotations

from openai import AsyncOpenAI


class Embeddings:
    """Thin async wrapper over an OpenAI client bound to one embedding
    model. The model travels with the wrapper (not passed per-call) so
    every embedding produced through one instance is comparable - mixing
    models within a single index would make vectors incompatible with each
    other despite sharing a column.
    """

    def __init__(self, client: AsyncOpenAI, *, model: str) -> None:
        self._client = client
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed ``texts`` in one batched API call, returned in the same
        order they were given - explicitly re-sorted by the response's own
        ``index`` field rather than trusted implicitly, since callers zip
        this against their input list to know which vector belongs to
        which chunk.
        """
        if not texts:
            return []
        response = await self._client.embeddings.create(input=texts, model=self._model)
        by_index = {item.index: item.embedding for item in response.data}
        return [by_index[i] for i in range(len(texts))]


def get_embeddings_client(*, api_key: str, model: str) -> Embeddings:
    return Embeddings(AsyncOpenAI(api_key=api_key), model=model)

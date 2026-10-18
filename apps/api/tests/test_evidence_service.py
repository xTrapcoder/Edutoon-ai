"""``services/evidence.py`` - Phase B: indexing orchestration.

Uses a real ``Embeddings`` wrapping a real ``AsyncOpenAI`` client with only
``embeddings.create`` monkeypatched (same pattern as
``test_embeddings_provider.py``) - never a real network call, but the
orchestration is exercised exactly as it runs in production.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from anthropic import AsyncAnthropic
from anthropic.types import Message, ToolUseBlock
from anthropic.types import Usage as AnthropicUsage
from openai import AsyncOpenAI
from openai.types import CreateEmbeddingResponse, Embedding
from openai.types.create_embedding_response import Usage

from edutoon.core.errors import ConflictError, NotFoundError
from edutoon.providers import errors as provider_errors
from edutoon.providers.embeddings import Embeddings
from edutoon.providers.llm import LLM, Verdict
from edutoon.repositories import claim_verifications as claim_verifications_repo
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import source_chunks as source_chunks_repo
from edutoon.repositories import uploaded_sources as uploaded_sources_repo
from edutoon.repositories import users as users_repo
from edutoon.repositories.source_chunks import NewSourceChunk
from edutoon.services import evidence as evidence_service
from edutoon.services import job_errors
from tests.leaky_errors import assert_clean, leaky_openai
from tests.storage_isolation import UPLOADS_BUCKET

# Stands in for an external provider (OpenAI / Anthropic): these tests prove how our code
# handles a response, never how a real provider behaves. See "Provider-mocked tests"
# in apps/api/README.md.
pytestmark = pytest.mark.provider_mocked

MODEL = "text-embedding-3-small"
LLM_MODEL = "claude-sonnet-5"


def _fake_embeddings(monkeypatch, vector_for: dict[str, list[float]] | None = None) -> Embeddings:
    """A real Embeddings/AsyncOpenAI pair whose ``create`` call returns a
    deterministic vector per input string (defaulting to an all-zero vector
    of the right dimensionality when ``vector_for`` doesn't cover it).
    """
    client = AsyncOpenAI(api_key="sk-test-not-a-real-key")

    async def _create(*, input, model, **_kwargs):  # noqa: ANN001, ARG001 - test double
        vectors = [(vector_for or {}).get(text, [0.0] * 1536) for text in input]
        return CreateEmbeddingResponse(
            object="list",
            model=model,
            usage=Usage(prompt_tokens=0, total_tokens=0),
            data=[
                Embedding(object="embedding", index=i, embedding=v) for i, v in enumerate(vectors)
            ],
        )

    monkeypatch.setattr(client.embeddings, "create", _create)
    return Embeddings(client, model=MODEL)


def _fake_llm(monkeypatch, tool_input: dict[str, Any]) -> LLM:
    """A real LLM/AsyncAnthropic pair whose ``messages.create`` call always
    returns the given verdict tool input - same monkeypatch-the-SDK-call
    pattern as ``_fake_embeddings`` above and ``test_llm_provider.py``.
    """
    client = AsyncAnthropic(api_key="sk-ant-test-not-a-real-key")
    message = Message(
        id="msg_1",
        content=[
            ToolUseBlock(type="tool_use", id="toolu_1", name="submit_verdict", input=tool_input)
        ],
        model=LLM_MODEL,
        role="assistant",
        type="message",
        usage=AnthropicUsage(input_tokens=10, output_tokens=10),
    )
    monkeypatch.setattr(client.messages, "create", AsyncMock(return_value=message))
    return LLM(client, model=LLM_MODEL)


async def _make_source(db_session, num_chunks: int = 2):
    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    project = await projects_repo.create(
        db_session, owner_id=owner.id, title="P", source_type="pdf"
    )
    source = await uploaded_sources_repo.create(
        db_session,
        project_id=project.id,
        original_filename="notes.pdf",
        storage_bucket=UPLOADS_BUCKET,
        storage_key=f"{project.id}/notes.pdf",
        byte_size=1024,
        checksum_sha256=uuid4().hex + uuid4().hex[:32],
    )
    chunks = await source_chunks_repo.create_many(
        db_session,
        [
            NewSourceChunk(
                source_id=source.id, project_id=project.id, chunk_index=i, content=f"chunk {i}"
            )
            for i in range(num_chunks)
        ],
    )
    return project, source, chunks


# --- enqueue_indexing_job ------------------------------------------------------------


async def test_enqueue_indexing_job_creates_a_build_evidence_job(db_session):
    project, source, _ = await _make_source(db_session)

    job = await evidence_service.enqueue_indexing_job(
        db_session, project_id=project.id, source_id=source.id
    )

    assert job.kind == "build_evidence"
    assert job.status == "queued"
    assert job.payload == {"source_id": str(source.id)}


async def test_enqueuing_twice_for_the_same_source_raises_conflict_error(db_session):
    project, source, _ = await _make_source(db_session)
    await evidence_service.enqueue_indexing_job(
        db_session, project_id=project.id, source_id=source.id
    )

    with pytest.raises(ConflictError):
        await evidence_service.enqueue_indexing_job(
            db_session, project_id=project.id, source_id=source.id
        )


# --- index_source ----------------------------------------------------------------


async def test_index_source_embeds_every_chunk(db_session, monkeypatch):
    _, source, chunks = await _make_source(db_session, num_chunks=3)
    embeddings = _fake_embeddings(monkeypatch, {f"chunk {i}": [float(i)] * 1536 for i in range(3)})

    count = await evidence_service.index_source(db_session, embeddings, source_id=source.id)

    assert count == 3
    remaining = await source_chunks_repo.list_missing_embedding_by_source(db_session, source.id)
    assert remaining == []
    for chunk in chunks:
        updated = await source_chunks_repo.get_by_id(db_session, chunk.id)
        assert updated is not None
        assert updated.embedding_model == MODEL


async def test_index_source_only_touches_chunks_missing_an_embedding(db_session, monkeypatch):
    """Re-running indexing (e.g. after a partial failure) must not re-embed
    - and therefore not re-spend an API call on - chunks that already have
    a vector.
    """
    _, source, chunks = await _make_source(db_session, num_chunks=2)
    await source_chunks_repo.update_embedding(
        db_session, chunk_id=chunks[0].id, embedding=[9.0] * 1536, embedding_model="pre-existing"
    )
    embeddings = _fake_embeddings(monkeypatch)
    create_spy = AsyncMock(wraps=embeddings._client.embeddings.create)  # noqa: SLF001
    monkeypatch.setattr(embeddings._client.embeddings, "create", create_spy)  # noqa: SLF001

    count = await evidence_service.index_source(db_session, embeddings, source_id=source.id)

    assert count == 1
    create_spy.assert_called_once()
    (call_kwargs,) = [call.kwargs for call in create_spy.call_args_list]
    assert call_kwargs["input"] == ["chunk 1"]
    untouched = await source_chunks_repo.get_by_id(db_session, chunks[0].id)
    assert untouched is not None
    assert untouched.embedding_model == "pre-existing"  # left exactly as it was


async def test_index_source_with_nothing_to_embed_is_a_noop(db_session, monkeypatch):
    _, source, _ = await _make_source(db_session, num_chunks=0)
    embeddings = _fake_embeddings(monkeypatch)

    assert await evidence_service.index_source(db_session, embeddings, source_id=source.id) == 0


# --- run_build_evidence_job --------------------------------------------------------


async def test_run_build_evidence_job_succeeds_and_records_the_chunk_count(db_session, monkeypatch):
    project, source, _ = await _make_source(db_session, num_chunks=2)
    embeddings = _fake_embeddings(monkeypatch)
    job = await evidence_service.enqueue_indexing_job(
        db_session, project_id=project.id, source_id=source.id
    )

    completed = await evidence_service.run_build_evidence_job(db_session, embeddings, job_id=job.id)

    assert completed.status == "succeeded"
    assert completed.result == {"chunks_embedded": 2}
    assert completed.attempts == 1
    assert completed.started_at is not None
    assert completed.finished_at is not None


async def test_run_build_evidence_job_marks_failed_on_embedding_error(db_session):
    project, source, _ = await _make_source(db_session, num_chunks=1)
    job = await evidence_service.enqueue_indexing_job(
        db_session, project_id=project.id, source_id=source.id
    )
    client = AsyncOpenAI(api_key="sk-test-not-a-real-key")

    async def _boom(**_kwargs):
        raise RuntimeError("openai is unreachable")

    client.embeddings.create = _boom  # type: ignore[method-assign]
    embeddings = Embeddings(client, model=MODEL)

    completed = await evidence_service.run_build_evidence_job(db_session, embeddings, job_id=job.id)

    assert completed.status == "failed"
    # A bare exception's text is never shown to the owner - only a fixed sentence.
    assert completed.error == job_errors.GENERIC_ERROR
    assert "unreachable" not in completed.error
    remaining = await source_chunks_repo.list_missing_embedding_by_source(db_session, source.id)
    assert len(remaining) == 1  # nothing was embedded


async def test_run_build_evidence_job_raises_not_found_for_a_missing_job(db_session, monkeypatch):
    embeddings = _fake_embeddings(monkeypatch)

    with pytest.raises(NotFoundError):
        await evidence_service.run_build_evidence_job(db_session, embeddings, job_id=uuid4())


async def test_run_build_evidence_job_increments_attempts_on_retry(db_session, monkeypatch):
    project, source, _ = await _make_source(db_session, num_chunks=1)
    embeddings = _fake_embeddings(monkeypatch)
    job = await evidence_service.enqueue_indexing_job(
        db_session, project_id=project.id, source_id=source.id
    )

    first = await evidence_service.run_build_evidence_job(db_session, embeddings, job_id=job.id)
    # A second run is safe (idempotent indexing underneath) even though a
    # real system wouldn't normally re-run an already-succeeded job.
    second = await evidence_service.run_build_evidence_job(db_session, embeddings, job_id=job.id)

    assert first.attempts == 1
    assert second.attempts == 2
    assert second.result == {"chunks_embedded": 0}  # nothing left to embed the second time


# --- verify_claim --------------------------------------------------------------------

QUERY_VECTOR = [1.0] + [0.0] * 1535


async def _make_indexed_chunk(db_session, *, project=None, content: str = "The sky is blue."):
    """A project with one already-embedded chunk (embedding set directly,
    bypassing the embeddings provider, since only the retrieval/persistence
    side of verify_claim is under test here - Phase B already covers
    indexing itself).
    """
    if project is None:
        owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
        project = await projects_repo.create(
            db_session, owner_id=owner.id, title="P", source_type="pdf"
        )
    source = await uploaded_sources_repo.create(
        db_session,
        project_id=project.id,
        original_filename="notes.pdf",
        storage_bucket=UPLOADS_BUCKET,
        storage_key=f"{project.id}/{uuid4()}.pdf",
        byte_size=1024,
        checksum_sha256=uuid4().hex + uuid4().hex[:32],
    )
    [chunk] = await source_chunks_repo.create_many(
        db_session,
        [
            NewSourceChunk(
                source_id=source.id, project_id=project.id, chunk_index=0, content=content
            )
        ],
    )
    chunk = await source_chunks_repo.update_embedding(
        db_session, chunk_id=chunk.id, embedding=QUERY_VECTOR, embedding_model=MODEL
    )
    assert chunk is not None
    return project, chunk


async def test_verify_claim_persists_supported_verdict_with_its_citation(db_session, monkeypatch):
    project, chunk = await _make_indexed_chunk(db_session, content="The sky is blue.")
    claim_text = "The sky is blue."
    embeddings = _fake_embeddings(monkeypatch, {claim_text: QUERY_VECTOR})
    llm = _fake_llm(
        monkeypatch,
        {
            "status": "supported",
            "rationale": "Directly stated in the source.",
            "cited_passage_ids": [str(chunk.id)],
        },
    )

    verification = await evidence_service.verify_claim(
        db_session, embeddings, llm, project_id=project.id, claim_text=claim_text
    )

    assert verification.status == "supported"
    assert verification.embedding_model == MODEL
    assert verification.llm_model == LLM_MODEL
    citations = await claim_verifications_repo.list_citations(db_session, verification.id)
    assert [c.source_chunk_id for c in citations] == [chunk.id]


async def test_verify_claim_downgrades_a_supported_claim_that_names_a_place_the_source_lacks(
    db_session, monkeypatch
):
    """Regression: the judge approved "Factory towns in India ..." on the strength of the three
    English cities, and the claim was stored as supported. A name no passage contains keeps it
    from being treated as established; its citation and the judge's words are kept."""
    project, chunk = await _make_indexed_chunk(
        db_session,
        content=(
            "Factory towns grew very fast. Manchester, Birmingham and Leeds filled with workers."
        ),
    )
    claim_text = "Factory towns in India grew rapidly, with workers filling Manchester and Leeds."
    embeddings = _fake_embeddings(monkeypatch, {claim_text: QUERY_VECTOR})
    llm = _fake_llm(
        monkeypatch,
        {
            "status": "supported",
            "rationale": "Passage [p1] says the towns grew very fast.",
            "cited_passage_ids": [str(chunk.id)],
        },
    )

    verification = await evidence_service.verify_claim(
        db_session, embeddings, llm, project_id=project.id, claim_text=claim_text
    )

    assert verification.status == "partially_supported"
    assert verification.rationale is not None
    assert "Passage [p1] says the towns grew very fast." in verification.rationale
    assert "'India'" in verification.rationale
    citations = await claim_verifications_repo.list_citations(db_session, verification.id)
    assert [c.source_chunk_id for c in citations] == [chunk.id]


async def test_verify_claim_downgrades_a_supported_claim_whose_citation_does_not_hold_its_wording(
    db_session, monkeypatch
):
    """Regression (a real run): the judge approved "Humans alter ecosystems through land clearing,
    which involves removing vegetation to make way for agriculture or development" citing a page
    about energy flow. A supported claim's wording has to be in what it cites."""
    project, energy = await _make_indexed_chunk(
        db_session,
        content=(
            "Energy does not recycle through an ecosystem. It flows from the sun to producers "
            "and is finally lost as heat."
        ),
    )
    claim_text = (
        "Humans alter ecosystems through land clearing, which involves removing vegetation to "
        "make way for agriculture or development."
    )
    embeddings = _fake_embeddings(monkeypatch, {claim_text: QUERY_VECTOR})
    llm = _fake_llm(
        monkeypatch,
        {
            "status": "supported",
            "rationale": "Passage [p1] discusses ecosystems.",
            "cited_passage_ids": [str(energy.id)],
        },
    )

    verification = await evidence_service.verify_claim(
        db_session, embeddings, llm, project_id=project.id, claim_text=claim_text
    )

    assert verification.status == "partially_supported"
    assert "Passage [p1] discusses ecosystems." in verification.rationale
    assert "of the claim's wording is in the passages it cites" in verification.rationale


async def test_verify_claim_points_a_true_claim_at_the_passage_that_states_it(
    db_session, monkeypatch
):
    """Regression (a real run): "Animals that eat plants are called herbivores" was true, and the
    source says so, but the judge cited a page about energy flow."""
    project, energy = await _make_indexed_chunk(
        db_session,
        content=(
            "Energy does not recycle through an ecosystem. It flows from the sun to producers "
            "and is finally lost as heat."
        ),
    )
    _project, consumers = await _make_indexed_chunk(
        db_session,
        project=project,
        content=(
            "Animals cannot make their own food. Herbivores are animals that eat plants and "
            "carnivores are animals that eat other animals."
        ),
    )
    claim_text = "Animals that eat plants are called herbivores."
    embeddings = _fake_embeddings(monkeypatch, {claim_text: QUERY_VECTOR})
    llm = _fake_llm(
        monkeypatch,
        {
            "status": "supported",
            "rationale": "Directly stated.",
            "cited_passage_ids": [str(energy.id)],
        },
    )

    verification = await evidence_service.verify_claim(
        db_session, embeddings, llm, project_id=project.id, claim_text=claim_text
    )

    assert verification.status == "supported"
    citations = await claim_verifications_repo.list_citations(db_session, verification.id)
    assert [c.source_chunk_id for c in citations] == [consumers.id]
    assert "citation was corrected" in verification.rationale


async def test_verify_claim_stores_one_citation_when_the_judge_names_a_passage_twice(
    db_session, monkeypatch
):
    """Regression (a real run): a judge that cited the same passage twice raised "This source chunk
    is already cited by this verification" and failed every claim of that topic."""
    project, chunk = await _make_indexed_chunk(db_session, content="The sky is blue.")
    claim_text = "The sky is blue."
    embeddings = _fake_embeddings(monkeypatch, {claim_text: QUERY_VECTOR})
    llm = _fake_llm(
        monkeypatch,
        {
            "status": "supported",
            "rationale": "Directly stated.",
            "cited_passage_ids": [str(chunk.id), str(chunk.id)],
        },
    )

    verification = await evidence_service.verify_claim(
        db_session, embeddings, llm, project_id=project.id, claim_text=claim_text
    )

    assert verification.status == "supported"
    citations = await claim_verifications_repo.list_citations(db_session, verification.id)
    assert [c.source_chunk_id for c in citations] == [chunk.id]


async def test_verify_claim_downgrades_supported_with_no_citations_to_unsupported(
    db_session, monkeypatch
):
    """The core guarantee: the LLM saying "supported" is not enough on its
    own - without at least one real citation, the persisted status must
    never be "supported".
    """
    project, _chunk = await _make_indexed_chunk(db_session)
    claim_text = "An unrelated claim."
    embeddings = _fake_embeddings(monkeypatch, {claim_text: QUERY_VECTOR})
    llm = _fake_llm(
        monkeypatch,
        {
            "status": "supported",
            "rationale": "Confident but points at nothing.",
            "cited_passage_ids": [],
        },
    )

    verification = await evidence_service.verify_claim(
        db_session, embeddings, llm, project_id=project.id, claim_text=claim_text
    )

    assert verification.status == "unsupported"
    assert await claim_verifications_repo.list_citations(db_session, verification.id) == []


async def test_verify_claim_persists_unsupported_verdict_with_no_citations(db_session, monkeypatch):
    project, _chunk = await _make_indexed_chunk(db_session)
    claim_text = "Something not in the source."
    embeddings = _fake_embeddings(monkeypatch, {claim_text: QUERY_VECTOR})
    llm = _fake_llm(
        monkeypatch,
        {"status": "unsupported", "rationale": "Not mentioned.", "cited_passage_ids": []},
    )

    verification = await evidence_service.verify_claim(
        db_session, embeddings, llm, project_id=project.id, claim_text=claim_text
    )

    assert verification.status == "unsupported"
    assert verification.rationale == "Not mentioned."


async def test_verify_claim_drops_a_cited_id_outside_the_candidate_set(db_session, monkeypatch):
    """Defense in depth at the persistence boundary: even if judge_claim's
    own filtering somehow let a bogus id through, verify_claim must still
    only ever persist citations for chunks that were real candidates.
    """
    project, _chunk = await _make_indexed_chunk(db_session)
    claim_text = "Claim"
    embeddings = _fake_embeddings(monkeypatch, {claim_text: QUERY_VECTOR})
    llm = LLM(AsyncAnthropic(api_key="sk-ant-test-not-a-real-key"), model=LLM_MODEL)
    monkeypatch.setattr(
        llm,
        "judge_claim",
        AsyncMock(
            return_value=Verdict(
                status="supported", rationale="...", cited_passage_ids=["not-a-real-chunk-id"]
            )
        ),
    )

    verification = await evidence_service.verify_claim(
        db_session, embeddings, llm, project_id=project.id, claim_text=claim_text
    )

    assert verification.status == "unsupported"  # downgraded: the only "citation" was bogus
    assert await claim_verifications_repo.list_citations(db_session, verification.id) == []


async def test_verify_claim_with_no_indexed_material_returns_unsupported_without_calling_the_llm(
    db_session, monkeypatch
):
    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    project = await projects_repo.create(
        db_session, owner_id=owner.id, title="Empty", source_type="pdf"
    )
    embeddings = _fake_embeddings(monkeypatch, {"Claim": QUERY_VECTOR})
    llm = _fake_llm(
        monkeypatch, {"status": "supported", "rationale": "...", "cited_passage_ids": []}
    )
    judge_spy = AsyncMock(wraps=llm.judge_claim)
    monkeypatch.setattr(llm, "judge_claim", judge_spy)

    verification = await evidence_service.verify_claim(
        db_session, embeddings, llm, project_id=project.id, claim_text="Claim"
    )

    assert verification.status == "unsupported"
    assert "no indexed source material" in verification.rationale.lower()
    judge_spy.assert_not_called()


async def test_verify_claim_never_retrieves_another_projects_chunks(db_session, monkeypatch):
    _project_a, _chunk_a = await _make_indexed_chunk(db_session, content="Project A content.")
    project_b, chunk_b = await _make_indexed_chunk(db_session, content="Project B content.")
    claim_text = "Claim"
    embeddings = _fake_embeddings(monkeypatch, {claim_text: QUERY_VECTOR})
    llm = _fake_llm(
        monkeypatch,
        {"status": "supported", "rationale": "...", "cited_passage_ids": [str(chunk_b.id)]},
    )

    verification = await evidence_service.verify_claim(
        db_session, embeddings, llm, project_id=project_b.id, claim_text=claim_text
    )

    citations = await claim_verifications_repo.list_citations(db_session, verification.id)
    assert [c.source_chunk_id for c in citations] == [chunk_b.id]


async def test_a_provider_failure_tells_the_owner_a_fixed_sentence_and_nothing_else(db_session):
    project, source, _ = await _make_source(db_session, num_chunks=1)
    job = await evidence_service.enqueue_indexing_job(
        db_session, project_id=project.id, source_id=source.id
    )
    client = AsyncOpenAI(api_key="sk-test-not-a-real-key")

    async def _rate_limited(**_kwargs):
        raise leaky_openai(429)

    client.embeddings.create = _rate_limited  # type: ignore[method-assign]

    completed = await evidence_service.run_build_evidence_job(
        db_session, Embeddings(client, model=MODEL), job_id=job.id
    )

    assert completed.status == "failed" and completed.error == provider_errors.RATE_LIMITED
    assert_clean(completed.error)

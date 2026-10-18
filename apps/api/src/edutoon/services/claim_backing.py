"""Which supported claims are backed by what they cite - read side, for script generation.

``services.evidence.verify_claim`` now refuses to store a claim as supported when the passages it
cites do not hold its wording (``domain.claim_grounding``). Claims verified before that rule exist,
and the judge's citation is the only thing standing between a script and a statement the source does
not make, so a script also applies the rule to the claims it is about to rest on:

- a claim whose cited passages cover enough of its wording is kept;
- one that does not, but is stated by another passage of the project's own material, is kept (the
  judge cited the wrong page; the claim is true to the source);
- the rest are left out of the script. They stay in the ledger untouched (it is append-only); they
  are simply not something a lesson may say.

A general-knowledge claim has no source and is never filtered here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.domain.claim_grounding import (
    CITATION_COVERAGE_FLOOR,
    best_covering_passage,
    claim_coverage,
)
from edutoon.repositories import claim_verifications as claim_verifications_repo
from edutoon.repositories import source_chunks as source_chunks_repo
from edutoon.repositories.claim_verifications import ClaimVerificationRecord


@dataclass(frozen=True, slots=True)
class BackedClaims:
    claims: list[ClaimVerificationRecord]
    left_out: list[ClaimVerificationRecord]


async def keep_backed_claims(
    session: AsyncSession, *, project_id: UUID, claims: Sequence[ClaimVerificationRecord]
) -> BackedClaims:
    """``claims`` (order kept) split into those a script may rest on and those it may not."""
    checkable = [claim for claim in claims if claim.status == "supported"]
    if not checkable:
        return BackedClaims(claims=list(claims), left_out=[])

    citations = await claim_verifications_repo.list_citations_for(
        session, [claim.id for claim in checkable]
    )
    chunks = await source_chunks_repo.list_by_project_in_order(session, project_id)
    text_by_chunk = {chunk.id: chunk.content for chunk in chunks}
    all_texts = list(text_by_chunk.values())

    left_out: list[ClaimVerificationRecord] = []
    for claim in checkable:
        cited_texts = [
            text_by_chunk[citation.source_chunk_id]
            for citation in citations.get(claim.id, [])
            if citation.source_chunk_id in text_by_chunk
        ]
        if claim_coverage(claim.claim_text, cited_texts) >= CITATION_COVERAGE_FLOOR:
            continue
        if best_covering_passage(claim.claim_text, all_texts) is not None:
            continue
        left_out.append(claim)
    dropped = {claim.id for claim in left_out}
    return BackedClaims(
        claims=[claim for claim in claims if claim.id not in dropped], left_out=left_out
    )

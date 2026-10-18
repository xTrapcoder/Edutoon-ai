"""``services.claim_backing``: a script rests only on claims whose citations hold their wording.

The cases are the three seen in fresh ecosystems runs: a claim cited to the passage that states
it, a true claim cited to the wrong page (another passage states it), and a claim dressed in
words the source never uses, cited to a page that does not contain it.
"""

from __future__ import annotations

from uuid import uuid4

from edutoon.repositories import claim_verifications as claim_verifications_repo
from edutoon.repositories import projects as projects_repo
from edutoon.repositories import source_chunks as source_chunks_repo
from edutoon.repositories import uploaded_sources as uploaded_sources_repo
from edutoon.repositories import users as users_repo
from edutoon.repositories.claim_verifications import NewClaimCitation
from edutoon.repositories.source_chunks import NewSourceChunk
from edutoon.services import claim_backing
from tests.storage_isolation import UPLOADS_BUCKET

ENERGY = (
    "Energy does not recycle through an ecosystem. It flows in one direction, from the sun to "
    "producers, then to consumers, and is finally lost as heat."
)
CONSUMERS = (
    "Animals cannot make their own food, so they are called consumers. Herbivores are animals "
    "that eat plants. Carnivores are animals that eat other animals."
)


async def _project_with_chunks(db_session):
    owner = await users_repo.create(db_session, email=f"{uuid4()}@example.com")
    project = await projects_repo.create(
        db_session, owner_id=owner.id, title="Ecosystems", source_type="pdf"
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
    energy, consumers = await source_chunks_repo.create_many(
        db_session,
        [
            NewSourceChunk(
                source_id=source.id, project_id=project.id, chunk_index=0, content=ENERGY
            ),
            NewSourceChunk(
                source_id=source.id, project_id=project.id, chunk_index=1, content=CONSUMERS
            ),
        ],
    )
    return project, energy, consumers


async def _claim(db_session, project, text: str, cited_chunk, *, status: str = "supported"):
    return await claim_verifications_repo.create(
        db_session,
        project_id=project.id,
        claim_text=text,
        status=status,
        embedding_model="m",
        llm_model="m",
        citations=[NewClaimCitation(source_chunk_id=cited_chunk.id, similarity=0.5)],
    )


async def test_claims_are_kept_repaired_or_left_out_by_what_their_citations_hold(db_session):
    project, energy, consumers = await _project_with_chunks(db_session)
    stated = await _claim(
        db_session, project, "Energy flows in one direction, from the sun to producers.", energy
    )
    mis_cited = await _claim(
        db_session, project, "Animals that eat plants are called herbivores.", energy
    )
    embellished = await _claim(
        db_session,
        project,
        "Fungi and bacteria are tiny living things that we cannot see with our eyes.",
        consumers,
    )

    backed = await claim_backing.keep_backed_claims(
        db_session, project_id=project.id, claims=[stated, mis_cited, embellished]
    )

    assert [claim.id for claim in backed.claims] == [stated.id, mis_cited.id]
    assert [claim.id for claim in backed.left_out] == [embellished.id]


async def test_a_claim_with_no_source_is_never_filtered(db_session):
    project, energy, _consumers = await _project_with_chunks(db_session)
    general = await claim_verifications_repo.create(
        db_session,
        project_id=project.id,
        claim_text="Newton's third law says every action has an equal and opposite reaction.",
        status="general_knowledge",
        embedding_model="m",
        llm_model="m",
    )

    backed = await claim_backing.keep_backed_claims(
        db_session, project_id=project.id, claims=[general]
    )

    assert backed.claims == [general] and backed.left_out == []


async def test_no_claims_means_nothing_to_check(db_session):
    project, _energy, _consumers = await _project_with_chunks(db_session)

    backed = await claim_backing.keep_backed_claims(db_session, project_id=project.id, claims=[])

    assert backed.claims == [] and backed.left_out == []

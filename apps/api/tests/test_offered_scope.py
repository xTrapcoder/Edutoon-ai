"""The product offers exactly five modes.

PDF, pasted text, PDF + keywords, PDF + instructions and a scanned PDF (read by local OCR, which is
part of reading a PDF and needs no project setting). A topic with no document was removed on
2026-10-18 and has no switch; a PowerPoint deck project is dormant (off by default); free-text
instructions are on. Here the *defaults* are held, each refusal says what to do instead, and the
offered modes still create normally.
"""

from __future__ import annotations

import pytest

from edutoon.core.config import get_settings
from edutoon.core.errors import ValidationError
from edutoon.domain.topic import (
    INSTRUCTIONS_UNSUPPORTED,
    KEYWORD_ONLY_UNSUPPORTED,
    PPTX_UNSUPPORTED,
)
from edutoon.repositories import users as users_repo
from edutoon.services import projects as projects_service

SWITCHES = ("PPTX_PROJECTS_ENABLED", "INSTRUCTIONS_ENABLED")


@pytest.fixture
def default_scope(monkeypatch):
    """The settings as shipped: every scope switch at its default."""
    for name in SWITCHES:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _owner(db_session):
    from uuid import uuid4

    return await users_repo.create(db_session, email=f"{uuid4()}@example.com")


def test_the_defaults_are_decks_off_instructions_on_and_no_topic_only_switch(default_scope) -> None:
    settings = get_settings()

    assert settings.PPTX_PROJECTS_ENABLED is False
    assert settings.INSTRUCTIONS_ENABLED is True
    assert not hasattr(settings, "KEYWORD_ONLY_PROJECTS_ENABLED")


@pytest.mark.parametrize(
    ("source_type", "message"),
    [("topic", KEYWORD_ONLY_UNSUPPORTED), ("pptx", PPTX_UNSUPPORTED)],
)
async def test_a_project_outside_the_offered_modes_is_refused_with_a_reason(
    db_session, default_scope, source_type, message
) -> None:
    owner = await _owner(db_session)

    with pytest.raises(ValidationError) as refused:
        await projects_service.create_project(
            db_session, owner_id=owner.id, title="Out of scope", source_type=source_type
        )

    assert str(refused.value) == message


@pytest.mark.parametrize("source_type", ["pdf", "text"])
async def test_the_pdf_and_pasted_text_modes_still_create_normally(
    db_session, default_scope, source_type
) -> None:
    owner = await _owner(db_session)

    project = await projects_service.create_project(
        db_session, owner_id=owner.id, title="In scope", source_type=source_type
    )

    assert project.source_type == source_type and project.instructions is None


@pytest.mark.parametrize(
    ("source_type", "instructions"),
    [("pdf", "Make it suitable for a beginner."), ("text", "Focus on chapter 2.")],
)
async def test_instructions_are_accepted_on_a_new_project(
    db_session, default_scope, source_type, instructions
) -> None:
    owner = await _owner(db_session)

    project = await projects_service.create_project(
        db_session,
        owner_id=owner.id,
        title="With instructions",
        source_type=source_type,
        instructions=instructions,
    )

    assert project.instructions == instructions


async def test_instructions_can_be_added_to_an_existing_project(db_session, default_scope) -> None:
    owner = await _owner(db_session)
    project = await projects_service.create_project(
        db_session, owner_id=owner.id, title="In scope", source_type="pdf"
    )

    updated = await projects_service.update_project(
        db_session, project_id=project.id, owner_id=owner.id, instructions="Be brief."
    )

    assert updated.instructions == "Be brief."


async def test_with_the_switch_off_instructions_are_refused_with_a_reason(
    db_session, monkeypatch
) -> None:
    monkeypatch.setenv("INSTRUCTIONS_ENABLED", "false")
    get_settings.cache_clear()
    try:
        owner = await _owner(db_session)
        with pytest.raises(ValidationError) as refused:
            await projects_service.create_project(
                db_session,
                owner_id=owner.id,
                title="No instructions here",
                source_type="pdf",
                instructions="Be brief.",
            )
    finally:
        get_settings.cache_clear()

    assert str(refused.value) == INSTRUCTIONS_UNSUPPORTED


def test_the_error_messages_say_what_to_do_instead() -> None:
    assert "Upload a PDF" in KEYWORD_ONLY_UNSUPPORTED
    assert "paste your text" in KEYWORD_ONLY_UNSUPPORTED
    assert "paste your text" in PPTX_UNSUPPORTED and "keywords" in PPTX_UNSUPPORTED
    assert "add keywords to a PDF" in INSTRUCTIONS_UNSUPPORTED

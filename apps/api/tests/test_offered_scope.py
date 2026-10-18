"""The product offers exactly three modes: PDF, pasted text, PDF + keywords (a focused video).

Topic-only projects, PowerPoint deck projects and free-text instructions were taken out of the
offered scope on 2026-10-18. Their code is kept (and tested elsewhere with the switches on, see
``conftest.py``); here the *defaults* are held off, each refusal says what to do instead, and the
three offered modes still create normally.
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

SWITCHES = ("KEYWORD_ONLY_PROJECTS_ENABLED", "PPTX_PROJECTS_ENABLED", "INSTRUCTIONS_ENABLED")


@pytest.fixture
def default_scope(monkeypatch):
    """The settings as shipped: every out-of-scope switch at its default."""
    for name in SWITCHES:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _owner(db_session):
    from uuid import uuid4

    return await users_repo.create(db_session, email=f"{uuid4()}@example.com")


def test_every_out_of_scope_mode_is_off_by_default(default_scope) -> None:
    settings = get_settings()

    assert settings.KEYWORD_ONLY_PROJECTS_ENABLED is False
    assert settings.PPTX_PROJECTS_ENABLED is False
    assert settings.INSTRUCTIONS_ENABLED is False


@pytest.mark.parametrize(
    ("source_type", "instructions", "message"),
    [
        ("topic", None, KEYWORD_ONLY_UNSUPPORTED),
        ("pptx", None, PPTX_UNSUPPORTED),
        ("pdf", "Make it suitable for a beginner.", INSTRUCTIONS_UNSUPPORTED),
        ("text", "Focus on chapter 2.", INSTRUCTIONS_UNSUPPORTED),
    ],
)
async def test_a_project_outside_the_offered_modes_is_refused_with_a_reason(
    db_session, default_scope, source_type, instructions, message
) -> None:
    owner = await _owner(db_session)

    with pytest.raises(ValidationError) as refused:
        await projects_service.create_project(
            db_session,
            owner_id=owner.id,
            title="Out of scope",
            source_type=source_type,
            instructions=instructions,
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


async def test_instructions_cannot_be_added_to_an_existing_project_either(
    db_session, default_scope
) -> None:
    owner = await _owner(db_session)
    project = await projects_service.create_project(
        db_session, owner_id=owner.id, title="In scope", source_type="pdf"
    )

    with pytest.raises(ValidationError) as refused:
        await projects_service.update_project(
            db_session,
            project_id=project.id,
            owner_id=owner.id,
            instructions="Be brief.",
        )

    assert str(refused.value) == INSTRUCTIONS_UNSUPPORTED


async def test_other_settings_can_still_be_edited_without_the_instructions_switch(
    db_session, default_scope
) -> None:
    owner = await _owner(db_session)
    project = await projects_service.create_project(
        db_session, owner_id=owner.id, title="Before", source_type="pdf"
    )

    updated = await projects_service.update_project(
        db_session, project_id=project.id, owner_id=owner.id, title="After"
    )

    assert updated.title == "After"


def test_the_error_messages_say_what_to_do_instead() -> None:
    assert "Upload a PDF" in KEYWORD_ONLY_UNSUPPORTED
    assert "paste your text" in PPTX_UNSUPPORTED and "keywords" in PPTX_UNSUPPORTED
    assert "add keywords to a PDF" in INSTRUCTIONS_UNSUPPORTED

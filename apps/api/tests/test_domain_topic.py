"""``domain/topic.py`` - vocabulary, limits and value objects. Pure: no
fixtures, no DB.

Also home to the guard that keeps the *whole* domain package pure (rule 1):
no module in it may import a repository, provider, service, the database layer
or configuration.
"""

from __future__ import annotations

import ast
import dataclasses
import pkgutil
from pathlib import Path
from uuid import uuid4

import pytest

import edutoon.domain as domain_package
from edutoon.domain.topic import (
    MODE_KEYWORDS,
    MODE_PDF,
    MODE_PDF_KEYWORDS,
    ORIGIN_SOURCE,
    ORIGIN_SUGGESTED,
    ORIGIN_USER,
    STATUS_ACCEPTED,
    STATUS_PROPOSED,
    STATUS_REJECTED,
    TOPIC_ORIGINS,
    TOPIC_STATUSES,
    PassageSelection,
    SourcePassage,
    TopicDraft,
    TopicLimits,
    TopicNodeDraft,
    TopicSeed,
    ValidatedTopicNode,
    ValidatedTopicTree,
)


def _limits(**overrides: int) -> TopicLimits:
    values = {
        "max_topics": 30,
        "max_depth": 3,
        "max_title_chars": 80,
        "max_summary_chars": 240,
        "max_keywords_per_topic": 8,
        "max_keyword_chars": 40,
        "max_evidence_per_topic": 6,
    }
    values.update(overrides)
    return TopicLimits(**values)


# --- vocabulary --------------------------------------------------------------------


def test_origins_are_exactly_source_user_and_suggested() -> None:
    assert sorted(TOPIC_ORIGINS) == ["source", "suggested", "user"]
    assert sorted([ORIGIN_SOURCE, ORIGIN_USER, ORIGIN_SUGGESTED]) == sorted(TOPIC_ORIGINS)


def test_statuses_are_exactly_proposed_accepted_and_rejected() -> None:
    assert sorted(TOPIC_STATUSES) == ["accepted", "proposed", "rejected"]
    assert sorted([STATUS_PROPOSED, STATUS_ACCEPTED, STATUS_REJECTED]) == sorted(TOPIC_STATUSES)


def test_input_modes_are_the_three_approved_ones() -> None:
    assert {MODE_PDF, MODE_KEYWORDS, MODE_PDF_KEYWORDS} == {"pdf", "keywords", "pdf_keywords"}


# --- limits ------------------------------------------------------------------------


def test_limits_are_accepted_when_every_bound_is_positive() -> None:
    limits = _limits()

    assert limits.max_topics == 30
    assert limits.max_depth == 3


@pytest.mark.parametrize(
    "field",
    [
        "max_topics",
        "max_depth",
        "max_title_chars",
        "max_summary_chars",
        "max_keywords_per_topic",
        "max_keyword_chars",
        "max_evidence_per_topic",
    ],
)
@pytest.mark.parametrize("bad", [0, -1])
def test_limits_reject_a_non_positive_bound(field: str, bad: int) -> None:
    with pytest.raises(ValueError, match=field):
        _limits(**{field: bad})


def test_limits_have_no_defaults_so_the_caller_must_supply_every_bound() -> None:
    """The domain never reads configuration: every number comes in explicitly,
    so it has exactly one home (the settings the service reads).
    """
    with pytest.raises(TypeError):
        TopicLimits()  # type: ignore[call-arg]
    assert all(
        field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING
        for field in dataclasses.fields(TopicLimits)
    )


# --- value objects -------------------------------------------------------------------


def test_value_objects_are_immutable() -> None:
    passage = SourcePassage(chunk_id="c1", text="Text")
    seed = TopicSeed(ref="s1", title="Photosynthesis")
    node = TopicNodeDraft(ref="n1", title="Light")

    frozen = [(passage, "text"), (seed, "title"), (node, "title"), (_limits(), "max_topics")]
    for obj, attribute in frozen:
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(obj, attribute, "changed")


def test_a_draft_node_cannot_carry_depth_position_or_origin() -> None:
    """The model may say which topic a node belongs under, never where it
    sits or what kind it is - those are decided by the rules.
    """
    names = {field.name for field in dataclasses.fields(TopicNodeDraft)}

    assert names == {
        "ref",
        "title",
        "parent_ref",
        "summary",
        "keywords",
        "evidence_chunk_ids",
        "seed_ref",
    }
    for forbidden in ("depth", "position", "origin", "status", "id"):
        assert forbidden not in names


def test_draft_defaults_are_empty() -> None:
    node = TopicNodeDraft(ref="n", title="T")

    assert node.parent_ref is None
    assert node.summary is None
    assert node.keywords == ()
    assert node.evidence_chunk_ids == ()
    assert node.seed_ref is None
    assert TopicDraft().nodes == ()


def test_source_passage_defaults_to_a_full_chunk_with_no_pages() -> None:
    passage = SourcePassage(chunk_id="c1", text="Text")

    assert passage.excerpt is False
    assert passage.page_from is None
    assert passage.page_to is None


def test_seed_position_defaults_to_zero() -> None:
    assert TopicSeed(ref="s", title="T").position == 0


def test_validated_result_types_carry_what_the_service_needs() -> None:
    node = ValidatedTopicNode(
        index=0,
        parent_index=None,
        title="T",
        summary=None,
        keywords=(),
        origin="user",
        depth=0,
        position=0,
        evidence_chunk_ids=(uuid4(),),
    )
    tree = ValidatedTopicTree(mode="pdf", nodes=(node,))
    selection = PassageSelection((), 0, 0, 0, 0, False)

    assert node.seed_ref is None
    assert tree.dropped_uncited == 0
    assert selection.truncated is False


# --- the domain package stays pure (rule 1) ---------------------------------------------

_STDLIB_ALLOWED = {
    "__future__",
    "collections",
    "dataclasses",
    "hashlib",
    "re",
    "typing",
    "unicodedata",
    "uuid",
}


def _domain_modules() -> list[str]:
    return [module.name for module in pkgutil.iter_modules(domain_package.__path__)]


def _imported_modules(source: str) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
    return imported


@pytest.mark.parametrize("module_name", _domain_modules())
def test_a_domain_module_imports_only_the_standard_library_and_other_domain_modules(
    module_name: str,
) -> None:
    path = Path(domain_package.__path__[0]) / f"{module_name}.py"

    imported = _imported_modules(path.read_text(encoding="utf-8"))

    offenders = {
        name
        for name in imported
        if name.split(".")[0] not in _STDLIB_ALLOWED and not name.startswith("edutoon.domain")
    }
    assert offenders == set(), f"{module_name} imports non-domain modules: {sorted(offenders)}"


@pytest.mark.parametrize("module_name", ["topic", "topic_tree"])
@pytest.mark.parametrize(
    "forbidden",
    [
        "edutoon.providers",
        "edutoon.repositories",
        "edutoon.services",
        "edutoon.core.config",
        "edutoon.db",
        "edutoon.api",
        "os",
        "sqlalchemy",
        "anthropic",
        "openai",
    ],
)
def test_the_topic_modules_never_import_infrastructure_or_configuration(
    module_name: str, forbidden: str
) -> None:
    path = Path(domain_package.__path__[0]) / f"{module_name}.py"

    imported = _imported_modules(path.read_text(encoding="utf-8"))

    assert not any(name == forbidden or name.startswith(forbidden + ".") for name in imported)

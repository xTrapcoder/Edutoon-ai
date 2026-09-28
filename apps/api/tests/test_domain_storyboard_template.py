"""``domain.storyboard_template`` - the six fixed scene templates, their slot
contracts and orientation-aware layout (Phase 9).

No I/O, no provider, no database. Mutation targets: character limits,
orientation lookup, and the vertical-vs-horizontal layout swap (spec section
25, items 5 and 7).
"""

from __future__ import annotations

import pytest

from edutoon.domain.storyboard_template import (
    TEMPLATES,
    TEXT_ONLY_TEMPLATES,
    StoryboardContentError,
    layout_for,
    validate_illustration,
    validate_scene_content,
)

VALID_CONTENT: dict[str, dict[str, object]] = {
    "title_card": {"title": "Photosynthesis", "subtitle": "How plants make food"},
    "bullet_points": {"title": "Recap", "bullets": ["Sunlight", "Water", "Carbon dioxide"]},
    "labelled_diagram": {
        "illustration": {"description": "A leaf cross-section", "subject_labels": ["leaf"]},
        "labels": [{"text": "Stoma", "position": "bottom"}, {"text": "Chloroplast", "position": "centre"}],
    },
    "split_text_illustration": {
        "text": "Chlorophyll absorbs sunlight to power the reaction.",
        "illustration": {"description": "A close-up of a green leaf"},
    },
    "full_bleed_illustration": {
        "illustration": {"description": "A sunlit forest canopy"},
        "caption": "Sunlight drives the whole process",
    },
    "formula_or_code": {
        "content": "6CO2 + 6H2O -> C6H12O6 + 6O2",
        "label": "Overall reaction",
        "kind": "formula",
    },
}


# --- all six templates accept their documented valid content -------------------------


def test_templates_is_exactly_the_six_documented_values() -> None:
    assert TEMPLATES == {
        "title_card",
        "bullet_points",
        "labelled_diagram",
        "split_text_illustration",
        "full_bleed_illustration",
        "formula_or_code",
    }


@pytest.mark.parametrize("template", sorted(TEMPLATES))
def test_the_documented_valid_content_is_accepted(template: str) -> None:
    validate_scene_content(template, VALID_CONTENT[template])


def test_there_is_no_section_recap_template() -> None:
    assert "section_recap" not in TEMPLATES


# --- text-only templates remain renderable without an illustration -------------------


def test_text_only_templates_are_exactly_the_documented_three() -> None:
    assert TEXT_ONLY_TEMPLATES == {"title_card", "bullet_points", "formula_or_code"}


@pytest.mark.parametrize("template", sorted(TEXT_ONLY_TEMPLATES))
def test_text_only_templates_accept_content_with_no_illustration_key(template: str) -> None:
    content = {k: v for k, v in VALID_CONTENT[template].items() if k != "illustration"}
    validate_scene_content(template, content)
    assert "illustration" not in content


# --- title_card ------------------------------------------------------------------


def test_title_card_requires_a_title() -> None:
    with pytest.raises(StoryboardContentError, match="title"):
        validate_scene_content("title_card", {"subtitle": "x"})


def test_title_card_subtitle_is_optional() -> None:
    validate_scene_content("title_card", {"title": "Just a title"})


def test_title_card_title_over_the_character_limit_is_rejected() -> None:
    with pytest.raises(StoryboardContentError, match="60"):
        validate_scene_content("title_card", {"title": "x" * 61})


def test_title_card_title_at_exactly_the_limit_is_accepted() -> None:
    validate_scene_content("title_card", {"title": "x" * 60})


def test_title_card_subtitle_over_the_character_limit_is_rejected() -> None:
    with pytest.raises(StoryboardContentError, match="100"):
        validate_scene_content("title_card", {"title": "t", "subtitle": "x" * 101})


# --- bullet_points -----------------------------------------------------------------


def test_bullet_points_requires_bullets() -> None:
    with pytest.raises(StoryboardContentError, match="bullets"):
        validate_scene_content("bullet_points", {"title": "T"})


def test_bullet_points_title_is_optional() -> None:
    validate_scene_content("bullet_points", {"bullets": ["one", "two"]})


@pytest.mark.parametrize("count", [0, 1])
def test_bullet_points_rejects_too_few_bullets(count: int) -> None:
    with pytest.raises(StoryboardContentError, match="between 2 and 5"):
        validate_scene_content("bullet_points", {"bullets": ["x"] * count})


def test_bullet_points_rejects_too_many_bullets() -> None:
    with pytest.raises(StoryboardContentError, match="between 2 and 5"):
        validate_scene_content("bullet_points", {"bullets": ["x"] * 6})


@pytest.mark.parametrize("count", [2, 5])
def test_bullet_points_accepts_the_boundary_counts(count: int) -> None:
    validate_scene_content("bullet_points", {"bullets": ["x"] * count})


def test_bullet_points_rejects_an_over_length_bullet() -> None:
    with pytest.raises(StoryboardContentError, match="80"):
        validate_scene_content("bullet_points", {"bullets": ["x" * 81, "y"]})


def test_bullet_points_rejects_an_empty_bullet() -> None:
    with pytest.raises(StoryboardContentError, match="non-empty"):
        validate_scene_content("bullet_points", {"bullets": ["", "y"]})


# --- labelled_diagram ---------------------------------------------------------------


def test_labelled_diagram_requires_an_illustration() -> None:
    with pytest.raises(StoryboardContentError, match="illustration"):
        validate_scene_content(
            "labelled_diagram", {"labels": [{"text": "A", "position": "top"}]}
        )


def test_labelled_diagram_requires_at_least_one_label() -> None:
    with pytest.raises(StoryboardContentError, match="between 1 and 6"):
        validate_scene_content(
            "labelled_diagram",
            {"illustration": {"description": "d"}, "labels": []},
        )


def test_labelled_diagram_rejects_more_than_six_labels() -> None:
    with pytest.raises(StoryboardContentError, match="between 1 and 6"):
        validate_scene_content(
            "labelled_diagram",
            {
                "illustration": {"description": "d"},
                "labels": [{"text": str(i), "position": "top"} for i in range(7)],
            },
        )


def test_labelled_diagram_rejects_an_unknown_label_position() -> None:
    with pytest.raises(StoryboardContentError, match="position"):
        validate_scene_content(
            "labelled_diagram",
            {
                "illustration": {"description": "d"},
                "labels": [{"text": "A", "position": "diagonally"}],
            },
        )


@pytest.mark.parametrize("position", ["top", "left", "right", "bottom", "centre"])
def test_labelled_diagram_accepts_every_documented_position(position: str) -> None:
    validate_scene_content(
        "labelled_diagram",
        {"illustration": {"description": "d"}, "labels": [{"text": "A", "position": position}]},
    )


def test_labelled_diagram_rejects_an_over_length_label() -> None:
    with pytest.raises(StoryboardContentError, match="30"):
        validate_scene_content(
            "labelled_diagram",
            {
                "illustration": {"description": "d"},
                "labels": [{"text": "x" * 31, "position": "top"}],
            },
        )


# --- split_text_illustration ---------------------------------------------------------


def test_split_text_illustration_requires_text() -> None:
    with pytest.raises(StoryboardContentError, match="text"):
        validate_scene_content(
            "split_text_illustration", {"illustration": {"description": "d"}}
        )


def test_split_text_illustration_requires_an_illustration() -> None:
    with pytest.raises(StoryboardContentError, match="illustration"):
        validate_scene_content("split_text_illustration", {"text": "hello"})


def test_split_text_illustration_rejects_over_length_text() -> None:
    with pytest.raises(StoryboardContentError, match="160"):
        validate_scene_content(
            "split_text_illustration",
            {"text": "x" * 161, "illustration": {"description": "d"}},
        )


# --- full_bleed_illustration ----------------------------------------------------------


def test_full_bleed_illustration_requires_an_illustration() -> None:
    with pytest.raises(StoryboardContentError, match="illustration"):
        validate_scene_content("full_bleed_illustration", {"caption": "hi"})


def test_full_bleed_illustration_caption_is_optional() -> None:
    validate_scene_content("full_bleed_illustration", {"illustration": {"description": "d"}})


def test_full_bleed_illustration_rejects_an_over_length_caption() -> None:
    with pytest.raises(StoryboardContentError, match="80"):
        validate_scene_content(
            "full_bleed_illustration",
            {"illustration": {"description": "d"}, "caption": "x" * 81},
        )


# --- formula_or_code -----------------------------------------------------------------


def test_formula_or_code_requires_content() -> None:
    with pytest.raises(StoryboardContentError, match="content"):
        validate_scene_content("formula_or_code", {"kind": "formula"})


def test_formula_or_code_requires_a_valid_kind() -> None:
    with pytest.raises(StoryboardContentError, match="kind"):
        validate_scene_content("formula_or_code", {"content": "E=mc^2", "kind": "poem"})


@pytest.mark.parametrize("kind", ["formula", "code"])
def test_formula_or_code_accepts_both_kinds(kind: str) -> None:
    validate_scene_content("formula_or_code", {"content": "x", "kind": kind})


def test_formula_or_code_label_is_optional() -> None:
    validate_scene_content("formula_or_code", {"content": "x", "kind": "code"})


def test_formula_or_code_rejects_over_length_content() -> None:
    with pytest.raises(StoryboardContentError, match="200"):
        validate_scene_content("formula_or_code", {"content": "x" * 201, "kind": "code"})


def test_formula_or_code_rejects_over_length_label() -> None:
    with pytest.raises(StoryboardContentError, match="40"):
        validate_scene_content(
            "formula_or_code", {"content": "x", "kind": "code", "label": "x" * 41}
        )


# --- illustration spec ---------------------------------------------------------------


def test_illustration_requires_a_description() -> None:
    with pytest.raises(StoryboardContentError, match="description"):
        validate_illustration({"subject_labels": ["a"]})


def test_illustration_subject_labels_are_optional() -> None:
    validate_illustration({"description": "d"})


def test_illustration_rejects_more_than_three_subject_labels() -> None:
    with pytest.raises(StoryboardContentError, match="3 items"):
        validate_illustration({"description": "d", "subject_labels": ["a", "b", "c", "d"]})


def test_illustration_rejects_an_over_length_subject_label() -> None:
    with pytest.raises(StoryboardContentError, match="30"):
        validate_illustration({"description": "d", "subject_labels": ["x" * 31]})


def test_illustration_never_accepts_an_image_url() -> None:
    """No image is generated in Phase 9 (spec section 9) - a url slipped in by
    a caller is simply not part of the contract and is ignored, never trusted
    or rendered as if the image existed.
    """
    validate_illustration({"description": "d", "url": "https://example.com/x.png"})


# --- unknown template / invalid structure ---------------------------------------------


def test_unknown_template_is_rejected() -> None:
    with pytest.raises(StoryboardContentError, match="Unknown template"):
        validate_scene_content("section_recap", {"title": "x"})


def test_non_object_content_is_rejected() -> None:
    with pytest.raises(StoryboardContentError, match="object"):
        validate_scene_content("title_card", "just a string")


def test_non_object_illustration_is_rejected() -> None:
    with pytest.raises(StoryboardContentError, match="illustration"):
        validate_scene_content("full_bleed_illustration", {"illustration": "a forest"})


# --- orientation-aware layout ----------------------------------------------------------


@pytest.mark.parametrize("template", sorted(TEMPLATES))
def test_every_template_has_both_orientations(template: str) -> None:
    layout_for(template, "horizontal")
    layout_for(template, "vertical")


@pytest.mark.parametrize("template", sorted(TEMPLATES))
def test_vertical_layout_differs_structurally_from_horizontal(template: str) -> None:
    horizontal = layout_for(template, "horizontal")
    vertical = layout_for(template, "vertical")
    assert horizontal.arrangement != vertical.arrangement
    assert horizontal.description != vertical.description


@pytest.mark.parametrize("template", sorted(TEMPLATES))
def test_layout_arrangement_names_its_own_orientation_not_the_other(template: str) -> None:
    """A mutation that swapped a template's horizontal and vertical
    ``LayoutSpec`` entries is caught here: each layout's own ``orientation``
    field must agree with the orientation it was looked up under.
    """
    for orientation in ("horizontal", "vertical"):
        layout = layout_for(template, orientation)
        assert layout.orientation == orientation
        assert layout.template == template


def test_layout_for_unknown_template_raises() -> None:
    with pytest.raises(StoryboardContentError, match="Unknown template"):
        layout_for("section_recap", "horizontal")


def test_layout_for_unknown_orientation_raises() -> None:
    with pytest.raises(StoryboardContentError, match="Unknown orientation"):
        layout_for("title_card", "diagonal")


def test_every_arrangement_tag_is_globally_unique() -> None:
    """No two (template, orientation) pairs share an arrangement tag - a
    stronger guarantee than "differs from its own other orientation", so a
    mutation that copied one template's layout onto another is also caught.
    """
    tags = [layout_for(t, o).arrangement for t in sorted(TEMPLATES) for o in ("horizontal", "vertical")]
    assert len(tags) == len(set(tags))

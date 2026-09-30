"""``resolve_provenance``: what happens to a scene that fails the provenance check.

Provenance is never weakened - unsupported text does not reach a stored scene - but where it fails
is not all alike. Text in a scene's *own* slots is what a viewer reads as the lesson: that is
still a hard failure. Text in its *illustration* is where false positives live (a description
says "two arrows"; a label names the Sun), so it falls back: the visual is dropped, or the scene
becomes a text-only slide made from the narration's own words. Pure functions, no fixtures.
"""

from __future__ import annotations

import pytest

from edutoon.domain.storyboard_provenance import (
    FALLBACK_MARKER,
    FabricatedContentError,
    check_provenance,
    resolve_provenance,
    safe_text_only_content,
)
from edutoon.domain.storyboard_template import (
    BULLET_MAX_CHARS,
    MAX_BULLETS,
    MIN_BULLETS,
    validate_scene_content,
)
from edutoon.domain.visual import with_visual_dropped

NARRATION = (
    "Water evaporates from the ocean and rises into the sky. "
    "There it cools and forms clouds. Rain then falls back to the ground."
)


def flow(*labels: str) -> dict:
    return {
        "type": "process_flow",
        "steps": [{"label": label} for label in labels],
    }


def diagram(description: str = "A water cycle", visual: object | None = None, **extra) -> dict:
    illustration: dict = {"description": description}
    if visual is not None:
        illustration["visual"] = visual
    return {"illustration": illustration, **extra}


# --- nothing to resolve ----------------------------------------------------------------------


def test_a_scene_that_passes_is_returned_as_it_is() -> None:
    content = diagram(visual=flow("Water evaporates", "Clouds form"))
    result = resolve_provenance("labelled_diagram", content, NARRATION)

    assert result.fallback is None
    assert result.template == "labelled_diagram" and result.content == content


# --- the scene's own text is still a hard failure --------------------------------------------


@pytest.mark.parametrize(
    ("template", "content"),
    [
        ("title_card", {"title": "Rain in 1998"}),
        ("bullet_points", {"bullets": ["Water rises", "It fell 40 cm in March"]}),
        ("labelled_diagram", diagram(labels=[{"text": "Rain in 1998", "position": "left"}])),
        ("split_text_illustration", diagram(text="In 1998 the rain fell.")),
        ("full_bleed_illustration", diagram(caption="A 1998 storm")),
        ("formula_or_code", {"content": "rain = 73 mm", "kind": "formula"}),
    ],
)
def test_unsupported_text_in_a_scenes_own_slots_still_fails(template: str, content: dict) -> None:
    with pytest.raises(FabricatedContentError):
        resolve_provenance(template, content, NARRATION)


def test_a_violation_in_both_the_scenes_text_and_its_illustration_fails() -> None:
    content = diagram(
        "A storm in 1998", visual=flow("Rain in 1999", "Water"), caption="Rain in 1998"
    )
    with pytest.raises(FabricatedContentError) as caught:
        resolve_provenance("full_bleed_illustration", content, NARRATION)
    paths = {v.slot_path for v in caught.value.violations}
    assert "caption" in paths and any(p.startswith("illustration") for p in paths)


# --- the first fallback: drop the visual -----------------------------------------------------


def test_unsupported_text_only_in_the_visual_drops_the_visual_and_keeps_everything_else() -> None:
    content = diagram(
        "A water cycle",
        visual=flow("Water evaporates", "Rain over 1998"),
        labels=[{"text": "Ocean", "position": "left"}],
    )

    result = resolve_provenance("labelled_diagram", content, NARRATION)

    assert result.fallback == "visual_dropped" and result.template == "labelled_diagram"
    assert "visual" not in result.content["illustration"]
    assert result.content["illustration"]["visual_dropped"] is True
    assert result.content["illustration"]["description"] == "A water cycle"  # kept
    assert result.content["labels"] == content["labels"]  # kept
    assert "1998" not in str(result.content)
    assert check_provenance(result.content, NARRATION) == []
    validate_scene_content("labelled_diagram", result.content)  # still a valid scene


def test_an_invented_number_in_a_visual_is_dropped_with_it() -> None:
    chart = {"type": "value_chart", "values": [73, 42]}
    result = resolve_provenance("labelled_diagram", diagram(visual=chart, labels=None), NARRATION)

    assert result.fallback == "visual_dropped"
    assert "73" not in str(result.content)


def test_the_original_content_is_never_mutated() -> None:
    content = diagram(visual=flow("Water", "Rain over 1998"))
    before = repr(content)
    resolve_provenance("labelled_diagram", content, NARRATION)
    assert repr(content) == before


# --- the second fallback: a text-only scene --------------------------------------------------


def test_unsupported_text_in_the_illustrations_description_becomes_a_text_only_scene() -> None:
    content = diagram("A storm over Delhi in 1998", visual=flow("Water evaporates", "Rain falls"))

    result = resolve_provenance("full_bleed_illustration", content, NARRATION)

    assert result.fallback == "text_only" and result.template == "bullet_points"
    assert result.content[FALLBACK_MARKER] is True
    assert "illustration" not in result.content
    assert check_provenance(result.content, NARRATION) == []
    validate_scene_content("bullet_points", result.content)


def test_unsupported_subject_labels_do_the_same() -> None:
    content = {"illustration": {"description": "A water cycle", "subject_labels": ["Delhi 1998"]}}
    result = resolve_provenance("split_text_illustration", {**content, "text": "Water."}, NARRATION)
    assert result.fallback == "text_only"


def test_dropping_the_visual_is_tried_before_replacing_the_scene() -> None:
    """The scene's description is fine, only the diagram is wrong: it keeps its description."""
    result = resolve_provenance(
        "full_bleed_illustration",
        diagram("A water cycle", visual=flow("Water", "In 1998")),
        NARRATION,
    )
    assert result.fallback == "visual_dropped" and result.template == "full_bleed_illustration"


# --- the text-only scene ---------------------------------------------------------------------


def test_the_text_only_scene_is_the_narrations_own_first_sentences() -> None:
    content = safe_text_only_content(NARRATION)

    assert content is not None
    assert content["bullets"] == [
        "Water evaporates from the ocean and rises into the sky",
        "There it cools and forms clouds",
        "Rain then falls back to the ground",
    ]
    assert content[FALLBACK_MARKER] is True


def test_every_bullet_fits_and_the_count_is_within_the_templates_bounds() -> None:
    long = (
        "This " + "very " * 60 + "long sentence goes on. And here is another one that also runs on."
    )
    content = safe_text_only_content(long)

    assert content is not None
    bullets = content["bullets"]
    assert MIN_BULLETS <= len(bullets) <= MAX_BULLETS
    assert all(0 < len(bullet) <= BULLET_MAX_CHARS for bullet in bullets)
    assert not any(bullet.endswith(" ") for bullet in bullets)


def test_a_long_sentence_is_cut_at_a_word_not_in_the_middle_of_one() -> None:
    sentence = " ".join(["photosynthesis"] * 12)  # 179 characters
    content = safe_text_only_content(sentence + ". Short one.")
    assert content is not None
    assert all(word == "photosynthesis" for word in content["bullets"][0].split())


def test_a_single_sentence_narration_is_split_into_clauses() -> None:
    content = safe_text_only_content(
        "Water evaporates from the ocean, then it forms clouds, and rain falls."
    )
    assert content is not None and len(content["bullets"]) >= MIN_BULLETS


def test_a_single_clause_narration_is_split_in_half() -> None:
    content = safe_text_only_content("Water evaporates and rises into the sky")
    assert content is not None and len(content["bullets"]) == 2


@pytest.mark.parametrize("narration", ["", "   ", "Water."])
def test_a_narration_too_short_to_make_two_bullets_gives_no_scene(narration: str) -> None:
    assert safe_text_only_content(narration) is None


def test_when_no_clean_fallback_exists_the_failure_is_reported_not_hidden() -> None:
    with pytest.raises(FabricatedContentError):
        resolve_provenance("full_bleed_illustration", diagram("A storm in 1998"), "Water.")


def test_the_text_only_scene_always_passes_the_check_it_is_a_fallback_for() -> None:
    for narration in (
        NARRATION,
        "In 1998, 40 cm of rain fell in March. It was recorded in Delhi.",
        "The Sun heats the ocean, and Earth's oceans evaporate.",
    ):
        content = safe_text_only_content(narration)
        assert content is not None and check_provenance(content, narration) == []


# --- with_visual_dropped ---------------------------------------------------------------------


def test_with_visual_dropped_removes_the_visual_and_sets_our_mark() -> None:
    content = diagram(visual=flow("a", "b"), labels=[])
    dropped = with_visual_dropped(content)

    assert dropped is not None
    assert "visual" not in dropped["illustration"] and dropped["illustration"]["visual_dropped"]
    assert "visual" in content["illustration"]  # the input is untouched


def test_with_visual_dropped_has_nothing_to_do_for_a_scene_without_a_visual() -> None:
    assert with_visual_dropped(diagram()) is None
    assert with_visual_dropped({"title": "x"}) is None

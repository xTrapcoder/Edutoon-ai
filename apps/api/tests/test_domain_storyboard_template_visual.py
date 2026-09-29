"""How a procedural visual sits inside the six Phase 9 templates (Phase 11).

Additive: every Phase 9 / 10 scene is still valid unchanged. A visual may go on the three
illustrated templates only; ``labelled_diagram`` no longer needs ``labels`` when it has one; and
``visual_dropped`` is the service's own mark on a scene whose model-written visual was invalid.
"""

from __future__ import annotations

import pytest

from edutoon.domain.storyboard_template import (
    StoryboardContentError,
    validate_illustration,
    validate_scene_content,
)
from tests.visual_corpus import cycle, flow, nodes

ILLUSTRATED = ["labelled_diagram", "split_text_illustration", "full_bleed_illustration"]
LABELS = [{"text": "Stoma", "position": "bottom"}]


def _scene(template: str, illustration: dict, **rest) -> dict:
    content: dict = {"illustration": illustration, **rest}
    if template == "split_text_illustration":
        content["text"] = "Water moves through the plant."
    return content


ILLUSTRATION = {"description": "How water moves"}


# --- a visual on each illustrated template -----------------------------------------------------


@pytest.mark.parametrize("template", ILLUSTRATED)
def test_each_illustrated_template_accepts_a_valid_visual(template):
    validate_scene_content(
        template, _scene(template, {**ILLUSTRATION, "visual": flow(3)}, labels=LABELS)
    )


@pytest.mark.parametrize("template", ILLUSTRATED)
def test_phase_9_content_with_no_visual_is_still_valid(template):
    validate_scene_content(template, _scene(template, ILLUSTRATION, labels=LABELS))


@pytest.mark.parametrize("template", ILLUSTRATED)
def test_an_invalid_visual_makes_the_scene_invalid_when_validated_as_stored(template):
    with pytest.raises(StoryboardContentError, match="visual"):
        validate_scene_content(
            template,
            _scene(template, {**ILLUSTRATION, "visual": {"type": "cycle", "nodes": nodes(2)}}),
        )


def test_validate_illustration_says_whether_it_carries_a_valid_visual():
    assert validate_illustration({**ILLUSTRATION, "visual": flow(3)}) is True
    assert validate_illustration(ILLUSTRATION) is False


def test_a_visual_too_big_for_a_split_scene_is_invalid_there_only():
    big = {**ILLUSTRATION, "visual": cycle(6)}
    with pytest.raises(StoryboardContentError, match="too large"):
        validate_scene_content("split_text_illustration", _scene("split_text_illustration", big))
    validate_scene_content("full_bleed_illustration", _scene("full_bleed_illustration", big))
    validate_scene_content("labelled_diagram", _scene("labelled_diagram", big, labels=LABELS))


# --- labelled_diagram: honestly "labelled parts", labels optional with a visual ----------------


def test_a_labelled_diagram_with_a_visual_needs_no_labels():
    validate_scene_content(
        "labelled_diagram", _scene("labelled_diagram", {**ILLUSTRATION, "visual": flow(3)})
    )


def test_a_labelled_diagram_with_neither_visual_nor_labels_is_still_invalid():
    with pytest.raises(StoryboardContentError, match="labels"):
        validate_scene_content("labelled_diagram", _scene("labelled_diagram", ILLUSTRATION))


def test_labels_given_alongside_a_visual_are_still_validated():
    bad = [{"text": "x" * 31, "position": "top"}]
    with pytest.raises(StoryboardContentError, match="at most 30"):
        validate_scene_content(
            "labelled_diagram",
            _scene("labelled_diagram", {**ILLUSTRATION, "visual": flow(3)}, labels=bad),
        )


def test_a_labelled_diagram_whose_visual_was_dropped_may_have_no_labels():
    content = _scene("labelled_diagram", {**ILLUSTRATION, "visual_dropped": True})
    validate_scene_content("labelled_diagram", content)


def test_a_labelled_diagram_with_null_labels_and_a_visual_is_valid():
    content = _scene("labelled_diagram", {**ILLUSTRATION, "visual": flow(3)}, labels=None)
    validate_scene_content("labelled_diagram", content)


# --- visual_dropped ----------------------------------------------------------------------------


@pytest.mark.parametrize("template", ILLUSTRATED)
def test_visual_dropped_true_without_a_visual_is_valid(template):
    validate_scene_content(
        template, _scene(template, {**ILLUSTRATION, "visual_dropped": True}, labels=LABELS)
    )


@pytest.mark.parametrize("bad", [False, "true", 1, None, [], {}])
def test_visual_dropped_may_only_be_true(bad):
    with pytest.raises(StoryboardContentError, match="visual_dropped"):
        validate_illustration({**ILLUSTRATION, "visual_dropped": bad})


def test_a_scene_cannot_have_both_a_visual_and_a_drop_mark():
    with pytest.raises(StoryboardContentError, match="both"):
        validate_illustration({**ILLUSTRATION, "visual": flow(3), "visual_dropped": True})


# --- the text-only templates are unchanged -----------------------------------------------------


@pytest.mark.parametrize(
    ("template", "content"),
    [
        ("title_card", {"title": "T"}),
        ("bullet_points", {"bullets": ["a", "b"]}),
        ("formula_or_code", {"content": "x", "kind": "formula"}),
    ],
)
def test_text_only_templates_neither_need_nor_read_an_illustration(template, content):
    validate_scene_content(template, content)

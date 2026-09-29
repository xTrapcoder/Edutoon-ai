"""The compositor with procedural visuals and the deterministic fallbacks (Phase 11).

Real frames, no FFmpeg. What is proved: each visual form draws, in both orientations; drawing is a
pure function of its inputs; a scene with no valid visual - or one that cannot fit its region -
draws a deliberate text fallback (never an empty panel, never anything that implies a picture),
and does so identically whether the visual was absent, invalid or too big; the text-only templates
are exactly as they were; and pinned frame hashes make any drawing change fail loudly until the
drawing version is bumped on purpose.
"""

from __future__ import annotations

import copy
import hashlib

import numpy as np
import pytest

from edutoon.domain import visual as v
from edutoon.domain.storyboard_template import validate_scene_content
from edutoon.providers.renderer.compositor import (
    ACCENT,
    ACCENT_SOFT,
    SceneCompositor,
    background,
)
from tests.visual_corpus import comparison, components, cycle, flow, tree

SIZES = {"horizontal": (960, 540), "vertical": (540, 960)}
DURATION_MS = 12_000
HOLD_MS = 11_000  # after every reveal has finished, before the scene fades out


def compositor(template: str, content: dict, orientation: str = "horizontal", size=None):
    validate_scene_content(template, content)
    width, height = size or SIZES[orientation]
    return SceneCompositor(
        template=template,
        content=content,
        orientation=orientation,
        width=width,
        height=height,
        duration_ms=DURATION_MS,
    )


def frame_of(template, content, orientation="horizontal", t_ms=HOLD_MS, size=None):
    return compositor(template, content, orientation, size).frame(t_ms)


def digest(image) -> str:
    """A short hash of a frame - assertions compare these, never megabytes of raw bytes."""
    return hashlib.sha256(image.tobytes()).hexdigest()[:16]


def pixels(image) -> np.ndarray:
    return np.asarray(image.convert("RGB"))


def ink_fraction(image) -> float:
    return float((pixels(image) != pixels(background(*image.size))).any(axis=2).mean())


def scene(template: str, visual: dict | None, **extra) -> dict:
    illustration: dict = {"description": "Water moves", "subject_labels": ["water", "rain"]}
    if visual is not None:
        illustration["visual"] = visual
    content: dict = {"illustration": illustration, **extra}
    if template == "split_text_illustration":
        content["text"] = "Water moves in a loop."
    if template == "labelled_diagram" and visual is None:
        content["labels"] = [
            {"text": "Ocean", "position": "left"},
            {"text": "Cloud", "position": "top"},
        ]
    return content


FORMS = {
    "process_flow": flow(4, title="How it works", focus=1),
    "cycle": cycle(4, title="The water cycle"),
    "hierarchy": tree([None, 0, 0, 1, 1], title="Kinds"),
    "comparison": comparison(2, 3, title="Two things"),
    "components_hub": components(4, "hub", title="Parts"),
    "components_layers": components(3, "layers"),
}


# --- every form draws ----------------------------------------------------------------------------


@pytest.mark.parametrize("orientation", ["horizontal", "vertical"])
@pytest.mark.parametrize("form", sorted(FORMS))
@pytest.mark.parametrize("template", ["labelled_diagram", "full_bleed_illustration"])
def test_every_visual_form_draws_something_readable(template, form, orientation):
    frame = frame_of(template, scene(template, FORMS[form]), orientation)
    assert ink_fraction(frame) > 0.02, f"{form} looks blank"
    colours = np.unique(pixels(frame).reshape(-1, 3), axis=0)
    assert len(colours) > 20


@pytest.mark.parametrize("orientation", ["horizontal", "vertical"])
def test_a_visual_scene_is_drawn_with_connectors_and_numbered_steps(orientation):
    frame = pixels(frame_of("labelled_diagram", scene("labelled_diagram", flow(4)), orientation))
    accent = np.all(frame == np.array(ACCENT), axis=2)
    assert accent.sum() > 100, "no arrows or step numbers in the accent colour"


@pytest.mark.parametrize("orientation", ["horizontal", "vertical"])
def test_a_visual_on_a_split_scene_sits_beside_or_above_the_text(orientation):
    content = scene("split_text_illustration", flow(3))
    frame = frame_of("split_text_illustration", content, orientation)
    changed = (pixels(frame) != pixels(background(*frame.size))).any(axis=2)
    height, width = changed.shape
    if orientation == "horizontal":
        assert changed[:, : width // 2 - 20].any() and changed[:, width // 2 + 20 :].any()
    else:
        assert changed[: height // 2 - 20].any() and changed[height // 2 + 20 :].any()


def test_a_full_bleed_visual_keeps_its_caption_below_it_and_clear_of_the_diagram():
    content = scene("full_bleed_illustration", flow(4), caption="Four steps, one idea")
    for orientation in ("horizontal", "vertical"):
        compo = compositor("full_bleed_illustration", content, orientation)
        caption_layer = compo.layers[-1]
        others = compo.layers[:-1]
        top = caption_layer.y
        assert all(layer.y + layer.rgb.height <= top for layer in others), "the caption overlaps"


# --- determinism -------------------------------------------------------------------------------


@pytest.mark.parametrize("form", sorted(FORMS))
def test_drawing_a_visual_is_deterministic(form):
    content = scene("labelled_diagram", FORMS[form])
    first = compositor("labelled_diagram", content)
    second = compositor("labelled_diagram", content)
    for t in (0, 900, 2_500, 6_000, HOLD_MS):
        assert digest(first.frame(t)) == digest(second.frame(t))
        assert first.state_key(t) == second.state_key(t)


def test_identical_content_in_different_key_order_draws_the_same_pixels():
    a = {"type": "process_flow", "nodes": [{"label": "One"}, {"label": "Two"}]}
    b = {"nodes": [{"label": "One"}, {"label": "Two"}], "type": "process_flow"}
    assert digest(frame_of("labelled_diagram", scene("labelled_diagram", a))) == digest(
        frame_of("labelled_diagram", scene("labelled_diagram", b))
    )


def test_a_still_visual_has_one_animation_state_so_it_is_drawn_once():
    compo = compositor("labelled_diagram", scene("labelled_diagram", flow(3)))
    assert len({compo.state_key(t) for t in np.arange(9_000, 10_500, 33.3)}) == 1
    assert len({compo.state_key(t) for t in np.arange(0, 4_000, 33.3)}) > 10


def test_visual_steps_appear_one_after_another():
    compo = compositor("labelled_diagram", scene("labelled_diagram", flow(4)))
    shown = []
    for t in (300, 1_500, 3_000, 5_000, 9_000):
        opacities = [state[0] for state in compo.state_key(t)]
        shown.append(sum(1 for o in opacities if o > 250))
    assert shown == sorted(shown) and shown[0] < shown[-1]


# --- the deterministic fallbacks ---------------------------------------------------------------


@pytest.mark.parametrize("orientation", ["horizontal", "vertical"])
@pytest.mark.parametrize(
    "template", ["labelled_diagram", "split_text_illustration", "full_bleed_illustration"]
)
def test_without_a_visual_a_scene_is_never_blank_and_never_an_empty_panel(template, orientation):
    content = scene(
        template, None, **({"caption": "Light becomes food"} if "bleed" in template else {})
    )
    frame = frame_of(template, content, orientation)
    assert ink_fraction(frame) > 0.01
    lavender = np.all(pixels(frame) == np.array(ACCENT_SOFT), axis=2).mean()
    assert lavender < 0.01, "an illustration-style panel is being drawn"


@pytest.mark.parametrize("orientation", ["horizontal", "vertical"])
def test_a_full_bleed_scene_without_a_visual_is_a_strong_statement_card(orientation):
    content = scene("full_bleed_illustration", None, caption="Light becomes food")
    frame = pixels(frame_of("full_bleed_illustration", content, orientation))
    height, width = frame.shape[:2]
    inset = frame[height // 2, width // 2]
    assert tuple(inset) != tuple(pixels(background(width, height))[height // 2, width // 2])
    blue = (frame[..., 2].astype(int) - frame[..., 0].astype(int)) > 60
    assert blue.mean() > 0.4, "the card should fill most of the frame"
    white = np.all(frame > 235, axis=2)
    assert white.sum() > 200, "the statement text is drawn in white"


def test_the_statement_card_uses_the_caption_else_the_description_never_a_placeholder_label():
    with_caption = compositor(
        "full_bleed_illustration",
        scene("full_bleed_illustration", None, caption="Light becomes food"),
    )
    without = compositor("full_bleed_illustration", scene("full_bleed_illustration", None))
    assert with_caption._idea() == "Light becomes food"  # noqa: SLF001
    assert without._idea() == "Water moves"  # noqa: SLF001
    for compo in (with_caption, without):
        assert "llustration" not in compo._idea()  # noqa: SLF001


@pytest.mark.parametrize("orientation", ["horizontal", "vertical"])
def test_a_labelled_diagram_without_a_visual_shows_its_labels_as_numbered_parts(orientation):
    content = scene("labelled_diagram", None)
    with_labels = frame_of("labelled_diagram", content, orientation)
    fewer = copy.deepcopy(content)
    fewer["labels"] = fewer["labels"][:1]
    assert digest(with_labels) != digest(frame_of("labelled_diagram", fewer, orientation))
    assert np.all(pixels(with_labels) == np.array(ACCENT), axis=2).sum() > 50  # numbered discs


def test_a_labelled_diagram_with_no_labels_and_no_visual_falls_back_to_the_statement_card():
    content = {"illustration": {"description": "The parts of a network", "visual_dropped": True}}
    frame = frame_of("labelled_diagram", content)
    assert ink_fraction(frame) > 0.3


def test_a_split_scene_without_a_visual_gives_its_sentence_the_whole_width():
    compo = compositor("split_text_illustration", scene("split_text_illustration", None))
    xs = [layer.x for layer in compo.layers] + [layer.x + layer.rgb.width for layer in compo.layers]
    assert max(xs) - min(xs) > SIZES["horizontal"][0] * 0.3
    assert all(layer.rgb.width < SIZES["horizontal"][0] for layer in compo.layers)


# --- absent, invalid and too-big visuals fall back identically ---------------------------------


@pytest.mark.parametrize("orientation", ["horizontal", "vertical"])
@pytest.mark.parametrize(
    "template", ["labelled_diagram", "split_text_illustration", "full_bleed_illustration"]
)
def test_an_invalid_visual_draws_exactly_the_fallback_of_a_scene_with_none(template, orientation):
    extra = {"caption": "Same words"} if "bleed" in template else {}
    invalid = {"type": "cycle", "nodes": [{"label": "only one"}]}
    plain = scene(template, None, **extra)
    broken = copy.deepcopy(plain)
    broken["illustration"]["visual"] = invalid
    compo_plain = SceneCompositor(
        template=template,
        content=plain,
        orientation=orientation,
        width=SIZES[orientation][0],
        height=SIZES[orientation][1],
        duration_ms=DURATION_MS,
    )
    compo_broken = SceneCompositor(
        template=template,
        content=broken,
        orientation=orientation,
        width=SIZES[orientation][0],
        height=SIZES[orientation][1],
        duration_ms=DURATION_MS,
    )
    for t in (2_000, HOLD_MS):
        assert digest(compo_plain.frame(t)) == digest(compo_broken.frame(t))


def test_a_visual_that_overflows_its_region_draws_the_same_fallback_as_none():
    """Valid, but its words cannot fit even at the smallest allowed type: it falls back."""
    crowded = {
        "type": "process_flow",
        "nodes": [{"label": "Extraordinarily-long-unbreakable-words"[:28]} for _ in range(6)],
    }
    assert v.parse_visual(crowded)  # structurally valid
    template = "labelled_diagram"
    plain = scene(template, None)  # carries labels
    with_visual = copy.deepcopy(plain)
    with_visual["illustration"]["visual"] = crowded
    a = compositor(template, with_visual, size=(480, 270))
    b = compositor(template, plain, size=(480, 270))
    assert digest(a.frame(HOLD_MS)) == digest(b.frame(HOLD_MS))


def test_the_fallback_never_shows_json_or_a_stack_trace():
    """The renderer is handed content that was never validated (a bad visual): it draws the
    scene's own words and nothing else - no key names, braces or error text."""
    invalid = {"type": "cycle", "nodes": [{"label": "SENTINEL"}]}
    content = scene("full_bleed_illustration", invalid, caption="Only this")
    compo = SceneCompositor(
        template="full_bleed_illustration",
        content=content,
        orientation="horizontal",
        width=960,
        height=540,
        duration_ms=DURATION_MS,
    )
    assert compo._idea() == "Only this"  # noqa: SLF001
    assert ink_fraction(compo.frame(HOLD_MS)) > 0.01


# --- text-only templates are untouched ---------------------------------------------------------


TEXT_ONLY = {
    "title_card": {"title": "How networks work", "subtitle": "Packets and routes"},
    "bullet_points": {
        "title": "Key ideas",
        "bullets": ["Data is split into packets", "Routers forward packets"],
    },
    "formula_or_code": {"content": "speed = distance / time", "kind": "formula", "label": "Speed"},
}


@pytest.mark.parametrize("template", sorted(TEXT_ONLY))
def test_text_only_templates_ignore_a_visual_key_and_draw_exactly_as_before(template):
    plain = frame_of(template, TEXT_ONLY[template])
    sneaky = {**TEXT_ONLY[template], "illustration": {"description": "d", "visual": flow(3)}}
    assert digest(frame_of(template, sneaky)) == digest(plain)


# --- pinned frames: a drawing change must come with a version bump -----------------------------

GOLDEN_SCENES = {
    "flow": ("labelled_diagram", scene("labelled_diagram", flow(4, title="How it works", focus=1))),
    "cycle": ("labelled_diagram", scene("labelled_diagram", cycle(5))),
    "hierarchy": ("labelled_diagram", scene("labelled_diagram", tree([None, 0, 0, 1, 1]))),
    "comparison": (
        "full_bleed_illustration",
        scene("full_bleed_illustration", comparison(2, 3), caption="A contrast"),
    ),
    "hub": ("labelled_diagram", scene("labelled_diagram", components(4, "hub"))),
    "layers": (
        "split_text_illustration",
        scene("split_text_illustration", components(3, "layers")),
    ),
    "fallback_parts": ("labelled_diagram", scene("labelled_diagram", None)),
    "fallback_text": ("split_text_illustration", scene("split_text_illustration", None)),
    "fallback_statement": (
        "full_bleed_illustration",
        scene("full_bleed_illustration", None, caption="Light becomes food"),
    ),
    "title": ("title_card", TEXT_ONLY["title_card"]),
    "bullets": ("bullet_points", TEXT_ONLY["bullet_points"]),
    "formula": ("formula_or_code", TEXT_ONLY["formula_or_code"]),
}


def frame_hash(name: str, orientation: str) -> str:
    template, content = GOLDEN_SCENES[name]
    frame = frame_of(
        template,
        content,
        orientation,
        size=(480, 270) if orientation == "horizontal" else (270, 480),
    )
    return digest(frame)


# Regenerate with the output of a scratch script calling frame_hash() for every entry of
# GOLDEN_SCENES, but ONLY together with a deliberate bump of the drawing versions in domain.visual.
GOLDEN_FRAMES = {
    ("flow", "horizontal"): "29fc979aa3e0e53e",
    ("flow", "vertical"): "cea0acab9b656eac",
    ("cycle", "horizontal"): "52f5f6fe82d27264",
    ("cycle", "vertical"): "af37245ae10963d2",
    ("hierarchy", "horizontal"): "c360b614f890d0ce",
    ("hierarchy", "vertical"): "e99a65e436572b12",
    ("comparison", "horizontal"): "b52f72ffbb71de7b",
    ("comparison", "vertical"): "46d1d907613e074e",
    ("hub", "horizontal"): "08600e0986d2eb1f",
    ("hub", "vertical"): "23ac1f429fe3aac0",
    ("layers", "horizontal"): "8f020eddcd8d2d41",
    ("layers", "vertical"): "7a243f334c27e76e",
    ("fallback_parts", "horizontal"): "f4e6e2108ab9fbff",
    ("fallback_parts", "vertical"): "c3a3611975daeaff",
    ("fallback_text", "horizontal"): "37b80ea2c3f565fb",
    ("fallback_text", "vertical"): "aa6351d61ff3c989",
    ("fallback_statement", "horizontal"): "a5a57d9466a7bdf9",
    ("fallback_statement", "vertical"): "b75bd75cb2b3f45d",
    ("title", "horizontal"): "656afef101e388c8",
    ("title", "vertical"): "84b409a225a59768",
    ("bullets", "horizontal"): "72b6a64e2f3b8e5b",
    ("bullets", "vertical"): "462fd0dcd4ec6b29",
    ("formula", "horizontal"): "c9827f712a99262b",
    ("formula", "vertical"): "afbc16a2736c1f0b",
}


@pytest.mark.parametrize(("name", "orientation"), sorted(GOLDEN_FRAMES))
def test_pinned_frames_change_only_with_a_deliberate_drawing_version_bump(name, orientation):
    template, content = GOLDEN_SCENES[name]
    drawing = v.visual_drawing_id(template, content)
    assert frame_hash(name, orientation) == GOLDEN_FRAMES[(name, orientation)], (
        f"the pixels of {name!r} ({orientation}) changed. If that is intended, bump the drawing "
        f"version(s) in domain.visual (currently {drawing}) and regenerate GOLDEN_FRAMES; if it is "
        "not, something altered drawing by accident."
    )

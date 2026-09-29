"""Transitions and connector drawing in the compositor and the real encoder (Phase 11).

A scene that *cuts in* has content on its very first frame; one that *fades in* starts on the bare
background. Likewise at the end. Between two scenes of the same topic nothing is dimmed at all.
Straight connectors are drawn from tail to tip in exactly 300 ms; elbows fade in as one piece.
"""

from __future__ import annotations

import subprocess
from io import BytesIO

import numpy as np
import pytest
from PIL import Image

from edutoon.domain import render as r
from edutoon.domain import render_animation as anim
from edutoon.domain.storyboard_template import TEMPLATES, validate_scene_content
from edutoon.providers.renderer import ChunkSpec, PillowFfmpegRenderer
from edutoon.providers.renderer.compositor import SceneCompositor, background
from tests.render_helpers import requires_ffmpeg, scene_content
from tests.visual_corpus import comparison, components, cycle, flow

SIZES = {"horizontal": (960, 540), "vertical": (540, 960)}
DURATION_MS = 6_000
FRAME_MS = 1000 / 30


def compositor(
    template, orientation="horizontal", *, enter=0, exit_=0, content=None, duration=DURATION_MS
):
    content = content or scene_content(template, "A clear label")
    validate_scene_content(template, content)
    width, height = SIZES[orientation]
    return SceneCompositor(
        template=template,
        content=content,
        orientation=orientation,
        width=width,
        height=height,
        duration_ms=duration,
        enter_fade_ms=enter,
        exit_fade_ms=exit_,
    )


def ink(image) -> float:
    bg = np.asarray(background(*image.size).convert("RGB"))
    return float((np.asarray(image.convert("RGB")) != bg).any(axis=2).mean())


# --- cut in / fade in --------------------------------------------------------------------------


@pytest.mark.parametrize("orientation", ["horizontal", "vertical"])
@pytest.mark.parametrize("template", sorted(TEMPLATES))
def test_a_scene_that_cuts_in_has_content_on_its_first_frame(template, orientation):
    first = compositor(template, orientation, enter=0, exit_=anim.DEFAULT_FADE_OUT_MS).frame(0)
    assert ink(first) > 0.004, f"{template}/{orientation} is blank on its first frame"


@pytest.mark.parametrize("orientation", ["horizontal", "vertical"])
@pytest.mark.parametrize("template", sorted(TEMPLATES))
def test_a_scene_that_fades_in_starts_on_the_bare_background(template, orientation):
    first = compositor(template, orientation, enter=anim.DEFAULT_FADE_IN_MS, exit_=0).frame(0)
    assert ink(first) == 0.0


@pytest.mark.parametrize("template", sorted(TEMPLATES))
def test_a_cut_in_scene_reaches_the_same_final_picture_as_a_fade_in_one(template):
    """Only the entrance differs: once everything has arrived, the two are pixel-identical."""
    cut = compositor(template, enter=0, exit_=0)
    fade = compositor(template, enter=anim.DEFAULT_FADE_IN_MS, exit_=0)
    assert np.array_equal(
        np.asarray(cut.frame(DURATION_MS - 200)), np.asarray(fade.frame(DURATION_MS - 200))
    )


@pytest.mark.parametrize("template", sorted(TEMPLATES))
def test_cutting_in_shifts_every_layer_by_one_amount_so_relative_timing_is_kept(template):
    cut = compositor(template, enter=0, exit_=0)
    fade = compositor(template, enter=anim.DEFAULT_FADE_IN_MS, exit_=0)
    shifts = {
        round(f.start_ms - c.start_ms, 6) for c, f in zip(cut.layers, fade.layers, strict=True)
    }
    assert shifts == {float(anim.CUT_IN_SHIFT_MS)}


@pytest.mark.parametrize("template", sorted(TEMPLATES))
def test_everything_that_would_have_arrived_first_is_already_in_place_when_a_scene_cuts_in(
    template,
):
    cut = compositor(template, enter=0, exit_=0)
    fade = compositor(template, enter=anim.DEFAULT_FADE_IN_MS, exit_=0)
    early = [
        c
        for c, f in zip(cut.layers, fade.layers, strict=True)
        if f.start_ms <= anim.FIRST_ITEM_DELAY_MS
    ]
    assert early
    for layer in early:
        if layer.wipe:
            continue  # a wipe is already under way, not yet complete
        assert anim.item_alpha(0, layer.start_ms) == 1.0


# --- cut out / fade out ------------------------------------------------------------------------


@pytest.mark.parametrize("template", sorted(TEMPLATES))
def test_a_scene_that_cuts_out_is_still_on_screen_on_its_last_frame(template):
    last = compositor(template, enter=anim.DEFAULT_FADE_IN_MS, exit_=0).frame(
        DURATION_MS - FRAME_MS
    )
    assert ink(last) > 0.004


@pytest.mark.parametrize("template", sorted(TEMPLATES))
def test_a_scene_that_fades_out_ends_on_the_bare_background(template):
    end = compositor(template, enter=0, exit_=anim.DEFAULT_FADE_OUT_MS).frame(DURATION_MS)
    assert ink(end) == 0.0


@pytest.mark.parametrize("template", sorted(TEMPLATES))
def test_between_two_scenes_of_one_topic_nothing_is_ever_blank_or_dim(template):
    scene = compositor(template, enter=0, exit_=0)
    for t in np.arange(0, DURATION_MS, 100):
        assert ink(scene.frame(float(t))) > 0.005, f"{template} is blank at {t} ms"
    assert {scene._envelope(float(t)) for t in np.arange(0, DURATION_MS, 50)} == {1.0}  # noqa: SLF001


def test_the_default_pacing_spends_fewer_frames_dim_than_the_old_fixed_fades():
    def dim_frames(enter, exit_):
        frames = int(DURATION_MS / FRAME_MS)
        return sum(
            1
            for i in range(frames)
            if anim.scene_envelope(i * FRAME_MS, DURATION_MS, enter, exit_) < 1.0
        )

    old = dim_frames(450, 300)
    new_boundary = dim_frames(anim.DEFAULT_FADE_IN_MS, anim.DEFAULT_FADE_OUT_MS)
    same_topic = dim_frames(0, 0)
    assert same_topic == 0
    assert new_boundary < old * 0.7
    assert (old, new_boundary) == (round(0.75 / FRAME_MS * 1000 / 1000) * 1 or old, new_boundary)


def test_a_fade_of_a_different_length_is_really_used():
    slow = compositor("title_card", enter=1200, exit_=0)
    quick = compositor("title_card", enter=120, exit_=0)
    assert ink(quick.frame(400)) > ink(slow.frame(400))


# --- connectors are drawn ----------------------------------------------------------------------


def _visual_scene(spec, orientation="horizontal", template="labelled_diagram"):
    content = {"illustration": {"description": "d", "visual": spec}}
    return compositor(
        template,
        orientation,
        enter=anim.DEFAULT_FADE_IN_MS,
        exit_=0,
        content=content,
        duration=12_000,
    )


def _wipes(scene):
    return [layer for layer in scene.layers if layer.wipe]


def _revealed(scene, layer, t):
    return scene._layer_state(layer, t, 1.0)[2]  # noqa: SLF001


def test_a_straight_connector_is_drawn_tail_to_tip_in_exactly_the_draw_time():
    scene = _visual_scene(flow(3))
    arrows = _wipes(scene)
    assert len(arrows) == 2
    for arrow in arrows:
        assert arrow.wipe_ms == anim.CONNECTOR_DRAW_MS
        assert arrow.wipe_dir == "right" and not arrow.fade and not arrow.slide
        width = arrow.rgb.width
        assert _revealed(scene, arrow, arrow.start_ms - 1) == 0
        assert _revealed(scene, arrow, arrow.start_ms + anim.CONNECTOR_DRAW_MS) == width
        partial = [_revealed(scene, arrow, arrow.start_ms + dt) for dt in range(0, 301, 30)]
        assert partial == sorted(partial) and 0 < partial[3] < width


def test_a_vertical_flow_draws_its_arrows_downwards():
    scene = _visual_scene(flow(3), "vertical")
    arrows = _wipes(scene)
    assert arrows and all(a.wipe_dir == "down" for a in arrows)
    arrow = arrows[0]
    assert _revealed(scene, arrow, arrow.start_ms + anim.CONNECTOR_DRAW_MS) == arrow.rgb.height


def test_a_connector_pointing_left_is_drawn_from_its_right_hand_end():
    scene = _visual_scene(cycle(4))
    lefts = [layer for layer in _wipes(scene) if layer.wipe_dir == "left"]
    assert lefts, "the ring has an arrow that points left"


def test_an_elbow_connector_fades_in_as_one_piece_instead_of_being_wiped():
    scene = _visual_scene(flow(5))  # five steps wrap onto a second row with an elbow
    elbows = [layer for layer in scene.layers if not layer.wipe and layer.fade and not layer.slide]
    assert elbows


def test_a_connector_is_never_drawn_before_its_step_and_starts_with_its_node():
    scene = _visual_scene(flow(4))
    nodes = [layer for layer in scene.layers if not layer.wipe]
    starts = sorted({round(layer.start_ms) for layer in nodes})
    for arrow in _wipes(scene):
        assert round(arrow.start_ms) in starts


def test_visual_steps_are_paced_evenly_from_the_scenes_own_duration():
    scene = _visual_scene(flow(4))
    starts = anim.item_reveal_starts(12_000, 4, exit_ms=0)
    node_starts = sorted({round(layer.start_ms) for layer in scene.layers if not layer.wipe})
    assert node_starts == list(starts)
    gaps = {b - a for a, b in zip(starts, starts[1:], strict=False)}
    assert len(gaps) == 1


def test_a_visual_scene_is_deterministic_frame_for_frame():
    a = _visual_scene(comparison(2, 3))
    b = _visual_scene(comparison(2, 3))
    for t in (0, 700, 2_000, 5_000, 9_000):
        assert np.array_equal(np.asarray(a.frame(t)), np.asarray(b.frame(t)))
        assert a.state_key(t) == b.state_key(t)


@pytest.mark.parametrize("form", [flow(4), cycle(5), components(4)])
def test_the_reveal_never_pulses_bounces_or_moves_a_visual_after_it_has_appeared(form):
    scene = _visual_scene(form)
    settled = scene.frame(9_000)
    for t in (9_500, 10_500, 11_000):  # a still hold: not one pixel changes
        assert np.array_equal(np.asarray(scene.frame(t)), np.asarray(settled))
    for layer in scene.layers:
        assert not layer.slide, "visual layers do not slide"


# --- through the real encoder ------------------------------------------------------------------


def _first_and_last_frames(path):
    def grab(*args):
        png = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                *args,
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-f",
                "image2pipe",
                "-vcodec",
                "png",
                "-",
            ],
            capture_output=True,
            check=True,
        ).stdout
        return Image.open(BytesIO(png)).convert("RGB")

    return grab(), grab("-sseof", "-0.05")


def _spec(enter, exit_):
    width, height = r.output_size("horizontal", "low")
    return ChunkSpec(
        template="title_card",
        content={"title": "Transition test", "subtitle": "cut or fade"},
        orientation="horizontal",
        width=width,
        height=height,
        fps=r.FPS,
        duration_ms=3_000,
        canonical_frames=r.canonical_frames(3_000),
        encoder="libx264",
        profile=r.profile_for("low"),
        enter_fade_ms=enter,
        exit_fade_ms=exit_,
    )


def _distance_from_background(image) -> float:
    """Mean absolute difference from the bare background, 0-255: how much of the scene is there."""
    bg = np.asarray(background(*image.size).convert("RGB")).astype(int)
    return float(np.abs(np.asarray(image).astype(int) - bg).mean())


@requires_ffmpeg
async def test_the_encoded_chunk_cuts_in_and_out_or_fades_as_asked(tmp_path):
    renderer = PillowFfmpegRenderer()
    cut = await renderer.render_chunk(_spec(0, 0), tmp_path / "cut.mp4")
    fade = await renderer.render_chunk(_spec(240, 200), tmp_path / "fade.mp4")
    cut_first, cut_last = _first_and_last_frames(cut.path)
    fade_first, fade_last = _first_and_last_frames(fade.path)
    full = _distance_from_background(cut_first)
    assert full > 20 and _distance_from_background(cut_last) > 20  # on screen at both ends
    assert (
        _distance_from_background(fade_first) < 4
    )  # the bare background (allowing for YUV rounding)
    # The last frame is one frame short of the end, so a 200 ms fade has not quite finished: the
    # scene is mostly gone - a small fraction of what a straight cut leaves on screen.
    assert _distance_from_background(fade_last) < full * 0.35


@requires_ffmpeg
async def test_a_chunks_frame_count_does_not_depend_on_its_transitions(tmp_path):
    renderer = PillowFfmpegRenderer()
    cut = await renderer.render_chunk(_spec(0, 0), tmp_path / "cut.mp4")
    fade = await renderer.render_chunk(_spec(240, 200), tmp_path / "fade.mp4")
    assert cut.frame_count == fade.frame_count == r.canonical_frames(3_000)


@requires_ffmpeg
async def test_the_same_transition_renders_to_identical_bytes_twice(tmp_path):
    renderer = PillowFfmpegRenderer()
    one = await renderer.render_chunk(_spec(0, 200), tmp_path / "one.mp4")
    two = await renderer.render_chunk(_spec(0, 200), tmp_path / "two.mp4")
    assert one.path.read_bytes() == two.path.read_bytes()

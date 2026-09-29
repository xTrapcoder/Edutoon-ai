"""How procedural visuals take part in the Phase 10 render fingerprint (Phase 11).

The rule (spec, section M): a chunk is stored under a fingerprint of exactly the inputs that decide
its bytes. For a visual scene that includes the visual itself (it is part of the scene's content),
the drawing versions of the code that draws it, and everything Phase 10 already had (template,
duration, orientation, quality, fps, encoder). Consequences proved here: editing one visual
invalidates only that scene's chunk; bumping one form's drawing version invalidates only that
form; text-only scenes keep their fingerprints when visual drawing changes; a scene that merely
moves keeps its fingerprint.
"""

from __future__ import annotations

import copy

import pytest

from edutoon.domain import render as r
from edutoon.domain import visual as v
from tests.visual_corpus import comparison, components, cycle, flow, tree

FLOW = flow(3)
CYCLE = cycle(4)
TREE = tree([None, 0, 0])


def _illustrated(template: str, visual: dict | None, **extra) -> tuple[str, dict]:
    illustration: dict = {"description": "A diagram of the idea"}
    if visual is not None:
        illustration["visual"] = visual
    content: dict = {"illustration": illustration, **extra}
    if template == "labelled_diagram" and visual is None:
        content["labels"] = [{"text": "Part", "position": "top"}]
    if template == "split_text_illustration":
        content["text"] = "The idea in a sentence."
    return template, content


TITLE = ("title_card", {"title": "Intro"})
BULLETS = ("bullet_points", {"bullets": ["One", "Two"]})
FORMULA = ("formula_or_code", {"content": "E = mc^2", "kind": "formula"})


def fingerprint(spec, *, duration_ms=6000, orientation="horizontal", quality="high", **kw) -> str:
    template, content = spec
    return r.chunk_fingerprint(
        template=template,
        content=content,
        duration_ms=duration_ms,
        orientation=orientation,
        quality=quality,
        encoder=kw.pop("encoder", "libx264"),
        **kw,
    )


def _plan(scenes_by_segment: list[list[tuple[str, dict]]], durations=None, audio_hash=None):
    """A render plan for a script whose segment ``i`` has the given scenes, each 4 s."""
    segments, cursor = [], 0
    for index, specs in enumerate(scenes_by_segment):
        length = durations[index] if durations else 4000 * len(specs)
        each = length // len(specs)
        scenes, at = [], cursor
        for n, (template, content) in enumerate(specs):
            scenes.append(r.SceneInput(n, at, each, template, content))
            at += each
        segments.append(
            r.SegmentInput(
                f"seg-{index}",
                index,
                each * len(specs),
                (audio_hash or {}).get(index, f"a{index}"),
                scenes,
            )
        )
        cursor += each * len(specs)
    return r.build_render_plan(
        segments, orientation="horizontal", quality="high", encoder="libx264"
    )


# --- the drawing id ----------------------------------------------------------------------------


@pytest.mark.parametrize("spec", [TITLE, BULLETS, FORMULA], ids=["title", "bullets", "formula"])
def test_text_only_templates_have_no_drawing_id_even_if_content_carries_a_visual_key(spec):
    template, content = spec
    assert v.visual_drawing_id(template, content) is None
    sneaky = {**content, "illustration": {"description": "d", "visual": FLOW}}
    assert v.visual_drawing_id(template, sneaky) is None
    assert fingerprint((template, sneaky)) != fingerprint(spec)  # content differs, id does not


def test_an_illustrated_scene_with_a_valid_visual_names_the_framework_the_form_and_the_fallback():
    template, content = _illustrated("labelled_diagram", CYCLE)
    assert v.visual_drawing_id(template, content) == (
        "framework/1+cycle/1+fallback/labelled_diagram/1"
    )


@pytest.mark.parametrize("template", sorted(v.TEMPLATE_REGION))
def test_an_illustrated_scene_without_a_visual_names_the_framework_and_its_fallback(template):
    t, content = _illustrated(template, None)
    assert v.visual_drawing_id(t, content) == f"framework/1+fallback/{template}/1"


def test_an_invalid_visual_in_stored_content_is_treated_as_the_fallback():
    t, content = _illustrated("full_bleed_illustration", {"type": "cycle", "nodes": []})
    assert v.visual_drawing_id(t, content) == "framework/1+fallback/full_bleed_illustration/1"


def test_every_form_and_template_has_a_version():
    assert set(v.VISUAL_TYPE_VERSIONS) == set(v.VISUAL_TYPES)
    assert set(v.FALLBACK_VERSIONS) == {
        "labelled_diagram",
        "split_text_illustration",
        "full_bleed_illustration",
    }
    assert all(isinstance(n, int) and n >= 1 for n in v.VISUAL_TYPE_VERSIONS.values())


# --- what changes a scene's fingerprint --------------------------------------------------------


def test_adding_editing_or_removing_a_visual_changes_the_scene_fingerprint():
    none = fingerprint(_illustrated("full_bleed_illustration", None))
    with_flow = fingerprint(_illustrated("full_bleed_illustration", FLOW))
    edited = copy.deepcopy(FLOW)
    edited["nodes"][1]["label"] = "Changed"
    with_edit = fingerprint(_illustrated("full_bleed_illustration", edited))
    assert len({none, with_flow, with_edit}) == 3


def test_a_visuals_focus_and_title_are_part_of_the_fingerprint():
    base = fingerprint(_illustrated("full_bleed_illustration", FLOW))
    assert fingerprint(_illustrated("full_bleed_illustration", {**FLOW, "focus": 1})) != base
    assert fingerprint(_illustrated("full_bleed_illustration", {**FLOW, "title": "T"})) != base


def test_key_order_inside_a_visual_does_not_change_the_fingerprint():
    a = {"type": "process_flow", "nodes": [{"label": "A"}, {"label": "B"}]}
    b = {"nodes": [{"label": "A"}, {"label": "B"}], "type": "process_flow"}
    assert fingerprint(_illustrated("labelled_diagram", a)) == fingerprint(
        _illustrated("labelled_diagram", b)
    )


def test_identical_visual_scenes_share_one_fingerprint_and_are_rendered_once():
    spec = _illustrated("full_bleed_illustration", FLOW)
    plan = _plan([[spec, spec]])
    assert plan.chunks[0].chunk_hash == plan.chunks[1].chunk_hash
    assert len(r.chunks_to_render(plan, set())) == 1


@pytest.mark.parametrize(
    ("change", "value"),
    [
        ("duration_ms", 6001),
        ("orientation", "vertical"),
        ("quality", "low"),
        ("encoder", "h264_nvenc"),
        ("fps", 25),
    ],
)
def test_everything_phase_10_already_had_still_invalidates_a_visual_scene(change, value):
    spec = _illustrated("labelled_diagram", flow(4))
    kwargs = {"duration_ms": 6000, "orientation": "horizontal", "quality": "high"}
    if change in kwargs:
        assert fingerprint(spec, **{**kwargs, change: value}) != fingerprint(spec)
    elif change == "encoder":
        assert fingerprint(spec, encoder=value) != fingerprint(spec)
    else:
        assert fingerprint(spec, fps=value) != fingerprint(spec)


def test_the_template_and_the_visual_together_decide_it():
    flow_on_diagram = fingerprint(_illustrated("labelled_diagram", FLOW))
    flow_on_bleed = fingerprint(_illustrated("full_bleed_illustration", FLOW))
    assert flow_on_diagram != flow_on_bleed


# --- versions: a bump invalidates exactly what it names ----------------------------------------


def _mixed_scenes():
    return {
        "flow": _illustrated("labelled_diagram", FLOW),
        "cycle": _illustrated("full_bleed_illustration", CYCLE),
        "tree": _illustrated("split_text_illustration", TREE),
        "cmp": _illustrated("full_bleed_illustration", comparison()),
        "parts": _illustrated("labelled_diagram", components()),
        "fallback_diagram": _illustrated("labelled_diagram", None),
        "fallback_split": _illustrated("split_text_illustration", None),
        "fallback_bleed": _illustrated("full_bleed_illustration", None),
        "title": TITLE,
        "bullets": BULLETS,
        "formula": FORMULA,
    }


def _all(scenes):
    return {name: fingerprint(spec) for name, spec in scenes.items()}


def _changed(before, after):
    return {name for name in before if before[name] != after[name]}


def test_bumping_one_forms_version_invalidates_only_that_form(monkeypatch):
    scenes = _mixed_scenes()
    before = _all(scenes)
    monkeypatch.setitem(v.VISUAL_TYPE_VERSIONS, "cycle", 2)
    assert _changed(before, _all(scenes)) == {"cycle"}


@pytest.mark.parametrize("form", v.VISUAL_TYPES)
def test_every_forms_version_is_in_the_fingerprint(form, monkeypatch):
    specs = {
        "process_flow": FLOW,
        "cycle": CYCLE,
        "hierarchy": TREE,
        "comparison": comparison(),
        "labelled_components": components(),
    }
    spec = _illustrated("full_bleed_illustration", specs[form])
    before = fingerprint(spec)
    monkeypatch.setitem(v.VISUAL_TYPE_VERSIONS, form, 99)
    assert fingerprint(spec) != before


def test_bumping_the_framework_invalidates_every_visual_and_fallback_scene_but_no_text_scene(
    monkeypatch,
):
    scenes = _mixed_scenes()
    before = _all(scenes)
    monkeypatch.setattr(v, "VISUAL_FRAMEWORK_VERSION", 2)
    assert _changed(before, _all(scenes)) == set(scenes) - {"title", "bullets", "formula"}


def test_bumping_a_templates_fallback_invalidates_its_fallbacks_and_its_visuals_only(monkeypatch):
    scenes = _mixed_scenes()
    before = _all(scenes)
    monkeypatch.setitem(v.FALLBACK_VERSIONS, "full_bleed_illustration", 2)
    assert _changed(before, _all(scenes)) == {"cycle", "cmp", "fallback_bleed"}


def test_text_only_scenes_keep_their_fingerprints_whatever_the_visual_versions_do(monkeypatch):
    scenes = {k: _mixed_scenes()[k] for k in ("title", "bullets", "formula")}
    before = _all(scenes)
    monkeypatch.setattr(v, "VISUAL_FRAMEWORK_VERSION", 7)
    monkeypatch.setitem(v.VISUAL_TYPE_VERSIONS, "process_flow", 7)
    monkeypatch.setitem(v.FALLBACK_VERSIONS, "labelled_diagram", 7)
    assert _all(scenes) == before


# --- in a whole render plan --------------------------------------------------------------------


def test_editing_one_visual_invalidates_only_that_scenes_chunk():
    scenes = [
        [_illustrated("labelled_diagram", FLOW), TITLE],
        [_illustrated("full_bleed_illustration", CYCLE), BULLETS],
        [_illustrated("split_text_illustration", TREE), FORMULA],
    ]
    before = _plan(scenes)
    edited = copy.deepcopy(scenes)
    edited[1][0][1]["illustration"]["visual"]["nodes"][2]["label"] = "Edited"
    after = _plan(edited)
    to_render = r.chunks_to_render(after, set(before.chunk_hashes))
    assert [c.position for c in to_render] == [2]
    assert after.output_hash != before.output_hash


def test_a_form_version_bump_re_renders_only_that_forms_chunks_in_a_plan(monkeypatch):
    scenes = [
        [_illustrated("labelled_diagram", FLOW), _illustrated("full_bleed_illustration", CYCLE)],
        [_illustrated("labelled_diagram", TREE), TITLE],
    ]
    before = _plan(scenes)
    monkeypatch.setitem(v.VISUAL_TYPE_VERSIONS, "hierarchy", 2)
    after = _plan(scenes)
    to_render = r.chunks_to_render(after, set(before.chunk_hashes))
    assert [c.template for c in to_render] == ["labelled_diagram"]
    assert [c.position for c in to_render] == [2]


def test_a_visual_scene_that_only_moves_keeps_its_fingerprint_and_is_reused():
    visual_scene = _illustrated("full_bleed_illustration", FLOW)
    before = _plan([[TITLE, TITLE], [visual_scene, BULLETS]])
    # Segment 0 gets longer: everything after it moves, but no scene of segment 1 changes.
    after = _plan([[TITLE, TITLE], [visual_scene, BULLETS]], durations=[12000, 8000])
    assert before.chunks[2].start_ms != after.chunks[2].start_ms
    assert before.chunks[2].chunk_hash == after.chunks[2].chunk_hash
    unchanged = {c.chunk_hash for c in before.chunks[2:]}
    assert {c.chunk_hash for c in r.chunks_to_render(after, unchanged)} <= {
        c.chunk_hash for c in after.chunks[:2]
    }


def test_re_narrating_one_segment_re_renders_only_its_scenes():
    scenes = [
        [_illustrated("labelled_diagram", FLOW), TITLE],
        [_illustrated("full_bleed_illustration", CYCLE), BULLETS],
    ]
    before = _plan(scenes)
    after = _plan(scenes, durations=[8000, 9000])  # segment 1 got longer: its scenes change length
    assert [c.chunk_hash for c in after.chunks[:2]] == [c.chunk_hash for c in before.chunks[:2]]
    to_render = r.chunks_to_render(after, set(before.chunk_hashes))
    assert [c.position for c in to_render] == [2, 3]
    assert after.output_hash != before.output_hash


def test_the_narration_hash_changes_the_video_but_not_a_scenes_chunk():
    scenes = [[_illustrated("labelled_diagram", FLOW), TITLE]]
    before = _plan(scenes, audio_hash={0: "old"})
    after = _plan(scenes, audio_hash={0: "new"})
    assert before.chunk_hashes == after.chunk_hashes
    assert before.output_hash != after.output_hash


def test_text_only_scenes_of_an_untouched_script_stay_reusable_across_a_visual_edit():
    scenes = [[TITLE, _illustrated("labelled_diagram", FLOW)], [BULLETS, FORMULA]]
    before = _plan(scenes)
    edited = copy.deepcopy(scenes)
    edited[0][1][1]["illustration"]["visual"]["nodes"][0]["label"] = "New"
    to_render = r.chunks_to_render(_plan(edited), set(before.chunk_hashes))
    assert [c.template for c in to_render] == ["labelled_diagram"]

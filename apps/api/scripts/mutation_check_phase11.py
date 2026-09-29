"""Targeted mutation checks for Phase 11.

Each mutation breaks the implementation in one specific, plausible way (a dropped input from a
fingerprint, a check removed, an orientation swapped, a limit relaxed, a flag mis-reported) and runs
the tests that should notice. A mutation is *killed* if those tests fail; if they still pass, the
tests have a hole and the script exits non-zero. Every file is restored afterwards, whatever
happens.

    uv run python scripts/mutation_check_phase11.py            # all
    uv run python scripts/mutation_check_phase11.py timing     # names or areas containing "timing"

Needs `make up` (Postgres, Redis, MinIO) for the service tests and ffmpeg on PATH for the encoder
tests. Do not edit the mutated source files or these tests while it runs.
"""

# ruff: noqa: E501 - mutation source strings are quoted verbatim and read best on one line.
from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "edutoon"


@dataclass(frozen=True)
class Mutation:
    name: str
    area: str
    file: str
    old: str
    new: str
    tests: tuple[str, ...]
    keyword: str | None = None


RENDER = "domain/render.py"
VISUAL = "domain/visual.py"
LAYOUT = "domain/visual_layout.py"
SCHEMA = "domain/visual_schema.py"
PROVENANCE = "domain/storyboard_provenance.py"
TIMING = "domain/storyboard_timing.py"
STORYBOARD = "services/storyboard.py"
GLYPHS = "domain/text_glyphs.py"
SCENE = "providers/renderer/visual_scene.py"
COMPOSITOR = "providers/renderer/compositor.py"

T_VISUAL = "tests/test_domain_visual.py"
T_SCHEMA = "tests/test_visual_schema.py"
T_PROVENANCE = "tests/test_domain_provenance_visuals.py"
T_LAYOUT = "tests/test_domain_visual_layout.py"
T_FINGERPRINT = "tests/test_domain_render_visuals.py"
T_RENDER = "tests/test_domain_render.py"
T_TRANSITIONS = "tests/test_domain_render_transitions.py"
T_TIMING = "tests/test_domain_storyboard_timing.py"
T_COMPOSITOR = "tests/test_renderer_visuals.py"
T_ANIMATION = "tests/test_renderer_transitions.py"
T_GLYPHS = "tests/test_text_glyphs.py"
T_GLYPH_DRAW = "tests/test_renderer_glyphs.py"
T_STORYBOARD = "tests/test_services_storyboard.py"
T_STORYBOARD_API = "tests/test_storyboard_api.py"

MUTATIONS: tuple[Mutation, ...] = (
    # --- fingerprint / invalidation --------------------------------------------------------------
    Mutation(
        "the visual drawing id is left out of a scene's fingerprint",
        "fingerprint",
        RENDER,
        '        inputs["visual"] = drawing\n',
        "        pass\n",
        (T_FINGERPRINT,),
    ),
    Mutation(
        "the form's drawing version is left out of the drawing id",
        "fingerprint",
        VISUAL,
        '    kind = f"{visual.type}/{VISUAL_TYPE_VERSIONS[visual.type]}"\n',
        '    kind = f"{visual.type}"\n',
        (T_FINGERPRINT,),
    ),
    Mutation(
        "the framework version is left out of the fallback drawing id",
        "fingerprint",
        VISUAL,
        '        return f"framework/{VISUAL_FRAMEWORK_VERSION}+{fallback}"\n',
        '        return f"{fallback}"\n',
        (T_FINGERPRINT,),
    ),
    Mutation(
        "a text-only template gets a visual drawing id (its cached chunks would be invalidated)",
        "fingerprint",
        VISUAL,
        "    if fallback_version is None:\n        return None\n",
        '    if fallback_version is None:\n        return "framework/1"\n',
        (T_FINGERPRINT, T_RENDER),
    ),
    Mutation(
        "how a scene joins its neighbours is left out of the fingerprint",
        "fingerprint",
        RENDER,
        '        "transition": {"in": enter_fade_ms, "out": exit_fade_ms},\n',
        "",
        (T_TRANSITIONS, T_RENDER),
    ),
    # --- orientation ------------------------------------------------------------------------------
    Mutation(
        "process_flow: the horizontal layout is the vertical one",
        "orientation layout",
        LAYOUT,
        '    ("process_flow", "horizontal"): _flow_horizontal,',
        '    ("process_flow", "horizontal"): _flow_vertical,',
        (T_LAYOUT,),
    ),
    Mutation(
        "cycle: the vertical layout is the ring",
        "orientation layout",
        LAYOUT,
        '    ("cycle", "vertical"): _cycle_vertical,',
        '    ("cycle", "vertical"): _cycle_horizontal,',
        (T_LAYOUT,),
    ),
    Mutation(
        "hierarchy: the horizontal layout is the outline",
        "orientation layout",
        LAYOUT,
        '    ("hierarchy", "horizontal"): _hierarchy_horizontal,',
        '    ("hierarchy", "horizontal"): _hierarchy_vertical,',
        (T_LAYOUT,),
    ),
    Mutation(
        "comparison: the vertical layout is side by side",
        "orientation layout",
        LAYOUT,
        '    ("comparison", "vertical"): _comparison_vertical,',
        '    ("comparison", "vertical"): _comparison_horizontal,',
        (T_LAYOUT,),
    ),
    Mutation(
        "labelled_components: the horizontal layout is the stacked one",
        "orientation layout",
        LAYOUT,
        '    ("labelled_components", "horizontal"): _components_horizontal,',
        '    ("labelled_components", "horizontal"): _components_vertical,',
        (T_LAYOUT,),
    ),
    # --- limits and closed schema --------------------------------------------------------------------
    Mutation(
        "the process_flow node limit is relaxed",
        "visual limits",
        VISUAL,
        "PROCESS_FLOW_MAX_NODES: Final = 6",
        "PROCESS_FLOW_MAX_NODES: Final = 7",
        (T_VISUAL,),
    ),
    Mutation(
        "the hierarchy node limit is relaxed",
        "visual limits",
        VISUAL,
        "HIERARCHY_MAX_NODES: Final = 9",
        "HIERARCHY_MAX_NODES: Final = 10",
        (T_VISUAL,),
    ),
    Mutation(
        "the label length limit is relaxed",
        "visual limits",
        VISUAL,
        "LABEL_MAX_CHARS: Final = 28",
        "LABEL_MAX_CHARS: Final = 29",
        (T_VISUAL,),
    ),
    Mutation(
        "the total text budget check is off by one",
        "visual limits",
        VISUAL,
        "        if self.used > TOTAL_TEXT_MAX_CHARS:",
        "        if self.used >= TOTAL_TEXT_MAX_CHARS:",
        (T_VISUAL,),
    ),
    Mutation(
        "unknown keys are accepted",
        "closed schema",
        VISUAL,
        "    if unknown:\n",
        "    if False:\n",
        (T_VISUAL, T_SCHEMA),
    ),
    Mutation(
        "character validation is removed",
        "closed schema",
        VISUAL,
        "    if not is_drawable(value):\n",
        "    if False:\n",
        (T_VISUAL, T_SCHEMA),
    ),
    Mutation(
        "a hierarchy node may be its own parent",
        "closed schema",
        VISUAL,
        '                parent = _index(node["parent"], f"{where}.parent", below=index)',
        '                parent = _index(node["parent"], f"{where}.parent", below=index + 1)',
        (T_VISUAL, T_SCHEMA),
    ),
    Mutation(
        "before_after may have any number of columns",
        "closed schema",
        VISUAL,
        '        if relation == "before_after" and len(items) != 2:',
        '        if False and relation == "before_after" and len(items) != 2:',
        (T_VISUAL, T_SCHEMA),
    ),
    Mutation(
        "the model-supplied visual_dropped flag is trusted",
        "closed schema",
        VISUAL,
        '    cleaned = {key: value for key, value in illustration.items() if key != "visual_dropped"}',
        "    cleaned = dict(illustration)",
        (T_VISUAL, T_STORYBOARD),
    ),
    Mutation(
        "the LLM schema hard-codes a length instead of using the shared constant",
        "llm schema",
        SCHEMA,
        '        "maxLength": max_chars,',
        '        "maxLength": 28,',
        (T_SCHEMA,),
    ),
    # --- provenance ---------------------------------------------------------------------------------------
    Mutation(
        "text nested in lists inside a visual is not checked",
        "provenance",
        PROVENANCE,
        "        for index, item in enumerate(value):\n",
        "        for index, item in enumerate([]):\n",
        (T_PROVENANCE, "tests/test_domain_storyboard_provenance.py"),
    ),
    Mutation(
        "numeric JSON leaves bypass provenance",
        "provenance",
        PROVENANCE,
        "    for slot_path, number in _iter_numeric_slots(content):\n",
        "    for slot_path, number in []:\n",
        (T_PROVENANCE, T_STORYBOARD),
    ),
    Mutation(
        "any string under an enum key is skipped, not only the allowed values",
        "provenance",
        PROVENANCE,
        "        if in_visual and key in ENUM_FIELDS and value in ENUM_FIELDS[key]:",
        "        if in_visual and key in ENUM_FIELDS:",
        (T_PROVENANCE,),
    ),
    Mutation(
        "integer indices are exempt from provenance everywhere, not only inside a visual",
        "provenance",
        PROVENANCE,
        "        if in_visual and key in STRUCTURAL_INDEX_FIELDS and isinstance(value, int):",
        "        if key in STRUCTURAL_INDEX_FIELDS and isinstance(value, int):",
        (T_PROVENANCE,),
    ),
    # --- layout invariants ----------------------------------------------------------------------------------
    Mutation(
        "overlapping boxes are allowed",
        "layout",
        LAYOUT,
        "            if box.rect.intersects(boxes[other_index].rect):",
        "            if False:",
        (T_LAYOUT,),
    ),
    Mutation(
        "a connector starts short of its box (detached)",
        "layout",
        LAYOUT,
        "        x = cx + (half_w if dx > 0 else -half_w)",
        "        x = cx + (half_w + 9 if dx > 0 else -half_w)",
        (T_LAYOUT,),
    ),
    Mutation(
        "type may shrink below a role's minimum size",
        "layout",
        LAYOUT,
        "        return self.units.u(max(smallest, base * self.percent // 100))",
        "        return self.units.u(base * self.percent // 100)",
        (T_LAYOUT,),
    ),
    # --- timing / status ---------------------------------------------------------------------------------------
    Mutation(
        "the prefix sum drifts by a millisecond per item",
        "timing",
        TIMING,
        "        starts.append(cursor)\n        cursor += duration\n    return tuple(starts)",
        "        starts.append(cursor)\n        cursor += duration + 1\n    return tuple(starts)",
        (T_TIMING, T_RENDER),
    ),
    Mutation(
        "the read model shows the stored (stale) start_ms",
        "timing",
        STORYBOARD,
        "                start_ms=timeline.scene_starts_ms[index][position],",
        "                start_ms=scene.start_ms,",
        (T_STORYBOARD, T_STORYBOARD_API),
    ),
    Mutation(
        "timings are always reported authoritative",
        "timings_authoritative",
        TIMING,
        "        authoritative=(not narration_in_flight) and all(segment.settled for segment in segments),",
        "        authoritative=True,",
        (T_TIMING, T_STORYBOARD, T_STORYBOARD_API),
    ),
    Mutation(
        "narration in flight is ignored when reporting timings authoritative",
        "timings_authoritative",
        TIMING,
        "        authoritative=(not narration_in_flight) and all(segment.settled for segment in segments),",
        "        authoritative=all(segment.settled for segment in segments),",
        (T_TIMING, T_STORYBOARD),
    ),
    # --- dropped visuals ------------------------------------------------------------------------------------------
    Mutation(
        "the job's dropped_visual_count is not incremented",
        "dropped visuals",
        STORYBOARD,
        "                dropped_visuals += dropped_here\n",
        "                dropped_visuals += 0\n",
        (T_STORYBOARD,),
    ),
    Mutation(
        "the status's dropped_visual_count is always zero",
        "dropped visuals",
        STORYBOARD,
        "        dropped_visual_count=sum(\n",
        "        dropped_visual_count=0 * sum(\n",
        (T_STORYBOARD, T_STORYBOARD_API),
    ),
    # --- transitions -----------------------------------------------------------------------------------------------------
    Mutation(
        "the fade-in is applied inside a topic as well as at its boundary",
        "transitions",
        RENDER,
        "        opens_topic = position == 0 or topics[position] != topics[position - 1]",
        "        opens_topic = True",
        (T_TRANSITIONS, T_RENDER),
    ),
    Mutation(
        "the fade-out is applied inside a topic as well as at its boundary",
        "transitions",
        RENDER,
        "        closes_topic = position == len(pieces) - 1 or topics[position] != topics[position + 1]",
        "        closes_topic = True",
        (T_TRANSITIONS,),
    ),
    Mutation(
        "the fade at a topic boundary is removed (only the video edges fade)",
        "transitions",
        RENDER,
        "        opens_topic = position == 0 or topics[position] != topics[position - 1]",
        "        opens_topic = position == 0",
        (T_TRANSITIONS,),
    ),
    Mutation(
        "the fade-out at a topic boundary is removed",
        "transitions",
        RENDER,
        "        closes_topic = position == len(pieces) - 1 or topics[position] != topics[position + 1]",
        "        closes_topic = position == len(pieces) - 1",
        (T_TRANSITIONS,),
    ),
    Mutation(
        "the fade length bound is not enforced",
        "transitions",
        RENDER,
        "        if not 0 <= value <= MAX_FADE_MS:",
        "        if False:",
        (T_TRANSITIONS,),
    ),
    Mutation(
        "a scene that cuts in does not bring its content in already",
        "transitions",
        COMPOSITOR,
        "                layer.start_ms -= anim.CUT_IN_SHIFT_MS",
        "                layer.start_ms -= 0",
        (T_ANIMATION,),
    ),
    # --- drawing -----------------------------------------------------------------------------------------------------------
    Mutation(
        "a connector is drawn from the wrong end",
        "drawing",
        SCENE,
        '            direction = "right" if dx > 0 else "left"',
        '            direction = "left" if dx > 0 else "right"',
        (T_ANIMATION,),
    ),
    Mutation(
        "symbols the main font lacks fall back to nothing (dropped) instead of the other font",
        "glyphs",
        GLYPHS,
        '    if other(character):\n        return "fallback"\n',
        "    if other(character):\n        return None\n",
        (T_GLYPHS, T_GLYPH_DRAW),
    ),
)


def run_tests(tests: tuple[str, ...], keyword: str | None) -> tuple[bool, str]:
    command = ["uv", "run", "--frozen", "pytest", "-q", "-x", "-p", "no:cacheprovider", *tests]
    if keyword:
        command += ["-k", keyword]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    tail = [line for line in result.stdout.splitlines() if line.strip()]
    return result.returncode == 0, (tail[-1] if tail else "")


def main(selected: str | None) -> int:
    survivors: list[str] = []
    killed = 0
    for mutation in MUTATIONS:
        if selected and selected not in mutation.name and selected not in mutation.area:
            continue
        path = SRC / mutation.file
        original = path.read_bytes()
        text = original.decode("utf-8").replace("\r\n", "\n")
        if mutation.old not in text:
            print(f"?? cannot apply (source changed): {mutation.name}")
            survivors.append(f"{mutation.name} (NOT APPLIED)")
            continue
        started = time.perf_counter()
        try:
            path.write_bytes(text.replace(mutation.old, mutation.new, 1).encode("utf-8"))
            passed, summary = run_tests(mutation.tests, mutation.keyword)
        finally:
            path.write_bytes(original)
        seconds = time.perf_counter() - started
        if passed:
            survivors.append(mutation.name)
            print(f"SURVIVED  [{mutation.area}] {mutation.name}  ({seconds:.0f}s)  {summary}")
        else:
            killed += 1
            print(f"killed    [{mutation.area}] {mutation.name}  ({seconds:.0f}s)  {summary}")
        sys.stdout.flush()
    print(f"\n{killed} killed, {len(survivors)} survived")
    for name in survivors:
        print(f"  SURVIVOR: {name}")
    return 1 if survivors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else None))

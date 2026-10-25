"""Targeted mutation checks for Phase 10.

Each mutation breaks the implementation in one specific, plausible way (a rounding change, a
dropped input from a fingerprint, a check removed) and runs the tests that should notice. A
mutation is *killed* if those tests fail; if they still pass, the tests have a hole and the
script exits non-zero. Every file is restored afterwards, whatever happens.

    uv run python scripts/mutation_check_phase10.py            # all
    uv run python scripts/mutation_check_phase10.py timing     # names containing "timing"

Needs `make up` (Postgres, Redis, MinIO) and ffmpeg on PATH for the service and engine tests.
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


DOMAIN = "domain/render.py"
SERVICE = "services/render.py"
TOOLS = "providers/renderer/ffmpeg_tools.py"

T_DOMAIN = "tests/test_domain_render.py"
T_SERVICE = "tests/test_services_render.py"
T_ENGINE = "tests/test_renderer_engine.py"

MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "frame conversion floors instead of rounding",
        "frame conversion",
        DOMAIN,
        "return (t_ms * fps + 500) // 1000",
        "return (t_ms * fps) // 1000",
        (T_DOMAIN,),
    ),
    Mutation(
        "chunk end frame rounded per scene (drift accumulates)",
        "timing",
        DOMAIN,
        "                end_frame=frame_at(end_ms, fps),",
        "                end_frame=frame_at(start_ms, fps) + round(duration_ms * fps / 1000),",
        (T_DOMAIN,),
    ),
    Mutation(
        "timeline built from stored scene offsets, not the current narration",
        "timing",
        DOMAIN,
        "        start_ms, end_ms = cursor, cursor + duration_ms",
        "        start_ms, end_ms = scene.start_ms, scene.start_ms + duration_ms",
        (T_DOMAIN,),
    ),
    Mutation(
        "a gap between segments is tolerated",
        "timing gaps",
        DOMAIN,
        "        if scene.start_ms != cursor:",
        "        if False:",
        (T_DOMAIN,),
    ),
    Mutation(
        "scenes no longer have to add up to the narration",
        "timing",
        DOMAIN,
        "    if sum(scene.duration_ms for scene in scenes) != segment.audio_duration_ms:",
        "    if False:",
        (T_DOMAIN,),
    ),
    Mutation(
        "narration placed by per-segment rounding (audio drift)",
        "timing",
        TOOLS,
        "            target = sample_at(placement.end_ms, sample_rate) - sample_at(\n                placement.start_ms, sample_rate\n            )",
        "            target = round((placement.end_ms - placement.start_ms) * sample_rate / 1000)",
        (T_ENGINE,),
    ),
    Mutation(
        "assembly keeps every frame of every chunk (no trim)",
        "timing overlaps",
        TOOLS,
        "        if chunk.keep_frames != chunk.canonical_frames:",
        "        if False:",
        (T_ENGINE,),
    ),
    Mutation(
        "quality left out of the chunk fingerprint",
        "quality invalidation",
        DOMAIN,
        '        "size": output_size(orientation, quality),\n        "fps": fps,\n        "encoder": encoder,\n        "encoding": profile.fingerprint_fields(),',
        '        "fps": fps,\n        "encoder": encoder,',
        (T_DOMAIN,),
    ),
    Mutation(
        "orientation left out of the chunk fingerprint",
        "orientation invalidation",
        DOMAIN,
        '        "orientation": orientation,\n        "size": output_size(orientation, quality),',
        '        "size": output_size("horizontal", quality),',
        (T_DOMAIN,),
    ),
    Mutation(
        "scene content left out of the chunk fingerprint (stale chunk accepted)",
        "dependency invalidation",
        DOMAIN,
        '        "template": template,\n        "content": content,\n        "duration_ms": duration_ms,',
        '        "template": template,\n        "duration_ms": duration_ms,',
        (T_DOMAIN, T_SERVICE),
    ),
    Mutation(
        "scene duration left out of the chunk fingerprint (re-narration missed)",
        "narration invalidation",
        DOMAIN,
        '        "duration_ms": duration_ms,\n        "canonical_frames": canonical_frames(duration_ms, fps),',
        '        "canonical_frames": 0,',
        (T_DOMAIN,),
    ),
    Mutation(
        "renderer version left out of the fingerprint (old renderer's chunks reused)",
        "renderer version",
        DOMAIN,
        '        "renderer": RENDERER_VERSION,\n        "template": template,',
        '        "template": template,',
        (T_DOMAIN,),
    ),
    Mutation(
        "a chunk's fingerprint depends on where it sits (everything after an edit re-renders)",
        "partial rerender",
        DOMAIN,
        "                    duration_ms=duration_ms,\n                    orientation=orientation,\n                    quality=quality,\n                    encoder=encoder,\n                    fps=fps,\n                    enter_fade_ms=enter_fade_ms,\n                    exit_fade_ms=exit_fade_ms,\n                ),",
        "                    duration_ms=duration_ms + start_ms,\n                    orientation=orientation,\n                    quality=quality,\n                    encoder=encoder,\n                    fps=fps,\n                    enter_fade_ms=enter_fade_ms,\n                    exit_fade_ms=exit_fade_ms,\n                ),",
        (T_DOMAIN,),
    ),
    Mutation(
        "everything is re-rendered every time",
        "partial rerender",
        DOMAIN,
        "        if chunk.chunk_hash in valid_hashes or chunk.chunk_hash in seen:",
        "        if chunk.chunk_hash in seen:",
        (T_DOMAIN,),
    ),
    Mutation(
        "stored chunks are never reused by the job",
        "chunk reuse",
        SERVICE,
        "    return {h: record for h, record in stored.items() if h not in bad}",
        "    return {}",
        (T_SERVICE,),
        keyword="edit or renarrat or unchanged or crash or failed_chunk or damaged or assembly",
    ),
    Mutation(
        "a damaged stored chunk is trusted",
        "stale artifact detection",
        SERVICE,
        "        return size == record.byte_size",
        "        return True",
        (T_SERVICE,),
        keyword="damaged",
    ),
    Mutation(
        "a stale scene is reported as ready",
        "stale artifact detection",
        DOMAIN,
        '    if last_output_hash is not None and last_output_hash != current_hash:\n        return "stale"',
        '    if last_output_hash is not None and last_output_hash != current_hash:\n        return "ready"',
        (T_DOMAIN, T_SERVICE),
    ),
    Mutation(
        "an out-of-date video is reported as complete",
        "final completeness gate",
        SERVICE,
        "    output_is_current = output is not None and output.output_hash == plan.output_hash",
        "    output_is_current = output is not None",
        (T_SERVICE,),
    ),
    Mutation(
        "a second concurrent render is allowed",
        "concurrent job protection",
        SERVICE,
        '    if active is not None:\n        raise ConflictError(\n            "A video render is already queued or running',
        '    if False:\n        raise ConflictError(\n            "A video render is already queued or running',
        (T_SERVICE,),
    ),
    Mutation(
        "stale narration no longer blocks a render",
        "narration gate",
        SERVICE,
        "        if audio.content_hash != expected_hash:",
        "        if False:",
        (T_SERVICE,),
    ),
    Mutation(
        "an incomplete storyboard no longer blocks a render",
        "storyboard gate",
        SERVICE,
        "    if not storyboard.is_complete:",
        "    if False:",
        (T_SERVICE,),
    ),
    Mutation(
        "disk space is not checked",
        "pre-flight check",
        SERVICE,
        "    if free < needed:",
        "    if False:",
        (T_SERVICE,),
    ),
    Mutation(
        "the idempotent replay is skipped",
        "idempotency",
        SERVICE,
        "    if existing_job is not None:\n        return RenderEnqueued(",
        "    if False:\n        return RenderEnqueued(",
        (T_SERVICE,),
    ),
    Mutation(
        "one failed chunk discards the rest",
        "failure isolation",
        SERVICE,
        "                dest.unlink(missing_ok=True)\n                failures.append(",
        "                raise\n                failures.append(",
        (T_SERVICE,),
        keyword="failed_chunk",
    ),
    Mutation(
        "a video is assembled from an incomplete set of chunks",
        "final completeness gate",
        SERVICE,
        "        if failures:\n            count = len(failures)",
        "        if False:\n            count = len(failures)",
        (T_SERVICE,),
        keyword="failed_chunk",
    ),
    Mutation(
        "narration is re-recorded by a render (audio row rewritten)",
        "narration untouched",
        SERVICE,
        '    await progress.report("checking_stored_sections")',
        "    await progress.report(\"checking_stored_sections\")\n    await db.run(lambda s: s.execute(__import__('sqlalchemy').text('UPDATE script_segment_audio SET voice_model = voice_model || 1')))",
        (T_SERVICE,),
    ),
)


def run_tests(tests: tuple[str, ...], keyword: str | None) -> tuple[bool, str]:
    command = ["uv", "run", "pytest", "-q", "-x", "-p", "no:cacheprovider", *tests]
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

"""Phase 11 end-to-end verification: procedural visuals, transitions and timing on a real project.

Not part of the test suite (pytest's ``testpaths`` is ``tests``). It drives the same
``edutoon.services`` code the API and worker run, against the real local Postgres, MinIO, Piper
voice, FFmpeg and Pillow renderer - reusing the harness of ``verify_phase10.py``. The one thing
standing in for a real provider is the storyboard *scene composer* (no LLM key is used or needed):
a deterministic stand-in that emits scenes carrying procedural visuals of all five forms, text
fallbacks, symbols and deliberately invalid visuals; everything after it (validation, the drop of
invalid visuals, provenance, timing, planning, rendering, assembly) is the real code.

Checks (each prints PASS/FAIL and lands in ``report.json``):

  1  narrated script with procedural visuals   6  deterministic repeated render
  2  all five visual forms present             7  partial re-render (one visual edited)
  3  horizontal and vertical outputs           8  narration re-synthesis invalidation
  4  exact timing (frames, durations)          9  fallbacks (dropped visuals) and glyph frames
  5  no scene gaps or overlaps                 10 same-topic cuts and topic-boundary fades

plus performance figures. Outputs go to ``var/phase11-verification/`` (git-ignored).

Run from ``apps/api`` (needs ``ffmpeg`` on PATH, ``make voices`` done and ``make up`` running):

    uv run python scripts/verify_phase11.py                      # ~2.5 minute project, every check
    uv run python scripts/verify_phase11.py --minutes 10 --perf-only   # the 10 minute cap, timing only
"""

# ruff: noqa: E501
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import numpy as np
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify_phase10 as p10  # noqa: E402
from verify_phase10 import (  # noqa: E402
    Ctx,
    Recorder,
    audio_md5,
    describe_video,
    fetch_output,
    narrate,
    say,
    storyboard,
)

from edutoon.core.config import get_settings  # noqa: E402
from edutoon.db.database import dispose_engine  # noqa: E402
from edutoon.db.session import get_sessionmaker  # noqa: E402
from edutoon.domain import render as render_domain  # noqa: E402
from edutoon.domain import visual as visual_domain  # noqa: E402
from edutoon.domain.storyboard_provenance import check_provenance  # noqa: E402
from edutoon.domain.storyboard_template import validate_scene_content  # noqa: E402
from edutoon.providers.llm import (  # noqa: E402
    StoryboardSceneDraft,
    StoryboardSceneSetDraft,
    TokenUsage,
)
from edutoon.providers.renderer.compositor import SceneCompositor, background  # noqa: E402
from edutoon.providers.storage import get_storage_client  # noqa: E402
from edutoon.repositories import project_topics as topics_repo  # noqa: E402
from edutoon.repositories import render as render_repo  # noqa: E402
from edutoon.repositories import script_segment_audio as audio_repo  # noqa: E402
from edutoon.repositories import script_segment_scenes as scenes_repo  # noqa: E402
from edutoon.repositories import scripts as scripts_repo  # noqa: E402
from edutoon.services import projects as projects_service  # noqa: E402
from edutoon.services import render as render_service  # noqa: E402
from edutoon.services import script_audio as script_audio_service  # noqa: E402
from edutoon.services import storyboard as storyboard_service  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "var" / "phase11-verification"
VOICES = p10.VOICES

# Phase 10's measured baseline (apps/api/README.md, "Video rendering"), 10-minute high horizontal.
PHASE10_BASELINE = {"seconds": 102.66, "peak_rss_mb": 1223.9, "peak_rss_over_start_mb": 729.2}

# --- the stand-in scene composer -----------------------------------------------------------------
# Every builder returns content that carries no number, unit, month, currency or proper noun, so it
# passes the provenance rule whatever the narration says (checked at start-up against an EMPTY
# narration). Nothing here is a claim; it is shape and words for the renderer to draw.


def _flow() -> tuple[str, dict[str, Any]]:
    visual = {
        "type": "process_flow",
        "title": "How a leaf makes sugar",
        "focus": 2,
        "nodes": [
            {"label": "Light is absorbed", "detail": "by chlorophyll"},
            {"label": "Water is split"},
            {"label": "Sugar is built", "detail": "from carbon dioxide"},
            {"label": "Oxygen is released"},
        ],
    }
    return "labelled_diagram", {
        "illustration": {"description": "How a leaf works", "visual": visual}
    }


def _cycle() -> tuple[str, dict[str, Any]]:
    visual = {
        "type": "cycle",
        "title": "The food chain",
        "nodes": [
            {"label": "Sunlight"},
            {"label": "Plants grow"},
            {"label": "Animals eat plants", "detail": "and store the energy"},
            {"label": "Food returns to soil"},
        ],
    }
    return "full_bleed_illustration", {
        "illustration": {"description": "The food chain", "visual": visual},
        "caption": "Energy moves round in a loop",
    }


def _hierarchy() -> tuple[str, dict[str, Any]]:
    visual = {
        "type": "hierarchy",
        "title": "Parts of a plant",
        "nodes": [
            {"label": "Plants"},
            {"label": "Leaves", "parent": 0},
            {"label": "Stems", "parent": 0},
            {"label": "Roots", "parent": 0},
            {"label": "Stomata", "parent": 1},
            {"label": "Chlorophyll", "parent": 1},
        ],
    }
    return "labelled_diagram", {
        "illustration": {"description": "Parts of a plant", "visual": visual}
    }


def _comparison() -> tuple[str, dict[str, Any]]:
    visual = {
        "type": "comparison",
        "relation": "versus",
        "title": "A plant with and without light",
        "columns": [
            {"heading": "With light", "points": ["Makes sugar", "Grows well", "Stays green"]},
            {
                "heading": "Without light",
                "points": ["Makes no sugar", "Weakens slowly", "Turns pale"],
            },
        ],
    }
    return "full_bleed_illustration", {
        "illustration": {"description": "Light and growth", "visual": visual}
    }


def _before_after() -> tuple[str, dict[str, Any]]:
    visual = {
        "type": "comparison",
        "relation": "before_after",
        "columns": [
            {"heading": "In the dark", "points": ["No sugar", "Slow growth"]},
            {"heading": "In the light", "points": ["Sugar made", "Fast growth"]},
        ],
    }
    return "labelled_diagram", {"illustration": {"description": "Dark and light", "visual": visual}}


def _hub() -> tuple[str, dict[str, Any]]:
    visual = {
        "type": "labelled_components",
        "form": "hub",
        "subject": "A leaf",
        "parts": [
            {"label": "Chlorophyll", "detail": "catches the light"},
            {"label": "Stomata", "detail": "tiny openings"},
            {"label": "Veins", "detail": "carry water"},
            {"label": "Thin blade"},
        ],
    }
    return "labelled_diagram", {"illustration": {"description": "A leaf", "visual": visual}}


def _layers() -> tuple[str, dict[str, Any]]:
    visual = {
        "type": "labelled_components",
        "form": "layers",
        "subject": "A plant",
        "parts": [{"label": "Leaves"}, {"label": "Stem"}, {"label": "Roots"}],
    }
    return "split_text_illustration", {
        "text": "A plant is built in layers.",
        "illustration": {"description": "A plant", "visual": visual},
    }


def _symbols() -> tuple[str, dict[str, Any]]:
    visual = {
        "type": "process_flow",
        "title": "What plants do",
        "nodes": [
            {"label": "CO₂ + H₂O → sugar"},
            {"label": "light ≤ enough ✓"},
            {"label": "sugar → starch"},
        ],
    }
    return "labelled_diagram", {"illustration": {"description": "What plants do", "visual": visual}}


def _title() -> tuple[str, dict[str, Any]]:
    return "title_card", {"title": "Photosynthesis in a leaf", "subtitle": "how light becomes food"}


def _bullets() -> tuple[str, dict[str, Any]]:
    return "bullet_points", {
        "title": "Key ideas",
        "bullets": ["Leaves catch light", "Roots bring water", "Sugar feeds the plant"],
    }


def _formula() -> tuple[str, dict[str, Any]]:
    return "formula_or_code", {
        "content": "light + water + carbon dioxide → sugar + oxygen ✓",
        "kind": "formula",
        "label": "In words",
    }


def _fallback_parts() -> tuple[str, dict[str, Any]]:
    return "labelled_diagram", {
        "illustration": {"description": "The parts of a leaf", "subject_labels": ["Leaf parts"]},
        "labels": [
            {"text": "Blade", "position": "top"},
            {"text": "Veins", "position": "left"},
            {"text": "Stomata", "position": "right"},
        ],
    }


def _fallback_statement() -> tuple[str, dict[str, Any]]:
    return "full_bleed_illustration", {
        "illustration": {"description": "Light becomes food", "subject_labels": ["light", "sugar"]},
        "caption": "Light becomes food",
    }


def _fallback_text() -> tuple[str, dict[str, Any]]:
    return "split_text_illustration", {
        "text": "Plants store extra sugar as starch in roots, stems and seeds.",
        "illustration": {
            "description": "Where sugar is stored",
            "subject_labels": ["starch", "seeds"],
        },
    }


def _dropped_invalid() -> tuple[str, dict[str, Any]]:
    invalid = {"type": "cycle", "nodes": [{"label": "Lonely step"}]}  # a cycle needs three steps
    return "full_bleed_illustration", {
        "illustration": {"description": "A loop", "visual": invalid},
        "caption": "A loop needs more steps",
    }


def _dropped_too_big() -> tuple[str, dict[str, Any]]:
    big = {
        "type": "process_flow",
        "nodes": [{"label": w} for w in ("Sun", "Leaf", "Stem", "Root", "Seed")],
    }
    return "split_text_illustration", {
        "text": "These steps do not fit beside a sentence.",
        "illustration": {"description": "Many steps", "visual": big},
    }


BUILDERS = [
    _flow, _cycle, _hierarchy, _comparison, _hub, _layers, _before_after, _symbols,
    _title, _bullets, _formula, _fallback_parts, _fallback_statement, _fallback_text,
    _dropped_invalid, _dropped_too_big,
]  # fmt: skip
DROPPED_BUILDERS = {_dropped_invalid, _dropped_too_big}
SYMBOL_BUILDERS = {_symbols, _formula}


class VisualComposer:
    """Stands in for ``LLM.compose_storyboard_scenes``: cycles through the builders above."""

    def __init__(self) -> None:
        self.calls: list[int] = []
        self.emitted: dict[tuple[int, int], str] = {}

    async def compose_storyboard_scenes(
        self, segment: Any, *, max_scenes: int
    ) -> tuple[StoryboardSceneSetDraft, TokenUsage]:
        self.calls.append(segment.segment_index)
        count = min(max_scenes, 2 + segment.segment_index % 2)
        # Scene n of segment s always gets the same builder, so re-composing a segment after its
        # narration changes gives it the same kinds of scene (only the durations differ).
        first = 2 * segment.segment_index + segment.segment_index // 2
        scenes = []
        for n in range(count):
            builder = BUILDERS[(first + n) % len(BUILDERS)]
            template, content = builder()
            self.emitted[(segment.segment_index, n)] = builder.__name__
            scenes.append(StoryboardSceneDraft(template=template, content=content))
        return StoryboardSceneSetDraft(scenes=tuple(scenes)), TokenUsage(
            input_tokens=1, output_tokens=1
        )


def self_check_builders() -> None:
    """Every builder must be honest under an EMPTY narration (no protected token), and every one
    except the deliberately invalid two must satisfy its template's contract."""
    for builder in BUILDERS:
        template, content = builder()
        violations = check_provenance(content, "")
        assert not violations, f"{builder.__name__} carries a protected token: {violations}"
        if builder in DROPPED_BUILDERS:
            cleaned, dropped = visual_domain.without_invalid_visual(template, content)
            assert dropped, f"{builder.__name__} should be dropped"
            validate_scene_content(template, cleaned)
        else:
            validate_scene_content(template, content)


# --- helpers -----------------------------------------------------------------------------------------


class Checks:
    def __init__(self) -> None:
        self.results: dict[str, dict[str, Any]] = {}

    def record(self, name: str, passed: bool, **details: Any) -> None:
        self.results[name] = {"passed": bool(passed), **details}
        say(f"  {'PASS' if passed else 'FAIL'}  {name}" + (f"  {details}" if details else ""))

    @property
    def failed(self) -> list[str]:
        return [name for name, r in self.results.items() if not r["passed"]]


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def assign_topics(
    ctx: Ctx, project_id: UUID, script_id: UUID, per_topic: int = 3
) -> list[str]:
    """Group the script's segments into topics of ``per_topic`` segments (the topic id per segment)."""
    async with ctx.sessionmaker() as session:
        segments = await scripts_repo.list_segments(
            session, project_id=project_id, script_id=script_id
        )
        topics: dict[int, UUID] = {}
        keys: list[str] = []
        for segment in segments:
            group = segment.segment_index // per_topic
            if group not in topics:
                topics[group] = (
                    await topics_repo.create(
                        session, project_id=project_id, title=f"Topic {group + 1}", position=group
                    )
                ).id
            await session.execute(
                text("UPDATE script_segments SET topic_id = :t WHERE id = :s"),
                {"t": topics[group], "s": segment.id},
            )
            keys.append(str(topics[group]))
        await session.commit()
    return keys


async def status_of(ctx: Ctx, project_id: UUID, script_id: UUID, orientation: str, quality: str):
    async with ctx.sessionmaker() as session:
        assert ctx.user_id is not None
        return await render_service.get_render_status(
            session,
            project_id=project_id,
            owner_id=ctx.user_id,
            script_id=script_id,
            orientation=orientation,
            quality=quality,
        )


async def storyboard_status(ctx: Ctx, project_id: UUID, script_id: UUID):
    async with ctx.sessionmaker() as session:
        assert ctx.user_id is not None
        return await storyboard_service.get_storyboard_status(
            session, project_id=project_id, owner_id=ctx.user_id, script_id=script_id
        )


def extract_gray_frames(
    path: Path, indices: list[int], width: int, height: int
) -> dict[int, np.ndarray]:
    """The requested frames (by index) of ``path`` as grey arrays, in one FFmpeg pass."""
    wanted = sorted(set(indices))
    expression = "+".join(f"eq(n\\,{i})" for i in wanted)
    raw = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-vf",
            f"select='{expression}',format=gray",
            "-fps_mode",
            "passthrough",
            "-f",
            "rawvideo",
            "-",
        ],
        capture_output=True,
        check=True,
    ).stdout
    size = width * height
    assert len(raw) == size * len(wanted), (len(raw), size, len(wanted))
    return {
        i: np.frombuffer(raw[k * size : (k + 1) * size], dtype=np.uint8).reshape(height, width)
        for k, i in enumerate(wanted)
    }


def save_frame(path: Path, index: int, dest: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(path),
            "-vf",
            f"select='eq(n\\,{index})'",
            "-fps_mode",
            "passthrough",
            "-frames:v",
            "1",
            str(dest),
        ],
        check=True,
    )


def dist(frame: np.ndarray, bg: np.ndarray) -> float:
    return float(np.abs(frame.astype(int) - bg.astype(int)).mean())


# --- performance micro-measurements (in process, no FFmpeg) ---------------------------------------------


def visual_costs() -> dict[str, Any]:
    """MEASURED: compositor setup and per-frame cost of each visual form and of the text templates,
    for a 6 second scene at 1920x1080 / 1080x1920 (the same measurement made in Stage A)."""
    forms = {
        "process_flow": _flow(),
        "cycle": _cycle(),
        "hierarchy": _hierarchy(),
        "comparison": _comparison(),
        "hub": _hub(),
        "layers": _layers(),
        "title_card (text only)": _title(),
        "bullet_points (text only)": _bullets(),
        "fallback statement": _fallback_statement(),
    }
    frames = render_domain.canonical_frames(6000)
    out: dict[str, Any] = {}
    for orientation, (w, h) in (("horizontal", (1920, 1080)), ("vertical", (1080, 1920))):
        for name, (template, content) in forms.items():
            # warm the font cache once so the first template is not charged for it
            started = time.perf_counter()
            compositor = SceneCompositor(
                template=template,
                content=content,
                orientation=orientation,
                width=w,
                height=h,
                duration_ms=6000,
            )
            setup = time.perf_counter() - started
            drawn, previous = 0, None
            started = time.perf_counter()
            for i in range(frames):
                t = i * 1000 / render_domain.FPS
                key = compositor.state_key(t)
                if key != previous:
                    compositor.frame(t).tobytes()
                    previous = key
                    drawn += 1
            draw = time.perf_counter() - started
            out[f"{orientation}/{name}"] = {
                "setup_ms": round(setup * 1000),
                "layers": len(compositor.layers),
                "distinct_frames": drawn,
                "distinct_percent": round(100 * drawn / frames, 1),
                "frame_draw_ms_total": round(draw * 1000),
                "ms_per_distinct_frame": round(1000 * draw / max(drawn, 1), 1),
            }
    return out


# --- main ---------------------------------------------------------------------------------------------------


async def main(minutes: float, keep: bool, perf_only: bool) -> int:
    settings = get_settings()
    OUT.mkdir(parents=True, exist_ok=True)
    self_check_builders()
    storage = get_storage_client(
        endpoint_url=settings.STORAGE_ENDPOINT_URL,
        access_key_id=settings.STORAGE_ACCESS_KEY_ID,
        secret_access_key=settings.STORAGE_SECRET_ACCESS_KEY,
    )
    composer = VisualComposer()
    ctx = Ctx(get_sessionmaker(), storage, Recorder(), composer, {})  # type: ignore[arg-type]
    report = ctx.report
    checks = Checks()
    report["settings"] = {
        "fade_in_ms": settings.RENDER_FADE_IN_MS,
        "fade_out_ms": settings.RENDER_FADE_OUT_MS,
        "renderer_version": render_domain.RENDERER_VERSION,
    }
    report["machine"] = {
        "cpu": "i7-12700H (14 cores / 20 threads)",
        "ffmpeg": subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, text=True
        ).stdout.splitlines()[0],
        "concurrency": settings.RENDER_CONCURRENCY,
    }

    segments = max(4, round(minutes * 60 / 12.5))
    say(f"== Phase 11 verification: {segments} segments (~{minutes} min), builders self-checked ==")
    project_id = await p10.make_project(ctx, "Phase 11 verification", segments)
    narration = await narrate(ctx, project_id)
    script_id = UUID(narration["script_id"])
    total_ms = int(narration["result"]["measured_duration_ms"])
    topic_keys = await assign_topics(ctx, project_id, script_id)
    report["narration"] = {
        "segments": segments,
        "seconds": narration["seconds"],
        "measured_duration_ms": total_ms,
        "voice": narration["result"]["voice_model"],
    }
    say(
        f"[narration] real Piper: {segments} segments, {total_ms / 1000:.1f}s of audio in {narration['seconds']}s; {len(set(topic_keys))} topics"
    )

    sb = await storyboard(ctx, project_id, script_id)
    sb_status = await storyboard_status(ctx, project_id, script_id)
    all_scenes = [scene for seg in sb_status.segments for scene in seg.scenes]
    kinds: set[str] = set()
    for detail in all_scenes:
        visual = visual_domain.visual_of(detail.scene.content)
        if visual is not None:
            kinds.add(
                visual.type + (f":{visual.form}" if visual.type == "labelled_components" else "")
            )
    expected_dropped = sum(
        1 for name in composer.emitted.values() if name in {"_dropped_invalid", "_dropped_too_big"}
    )
    report["storyboard"] = {
        "composer_calls": sb["composer_calls"],
        "scenes": len(all_scenes),
        "visual_forms": sorted(kinds),
        "dropped_visual_count": sb_status.dropped_visual_count,
        "timings_authoritative": sb_status.timings_authoritative,
        "provider_calls": sb["result"].get("provider_calls"),
    }
    say(
        f"[storyboard] {len(all_scenes)} scenes; forms {sorted(kinds)}; dropped visuals {sb_status.dropped_visual_count} (expected {expected_dropped})"
    )
    say("== checks ==")
    checks.record(
        "1 real narrated script with procedural visuals",
        bool(kinds) and total_ms > 0,
        scenes=len(all_scenes),
        seconds_of_audio=round(total_ms / 1000, 1),
    )
    checks.record(
        "2 all five visual forms present",
        {
            "process_flow",
            "cycle",
            "hierarchy",
            "comparison",
            "labelled_components:hub",
            "labelled_components:layers",
        }
        <= kinds,
        forms=sorted(kinds),
    )
    checks.record(
        "9a invalid visuals are dropped, counted and never fail the storyboard",
        sb_status.dropped_visual_count == expected_dropped and sb_status.is_complete,
        dropped=sb_status.dropped_visual_count,
        expected=expected_dropped,
    )
    checks.record(
        "timings_authoritative is true once narration and storyboard are current",
        sb_status.timings_authoritative is True,
    )

    # ---- the renders ----------------------------------------------------------------------------------------------
    report["renders"] = {}
    files: dict[tuple[str, str], Path] = {}
    plan = (
        orientations := (
            ("horizontal", "high"),
            ("vertical", "high"),
            ("horizontal", "low"),
            ("vertical", "low"),
        )
    )
    del plan
    for orientation, quality in (
        orientations if not perf_only else (("horizontal", "high"), ("vertical", "high"))
    ):
        job, stats = await p10.render(ctx, project_id, script_id, orientation, quality)
        path = await fetch_output(
            ctx, project_id, script_id, orientation, quality, OUT / f"{orientation}-{quality}.mp4"
        )
        files[(orientation, quality)] = path
        described = describe_video(path, total_ms)
        described["job_result"] = {
            k: job.result[k]
            for k in ("chunk_total", "chunks_rendered", "chunks_reused", "assembly_seconds")
            if k in job.result
        }
        report["renders"][f"{orientation}-{quality}"] = {"stats": stats, "video": described}
        say(
            f"[render {orientation}/{quality}] {stats['seconds']}s chunks={stats['chunks_drawn']} {described['video']['width']}x{described['video']['height']} "
            f"{described['size_bytes'] / 1e6:.2f} MB frames={described['video_frames']}/{described['expected_frames']} "
            f"v-a={described['video_minus_narration_ms']}ms peakRSS={stats['peak_rss_mb']}MB (+{stats['peak_rss_over_start_mb']}) sysCPU={stats['system_cpu_percent_mean']}%"
        )
        ok = (
            described["video_frames"] == described["expected_frames"]
            and described["decodes_cleanly"]
            and len(described["pts_steps"]) == 1
            and abs(described["video_minus_narration_ms"]) <= 120
            and abs(described["audio_minus_narration_ms"]) <= 120
        )
        checks.record(
            f"3/4 {orientation}/{quality}: exact frames, clean decode, durations within tolerance",
            ok,
            frames=f"{described['video_frames']}/{described['expected_frames']}",
        )

    md5s = {k: audio_md5(v) for k, v in files.items()}
    if ("horizontal", "high") in md5s and ("vertical", "high") in md5s:
        checks.record(
            "3 the narration is identical in both orientations",
            md5s[("horizontal", "high")] == md5s[("vertical", "high")],
        )

    status = await status_of(ctx, project_id, script_id, "horizontal", "high")
    scenes = list(status.scenes)
    gaps = [
        (a.position, b.position)
        for a, b in zip(scenes, scenes[1:], strict=False)
        if a.start_ms + a.duration_ms != b.start_ms
    ]
    last = scenes[-1]
    checks.record(
        "5 no scene gaps or overlaps",
        not gaps and last.start_ms + last.duration_ms == total_ms and scenes[0].start_ms == 0,
        scenes=len(scenes),
        total_ms=total_ms,
    )
    report["timeline"] = {"scenes": len(scenes), "gaps": gaps, "total_ms": total_ms}

    if not perf_only:
        await extra_checks(
            ctx, checks, report, project_id, script_id, files, scenes, topic_keys, total_ms
        )

    # ---- performance -------------------------------------------------------------------------------------------------
    high = report["renders"].get("horizontal-high")
    if high:
        stats = high["stats"]
        minutes_of_video = total_ms / 60_000
        report["performance"] = {
            "horizontal_high": {
                "seconds": stats["seconds"],
                "chunks_drawn": stats["chunks_drawn"],
                "peak_rss_mb": stats["peak_rss_mb"],
                "peak_rss_over_start_mb": stats["peak_rss_over_start_mb"],
                "output_mb": round(high["video"]["size_bytes"] / 1e6, 1),
            },
            "video_minutes": round(minutes_of_video, 2),
            "seconds_per_video_minute": round(stats["seconds"] / minutes_of_video, 1),
            "phase10_baseline_10_minutes": PHASE10_BASELINE,
            "MEASURED_vs_baseline_if_10_minutes": (
                {
                    "seconds_delta": round(stats["seconds"] - PHASE10_BASELINE["seconds"], 1),
                    "seconds_percent": round(
                        100 * (stats["seconds"] / PHASE10_BASELINE["seconds"] - 1), 1
                    ),
                    "peak_rss_delta_mb": round(
                        stats["peak_rss_mb"] - PHASE10_BASELINE["peak_rss_mb"], 1
                    ),
                }
                if 9.0 <= minutes_of_video <= 11.0
                else "not a 10-minute project: compare seconds_per_video_minute (Phase 10: 10.7)"
            ),
        }
        say(f"[performance] {report['performance']}")
    report["visual_costs_MEASURED"] = visual_costs()
    for key in (
        "horizontal/process_flow",
        "horizontal/cycle",
        "horizontal/title_card (text only)",
        "horizontal/bullet_points (text only)",
        "vertical/process_flow",
    ):
        say(f"[cost] {key}: {report['visual_costs_MEASURED'][key]}")

    # ---- cleanup -----------------------------------------------------------------------------------------------------------
    if not keep:
        async with ctx.sessionmaker() as session:
            assert ctx.user_id is not None
            locations = await render_repo.list_storage_locations_for_project(session, project_id)
            doomed = await projects_service.delete_project(
                session, project_id=project_id, owner_id=ctx.user_id
            )
            await session.commit()
        await projects_service.remove_stored_files(storage, project_id=project_id, files=doomed)
        gone = [
            not await storage.object_exists(bucket=loc.bucket, key=loc.key) for loc in locations
        ]
        report["deletion"] = {"render_objects": len(locations), "all_removed": all(gone)}
        async with ctx.sessionmaker() as session:
            await session.execute(text("DELETE FROM users WHERE id = :i"), {"i": ctx.user_id})
            await session.commit()
    report["checks"] = checks.results
    (OUT / ("report-perf.json" if perf_only else "report.json")).write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    await dispose_engine()
    passed = sum(1 for r in checks.results.values() if r["passed"])
    say(f"\n{passed}/{len(checks.results)} checks passed")
    for name in checks.failed:
        say(f"  FAILED: {name}")
    return 1 if checks.failed else 0


async def extra_checks(
    ctx: Ctx,
    checks: Checks,
    report: dict[str, Any],
    project_id: UUID,
    script_id: UUID,
    files: dict[tuple[str, str], Path],
    scenes: list[Any],
    topic_keys: list[str],
    total_ms: int,
) -> None:
    assert ctx.user_id is not None
    storage = ctx.storage
    settings = get_settings()

    # ---- 10: same-topic cuts and topic-boundary fades, measured on the finished video ---------------------------------
    low = files[("horizontal", "low")]
    width, height = render_domain.output_size("horizontal", "low")
    starts = [render_domain.frame_at(s.start_ms) for s in scenes]
    ends = starts[1:] + [render_domain.frame_at(total_ms)]
    steady_at = [render_domain.frame_at(s.start_ms + round(0.85 * s.duration_ms)) for s in scenes]
    wanted = set(steady_at)
    for i in range(len(scenes)):
        wanted.add(starts[i])
        wanted.add(ends[i] - 1)
    frames = extract_gray_frames(low, sorted(wanted), width, height)
    # The bare background as the *video* holds it: frame 0 (the first scene fades in from nothing).
    # Comparing against Pillow's own background instead would mix in the limited-range YUV offset.
    bg = frames[starts[0]]
    same_last, same_first, topic_last, topic_first = [], [], [], []
    for i, scene in enumerate(scenes):
        steady = max(dist(frames[steady_at[i]], bg), 1e-6)
        last_ratio = dist(frames[ends[i] - 1], bg) / steady
        first_ratio = dist(frames[starts[i]], bg) / steady
        topic_here = topic_keys[scene.segment_index]
        opens = i == 0 or topic_keys[scenes[i - 1].segment_index] != topic_here
        closes = i == len(scenes) - 1 or topic_keys[scenes[i + 1].segment_index] != topic_here
        (topic_first if opens else same_first).append(first_ratio)
        (topic_last if closes else same_last).append(last_ratio)
    cuts_ok = min(same_last, default=1) >= 0.6 and min(same_first, default=1) >= 0.05
    fades_ok = max(topic_last, default=0) <= 0.4 and max(topic_first, default=0) <= 0.02
    report["transitions"] = {
        "same_topic_cuts": {
            "boundaries": len(same_first),
            "last_frame_min_ratio": round(min(same_last, default=0), 2),
            "first_frame_min_ratio": round(min(same_first, default=0), 3),
            "first_frame_ratios": sorted(round(r, 3) for r in same_first),
            "last_frame_ratios": sorted(round(r, 3) for r in same_last),
        },
        "topic_boundary_fades": {
            "boundaries": len(topic_first),
            "last_frame_max_ratio": round(max(topic_last, default=0), 2),
            "first_frame_max_ratio": round(max(topic_first, default=0), 3),
            "first_frame_ratios": sorted(round(r, 3) for r in topic_first),
            "last_frame_ratios": sorted(round(r, 3) for r in topic_last),
        },
    }
    checks.record(
        "10a same-topic scenes are joined by straight cuts (content on both sides of the cut)",
        cuts_ok,
        **{
            k: v
            for k, v in report["transitions"]["same_topic_cuts"].items()
            if not k.endswith("ratios")
        },
    )
    checks.record(
        "10b topic boundaries and the video edges fade (out to dim, in from the bare background)",
        fades_ok,
        **{
            k: v
            for k, v in report["transitions"]["topic_boundary_fades"].items()
            if not k.endswith("ratios")
        },
    )

    # ---- 9b: fallbacks and glyphs on screen -----------------------------------------------------------------------------
    high = files.get(("horizontal", "high")) or low
    hw, hh = render_domain.output_size(
        "horizontal", "high" if high == files.get(("horizontal", "high")) else "low"
    )
    async with ctx.sessionmaker() as session:
        stored = {}
        for seg in await scripts_repo.list_segments(
            session, project_id=project_id, script_id=script_id
        ):
            for row in await scenes_repo.list_by_segment_id(session, seg.id):
                stored[(seg.segment_index, row.scene_index)] = row
    picks: dict[str, tuple[int, Any]] = {}
    for scene in scenes:
        row = stored[(scene.segment_index, scene.scene_index)]
        content = row.content
        illustration = (
            content.get("illustration") if isinstance(content.get("illustration"), dict) else {}
        )
        visual = visual_domain.visual_of(content)
        label = None
        if illustration.get("visual_dropped"):
            label = "fallback_dropped_visual"
        elif visual is None and row.template == "labelled_diagram":
            label = "fallback_parts"
        elif visual is None and row.template == "split_text_illustration":
            label = "fallback_text"
        elif visual is None and row.template == "full_bleed_illustration":
            label = "fallback_statement"
        elif visual is not None and "→" in json.dumps(content, ensure_ascii=False):
            label = "glyphs_in_a_visual"
        elif row.template == "formula_or_code" and "→" in json.dumps(content, ensure_ascii=False):
            label = "glyphs_in_a_formula"
        if label and label not in picks:
            picks[label] = (
                render_domain.frame_at(scene.start_ms + round(0.85 * scene.duration_ms)),
                scene,
            )
    saved: dict[str, str] = {}
    blank = []
    hbg = np.asarray(background(hw, hh).convert("L"))
    grabbed = extract_gray_frames(high, [i for i, _ in picks.values()], hw, hh) if picks else {}
    for label, (index, _scene) in picks.items():
        dest = OUT / f"frame-{label}.png"
        save_frame(high, index, dest)
        saved[label] = dest.name
        if dist(grabbed[index], hbg) < 3.0:
            blank.append(label)
    report["frames_saved"] = saved
    say(f"[frames] saved {sorted(saved)} to {OUT}")
    checks.record(
        "9b fallback and glyph scenes are drawn (not blank) in the finished video",
        len(picks) >= 5 and not blank,
        found=sorted(picks),
        blank=blank,
    )
    checks.record(
        "11 glyph scenes present (arrows, comparison signs, check marks; see saved frames)",
        {"glyphs_in_a_visual", "glyphs_in_a_formula"} <= set(picks),
        frames=[saved.get("glyphs_in_a_visual"), saved.get("glyphs_in_a_formula")],
    )

    # ---- 6: deterministic repeated render ------------------------------------------------------------------------------------
    before_sha = sha256_of(files[("horizontal", "low")])
    async with ctx.sessionmaker() as session:
        locations = await render_repo.list_storage_locations_for_project(session, project_id)
        chunk_hashes_before = {
            s.chunk_hash
            for s in (await status_of(ctx, project_id, script_id, "horizontal", "low")).scenes
        }
        await session.execute(
            text("DELETE FROM render_outputs WHERE project_id = :p"), {"p": project_id}
        )
        await session.execute(
            text("DELETE FROM render_chunks WHERE project_id = :p"), {"p": project_id}
        )
        await session.commit()
    for location in locations:
        await storage.delete_object(bucket=location.bucket, key=location.key)
    job, stats = await p10.render(ctx, project_id, script_id, "horizontal", "low")
    again = await fetch_output(
        ctx, project_id, script_id, "horizontal", "low", OUT / "horizontal-low-again.mp4"
    )
    after_sha = sha256_of(again)
    chunk_hashes_after = {
        s.chunk_hash
        for s in (await status_of(ctx, project_id, script_id, "horizontal", "low")).scenes
    }
    checks.record(
        "6 an identical render (all artefacts deleted first) is byte-identical",
        before_sha == after_sha
        and chunk_hashes_before == chunk_hashes_after
        and stats["chunks_drawn"] > 0,
        redrawn_chunks=stats["chunks_drawn"],
        sha=before_sha[:16],
    )
    report["determinism"] = {
        "first_sha256": before_sha,
        "second_sha256": after_sha,
        "chunks_redrawn": stats["chunks_drawn"],
    }

    # ---- unchanged re-render does no work ----------------------------------------------------------------------------------------
    job, stats = await p10.render(ctx, project_id, script_id, "horizontal", "low")
    checks.record(
        "an unchanged re-render draws nothing and reuses the output",
        stats["chunks_drawn"] == 0 and job.result.get("output_reused") is True,
    )

    # ---- 7: partial re-render: edit ONE visual ---------------------------------------------------------------------------------------
    target = next(
        s
        for s in scenes
        if visual_domain.visual_of(stored[(s.segment_index, s.scene_index)].content) is not None
        and stored[(s.segment_index, s.scene_index)].template == "labelled_diagram"
    )
    row = stored[(target.segment_index, target.scene_index)]
    edited = json.loads(json.dumps(row.content))
    edited["illustration"]["visual"][
        "title" if "title" in edited["illustration"]["visual"] else "type"
    ] = (
        "An edited title"
        if "title" in edited["illustration"]["visual"]
        else edited["illustration"]["visual"]["type"]
    )
    if edited == row.content:  # the visual had no title: change its first label instead
        nodes = edited["illustration"]["visual"].get("nodes") or edited["illustration"][
            "visual"
        ].get("parts")
        nodes[0]["label"] = "Edited label"
    async with ctx.sessionmaker() as session:
        await session.execute(
            text("UPDATE script_segment_scenes SET content = CAST(:c AS jsonb) WHERE id = :i"),
            {"c": json.dumps(edited), "i": row.id},
        )
        await session.commit()
    stale = [
        s
        for s in (await status_of(ctx, project_id, script_id, "horizontal", "low")).scenes
        if s.state != "ready"
    ]
    job, stats = await p10.render(ctx, project_id, script_id, "horizontal", "low")
    checks.record(
        "7 editing one visual re-renders only that scene",
        stats["chunks_drawn"] == len(stale) == 1,
        chunks_drawn=stats["chunks_drawn"],
        stale_before=len(stale),
        chunks_reused=job.result["chunks_reused"],
    )
    report["partial_visual_edit"] = {
        "edited_scene": [target.segment_index, target.scene_index],
        "chunks_drawn": stats["chunks_drawn"],
        "chunks_total": job.result["chunk_total"],
    }

    # ---- 8: narration re-synthesis invalidation, and the start_ms regression -----------------------------------------------------------
    async with ctx.sessionmaker() as session:
        segs = await scripts_repo.list_segments(session, project_id=project_id, script_id=script_id)
        audio_before = {s.id: await audio_repo.get_by_segment_id(session, s.id) for s in segs}
    target_seg = segs[len(segs) // 2]
    new_text = (
        p10.narration_for(target_seg.segment_index)
        + " This extra sentence makes the narration longer than before."
    )
    async with ctx.sessionmaker() as session:
        await session.execute(
            text("UPDATE script_segments SET narration_text = :t WHERE id = :i"),
            {"t": new_text, "i": target_seg.id},
        )
        await session.commit()
        enq = await script_audio_service.enqueue_synthesis(
            session,
            project_id=project_id,
            owner_id=ctx.user_id,
            script_id=script_id,
            idempotency_key=str(uuid4()),
        )
        await session.commit()
        await script_audio_service.run_synthesise_voice_job(
            session, storage, job_id=enq.job.id, voices_dir=VOICES, sessionmaker=ctx.sessionmaker
        )
        await session.commit()
        audio_after = {s.id: await audio_repo.get_by_segment_id(session, s.id) for s in segs}
    changed = [
        i for i in audio_after if audio_after[i].content_hash != audio_before[i].content_hash
    ]
    stale_status = await storyboard_status(ctx, project_id, script_id)
    later = next(
        d for d in stale_status.segments if d.segment.segment_index == target_seg.segment_index + 1
    )
    prefix = sum(
        (audio_after[d.segment.id].measured_duration_ms or 0)
        for d in stale_status.segments
        if d.segment.segment_index <= target_seg.segment_index
    )
    async with ctx.sessionmaker() as session:
        stored_first = (await scenes_repo.list_by_segment_id(session, later.segment.id))[0].start_ms
    shown_first = later.scenes[0].start_ms
    checks.record(
        "8a a re-narrated segment leaves later scenes' STORED start_ms stale but the read model shows the current one",
        len(changed) == 1
        and shown_first == prefix
        and stored_first != prefix
        and stale_status.timings_authoritative is False,
        shown=shown_first,
        current_prefix_sum=prefix,
        stored=stored_first,
        timings_authoritative=stale_status.timings_authoritative,
    )
    sb2 = await storyboard(ctx, project_id, script_id)
    fresh = await storyboard_status(ctx, project_id, script_id)
    scenes_now = list((await status_of(ctx, project_id, script_id, "horizontal", "low")).scenes)
    not_ready = [s for s in scenes_now if s.state != "ready"]
    job, stats = await p10.render(ctx, project_id, script_id, "horizontal", "low")
    new_total = sum(a.measured_duration_ms or 0 for a in audio_after.values())
    path = await fetch_output(
        ctx,
        project_id,
        script_id,
        "horizontal",
        "low",
        OUT / "horizontal-low-after-renarration.mp4",
    )
    described = describe_video(path, new_total)
    checks.record(
        "8b re-synthesis re-renders only the affected scenes; the rest are reused",
        stats["chunks_drawn"] == len({s.chunk_hash for s in not_ready})
        and job.result["chunks_reused"] >= 1
        and sb2["composer_calls"] == 1,
        chunks_drawn=stats["chunks_drawn"],
        scenes_not_ready=len(not_ready),
        chunks_reused=job.result["chunks_reused"],
        composer_calls=sb2["composer_calls"],
    )
    checks.record(
        "8c timings are authoritative again and the new video is frame-exact",
        fresh.timings_authoritative is True
        and described["video_frames"] == described["expected_frames"]
        and described["decodes_cleanly"],
        frames=f"{described['video_frames']}/{described['expected_frames']}",
    )
    report["renarration"] = {
        "segment": target_seg.segment_index,
        "audio_rows_changed": len(changed),
        "chunks_drawn": stats["chunks_drawn"],
        "not_ready": len(not_ready),
        "dropped_visual_count_after": fresh.dropped_visual_count,
    }
    del settings


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=float, default=2.5)
    parser.add_argument(
        "--keep", action="store_true", help="leave the project and objects in place"
    )
    parser.add_argument(
        "--perf-only",
        action="store_true",
        help="only the two high-quality renders and the cost measurements",
    )
    args = parser.parse_args()
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is not on PATH")
    raise SystemExit(asyncio.run(main(args.minutes, args.keep, args.perf_only)))

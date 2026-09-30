"""Phase 10 end-to-end verification: a real narrated project rendered to real MP4s.

Not part of the test suite (pytest's ``testpaths`` is ``tests``); it drives the same
``edutoon.services`` code the API and worker run, against the real local Postgres, MinIO, Piper
voice, FFmpeg and Pillow renderer. The one thing standing in for a real provider is the storyboard
*scene composer* (there is no LLM key): a deterministic stand-in that builds scenes from the
narration's own words, which then go through the real storyboard job (timing, validation and the
provenance rule are all real).

What it produces (under ``var/phase10-verification/``):

* ``*.mp4`` - the rendered videos, and ``report.json`` - every measurement below;
* stdout - a readable account of each check.

Run from ``apps/api`` (needs ``ffmpeg`` on PATH, ``make voices`` done and ``make up`` running):

    uv run python scripts/verify_phase10.py            # ~2.5 minute project
    uv run python scripts/verify_phase10.py --minutes 10   # the current maximum length
"""

# ruff: noqa: E501
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psutil
from sqlalchemy import text

from edutoon.core.config import get_settings
from edutoon.db.database import dispose_engine
from edutoon.db.session import get_sessionmaker
from edutoon.domain import render as render_domain
from edutoon.providers.llm import StoryboardSceneDraft, StoryboardSceneSetDraft, TokenUsage
from edutoon.providers.renderer import (
    AssemblySpec,
    ChunkSpec,
    MediaProbe,
    PillowFfmpegRenderer,
    RenderedChunk,
    RendererError,
)
from edutoon.providers.storage import get_storage_client
from edutoon.repositories import jobs as jobs_repo
from edutoon.repositories import render as render_repo
from edutoon.repositories import script_segment_audio as audio_repo
from edutoon.repositories import script_segment_scenes as scenes_repo
from edutoon.repositories import scripts as scripts_repo
from edutoon.repositories.scripts import NewScriptSegment
from edutoon.services import projects as projects_service
from edutoon.services import render as render_service
from edutoon.services import script_audio as script_audio_service
from edutoon.services import storyboard as storyboard_service
from edutoon.services import users as users_service

OUT = Path(__file__).resolve().parents[1] / "var" / "phase10-verification"
VOICES = Path(__file__).resolve().parents[1] / "var" / "piper-voices"

SENTENCES = [
    "Photosynthesis is how green plants make their own food from light.",
    "Leaves collect light from the sun using a green pigment called chlorophyll.",
    "Water travels up from the roots through thin tubes in the stem.",
    "Carbon dioxide enters the leaf through tiny openings called stomata.",
    "Inside the leaf, light energy turns water and carbon dioxide into sugar.",
    "The sugar gives the plant energy to grow and to build new leaves.",
    "Oxygen is released into the air as a useful by-product of the process.",
    "Without light the plant cannot make sugar, and it slowly weakens.",
    "A healthy leaf is thin and wide so that it can catch plenty of light.",
    "Plants store extra sugar as starch in roots, stems and seeds.",
    "Animals depend on plants for food and for the oxygen that plants release.",
    "So the light from the sun is the start of almost every food chain.",
]

TEMPLATES = [
    "title_card",
    "bullet_points",
    "labelled_diagram",
    "split_text_illustration",
    "full_bleed_illustration",
    "formula_or_code",
]


def narration_for(index: int) -> str:
    """Three related sentences per segment (about 10-12 seconds of speech)."""
    picks = [SENTENCES[(index * 3 + k) % len(SENTENCES)] for k in range(3)]
    return " ".join(picks)


def _words(text: str, count: int, *, start: int = 0) -> str:
    return " ".join(text.replace(",", "").rstrip(".").split()[start : start + count])


def _clip(text: str, limit: int) -> str:
    text = text.rstrip(".")
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0]


class SceneComposer:
    """Stands in for ``LLM.compose_storyboard_scenes``: builds scenes from the segment's own
    words, cycling through all six templates. Counts its calls."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    async def compose_storyboard_scenes(
        self, segment: Any, *, max_scenes: int
    ) -> tuple[StoryboardSceneSetDraft, TokenUsage]:
        self.calls.append(segment.segment_index)
        sentences = [s.strip() for s in segment.narration_text.split(". ") if s.strip()]
        first, second = sentences[0], sentences[1 % len(sentences)]
        count = min(max_scenes, 2 + segment.segment_index % 2)
        scenes = []
        for n in range(count):
            template = TEMPLATES[(segment.segment_index + n) % len(TEMPLATES)]
            scenes.append(
                StoryboardSceneDraft(
                    template=template, content=self._content(template, first, second)
                )
            )
        return StoryboardSceneSetDraft(scenes=tuple(scenes)), TokenUsage(
            input_tokens=1, output_tokens=1
        )

    @staticmethod
    def _content(template: str, first: str, second: str) -> dict[str, Any]:
        illustration = {
            "description": _clip(f"An illustration of {_words(first, 7, start=2).lower()}", 150),
            "subject_labels": [
                _words(second, 1, start=1).lower(),
                _words(first, 1, start=3).lower(),
            ],
        }
        return {
            "title_card": {
                "title": _clip(_words(first, 5), 60),
                "subtitle": _clip(_words(second, 9), 100),
            },
            "bullet_points": {
                "title": _clip(_words(first, 4), 60),
                "bullets": [_clip(first, 80), _clip(second, 80)],
            },
            "labelled_diagram": {
                "illustration": illustration,
                "labels": [
                    {"text": _words(first, 1, start=1).lower(), "position": "top"},
                    {"text": _words(second, 1, start=2).lower(), "position": "left"},
                    {"text": _words(first, 1, start=4).lower(), "position": "right"},
                ],
            },
            "split_text_illustration": {"text": _clip(first, 150), "illustration": illustration},
            "full_bleed_illustration": {"illustration": illustration, "caption": _clip(second, 78)},
            "formula_or_code": {
                "content": "light + water + carbon dioxide -> sugar + oxygen",
                "label": "In words",
                "kind": "formula",
            },
        }[template]


class WorkerKilled(BaseException):
    """A worker process dying: not an Exception, so nothing in the job can swallow it."""


class Recorder:
    """Wraps the real renderer to record every chunk drawn and to fail or 'die' on request."""

    def __init__(self) -> None:
        self.inner = PillowFfmpegRenderer()
        self.drawn: list[str] = []
        self.fail_on: tuple[str, int] | None = None
        self.die_after: int | None = None
        self.assemblies = 0
        self.dead = False
        self.delay = 0.0

    async def render_chunk(self, spec: ChunkSpec, dest: Path) -> RenderedChunk:
        label = json.dumps(spec.content, sort_keys=True)[:60]
        if self.dead or (self.die_after is not None and len(self.drawn) >= self.die_after):
            self.dead = True
            raise WorkerKilled()
        if self.fail_on is not None and self.fail_on == (
            json.dumps(spec.content, sort_keys=True),
            spec.duration_ms,
        ):
            raise RendererError("simulated failure")
        self.drawn.append(label)
        if self.delay:
            await asyncio.sleep(self.delay)
        return await self.inner.render_chunk(spec, dest)

    async def assemble(self, spec: AssemblySpec, dest: Path) -> None:
        self.assemblies += 1
        await self.inner.assemble(spec, dest)

    async def probe(self, path: Path) -> MediaProbe:
        return await self.inner.probe(path)


@dataclass
class Sampler:
    """Measures a stage: peak memory of this process and its children (FFmpeg) - reported with the
    process's own memory at the start, since it already holds the Piper voice and the database
    driver - whole-machine CPU utilisation, and (if an NVIDIA GPU is present) GPU utilisation."""

    interval: float = 0.1
    peak_rss: int = 0
    baseline_rss: int = 0
    gpu_samples: list[float] = field(default_factory=list)
    system_cpu_samples: list[float] = field(default_factory=list)
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def __enter__(self) -> Sampler:
        self._process = psutil.Process()
        self.baseline_rss = self._process.memory_info().rss
        psutil.cpu_percent(None)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _gpu(self) -> None:
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            ).stdout.strip()
            if out:
                self.gpu_samples.append(float(out.splitlines()[0]))
        except (OSError, ValueError, subprocess.SubprocessError):
            pass

    def _run(self) -> None:
        ticks = 0
        while not self._stop.wait(self.interval):
            try:
                processes = [self._process, *self._process.children(recursive=True)]
                rss = 0
                for proc in processes:
                    try:
                        rss += proc.memory_info().rss
                    except psutil.Error:
                        continue
            except psutil.Error:
                continue
            self.peak_rss = max(self.peak_rss, rss)
            self.system_cpu_samples.append(psutil.cpu_percent(None))
            ticks += 1
            if ticks % 10 == 0:
                self._gpu()

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join()

    def summary(self) -> dict[str, float]:
        cpu = self.system_cpu_samples
        return {
            "peak_rss_mb": round(self.peak_rss / 1e6, 1),
            "process_rss_at_start_mb": round(self.baseline_rss / 1e6, 1),
            "peak_rss_over_start_mb": round((self.peak_rss - self.baseline_rss) / 1e6, 1),
            "system_cpu_percent_mean": round(sum(cpu) / len(cpu), 1) if cpu else 0.0,
            "system_cpu_percent_peak": max(cpu, default=0.0),
            "gpu_util_percent_mean": round(sum(self.gpu_samples) / len(self.gpu_samples), 1)
            if self.gpu_samples
            else -1.0,
            "gpu_util_percent_peak": max(self.gpu_samples, default=-1.0),
        }


def ffprobe(path: Path) -> dict[str, Any]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture_output=True,
        check=True,
    ).stdout
    return json.loads(out)  # type: ignore[no-any-return]


def audio_md5(path: Path) -> str:
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-f", "md5", "-"],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    return out.strip()


def video_packets(path: Path) -> list[tuple[int, bool]]:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "packet=pts,flags",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    return [(int(a), "K" in b) for a, b in (row.split(",") for row in out.split() if row)]


def say(message: str) -> None:
    print(message, flush=True)


@dataclass
class Ctx:
    sessionmaker: Any
    storage: Any
    recorder: Recorder
    composer: SceneComposer
    report: dict[str, Any]
    user_id: UUID | None = None


async def make_project(
    ctx: Ctx, title: str, segments: int, *, orientation: str = "horizontal"
) -> UUID:
    settings = get_settings()
    async with ctx.sessionmaker() as session:
        if ctx.user_id is None:
            user = await users_service.create_user(
                session,
                email=f"verify-{uuid4()}@example.com",
                clerk_user_id=f"verify_{uuid4()}",
                display_name="Phase 10 verification",
            )
            ctx.user_id = user.id
        project = await projects_service.create_project(
            session, owner_id=ctx.user_id, title=title, source_type="pdf", orientation=orientation
        )
        script = await scripts_repo.create(
            session,
            project_id=project.id,
            llm_model="verification",
            segments=[
                NewScriptSegment(
                    segment_index=i, narration_text=narration_for(i), estimated_duration_ms=10_000
                )
                for i in range(segments)
            ],
        )
        await scripts_repo.set_status(
            session, project_id=project.id, script_id=script.id, status="approved"
        )
        await session.commit()
    del settings
    return project.id


async def narrate(ctx: Ctx, project_id: UUID) -> dict[str, Any]:
    """The real path: enqueue, then run the real synthesise_voice job with the real Piper voice."""
    settings = get_settings()
    async with ctx.sessionmaker() as session:
        script = (await scripts_repo.list_by_project(session, project_id, limit=5)).items[0]
        project_owner = ctx.user_id
        assert project_owner is not None
        enq = await script_audio_service.enqueue_synthesis(
            session,
            project_id=project_id,
            owner_id=project_owner,
            script_id=script.id,
            idempotency_key=str(uuid4()),
        )
        await session.commit()
        started = time.perf_counter()
        job = await script_audio_service.run_synthesise_voice_job(
            session,
            ctx.storage,
            job_id=enq.job.id,
            voices_dir=VOICES,
            sessionmaker=ctx.sessionmaker,
        )
        await session.commit()
    del settings
    assert job.status == "succeeded", job.error
    return {
        "seconds": round(time.perf_counter() - started, 1),
        "result": job.result,
        "script_id": str(script.id),
    }


async def storyboard(ctx: Ctx, project_id: UUID, script_id: UUID) -> dict[str, Any]:
    async with ctx.sessionmaker() as session:
        assert ctx.user_id is not None
        enq = await storyboard_service.enqueue_storyboard_generation(
            session,
            project_id=project_id,
            owner_id=ctx.user_id,
            script_id=script_id,
            idempotency_key=str(uuid4()),
        )
        await session.commit()
        before = len(ctx.composer.calls)
        job = await storyboard_service.run_generate_storyboard_job(
            session,
            ctx.composer,
            job_id=enq.job.id,
            sessionmaker=ctx.sessionmaker,  # type: ignore[arg-type]
        )
        await session.commit()
    assert job.status == "succeeded" and job.result and job.result["outcome"] == "complete", (
        job.status,
        job.error,
        job.result,
    )
    return {"composer_calls": len(ctx.composer.calls) - before, "result": job.result}


async def render(
    ctx: Ctx,
    project_id: UUID,
    script_id: UUID,
    orientation: str,
    quality: str,
    *,
    expect: str = "succeeded",
) -> tuple[Any, dict[str, Any]]:
    async with ctx.sessionmaker() as session:
        assert ctx.user_id is not None
        enq = await render_service.enqueue_render(
            session,
            project_id=project_id,
            owner_id=ctx.user_id,
            script_id=script_id,
            orientation=orientation,
            quality=quality,
            idempotency_key=str(uuid4()),
        )
        await session.commit()
        ctx.recorder.drawn.clear()
        ctx.recorder.assemblies = 0
        started = time.perf_counter()
        with Sampler() as sampler:
            try:
                job = await render_service.run_render_video_job(
                    session,
                    ctx.storage,
                    ctx.recorder,
                    job_id=enq.job.id,
                    sessionmaker=ctx.sessionmaker,  # type: ignore[arg-type]
                )
            finally:
                await session.commit()
        elapsed = time.perf_counter() - started
    assert job.status == expect, (job.status, job.error, job.result)
    return job, {
        "seconds": round(elapsed, 2),
        "chunks_drawn": len(ctx.recorder.drawn),
        "assemblies": ctx.recorder.assemblies,
        "estimate": {
            k: getattr(enq.estimate, k)
            for k in ("chunk_count", "chunks_to_render", "chunks_reusable")
        },
        **sampler.summary(),
    }


async def fetch_output(
    ctx: Ctx, project_id: UUID, script_id: UUID, orientation: str, quality: str, dest: Path
) -> Path:
    async with ctx.sessionmaker() as session:
        out = await render_repo.get_output(
            session,
            project_id=project_id,
            script_id=script_id,
            orientation=orientation,
            quality=quality,
        )
    assert out is not None
    await ctx.storage.download_file(bucket=out.storage_bucket, key=out.storage_key, dest=dest)
    return dest


def describe_video(path: Path, expected_ms: int) -> dict[str, Any]:
    info = ffprobe(path)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
    packets = video_packets(path)
    duration_ms = float(info["format"]["duration"]) * 1000
    result = {
        "file": path.name,
        "size_bytes": int(info["format"]["size"]),
        "bitrate_kbps": round(int(info["format"]["bit_rate"]) / 1000, 1),
        "container_duration_ms": round(duration_ms, 1),
        "video": {
            k: video.get(k)
            for k in (
                "codec_name",
                "profile",
                "level",
                "pix_fmt",
                "width",
                "height",
                "r_frame_rate",
                "color_space",
                "duration",
            )
        },
        "audio": {
            k: audio.get(k)
            for k in ("codec_name", "profile", "sample_rate", "channels", "bit_rate", "duration")
        },
        "video_frames": len(packets),
        "expected_frames": render_domain.frame_at(expected_ms),
        "pts_steps": sorted({b[0] - a[0] for a, b in zip(packets, packets[1:], strict=False)}),
        "keyframes": sum(1 for _, k in packets if k),
        "video_minus_narration_ms": round(float(video["duration"]) * 1000 - expected_ms, 1),
        "audio_minus_narration_ms": round(float(audio["duration"]) * 1000 - expected_ms, 1),
        "decodes_cleanly": subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"], capture_output=True
        ).stderr
        == b"",
    }
    return result


async def db_restart_check(ctx: Ctx, container: str) -> dict[str, Any]:
    """Kill and restart the Postgres container while a render is running, then render again: every chunk
    committed before the restart must be reused, and the result must still be a verified video."""
    project_id = await make_project(ctx, "Phase 10 database restart", 6)
    narration = await narrate(ctx, project_id)
    script_id = UUID(narration["script_id"])
    await storyboard(ctx, project_id, script_id)
    assert ctx.user_id is not None
    ctx.recorder.delay = 3.0
    ctx.recorder.drawn.clear()

    async def run_job(job_id: UUID) -> str:
        try:
            async with ctx.sessionmaker() as session:
                job = await render_service.run_render_video_job(
                    session,
                    ctx.storage,
                    ctx.recorder,
                    job_id=job_id,
                    sessionmaker=ctx.sessionmaker,  # type: ignore[arg-type]
                )
                await session.commit()
                return f"job ended {job.status}"
        except Exception as exc:  # noqa: BLE001 - the point is to see what a dead database does
            return f"job raised {type(exc).__name__}"

    async with ctx.sessionmaker() as session:
        enq = await render_service.enqueue_render(
            session,
            project_id=project_id,
            owner_id=ctx.user_id,
            script_id=script_id,
            orientation="horizontal",
            quality="low",
            idempotency_key=str(uuid4()),
        )
        await session.commit()
    task = asyncio.create_task(run_job(enq.job.id))

    async def committed() -> int:
        async with ctx.sessionmaker() as session:
            return int(
                (
                    await session.execute(
                        text("SELECT count(*) FROM render_chunks WHERE project_id = :p"),
                        {"p": project_id},
                    )
                ).scalar_one()
            )

    while await committed() < 3:
        await asyncio.sleep(0.2)
    say("[db restart] killing and restarting the Postgres container mid-render ...")
    # A crash rather than a polite stop, so the interruption is immediate and the database has to recover.
    # (in threads, so the worker keeps running - and keeps hitting the dead database - meanwhile)
    await asyncio.to_thread(
        subprocess.run, ["docker", "kill", container], check=True, capture_output=True
    )
    await asyncio.sleep(6)
    await asyncio.to_thread(
        subprocess.run, ["docker", "start", container], check=True, capture_output=True
    )
    outcome = await task
    for _ in range(60):
        try:
            before = await committed()
            break
        except Exception:  # noqa: BLE001 - the database is still coming back
            await asyncio.sleep(1)
    else:
        raise RuntimeError("the database did not come back")
    say(f"[db restart] {outcome}; {before} chunks were committed and are still there")
    ctx.recorder.delay = 0.0
    async with ctx.sessionmaker() as session:
        await jobs_repo.update(
            session,
            job_id=enq.job.id,
            status="running",
            started_at=datetime(2020, 1, 1, tzinfo=UTC),
        )
        await session.commit()
    job, stats = await render(ctx, project_id, script_id, "horizontal", "low")
    path = await fetch_output(
        ctx, project_id, script_id, "horizontal", "low", OUT / "after-db-restart.mp4"
    )
    result = {
        "outcome_of_interrupted_job": outcome,
        "chunks_committed_before_restart": before,
        "chunks_drawn_after": stats["chunks_drawn"],
        "chunks_reused_after": job.result["chunks_reused"],
        "video": describe_video(path, int(job.result["duration_ms"])),
    }
    say(
        f"[db restart] resumed: drew {stats['chunks_drawn']}, reused {job.result['chunks_reused']}; "
        f"frames {result['video']['video_frames']}/{result['video']['expected_frames']} decodes cleanly={result['video']['decodes_cleanly']}"
    )
    assert job.result["chunks_reused"] >= before and result["video"]["decodes_cleanly"]
    async with ctx.sessionmaker() as session:
        files = await projects_service.delete_project(
            session, project_id=project_id, owner_id=ctx.user_id
        )
        await session.commit()
    await projects_service.remove_stored_files(ctx.storage, project_id=project_id, files=files)
    return result


async def main(minutes: float, keep: bool, db_restart_container: str | None = None) -> None:
    settings = get_settings()
    OUT.mkdir(parents=True, exist_ok=True)
    storage = get_storage_client(
        endpoint_url=settings.STORAGE_ENDPOINT_URL,
        access_key_id=settings.STORAGE_ACCESS_KEY_ID,
        secret_access_key=settings.STORAGE_SECRET_ACCESS_KEY,
    )
    ctx = Ctx(get_sessionmaker(), storage, Recorder(), SceneComposer(), {})
    report = ctx.report
    report["machine"] = {
        "cpu": "i7-12700H (14 cores / 20 threads)",
        "ram_gb": round(psutil.virtual_memory().total / 1e9, 1),
        "ffmpeg": subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, text=True
        ).stdout.splitlines()[0],
        "concurrency": settings.RENDER_CONCURRENCY,
    }
    if db_restart_container:
        result = await db_restart_check(ctx, db_restart_container)
        report["db_restart"] = result
        async with ctx.sessionmaker() as session:
            await session.execute(text("DELETE FROM users WHERE id = :i"), {"i": ctx.user_id})
            await session.commit()
        (OUT / "report-db-restart.json").write_text(
            json.dumps(report, indent=2, default=str), encoding="utf-8"
        )
        await dispose_engine()
        return
    segments = max(4, round(minutes * 60 / 12.5))
    say(f"== project of {segments} segments (~{minutes} min) ==")

    project_id = await make_project(ctx, "Phase 10 verification", segments)
    narration = await narrate(ctx, project_id)
    script_id = UUID(narration["script_id"])
    total_ms = int(narration["result"]["measured_duration_ms"])
    report["narration"] = {
        "segments": segments,
        "seconds": narration["seconds"],
        "measured_duration_ms": total_ms,
        "voice": narration["result"]["voice_model"],
    }
    say(
        f"[narration] real Piper: {segments} segments, {total_ms / 1000:.1f}s of audio in {narration['seconds']}s"
    )

    sb = await storyboard(ctx, project_id, script_id)
    async with ctx.sessionmaker() as session:
        status = await render_service.get_render_status(
            session,
            project_id=project_id,
            owner_id=ctx.user_id,
            script_id=script_id,
            orientation="horizontal",
            quality="high",
        )
    report["storyboard"] = {
        "composer_calls": sb["composer_calls"],
        "scenes": len(status.scenes),
        "templates": sorted({s.template for s in status.scenes}),
    }
    say(
        f"[storyboard] {sb['composer_calls']} composer calls -> {len(status.scenes)} scenes, templates {report['storyboard']['templates']}"
    )

    # ---- the four main renders ---
    report["renders"] = {}
    files: dict[tuple[str, str], Path] = {}
    for orientation, quality in (
        ("horizontal", "high"),
        ("vertical", "high"),
        ("horizontal", "low"),
        ("vertical", "low"),
    ):
        job, stats = await render(ctx, project_id, script_id, orientation, quality)
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
            f"[render {orientation}/{quality}] {stats['seconds']}s  chunks={stats['chunks_drawn']}  "
            f"{described['video']['width']}x{described['video']['height']}  {described['size_bytes'] / 1e6:.2f} MB  "
            f"frames={described['video_frames']}/{described['expected_frames']}  "
            f"v-a={described['video_minus_narration_ms']}ms a-a={described['audio_minus_narration_ms']}ms  "
            f"peakRSS={stats['peak_rss_mb']}MB (+{stats['peak_rss_over_start_mb']}) sysCPU={stats['system_cpu_percent_mean']}% gpu={stats['gpu_util_percent_mean']}%"
        )

    md5s = {k: audio_md5(v) for k, v in files.items()}
    report["narration_identical_across_renders"] = {
        "audio_md5": {f"{o}-{q}": v for (o, q), v in md5s.items()},
        "horizontal_vs_vertical_high_identical": md5s[("horizontal", "high")]
        == md5s[("vertical", "high")],
    }
    say(
        f"[orientation] narration audio identical horizontal vs vertical: {report['narration_identical_across_renders']['horizontal_vs_vertical_high_identical']}"
    )

    # ---- nothing changed: no work ---
    job, stats = await render(ctx, project_id, script_id, "horizontal", "high")
    report["rerender_unchanged"] = {
        "stats": stats,
        "output_reused": job.result.get("output_reused"),
    }
    say(
        f"[unchanged re-render] chunks drawn={stats['chunks_drawn']} output_reused={job.result.get('output_reused')}"
    )

    # ---- partial re-render through the real product path ---
    async with ctx.sessionmaker() as session:
        segs = await scripts_repo.list_segments(session, project_id=project_id, script_id=script_id)
        audio_before = {s.id: (await audio_repo.get_by_segment_id(session, s.id)) for s in segs}
        before = await render_repo.list_output_scenes(
            session,
            output_id=(
                await render_repo.get_output(
                    session,
                    project_id=project_id,
                    script_id=script_id,
                    orientation="horizontal",
                    quality="high",
                )
            ).id,  # type: ignore[union-attr]
        )
    target = len(segs) // 2
    target_seg = segs[target]
    say(f"[partial] changing narration of segment {target} (id {target_seg.id})")
    new_text = (
        narration_for(target) + " This extra sentence makes the narration longer than before."
    )
    async with ctx.sessionmaker() as session:
        await session.execute(
            text("UPDATE script_segments SET narration_text = :t WHERE id = :i"),
            {"t": new_text, "i": target_seg.id},
        )
        await session.commit()
    async with ctx.sessionmaker() as session:
        assert ctx.user_id is not None
        enq = await script_audio_service.enqueue_synthesis(
            session,
            project_id=project_id,
            owner_id=ctx.user_id,
            script_id=script_id,
            idempotency_key=str(uuid4()),
        )
        await session.commit()
        resynth = await script_audio_service.run_synthesise_voice_job(
            session,
            ctx.storage,
            job_id=enq.job.id,
            voices_dir=VOICES,
            sessionmaker=ctx.sessionmaker,
        )
        await session.commit()
        audio_after = {s.id: (await audio_repo.get_by_segment_id(session, s.id)) for s in segs}
    changed_audio = [
        str(i) for i in audio_after if audio_after[i].content_hash != audio_before[i].content_hash
    ]
    say(f"[partial] re-synthesis touched {len(changed_audio)} of {len(segs)} segments' audio")
    async with ctx.sessionmaker() as session:
        s1 = await render_service.get_render_status(
            session,
            project_id=project_id,
            owner_id=ctx.user_id,
            script_id=script_id,
            orientation="horizontal",
            quality="high",
        )
    say(f"[partial] after narration change (before storyboard regenerates): blockers={s1.blockers}")
    sb2 = await storyboard(ctx, project_id, script_id)
    async with ctx.sessionmaker() as session:
        s2 = await render_service.get_render_status(
            session,
            project_id=project_id,
            owner_id=ctx.user_id,
            script_id=script_id,
            orientation="horizontal",
            quality="high",
        )
    states = [(s.segment_index, s.scene_index, s.state) for s in s2.scenes]
    stale = [t for t in states if t[2] != "ready"]
    say(
        f"[partial] storyboard regenerated with {sb2['composer_calls']} composer call(s); scenes not ready: {stale}"
    )
    job, stats = await render(ctx, project_id, script_id, "horizontal", "high")
    path = await fetch_output(
        ctx, project_id, script_id, "horizontal", "high", OUT / "horizontal-high-after-edit.mp4"
    )
    new_total = sum(a.measured_duration_ms or 0 for a in audio_after.values())
    async with ctx.sessionmaker() as session:
        out = await render_repo.get_output(
            session,
            project_id=project_id,
            script_id=script_id,
            orientation="horizontal",
            quality="high",
        )
        after = await render_repo.list_output_scenes(session, output_id=out.id)  # type: ignore[union-attr]
    reused_positions = [
        b.position for b in before if any(a.chunk_hash == b.chunk_hash for a in after)
    ]
    report["partial_rerender"] = {
        "edited_segment_index": target,
        "audio_rows_changed": len(changed_audio),
        "composer_calls": sb2["composer_calls"],
        "scenes_not_ready_before_render": len(stale),
        "estimate": stats["estimate"],
        "chunks_drawn": stats["chunks_drawn"],
        "chunks_total": len(after),
        "chunks_reused_by_hash": len(reused_positions),
        "seconds": stats["seconds"],
        "other_segments_audio_untouched": len(changed_audio) == 1,
        "video": describe_video(path, new_total),
    }
    say(
        f"[partial] render drew {stats['chunks_drawn']} of {len(after)} chunks; {len(reused_positions)} chunks kept the same fingerprint; "
        f"estimate said {stats['estimate']}"
    )
    assert stats["chunks_drawn"] == len(stale), "re-rendered more than the changed scenes"
    assert len(changed_audio) == 1
    del resynth, s1

    # ---- crash, then resume; failure, then retry (a second project, so every chunk is new) -----
    p2 = await make_project(ctx, "Phase 10 crash and retry", 6)
    n2 = await narrate(ctx, p2)
    s2_id = UUID(n2["script_id"])
    await storyboard(ctx, p2, s2_id)
    ctx.recorder.die_after = 9
    async with ctx.sessionmaker() as session:
        assert ctx.user_id is not None
        enq = await render_service.enqueue_render(
            session,
            project_id=p2,
            owner_id=ctx.user_id,
            script_id=s2_id,
            orientation="vertical",
            quality="low",
            idempotency_key=str(uuid4()),
        )
        await session.commit()
        try:
            await render_service.run_render_video_job(
                session,
                ctx.storage,
                ctx.recorder,
                job_id=enq.job.id,
                sessionmaker=ctx.sessionmaker,  # type: ignore[arg-type]
            )
        except WorkerKilled:
            say("[crash] worker killed mid-render")
        await session.rollback()
    async with ctx.sessionmaker() as session:
        stored = await session.execute(
            text("SELECT count(*) FROM render_chunks WHERE project_id = :p"), {"p": p2}
        )
        chunks_after_kill = stored.scalar_one()
        await jobs_repo.update(
            session,
            job_id=enq.job.id,
            status="running",
            started_at=datetime(2020, 1, 1, tzinfo=UTC),
        )
        await session.commit()
    say(f"[crash] {chunks_after_kill} finished chunks survived the kill")
    ctx.recorder.die_after = None
    ctx.recorder.dead = False
    job, stats = await render(ctx, p2, s2_id, "vertical", "low")
    report["crash_resume"] = {
        "chunks_before_kill": chunks_after_kill,
        "resume_estimate": stats["estimate"],
        "chunks_drawn_on_resume": stats["chunks_drawn"],
        "job_result": job.result,
    }
    say(
        f"[resume] drew {stats['chunks_drawn']} chunks, reused {job.result['chunks_reused']} (total {job.result['chunk_total']})"
    )
    assert job.result["chunks_reused"] >= chunks_after_kill

    async with ctx.sessionmaker() as session:
        status = await render_service.get_render_status(
            session,
            project_id=p2,
            owner_id=ctx.user_id,
            script_id=s2_id,
            orientation="horizontal",
            quality="low",
        )
    victim = status.scenes[2]
    async with ctx.sessionmaker() as session:
        victim_scene = [
            s
            for s in await scenes_repo.list_by_segment_id(session, victim.segment_id)
            if s.scene_index == victim.scene_index
        ][0]
    ctx.recorder.fail_on = (json.dumps(victim_scene.content, sort_keys=True), victim.duration_ms)
    job, stats = await render(ctx, p2, s2_id, "horizontal", "low", expect="failed")
    say(f"[failure] {job.error}")
    ctx.recorder.fail_on = None
    async with ctx.sessionmaker() as session:
        assert ctx.user_id is not None
        retry = await render_service.retry_render(
            session, project_id=p2, owner_id=ctx.user_id, idempotency_key=str(uuid4())
        )
        await session.commit()
        ctx.recorder.drawn.clear()
        job2 = await render_service.run_render_video_job(
            session,
            ctx.storage,
            ctx.recorder,
            job_id=retry.job.id,
            sessionmaker=ctx.sessionmaker,  # type: ignore[arg-type]
        )
        await session.commit()
    report["failure_retry"] = {
        "first_attempt": {
            "chunks_drawn": stats["chunks_drawn"],
            "error": job.error,
            "failed_chunks": len(job.result["failed_chunks"]),
        },
        "retry": {
            "chunks_drawn": len(ctx.recorder.drawn),
            "status": job2.status,
            "chunks_reused": job2.result.get("chunks_reused"),
        },
    }
    say(
        f"[retry] status={job2.status}; drew {len(ctx.recorder.drawn)} chunk(s); reused {job2.result.get('chunks_reused')}"
    )
    assert job2.status == "succeeded" and len(ctx.recorder.drawn) == 1

    # ---- Range reads straight from storage ---
    async with ctx.sessionmaker() as session:
        out = await render_repo.get_output(
            session,
            project_id=project_id,
            script_id=script_id,
            orientation="horizontal",
            quality="high",
        )
    assert out is not None
    data = (OUT / "horizontal-high-after-edit.mp4").read_bytes()  # the current stored video
    checks = {}
    for header, expected in (
        ("bytes=0-99", data[:100]),
        ("bytes=1000-1999", data[1000:2000]),
        ("bytes=-64", data[-64:]),
    ):
        stream = await storage.open_object(
            bucket=out.storage_bucket, key=out.storage_key, range_header=header
        )
        body = b"".join([chunk async for chunk in stream.iter_chunks(4096)])
        assert body == expected
        checks[header] = {
            "status": stream.status_code,
            "content_range": stream.content_range,
            "matches_file": True,
        }
    report["range_reads"] = checks
    say(f"[range] {checks}")

    # ---- scaling projection ---
    high = report["renders"]["horizontal-high"]
    per_min = {
        "seconds_per_video_minute": round(high["stats"]["seconds"] / (total_ms / 60_000), 1),
        "bytes_per_video_minute": round(high["video"]["size_bytes"] / (total_ms / 60_000)),
        "chunks_per_video_minute": round(
            high["video"]["job_result"]["chunk_total"] / (total_ms / 60_000), 1
        ),
    }
    report["measured_rates_high_horizontal"] = per_min
    report["projection_high_horizontal"] = {
        f"{m}_minutes": {
            "PROJECTED_render_seconds": round(per_min["seconds_per_video_minute"] * m),
            "PROJECTED_output_mb": round(per_min["bytes_per_video_minute"] * m / 1e6, 1),
            "PROJECTED_chunks": round(per_min["chunks_per_video_minute"] * m),
        }
        for m in (10, 30, 60)
    }
    say(f"[scaling] {per_min}")

    # ---- cleanup through the real deletion path ---
    if not keep:
        for pid in (project_id, p2):
            async with ctx.sessionmaker() as session:
                assert ctx.user_id is not None
                locations = await render_repo.list_storage_locations_for_project(session, pid)
                files_to_remove = await projects_service.delete_project(
                    session, project_id=pid, owner_id=ctx.user_id
                )
                await session.commit()
            await projects_service.remove_stored_files(
                storage, project_id=pid, files=files_to_remove
            )
            gone = [
                not await storage.object_exists(bucket=loc.bucket, key=loc.key) for loc in locations
            ]
            report.setdefault("deletion", {})[str(pid)] = {
                "render_objects": len(locations),
                "all_removed": all(gone),
            }
            say(
                f"[delete] project {pid}: {len(locations)} render objects, all removed: {all(gone)}"
            )
    if not keep and ctx.user_id is not None:
        async with ctx.sessionmaker() as session:
            await session.execute(text("DELETE FROM users WHERE id = :i"), {"i": ctx.user_id})
            await session.commit()
    (OUT / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    say(f"report written to {OUT / 'report.json'}")
    await dispose_engine()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=float, default=2.5)
    parser.add_argument(
        "--keep", action="store_true", help="leave the projects and objects in place"
    )
    parser.add_argument(
        "--db-restart",
        metavar="CONTAINER",
        help="only run the database-restart check, restarting this Docker container "
        "(for example edutoon-postgres-1)",
    )
    args = parser.parse_args()
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is not on PATH")
    asyncio.run(main(args.minutes, args.keep, args.db_restart))

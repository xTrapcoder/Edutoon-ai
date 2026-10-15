# ruff: noqa: E501 - SQL and demo wording are quoted on one line
"""Seed the smallest real training demo into the *development* stack (idempotent, removable).

What it creates, all labelled "Demo":

* a project owned by the administrator, an approved script and two **real** (tiny, FFmpeg-made) MP4
  render outputs in the outputs bucket - written directly, because producing them through the full
  pipeline needs a provider key or a long deck render;
* through the **real HTTP API** with a real Clerk token for the administrator: a course, two modules
  published from those renders, a three-question assessment on module 1 (published), the course
  published, and a pending invitation for the demo employee.

It does **not** enrol anyone (the employee does not exist until they first sign in) and does not
act as the employee. Needs the stack up (``make up``), the API running (``--api``), the migrations
applied, ffmpeg/ffprobe on PATH and the Clerk development keys in ``apps/web/.env.local``.

    uv run python scripts/seed_training_demo.py --employee-email you+employee@example.com
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

import httpx
from dev_clerk_token import mint  # the sibling script
from sqlalchemy import text

PROJECT_TITLE = "Demo: Workplace safety (seeded)"
COURSE_TITLE = "Demo: Safety induction"

# (module title, orientation, quality, width, height, test pattern, tone Hz)
VIDEOS = (
    ("Welcome and the rules of the site", "horizontal", "low", 640, 360, "testsrc2", 440),
    ("Emergency exits and lifting", "horizontal", "high", 1280, 720, "smptebars", 660),
)

QUESTIONS = (
    (
        "Where do you go after an evacuation alarm?",
        ("At the assembly point", "Back to your desk", "To the car park lift"),
        0,
    ),
    (
        "Who do you tell first about a spill on the floor?",
        ("Nobody, it will dry", "Your supervisor", "A passing visitor"),
        1,
    ),
    (
        "How should a heavy box be lifted?",
        (
            "Twisting from the waist",
            "Quickly, to get it over with",
            "With a straight back and bent knees",
        ),
        2,
    ),
)


def make_mp4(directory: Path, name: str, width: int, height: int, pattern: str, tone: int) -> Path:
    path = directory / f"{name}.mp4"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"{pattern}=size={width}x{height}:rate=30:duration=8",
            "-f", "lavfi", "-i", f"sine=frequency={tone}:sample_rate=22050:duration=8",
            "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p", "-g", "30", "-bf", "0",
            "-c:a", "aac", "-ac", "1", "-ar", "22050", "-shortest", "-movflags", "+faststart",
            str(path),
        ],
        check=True,
    )  # fmt: skip
    return path


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip
    return json.loads(out)


async def seed_rows(admin_email_hint: str | None) -> dict:
    from edutoon.core.config import get_settings
    from edutoon.db.session import get_sessionmaker
    from edutoon.domain.render import output_storage_key
    from edutoon.providers.storage import get_storage_client
    from edutoon.repositories import projects as projects_repo
    from edutoon.repositories import render as render_repo
    from edutoon.repositories import scripts as scripts_repo
    from edutoon.repositories.render import OutputScene

    settings = get_settings()
    storage = get_storage_client(
        endpoint_url=settings.STORAGE_ENDPOINT_URL,
        access_key_id=settings.STORAGE_ACCESS_KEY_ID,
        secret_access_key=settings.STORAGE_SECRET_ACCESS_KEY,
    )
    async with get_sessionmaker()() as session:
        admins = (
            await session.execute(
                text("SELECT id, email FROM users WHERE role = 'admin' AND status = 'active'")
            )
        ).all()
        if len(admins) != 1:
            raise SystemExit(f"expected exactly one active administrator, found {len(admins)}")
        admin_id, admin_email = admins[0]
        existing = (
            await session.execute(
                text("SELECT id FROM projects WHERE title = :t AND owner_id = :o"),
                {"t": PROJECT_TITLE, "o": admin_id},
            )
        ).first()
        if existing is not None:
            project_id = existing[0]
            script_id = (
                await session.execute(
                    text(
                        "SELECT id FROM scripts WHERE project_id = :p ORDER BY created_at LIMIT 1"
                    ),
                    {"p": project_id},
                )
            ).scalar_one()
            print("rows: demo project already present, reusing it", file=sys.stderr)
            return {
                "admin_email": admin_email,
                "project_id": str(project_id),
                "script_id": str(script_id),
            }

        project = await projects_repo.create(
            session, owner_id=admin_id, title=PROJECT_TITLE, source_type="topic"
        )
        script = await scripts_repo.create(session, project_id=project.id, llm_model="demo-seed/1")
        approved = await scripts_repo.set_status(
            session, project_id=project.id, script_id=script.id, status="approved"
        )
        assert approved is not None
        with tempfile.TemporaryDirectory() as tmp:
            for _title, orientation, quality, width, height, pattern, tone in VIDEOS:
                mp4 = make_mp4(Path(tmp), f"{orientation}-{quality}", width, height, pattern, tone)
                info = probe(mp4)
                video = next(s for s in info["streams"] if s["codec_type"] == "video")
                body = mp4.read_bytes()
                key = output_storage_key(project.id, script.id, orientation, quality)
                await storage.put_object(
                    bucket=settings.BUCKET_OUTPUTS, key=key, body=body, content_type="video/mp4"
                )
                num, den = video["avg_frame_rate"].split("/")
                await render_repo.replace_output(
                    session, project_id=project.id, script_id=script.id, job_id=None,
                    orientation=orientation, quality=quality,
                    output_hash=hashlib.sha256(body).hexdigest(), renderer_version="demo-seed/1",
                    encoder="libx264", fps=round(int(num) / int(den)), width=int(video["width"]),
                    height=int(video["height"]), duration_ms=round(float(info["format"]["duration"]) * 1000),
                    byte_size=len(body), storage_bucket=settings.BUCKET_OUTPUTS, storage_key=key,
                    probe={"demo": True},
                    scenes=[OutputScene(position=0, script_segment_id=uuid4(), scene_index=0, chunk_hash="demo")],
                )  # fmt: skip
        await session.commit()
    return {"admin_email": admin_email, "project_id": str(project.id), "script_id": str(script.id)}


def call(http: httpx.Client, admin_email: str, method: str, path: str, **kwargs) -> httpx.Response:
    headers = {
        "Authorization": f"Bearer {mint(email=admin_email)}",
        "Idempotency-Key": f"demo-seed-{uuid4()}",
    }
    response = http.request(method, path, headers=headers, **kwargs)
    print(f"  {method} {path.split('?')[0]} -> {response.status_code}", file=sys.stderr)
    return response


def seed_api(api: str, rows: dict, employee_email: str) -> dict:
    admin = rows["admin_email"]
    base = "/v1/admin/training"
    with httpx.Client(base_url=api, timeout=60) as http:
        listing = call(http, admin, "GET", f"{base}/courses")
        if any(c["title"] == COURSE_TITLE for c in listing.json().get("items", [])):
            raise SystemExit(f"a course titled {COURSE_TITLE!r} already exists; remove it first")
        course = call(
            http,
            admin,
            "POST",
            f"{base}/courses",
            json={
                "title": COURSE_TITLE,
                "description": "A two-module demo: module 2 unlocks after module 1's assessment is passed.",
            },
        )
        course.raise_for_status()
        course_id = course.json()["id"]
        modules = []
        for title, orientation, quality, *_ in VIDEOS:
            made = call(
                http,
                admin,
                "POST",
                f"{base}/courses/{course_id}/modules",
                json={
                    "project_id": rows["project_id"],
                    "script_id": rows["script_id"],
                    "orientation": orientation,
                    "quality": quality,
                    "title": title,
                },
            )
            made.raise_for_status()
            modules.append(made.json())
        payload = {
            "threshold_percent": 70,
            "questions": [
                {
                    "prompt": prompt,
                    "options": [
                        {"label": label, "is_correct": index == right}
                        for index, label in enumerate(labels)
                    ],
                }
                for prompt, labels, right in QUESTIONS
            ],
        }
        call(
            http, admin, "PUT", f"{base}/modules/{modules[0]['id']}/assessment", json=payload
        ).raise_for_status()
        live = call(http, admin, "POST", f"{base}/modules/{modules[0]['id']}/assessment/publish")
        live.raise_for_status()
        call(http, admin, "POST", f"{base}/courses/{course_id}/publish").raise_for_status()
        invitation = call(
            http,
            admin,
            "POST",
            "/v1/admin/invitations",
            json={"email": employee_email, "role": "employee", "display_name": "Demo Employee"},
        )
        invitation_note = (
            f"{invitation.status_code} {invitation.json().get('delivery', invitation.json())}"
        )
    return {
        "course_id": course_id,
        "module_1_id": modules[0]["id"],
        "module_2_id": modules[1]["id"],
        "assessment_published_enrolments_affected": live.json()["active_enrolments_affected"],
        "invitation": invitation_note,
    }  # fmt: skip


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--employee-email", required=True)
    parser.add_argument("--api", default="http://localhost:8000")
    args = parser.parse_args()
    rows = asyncio.run(seed_rows(None))
    result = seed_api(args.api, rows, args.employee_email)
    print(json.dumps({**{k: v for k, v in rows.items() if k != "admin_email"}, **result}, indent=2))


if __name__ == "__main__":
    main()

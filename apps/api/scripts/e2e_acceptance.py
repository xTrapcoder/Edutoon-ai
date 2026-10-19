"""Drive one project through the whole pipeline over HTTP, as the web app does. DEVELOPMENT ONLY.

    uv run python scripts/e2e_acceptance.py --mode pdf --file F.pdf --title "QA-ACC pdf"
    uv run python scripts/e2e_acceptance.py --mode text --file F.txt
    uv run python scripts/e2e_acceptance.py --mode keywords --file F.pdf --keywords "a;b"
    uv run python scripts/e2e_acceptance.py --mode instructions --file F.pdf --instructions "..."
    uv run python scripts/e2e_acceptance.py --mode scan --file F.pdf

It talks to the QA API (``dev_qa_api.py`` on :8001, QA database) with the QA stand-in token, needs
``dev_qa_worker.py`` running, and stops at the first failed or stuck step with the job's own
message. Every step waits for the job it started - a successful create or upload is never counted
as a finished video. Prints one JSON summary line at the end.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx

TERMINAL = {"succeeded", "failed", "cancelled"}


class StopRun(Exception):  # noqa: N818 - a stop signal, not an error type
    """A step failed or stalled; the message says which."""


class Driver:
    def __init__(self, base: str, token: str) -> None:
        self.http = httpx.Client(
            base_url=base, headers={"Authorization": f"Bearer {token}"}, timeout=120
        )

    def say(self, step: str, **info: Any) -> None:
        stamp = time.strftime("%H:%M:%S")
        print(f"[{stamp}] {step} {json.dumps(info, default=str)[:400]}", flush=True)

    def call(self, method: str, path: str, ok: tuple[int, ...] = (200, 201, 202), **kw: Any) -> Any:
        headers = kw.pop("headers", {})
        if method != "GET":
            headers["Idempotency-Key"] = str(uuid.uuid4())
        # uvicorn closes an idle keep-alive connection after 5 s, which a 5 s poll can race with:
        # a dropped connection is retried, never mistaken for a failed step. Repeating a mutation
        # is safe because the same Idempotency-Key is sent again.
        for attempt in range(4):
            try:
                r = self.http.request(method, path, headers=headers, **kw)
                break
            except (httpx.RemoteProtocolError, httpx.ConnectError, httpx.ReadError):
                if attempt == 3:
                    raise
                time.sleep(1)
        if r.status_code not in ok:
            raise StopRun(f"{method} {path} -> {r.status_code}: {r.text[:500]}")
        return r.json() if r.content else None

    def jobs(self, pid: str) -> list[dict[str, Any]]:
        return self.call("GET", f"/v1/projects/{pid}/jobs?limit=100")["items"]  # type: ignore[no-any-return]

    def wait_job(self, pid: str, kind: str, since: str, minutes: int = 40) -> dict[str, Any]:
        """Wait for the newest job of ``kind`` created at/after ``since`` to end; return it."""
        deadline = time.time() + minutes * 60
        while time.time() < deadline:
            mine = [j for j in self.jobs(pid) if j["kind"] == kind and j["created_at"] >= since]
            if mine:
                job = max(mine, key=lambda j: j["created_at"])
                if job["status"] in TERMINAL:
                    self.say(f"job {kind}", status=job["status"], error=job["error"])
                    if job["status"] != "succeeded":
                        raise StopRun(f"{kind} {job['status']}: {job['error']}")
                    return job  # type: ignore[no-any-return]
            time.sleep(5)
        raise StopRun(f"{kind} did not finish within {minutes} minutes")

    def now(self) -> str:
        """The API's own clock (a health call's Date header), in the API's timestamp format."""
        date = parsedate_to_datetime(self.http.get("/health").headers["date"])
        return date.strftime("%Y-%m-%dT%H:%M:%S")


def run(args: argparse.Namespace) -> dict[str, Any]:
    d = Driver(args.base, "ui-qa-token")
    health = d.http.get("/health").json()
    if health.get("worker") != "ok":
        raise StopRun(f"No worker is running for this API: {health}")
    path = Path(args.file)
    text_mode = args.mode == "text"
    body: dict[str, Any] = {
        "title": args.title or f"QA-ACC {args.mode} {path.stem}",
        "source_type": "text" if text_mode else "pdf",
        "language": "en-GB",
        "target_duration_ms": args.target_seconds * 1000,
    }
    if args.mode == "instructions":
        body["instructions"] = args.instructions
    coverage = None
    by: dict[str, int] = {}
    if args.project:
        pid = args.project
        done = [
            j for j in d.jobs(pid) if j["kind"] == "extract_claims" and j["status"] == "succeeded"
        ]
        if not done:
            raise StopRun("--project needs a project whose claims were already extracted")
        d.say("resuming after claims", id=pid)
    else:
        project = d.call("POST", "/v1/projects", json=body)
        pid = project["id"]
        d.say("project created", id=pid, mode=args.mode)
        t0 = d.now()
        if text_mode:
            src = d.call(
                "POST",
                f"/v1/projects/{pid}/sources/text",
                json={"title": path.name, "text": path.read_text(encoding="utf-8")},
            )
        else:
            src = d.call(
                "POST",
                f"/v1/projects/{pid}/sources",
                files={"file": (path.name, path.read_bytes(), "application/pdf")},
            )
        for _ in range(120):
            sources = d.call("GET", f"/v1/projects/{pid}/sources")["items"]
            src = next(s for s in sources if s["id"] == src["id"])
            if src["status"] in ("parsed", "failed"):
                break
            time.sleep(5)
        d.say(
            "source settled",
            status=src["status"],
            method=src.get("extraction_method"),
            ocr_pages=src.get("ocr_page_count"),
            reason=src.get("failure_reason"),
        )
        if src["status"] != "parsed":
            return {
                "project_id": pid,
                "outcome": "source_failed",
                "reason": src.get("failure_reason"),
            }
        d.wait_job(pid, "build_evidence", t0)

        if args.mode == "keywords":
            kws = [k.strip() for k in args.keywords.split(";") if k.strip()]
            d.call("POST", f"/v1/projects/{pid}/topics", json={"keywords": kws})
        t = d.now()
        d.call("POST", f"/v1/projects/{pid}/topics/extraction", json={})
        d.wait_job(pid, "extract_topics", t)
        topics = d.call("GET", f"/v1/projects/{pid}/topics")["items"]
        d.say("topics", titles=[x["title"] for x in topics], origins=[x["origin"] for x in topics])
        # The review step: a person accepts the topics they want covered. A script needs at least
        # 30 s per accepted topic, so with a short target they accept the first few and set the
        # rest aside.
        room = max(1, args.target_seconds // 30)
        for index, topic in enumerate(topics):
            if topic["status"] == "proposed":
                status = "accepted" if index < room else "rejected"
                d.call("PATCH", f"/v1/projects/{pid}/topics/{topic['id']}", json={"status": status})
        coverage = None
        if args.mode == "keywords":
            coverage = d.call("GET", f"/v1/projects/{pid}/topics/coverage")
            d.say("keyword coverage", coverage=coverage)

        t = d.now()
        d.call("POST", f"/v1/projects/{pid}/claim-verifications/extraction", json={})
        d.wait_job(pid, "extract_claims", t)
        claims = d.call("GET", f"/v1/projects/{pid}/claim-verifications?limit=100")["items"]
        by = {}
        for c in claims:
            by[c["status"]] = by.get(c["status"], 0) + 1
        d.say("claims", total=len(claims), by_status=by)

    t = d.now()
    d.call("POST", f"/v1/projects/{pid}/scripts", json={})
    job = d.wait_job(pid, "generate_script", t, minutes=90)
    scripts = d.call("GET", f"/v1/projects/{pid}/scripts")["items"]
    script = max(scripts, key=lambda s: s["created_at"])
    sid = script["id"]
    d.say(
        "script",
        id=sid,
        status=script["status"],
        shortfall=(job.get("result") or {}).get("shortfall_reason"),
    )
    d.call("POST", f"/v1/projects/{pid}/scripts/{sid}/approval")
    d.say("script approved")

    t = d.now()
    d.call("POST", f"/v1/projects/{pid}/scripts/{sid}/synthesis", json={})
    d.wait_job(pid, "synthesise_voice", t)
    t = d.now()
    d.call("POST", f"/v1/projects/{pid}/scripts/{sid}/storyboard", json={})
    d.wait_job(pid, "generate_storyboard", t, minutes=90)
    t = d.now()
    d.call(
        "POST",
        f"/v1/projects/{pid}/scripts/{sid}/render",
        json={"orientation": "horizontal", "quality": "high"},
    )
    d.wait_job(pid, "render_video", t, minutes=40)

    url = f"/v1/projects/{pid}/scripts/{sid}/render/video?orientation=horizontal&quality=high"
    rng = d.http.get(url, headers={"Range": "bytes=0-1023"})
    d.say(
        "stream",
        status=rng.status_code,
        length=len(rng.content),
        content_range=rng.headers.get("content-range"),
    )
    out = Path(args.out) / f"{args.label or args.mode}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    with d.http.stream("GET", url) as r, out.open("wb") as fh:
        for chunk in r.iter_bytes():
            fh.write(chunk)
    d.say("downloaded", path=str(out), bytes=out.stat().st_size)
    return {
        "project_id": pid,
        "script_id": sid,
        "video": str(out),
        "claims": by,
        "stream_status": rng.status_code,
        "outcome": "video_downloaded",
        "coverage": coverage,
    }


def main() -> None:
    p = argparse.ArgumentParser()
    modes = ["pdf", "text", "keywords", "instructions", "scan"]
    p.add_argument("--mode", required=True, choices=modes)
    p.add_argument("--file", required=True)
    p.add_argument("--title")
    p.add_argument("--label")
    p.add_argument("--project", help="resume a project whose claims are already extracted")
    p.add_argument("--keywords", default="")
    p.add_argument("--instructions", default="")
    p.add_argument("--target-seconds", type=int, default=90)
    p.add_argument("--base", default="http://127.0.0.1:8001")
    p.add_argument("--out", default="var/review/acceptance")
    args = p.parse_args()
    try:
        result = run(args)
    except StopRun as stop:
        print("STOPPED:", stop)
        print("RESULT", json.dumps({"outcome": "stopped", "reason": str(stop)}))
        sys.exit(1)
    print("RESULT", json.dumps(result, default=str))


if __name__ == "__main__":
    main()

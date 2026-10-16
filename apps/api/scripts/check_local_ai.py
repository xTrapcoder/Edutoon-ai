"""Does the LOCAL AI backend (Ollama) actually work with this project's prompts? No database, no
upload, no Clerk - just the two provider clients the API and the worker use, with
``AI_BACKEND=local``.

    uv run python scripts/check_local_ai.py

The unit tests prove how our code handles a reply; only this proves how *your* models behave. It
checks, in order:

1. the Ollama server answers and the three configured models are downloaded;
2. embeddings: the vector is stored-width (zero-padded to 1536) and a related sentence scores higher
   than an unrelated one;
3. the claim judge, on a claim the passage supports, one it contradicts and one it never mentions -
   the check that matters most, because it decides what reaches a script;
4. the three largest structured answers (topics, candidate claims, storyboard scenes with a visual),
   which is where a model or Ollama's schema handling is most likely to fall over.

Exit status 0 only if every check passed. Prints timings so you can judge whether a full video is
practical on this machine. Never prints a key (a local backend has none).
"""

from __future__ import annotations

import asyncio
import math
import sys
import time
from collections.abc import Awaitable, Callable

import httpx

from edutoon.core.config import get_settings
from edutoon.domain.claim_extraction import ClaimCandidateLimits
from edutoon.domain.topic import SourcePassage, TopicLimits
from edutoon.providers.embeddings import embeddings_from_settings
from edutoon.providers.errors import describe_provider_error
from edutoon.providers.llm import (
    ClaimTopicBrief,
    LLMResponseError,
    Passage,
    StoryboardSegmentBrief,
    TopicBrief,
    llm_from_settings,
)

PASSAGE = (
    "Photosynthesis is the process by which green plants convert sunlight into chemical energy "
    "stored in glucose. It takes place in the chloroplasts, where chlorophyll absorbs mostly red "
    "and blue light. Oxygen is released as a by-product, and carbon dioxide from the air is used "
    "up. In the light-dependent reactions water is split; in the Calvin cycle glucose is built."
)

JUDGE_CASES: list[tuple[str, str, set[str]]] = [
    (
        "supported",
        "Photosynthesis converts sunlight into chemical energy stored in glucose.",
        {"supported"},
    ),
    (
        "contradicted",
        "Photosynthesis releases carbon dioxide as its main by-product.",
        {"contradicted", "unsupported"},
    ),
    (
        "never mentioned",
        "The Great Wall of China is visible from the Moon with the naked eye.",
        {"unsupported", "contradicted"},
    ),
]

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)


async def timed(name: str, check: Callable[[], Awaitable[str]]) -> None:
    started = time.perf_counter()
    try:
        detail = await check()
        ok = True
    except Exception as exc:  # a check reports its failure; it never aborts the others
        detail = describe_provider_error(exc) or f"{type(exc).__name__}: {exc}"
        ok = False
    record(name, ok, f"{detail} ({time.perf_counter() - started:.1f}s)")


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


async def main() -> int:
    settings = get_settings()
    if settings.AI_BACKEND != "local":
        print("AI_BACKEND is not 'local' - set AI_BACKEND=local in apps/api/.env first.")
        return 2
    print(
        f"Ollama {settings.OLLAMA_BASE_URL} | llm={settings.LLM_MODEL} "
        f"script={settings.SCRIPT_LLM_MODEL} embed={settings.EMBEDDING_MODEL} "
        f"num_ctx={settings.OLLAMA_NUM_CTX}\n",
        flush=True,
    )

    # 1. The server, and the models.
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            tags = (await http.get(f"{settings.OLLAMA_BASE_URL.rstrip('/')}/api/tags")).json()
    except (httpx.HTTPError, ValueError):
        record("server", False, "Ollama did not answer - is it running (`ollama serve`)?")
        return 1
    have = {m["name"] for m in tags.get("models", [])} | {
        m["name"].removesuffix(":latest") for m in tags.get("models", [])
    }
    wanted = {settings.LLM_MODEL, settings.SCRIPT_LLM_MODEL, settings.EMBEDDING_MODEL}
    missing = sorted(wanted - have)
    record(
        "models downloaded",
        not missing,
        "all present" if not missing else f"missing {missing} - run `ollama pull <name>`",
    )
    if missing:
        return 1

    embeddings = embeddings_from_settings(settings)
    llm = llm_from_settings(settings, model=settings.LLM_MODEL)

    # 2. Embeddings.
    async def embed_check() -> str:
        vectors = await embeddings.embed(
            [
                "Plants turn sunlight into sugar.",
                "Green plants make glucose using light energy.",
                "The stock market closed lower on Tuesday.",
            ]
        )
        widths = {len(v) for v in vectors}
        related, unrelated = cosine(vectors[0], vectors[1]), cosine(vectors[0], vectors[2])
        if widths != {1536}:
            raise AssertionError(f"vector widths {widths}, expected 1536")
        if related <= unrelated:
            raise AssertionError(f"related {related:.3f} <= unrelated {unrelated:.3f}")
        return f"width 1536, related {related:.3f} > unrelated {unrelated:.3f}"

    await timed("embeddings", embed_check)

    # 3. The claim judge.
    for label, claim, acceptable in JUDGE_CASES:

        async def judge(claim: str = claim, acceptable: set[str] = acceptable) -> str:
            verdict = await llm.judge_claim(claim, [Passage(id="p1", content=PASSAGE)])
            if verdict.status not in acceptable:
                raise AssertionError(f"judged '{verdict.status}', expected one of {acceptable}")
            if verdict.status == "supported" and "p1" not in verdict.cited_passage_ids:
                raise AssertionError("said supported but cited nothing")
            return f"{verdict.status}, cited {verdict.cited_passage_ids}"

        await timed(f"judge: {label}", judge)

    # 4. The big structured answers.
    topic_limits = TopicLimits(
        max_topics=settings.MAX_TOPICS_PER_EXTRACTION,
        max_depth=settings.MAX_TOPIC_DEPTH,
        max_title_chars=settings.MAX_TOPIC_TITLE_CHARS,
        max_summary_chars=settings.MAX_TOPIC_SUMMARY_CHARS,
        max_keywords_per_topic=settings.MAX_TOPIC_KEYWORDS,
        max_keyword_chars=settings.MAX_TOPIC_KEYWORD_CHARS,
        max_evidence_per_topic=settings.MAX_TOPIC_EVIDENCE,
    )
    passages = [SourcePassage(chunk_id="c1", text=PASSAGE, page_from=1)]

    async def topics_check() -> str:
        draft, usage = await llm.extract_topics(
            TopicBrief(subject="Photosynthesis", language="en-IN"),
            passages,
            [],
            limits=topic_limits,
        )
        cited = sum(len(n.evidence_chunk_ids) for n in draft.nodes)
        if not draft.nodes or not cited:
            raise AssertionError("no topics, or none citing the passage")
        return f"{len(draft.nodes)} topics, {cited} citations, {usage.output_tokens} tokens"

    await timed("topics", topics_check)

    async def claims_check() -> str:
        draft, usage = await llm.extract_claims(
            ClaimTopicBrief(title="Photosynthesis", language="en-IN"),
            passages,
            limits=ClaimCandidateLimits(
                max_claims_per_topic=settings.MAX_CLAIMS_PER_TOPIC,
                max_claim_text_chars=settings.MAX_CLAIM_TEXT_CHARS,
            ),
        )
        if not draft.candidates:
            raise AssertionError("no candidate claims")
        return f"{len(draft.candidates)} candidates, {usage.output_tokens} tokens"

    await timed("candidate claims", claims_check)

    async def storyboard_check() -> str:
        draft, usage = await llm.compose_storyboard_scenes(
            StoryboardSegmentBrief(
                segment_index=0,
                narration_text=(
                    "Green plants make their own food. Light is absorbed by chlorophyll, water is "
                    "split, and carbon dioxide from the air is turned into glucose, with oxygen "
                    "released along the way."
                ),
                orientation="horizontal",
                topic_title="Photosynthesis",
            ),
            max_scenes=settings.MAX_SCENES_PER_SEGMENT,
        )
        if not draft.scenes:
            raise AssertionError("no scenes")
        return f"{len(draft.scenes)} scenes, {usage.output_tokens} tokens"

    await timed("storyboard scenes", storyboard_check)

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed.")
    if failed:
        print("Failed: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except LLMResponseError as exc:  # pragma: no cover - a check catches its own
        print(f"Unexpected: {exc}")
        sys.exit(1)

"""Measure the sentence-level support check against the model that will run it (needs Ollama).

Two things, both on the local backend named by ``.env``:

1. ``--eval`` - a small hand-labelled set of narration sentences against a fixed claim list
   (supported paraphrases, and sentences that reverse a cause, add a requirement, a reason or an
   importance). Prints how many the judge gets right and every miss. Nothing is stored.
2. ``--project <id>`` - run the judge over an existing project's approved script, segment by
   segment, against that topic's supported claims, and print every sentence it would take out.
   Read-only: the script is not changed.

It is a measurement, not a test: a result depends on the model and its version.

    uv run python scripts/check_support_judge.py --eval            # misses only
    uv run python scripts/check_support_judge.py --eval --verbose  # every case
    uv run python scripts/check_support_judge.py --project 01a148cb-add3-73c2-b2a4-f3b6945034a7
"""

from __future__ import annotations

import argparse
import asyncio
import time
from uuid import UUID

from sqlalchemy import text

from edutoon.core.config import get_settings
from edutoon.db.session import get_sessionmaker
from edutoon.providers.llm import LLM, Claim, llm_from_settings
from edutoon.services.llm_usage import new_usage_report
from edutoon.services.narration_support import SupportLog, judge_sentence, unsupported_in

CLAIMS = [
    "Light intensity limits photosynthesis by driving the light-dependent reactions faster until "
    "another factor becomes limiting.",
    "Carbon dioxide concentration is crucial as it serves as the raw material for the Calvin "
    "cycle.",
    "Temperature affects photosynthesis through enzyme activity, which works best within a "
    "specific range.",
    "Water availability limits photosynthesis by causing plants to close their stomata, reducing "
    "carbon dioxide intake.",
    "Chlorophyll absorbs red and blue light and reflects green light.",
    "The Calvin cycle takes place in the stroma of the chloroplast.",
    "Oxygen produced during photosynthesis leaves the leaf through the stomata.",
]

# (sentence, is it supported by the claims above?)
CASES: list[tuple[str, bool]] = [
    ("Plants need water to open their stomata.", False),
    ("Water is needed for stomata to open so that carbon dioxide can enter.", False),
    ("If a plant is short of water, it closes its stomata.", True),
    ("When water is scarce, the stomata close and less carbon dioxide gets in.", True),
    ("Chlorophyll absorbs red and blue light.", True),
    ("Chlorophyll absorbs green light most strongly.", False),
    ("The Calvin cycle happens in the stroma.", True),
    ("The Calvin cycle happens in the thylakoid membranes.", False),
    ("Higher temperature always increases the rate of photosynthesis.", False),
    ("Temperature changes affect the enzymes involved in photosynthesis.", True),
    ("Carbon dioxide is the raw material the Calvin cycle uses.", True),
    ("Plants release oxygen through their roots.", False),
    ("Oxygen leaves the leaf through the stomata.", True),
    ("Photosynthesis happens only during the day because plants sleep at night.", False),
    (
        "More light speeds up the light-dependent reactions until something else limits the rate.",
        True,
    ),
    ("Light intensity is the most important factor for photosynthesis.", False),
    ("This is crucial for the plant's survival and growth.", False),
    ("For example, a cactus in a desert keeps its stomata closed all day.", False),
    ("Because of this, the plant takes in less carbon dioxide.", False),
    ("Enzymes work best within a specific temperature range.", True),
    ("Too much heat damages the enzymes permanently.", False),
    ("Chlorophyll reflects green light, which is why leaves look green.", False),
    ("Carbon dioxide is the raw material for the Calvin cycle.", True),
]


async def run_eval(llm: LLM, *, verbose: bool = False) -> None:
    claims = [Claim(id=str(n), text=claim) for n, claim in enumerate(CLAIMS)]
    report = new_usage_report()
    support = SupportLog()
    right = 0
    started = time.perf_counter()
    for sentence, expected in CASES:
        judgement = await judge_sentence(llm, report, sentence, claims, support)
        got = judgement is not None and judgement.verdict == "supported"
        if got == expected:
            right += 1
            if verbose:
                print(f"  ok  : {sentence!r} -> {'supported' if got else 'unsupported'}")
        else:
            reason = judgement.reason if judgement else "no answer"
            print(f"  MISS: {sentence!r}\n        expected supported={expected}; judge: {reason}")
    seconds = time.perf_counter() - started
    print(f"{right}/{len(CASES)} sentences right, {seconds:.0f}s, {report.total.calls} calls")


async def run_project(llm: LLM, project_id: UUID) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                text(
                    "select s.segment_index, s.narration_text, s.topic_id from script_segments s "
                    "join scripts x on x.id = s.script_id "
                    "where x.project_id = :p and x.status = 'approved' order by s.segment_index"
                ),
                {"p": project_id},
            )
        ).all()
        claims_by_topic: dict[UUID, list[Claim]] = {}
        for _, _, topic_id in rows:
            if topic_id not in claims_by_topic:
                found = (
                    await session.execute(
                        text(
                            "select id, claim_text from claim_verifications where project_id = :p "
                            "and topic_id = :t and status = 'supported' order by created_at, id"
                        ),
                        {"p": project_id, "t": topic_id},
                    )
                ).all()
                claims_by_topic[topic_id] = [Claim(id=str(i), text=t) for i, t in found]
    report = new_usage_report()
    support = SupportLog()
    started = time.perf_counter()
    flagged_total = 0
    for index, narration, topic_id in rows:
        flagged = await unsupported_in(llm, report, narration, claims_by_topic[topic_id], support)
        flagged_total += len(flagged)
        for found in flagged:
            print(f"segment {index}: UNSUPPORTED  {found.text}\n    reason: {found.reason}")
    seconds = time.perf_counter() - started
    print(
        f"{len(rows)} segments, {support.judged_sentences} sentences judged "
        f"({support.second_opinions} second opinions), {flagged_total} flagged, "
        f"{support.unchecked_sentences} unchecked, {seconds:.0f}s"
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--eval", action="store_true")
    parser.add_argument("--verbose", action="store_true", help="with --eval: print every case")
    parser.add_argument("--project", type=UUID)
    args = parser.parse_args()
    settings = get_settings()
    llm = llm_from_settings(settings, model=settings.SCRIPT_LLM_MODEL)
    if args.eval:
        await run_eval(llm, verbose=args.verbose)
    if args.project:
        await run_project(llm, args.project)


if __name__ == "__main__":
    asyncio.run(main())

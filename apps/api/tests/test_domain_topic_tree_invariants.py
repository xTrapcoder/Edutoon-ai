"""``domain.topic_tree.validate_and_build`` under arbitrary model output.

The example-based tests in ``test_domain_topic_tree.py`` pin individual rules. This
one asks the question a misbehaving model poses: for *any* draft - blank and
duplicate refs, parents that do not exist, cycles, invented and repeated evidence
ids, seed links to nothing, over-long text, too many topics - the result is either
a refusal with one of the domain's own errors, or a tree in which every guarantee
in the function's docstring holds. Nothing else may come out: no other exception,
and no tree that breaks a rule.

The drafts come from a seeded generator (``random.Random(seed)``), not from chance,
so the run is identical every time and a failure names the seed that reproduces
it. Domain only: no database, no provider, no network.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from uuid import UUID, uuid4

from edutoon.domain.topic import (
    TopicDraft,
    TopicLimits,
    TopicNodeDraft,
    TopicSeed,
    ValidatedTopicTree,
)
from edutoon.domain.topic_tree import TopicError, evidence_count_is_valid, validate_and_build

CASES = 4_000
FIRST_SEED = 1_000

_TITLES = [
    "Photosynthesis",
    " photosynthesis ",  # the same term as the one above, differently spaced and cased
    "Cells",
    "Light reactions",
    "Calvin cycle",
    "Ünïcödé",
    "",
    "   ",
    "X" * 200,
]
_KEYWORDS = ["a", "A", "", " b ", "k" * 90, "ok"]
_UNKNOWN_REF = "nope"


@dataclass(frozen=True)
class Case:
    draft: TopicDraft
    seeds: list[TopicSeed]
    known_chunk_ids: set[UUID]
    limits: TopicLimits
    first_root_position: int | None

    def build(self) -> ValidatedTopicTree:
        return validate_and_build(
            self.draft,
            seeds=self.seeds,
            known_chunk_ids=self.known_chunk_ids,
            limits=self.limits,
            first_root_position=self.first_root_position,
        )


def _case(seed: int) -> Case:
    rng = random.Random(seed)
    chunk_ids = [uuid4() for _ in range(rng.choice([0, 0, 1, 3, 6]))]

    # A stored topic's position is unique among its siblings (a unique index), so
    # the seeds' positions are distinct.
    seed_count = rng.choice([0, 0, 1, 2, 4])
    positions = sorted(rng.sample(range(10), seed_count))
    seeds = [
        TopicSeed(
            ref=f"seed-{i}",
            title=rng.choice([*_TITLES[:6], f"Seed {i}"]),
            position=positions[i],
        )
        for i in range(seed_count)
    ]
    if not chunk_ids and not seeds:  # nothing to extract from is refused before this point
        seeds = [TopicSeed(ref="seed-0", title="Only topic", position=0)]

    refs = [f"t{i}" for i in range(rng.randint(0, 12))]
    evidence_pool = [str(chunk_id) for chunk_id in chunk_ids] + [str(uuid4()), "not-a-uuid", ""]
    nodes = []
    for i in range(len(refs)):
        nodes.append(
            TopicNodeDraft(
                ref=rng.choice([refs[i]] * 12 + [refs[max(0, i - 1)], " ", ""]),
                title=rng.choice([*_TITLES, *(seed.title for seed in seeds)]),
                parent_ref=rng.choice([None] * 4 + refs + [_UNKNOWN_REF, refs[i]]),
                summary=rng.choice([None, "", "A summary. " * rng.randint(0, 60)]),
                keywords=tuple(rng.choice(_KEYWORDS) for _ in range(rng.randint(0, 12))),
                evidence_chunk_ids=tuple(
                    rng.sample(evidence_pool, rng.randint(0, min(4, len(evidence_pool))))
                ),
                seed_ref=rng.choice([None, None, None, *(seed.ref for seed in seeds), "ghost"]),
            )
        )

    limits = TopicLimits(
        max_topics=rng.randint(1, 15),
        max_depth=rng.randint(1, 4),
        max_title_chars=rng.choice([20, 120, 500]),
        max_summary_chars=rng.choice([10, 60, 400]),
        max_keywords_per_topic=rng.randint(1, 8),
        max_keyword_chars=rng.choice([5, 40]),
        max_evidence_per_topic=rng.randint(1, 6),
    )
    lowest_free = max((seed.position for seed in seeds), default=-1) + 1
    first_root = None if rng.random() < 0.5 else lowest_free + rng.randint(0, 5)
    return Case(TopicDraft(tuple(nodes)), seeds, set(chunk_ids), limits, first_root)


def _violations(case: Case, tree: ValidatedTopicTree) -> list[str]:
    """Every guarantee ``validate_and_build`` documents, checked on ``tree``."""
    problems: list[str] = []
    limits = case.limits
    nodes = tree.nodes

    if not 1 <= len(nodes) <= limits.max_topics:
        problems.append(f"{len(nodes)} topics with a limit of {limits.max_topics}")

    slots: set[tuple[int | None, int]] = set()
    seeds_seen: dict[str, int] = {}
    for i, node in enumerate(nodes):
        if node.index != i:
            problems.append(f"node {i} has index {node.index}")
        if node.parent_index is None:
            expected_depth = 0
        elif node.parent_index >= i:
            problems.append(f"node {i}'s parent comes after it")
            continue
        else:
            expected_depth = nodes[node.parent_index].depth + 1
        if node.depth != expected_depth or node.depth >= limits.max_depth:
            problems.append(f"node {i} has depth {node.depth} (expected {expected_depth})")

        slot = (node.parent_index, node.position)
        if node.position < 0 or slot in slots:
            problems.append(f"node {i} has a negative or repeated position {slot}")
        slots.add(slot)

        cited = node.evidence_chunk_ids
        if not evidence_count_is_valid(node.origin, len(cited)):
            problems.append(f"node {i} is {node.origin!r} with {len(cited)} citations")
        if not set(cited) <= case.known_chunk_ids or len(set(cited)) != len(cited):
            problems.append(f"node {i} cites something it was not shown, or twice")
        if len(cited) > limits.max_evidence_per_topic:
            problems.append(f"node {i} cites {len(cited)} passages")
        if not case.known_chunk_ids and node.origin == "source":
            problems.append(f"node {i} is 'source' but there was no material")

        if len(node.keywords) > limits.max_keywords_per_topic or not all(
            keyword.strip() and len(keyword) <= limits.max_keyword_chars
            for keyword in node.keywords
        ):
            problems.append(f"node {i} has keywords outside the limits")
        if node.summary is not None and len(node.summary) > limits.max_summary_chars:
            problems.append(f"node {i} has a {len(node.summary)}-character summary")
        if not node.title.strip():
            problems.append(f"node {i} has a blank title")

        if node.seed_ref is None:
            if node.origin == "user":
                problems.append(f"node {i} is 'user' but is not one of the user's topics")
            if len(node.title) > limits.max_title_chars:
                problems.append(f"node {i} has a {len(node.title)}-character title")
            first_free = case.first_root_position
            if node.parent_index is None and first_free is not None and node.position < first_free:
                problems.append(f"new root {i} sits before the first free position")
        else:
            if node.origin != "user" or node.parent_index is not None:
                problems.append(f"seed node {i} is not a 'user' root")
            if node.seed_ref in seeds_seen:
                problems.append(f"seed {node.seed_ref} appears twice")
            seeds_seen[node.seed_ref] = i

    # Every user topic survives, with their own wording, where it already was.
    if set(seeds_seen) != {seed.ref for seed in case.seeds}:
        problems.append("a user topic was lost, or one that is not theirs appeared")
    for seed in case.seeds:
        if seed.ref in seeds_seen:
            node = nodes[seeds_seen[seed.ref]]
            if node.title != seed.title or node.position != seed.position:
                problems.append(f"seed {seed.ref} was reworded or moved")
    return problems


def test_any_draft_is_either_refused_by_the_domain_or_satisfies_every_guarantee() -> None:
    accepted = 0
    failures: list[str] = []

    for seed in range(FIRST_SEED, FIRST_SEED + CASES):
        case = _case(seed)
        try:
            tree = case.build()
        except TopicError:
            continue  # a refusal in the domain's own words is a correct outcome
        except Exception as exc:  # noqa: BLE001 - any other exception is the bug being hunted
            failures.append(f"seed {seed}: unexpected {type(exc).__name__}: {exc}")
            continue

        accepted += 1
        problems = _violations(case, tree)
        if case.build() != tree:
            problems.append("a second run gave a different tree")
        failures.extend(f"seed {seed}: {problem}" for problem in problems)

    assert failures == [], "\n".join(failures[:10])
    # Guards against passing vacuously if the generator drifts into only-garbage.
    assert accepted >= CASES // 20, f"only {accepted} of {CASES} drafts produced a tree"

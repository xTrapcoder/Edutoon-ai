"""Topic rules - pure domain logic (rule 1: no I/O, no imports of
repositories / providers / services, and no configuration reads).

Every bound a rule needs (a character budget, a topic cap, a depth limit) is
an explicit argument, exactly as ``filter_and_validate`` takes
``words_per_minute``. The service builds those arguments from settings; this
module never learns where they came from.

What lives here:

- :func:`normalise_user_keywords` - what a user's typed keywords must look like.
- :func:`resolve_input_mode` - which of the three extraction modes the data
  present means.
- :func:`select_passages` - a deterministic, whole-document, budget-respecting
  choice of which parts of the material the model sees.
- :func:`validate_and_build` - turns the model's raw :class:`TopicDraft` into a
  :class:`ValidatedTopicTree` that is safe to persist: cited evidence only,
  hierarchy proven, origins decided by the rules and never by the model, and
  every user keyword preserved.
- :func:`validate_hierarchy`, :func:`reading_order`, :func:`accepted_topics` -
  rules over *stored* topics, whose ``depth`` and ``position`` are whatever the
  caller wrote. These validate; they never rewrite.

Depth and position are assigned exactly once, when a *new* tree is built from a
draft (the model cannot supply them - a draft carries only parent links). From
then on they are ordinary stored facts: nothing here recomputes them for an
existing tree.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from uuid import UUID

from edutoon.domain.topic import (
    MODE_KEYWORDS,
    MODE_PDF,
    MODE_PDF_KEYWORDS,
    ORIGIN_SOURCE,
    ORIGIN_SUGGESTED,
    ORIGIN_USER,
    STATUS_ACCEPTED,
    InputMode,
    PassageSelection,
    SourcePassage,
    TopicDraft,
    TopicLike,
    TopicLimits,
    TopicOrigin,
    TopicSeed,
    ValidatedTopicNode,
    ValidatedTopicTree,
)

# The smallest leading excerpt worth showing the model for a passage, in
# characters. A tuning constant of the sampling strategy, not configuration:
# what *is* configurable - and is passed in - is the total budget.
DEFAULT_MIN_PASSAGE_CHARS = 400


# --- errors -----------------------------------------------------------------------


class TopicError(Exception):
    """Base class for every rule violation raised by this module. A service
    translates it into the application error that fits its context (a 4xx for
    caller input, a failed job for a bad model response); the domain itself
    stays free of the web layer.
    """


class NoTopicInputError(TopicError):
    """There is nothing to extract topics from: no material and no keywords."""


class InvalidKeywordError(TopicError):
    """A keyword the user supplied breaks a rule (blank, too long, too many)."""


class InvalidTopicError(TopicError):
    """A topic the model proposed is malformed (blank or over-long title,
    blank ref).
    """


class NoTopicsError(TopicError):
    """Validation left no topic at all - every proposal was dropped and there
    were no user keywords to fall back on. Reported, never persisted as an
    empty success.
    """


class TopicTreeError(TopicError):
    """The hierarchy itself is broken."""


class DuplicateRefError(TopicTreeError):
    def __init__(self, ref: str) -> None:
        super().__init__(f"Topic ref {ref!r} appears more than once.")
        self.ref = ref


class UnknownParentError(TopicTreeError):
    def __init__(self, child: str, parent: str) -> None:
        super().__init__(f"Topic {child} names a parent {parent} that does not exist.")
        self.child = child
        self.parent = parent


class CyclicHierarchyError(TopicTreeError):
    def __init__(self, member: str) -> None:
        super().__init__(f"Topic {member} is its own ancestor: the hierarchy has a cycle.")
        self.member = member


class TopicDepthError(TopicTreeError):
    """A depth is inconsistent with the parent chain, or beyond the limit."""


class TopicOrderError(TopicTreeError):
    """A position is negative, or two siblings share one."""


class TopicLimitError(TopicTreeError):
    def __init__(self, count: int, limit: int) -> None:
        super().__init__(f"The tree has {count} topics; the limit is {limit}.")
        self.count = count
        self.limit = limit


# --- text normalisation ----------------------------------------------------------------


def normalise_term(text: str) -> str:
    """NFKC-normalise and collapse every run of whitespace to one space."""
    return " ".join(unicodedata.normalize("NFKC", text).split())


def term_key(text: str) -> str:
    """The key two spellings of the same term share: normalised and
    case-folded. ``"Photosynthesis"`` and ``" photosynthesis "`` collide.
    """
    return normalise_term(text).casefold()


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _truncate(text: str, max_chars: int) -> str:
    """``text`` cut to at most ``max_chars``, at a word boundary where one is
    reasonably close, ending in an ellipsis to show it was cut.
    """
    if len(text) <= max_chars:
        return text
    cut = text[: max_chars - 1]
    head = cut.rpartition(" ")[0]
    if len(head) >= max_chars // 2:
        cut = head
    return cut.rstrip(" ,;:-") + "…"


# --- user keywords ---------------------------------------------------------------------


def normalise_user_keywords(raw: Sequence[str], *, max_keywords: int, max_length: int) -> list[str]:
    """Clean the keywords a user typed, strictly.

    Each is normalised (:func:`normalise_term`); duplicates that differ only in
    case or spacing collapse to the first spelling given. Anything wrong -
    blank, longer than ``max_length``, or more than ``max_keywords`` distinct
    ones - raises :class:`InvalidKeywordError` rather than being quietly fixed:
    these are the user's own words, so a problem is theirs to correct.
    """
    if max_keywords < 1 or max_length < 1:
        raise ValueError("max_keywords and max_length must be at least 1.")

    seen: set[str] = set()
    cleaned: list[str] = []
    for item in raw:
        term = normalise_term(item)
        if not term:
            raise InvalidKeywordError("A keyword must not be blank.")
        if len(term) > max_length:
            raise InvalidKeywordError(
                f"The keyword {term[:30]!r}... is {len(term)} characters; "
                f"the limit is {max_length}."
            )
        key = term.casefold()
        if key not in seen:
            seen.add(key)
            cleaned.append(term)

    if len(cleaned) > max_keywords:
        raise InvalidKeywordError(
            f"{len(cleaned)} distinct keywords were given; the limit is {max_keywords}."
        )
    return cleaned


# --- input mode --------------------------------------------------------------------------


def resolve_input_mode(*, has_passages: bool, has_seeds: bool) -> InputMode:
    """Which extraction mode the data present means. Never stored: it is
    always recomputed from what exists, so it cannot go stale.

    Raises :class:`NoTopicInputError` when neither is present.
    """
    if has_passages and has_seeds:
        return MODE_PDF_KEYWORDS
    if has_passages:
        return MODE_PDF
    if has_seeds:
        return MODE_KEYWORDS
    raise NoTopicInputError(
        "There is nothing to extract topics from: upload a PDF with readable text "
        "or add at least one keyword."
    )


def evidence_count_is_valid(origin: str, evidence_count: int) -> bool:
    """The evidence invariant per origin: a ``source`` topic must cite at least
    one chunk; a ``suggested`` topic has no material behind it, so cites none;
    a ``user`` topic may or may not, depending on whether the material happens
    to cover it. An unknown origin is never valid.
    """
    if origin == ORIGIN_SOURCE:
        return evidence_count >= 1
    if origin == ORIGIN_SUGGESTED:
        return evidence_count == 0
    if origin == ORIGIN_USER:
        return evidence_count >= 0
    return False


# --- passage selection -----------------------------------------------------------------


def select_passages(
    chunks: Sequence[SourcePassage],
    *,
    budget_chars: int,
    min_chars_per_passage: int = DEFAULT_MIN_PASSAGE_CHARS,
) -> PassageSelection:
    """Choose which parts of the material the model sees, within
    ``budget_chars`` characters of passage text.

    ``chunks`` are in reading order (the order of the document, or of the
    sources one after another). The choice is a pure function of the chunks
    and the budget: no randomness, no floats, no dependence on anything else,
    so the same input always yields the same selection.

    - **Fits the budget:** every chunk is returned in full; ``truncated`` is
      ``False``.
    - **Over budget:** the selection is spread across the *whole* document,
      never just its beginning.

      1. The number of chunks ``k`` is the largest for which an even spread of
         ``k`` chunks can each be given ``min(length, min_chars_per_passage)``
         characters. So a long document is sampled sparsely and a short-paged
         one densely - the budget is used, not wasted.
      2. Those ``k`` chunks are spaced evenly from the first to the last
         inclusive (integer arithmetic, so no rounding drift). The first
         chunk (title, introduction) and last (conclusion) are always
         included once more than one is chosen.
      3. The budget is shared between them by *water-filling*: a chunk shorter
         than its share is kept whole and its unused characters go to the
         longer ones, so the whole budget is used. Longer chunks are cut to
         their leading text and flagged ``excerpt``.

    ``chars_used`` never exceeds ``budget_chars``.

    This is one strategy behind a small seam: chunks and a budget in, a
    :class:`PassageSelection` out, nothing persisted. A windowed / map-reduce
    strategy can replace it - returning one selection per window - without
    touching the database model.
    """
    if budget_chars < 1:
        raise ValueError("budget_chars must be at least 1.")
    if min_chars_per_passage < 1:
        raise ValueError("min_chars_per_passage must be at least 1.")

    chunks_total = len(chunks)
    chars_total = sum(len(chunk.text) for chunk in chunks)

    # A chunk with no text can contribute nothing to choose or cut.
    candidates = [chunk for chunk in chunks if chunk.text]
    if not candidates:
        return PassageSelection((), chunks_total, 0, chars_total, 0, False)

    if chars_total <= budget_chars:
        return PassageSelection(
            passages=tuple(candidates),
            chunks_total=chunks_total,
            chunks_used=len(candidates),
            chars_total=chars_total,
            chars_used=chars_total,
            truncated=False,
        )

    lengths = [len(chunk.text) for chunk in candidates]
    picked = _spread_indices(lengths, budget_chars, min_chars_per_passage)
    allowances = _allocate([lengths[i] for i in picked], budget_chars)

    passages: list[SourcePassage] = []
    for i, allowance in zip(picked, allowances, strict=True):
        chunk = candidates[i]
        if allowance >= lengths[i]:
            passages.append(chunk)
            continue
        text = chunk.text[:allowance].rstrip()
        if text:
            passages.append(replace(chunk, text=text, excerpt=True))

    return PassageSelection(
        passages=tuple(passages),
        chunks_total=chunks_total,
        chunks_used=len(passages),
        chars_total=chars_total,
        chars_used=sum(len(passage.text) for passage in passages),
        truncated=True,
    )


def _even_positions(count: int, k: int) -> list[int]:
    """``k`` strictly increasing positions in ``range(count)`` spaced evenly,
    always including the first and, when ``k > 1``, the last. Integer maths
    only (round-half-up), so the result is exact and reproducible. Requires
    ``1 <= k <= count``: the spacing is then at least one, which is what keeps
    the positions distinct.
    """
    if k == 1:
        return [0]
    step_num = 2 * (count - 1)
    denominator = 2 * (k - 1)
    return [(i * step_num + (k - 1)) // denominator for i in range(k)]


def _spread_indices(lengths: Sequence[int], budget: int, min_chars: int) -> list[int]:
    """The indices of the chunks to show: the largest even spread that still
    lets every chosen chunk have ``min(its length, min_chars)`` characters.
    """
    count = len(lengths)
    for k in range(count, 0, -1):
        positions = _even_positions(count, k)
        needed = sum(min(lengths[p], min_chars) for p in positions)
        if needed <= budget:
            return positions
    # Even one chunk's minimum exceeds the budget: show the opening of the
    # document, cut to whatever the budget allows.
    return [0]


def _allocate(lengths: Sequence[int], budget: int) -> list[int]:
    """Share ``budget`` characters between chunks of the given ``lengths`` by
    water-filling. Each chunk gets at most its own length; the total is the
    budget exactly when the lengths do not fit in it. Deterministic: ties are
    broken by position, and the leftover characters (fewer than one per
    truncated chunk) go one each to the earliest truncated chunks.
    """
    if sum(lengths) <= budget:
        return list(lengths)

    allocation = [0] * len(lengths)
    remaining = budget
    left = len(lengths)
    by_length = sorted(range(len(lengths)), key=lambda i: (lengths[i], i))

    cut = len(by_length)
    for rank, i in enumerate(by_length):
        share = remaining // left
        if lengths[i] > share:
            cut = rank
            break
        allocation[i] = lengths[i]
        remaining -= lengths[i]
        left -= 1

    # The lengths do not all fit, so the loop above always breaks with at
    # least one chunk still to be truncated.
    truncated = sorted(by_length[cut:])
    level, extra = divmod(remaining, left)
    for rank, i in enumerate(truncated):
        allocation[i] = level + (1 if rank < extra else 0)
    return allocation


# --- draft validation ----------------------------------------------------------------


@dataclass(slots=True)
class _Entry:
    """One topic while a draft is being validated (working state only)."""

    title: str
    summary: str | None
    keywords: tuple[str, ...]
    evidence: tuple[UUID, ...]
    seed: TopicSeed | None
    parent: int | None
    survives: bool = True


def _coerce_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except (ValueError, AttributeError):
        return None


def _clean_title(raw: str, limits: TopicLimits) -> str:
    title = normalise_term(raw)
    if not title:
        raise InvalidTopicError("A topic title must not be blank.")
    if len(title) > limits.max_title_chars:
        raise InvalidTopicError(
            f"The topic title {title[:30]!r}... is {len(title)} characters; "
            f"the limit is {limits.max_title_chars}."
        )
    return title


def _clean_summary(raw: str | None, max_chars: int) -> str | None:
    if raw is None:
        return None
    summary = normalise_term(raw)
    return _truncate(summary, max_chars) if summary else None


def _clean_keywords(raw: Sequence[str], limits: TopicLimits) -> tuple[str, ...]:
    """The model's keywords for one topic, tidied leniently: blanks, duplicates
    and over-long terms are dropped and the list is capped. Unlike a user's
    keywords these are a by-product, so a wobble is repaired, not fatal.
    """
    seen: set[str] = set()
    kept: list[str] = []
    for item in raw:
        term = normalise_term(item)
        if not term or len(term) > limits.max_keyword_chars:
            continue
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        kept.append(term)
        if len(kept) == limits.max_keywords_per_topic:
            break
    return tuple(kept)


def _filter_evidence(
    raw: Sequence[str], known_chunk_ids: Collection[UUID], cap: int
) -> tuple[UUID, ...]:
    """Keep only ids the model was actually shown, de-duplicated in first-seen
    order and capped. An id it invented, or one that is not even a UUID, is
    dropped without comment - never trusted.
    """
    kept: dict[UUID, None] = {}
    for item in raw:
        chunk_id = _coerce_uuid(item)
        if chunk_id is not None and chunk_id in known_chunk_ids:
            kept.setdefault(chunk_id)
            if len(kept) == cap:
                break
    return tuple(kept)


def _first_cycle_member[K](parent_of: Mapping[K, K | None]) -> K | None:
    """Some member of a cycle in the parent map, or ``None`` if it is acyclic.
    A node that is its own parent is a cycle of one.
    """
    state: dict[K, int] = {}  # 1 = on the current walk, 2 = known acyclic
    for start in parent_of:
        walk: list[K] = []
        node: K | None = start
        while node is not None and node in parent_of and state.get(node, 0) == 0:
            state[node] = 1
            walk.append(node)
            node = parent_of[node]
        if node is not None and state.get(node) == 1:
            return node
        for visited in walk:
            state[visited] = 2
    return None


def validate_and_build(
    draft: TopicDraft,
    *,
    seeds: Sequence[TopicSeed],
    known_chunk_ids: Collection[UUID],
    limits: TopicLimits,
    first_root_position: int | None = None,
) -> ValidatedTopicTree:
    """Turn the model's raw ``draft`` into a tree that is safe to persist.

    ``known_chunk_ids`` are the chunks the model was actually shown (empty when
    it was given no material); ``seeds`` are the user's own topics. Which of
    the three modes applies follows from those two (:func:`resolve_input_mode`).

    What the rules guarantee about the result:

    - **Evidence.** Cited ids are narrowed to ``known_chunk_ids``, de-duplicated
      and capped. When there was material, a proposed topic that cites nothing
      is dropped (its cited descendants move up to its nearest surviving
      ancestor, or become roots) and counted in ``dropped_uncited``.
    - **Origin is decided here, never by the model.** A user seed is ``user``;
      any other surviving topic is ``source`` if it cites material, and
      ``suggested`` only when there was no material to cite. Consequently
      :func:`evidence_count_is_valid` holds for every node.
    - **Every user keyword survives, verbatim.** The model may link a node to a
      seed (``seed_ref``); if it did not but a node's title matches a seed's,
      that node is taken to be the seed; a seed nothing claims is added as a
      bare root. A seed's title is always the user's own wording, and a seed is
      always a root - a parent the model gave it is ignored.
    - **Hierarchy.** Unknown parents, duplicate refs and cycles raise; depth is
      assigned from the parent chain and must stay below ``limits.max_depth``;
      the total may not exceed ``limits.max_topics``.
    - **Order.** ``nodes`` are in reading order (depth-first, a parent before
      its children). Seeds keep their existing positions and come first; new
      roots follow from ``first_root_position``; children are numbered from 0
      in the order the model gave them. ``first_root_position`` is the caller's
      first free root position (``repositories.project_topics.next_position``)
      and defaults to one past the highest seed position.

    A blank or over-long *title* raises :class:`InvalidTopicError`; other text
    the model wrote (summary, keywords) is tidied or trimmed instead. Raises
    :class:`NoTopicsError` if nothing survives.
    """
    mode = resolve_input_mode(has_passages=bool(known_chunk_ids), has_seeds=bool(seeds))
    has_passages = mode != MODE_KEYWORDS

    seed_by_ref: dict[str, TopicSeed] = {}
    for seed in seeds:
        if seed.ref in seed_by_ref:
            raise ValueError(f"Seed ref {seed.ref!r} was supplied more than once.")
        seed_by_ref[seed.ref] = seed
    lowest_free = max((seed.position for seed in seeds), default=-1) + 1
    if first_root_position is None:
        first_root_position = lowest_free
    elif first_root_position < lowest_free:
        raise ValueError(
            f"first_root_position {first_root_position} collides with an existing seed "
            f"position; it must be at least {lowest_free}."
        )

    index_of: dict[str, int] = {}
    for i, node in enumerate(draft.nodes):
        ref = node.ref.strip()
        if not ref:
            raise InvalidTopicError("A topic ref must not be blank.")
        if ref in index_of:
            raise DuplicateRefError(ref)
        index_of[ref] = i

    seed_of = _claim_seeds(draft, seeds, seed_by_ref)

    entries: list[_Entry] = []
    for i, node in enumerate(draft.nodes):
        linked_seed = seed_of.get(i)
        # A user topic is always a root, whatever parent the model gave it.
        parent_ref = None if linked_seed is not None else _blank_to_none(node.parent_ref)
        parent: int | None = None
        if parent_ref is not None:
            if parent_ref not in index_of:
                raise UnknownParentError(node.ref, parent_ref)
            parent = index_of[parent_ref]
        entries.append(
            _Entry(
                title=(
                    linked_seed.title
                    if linked_seed is not None
                    else _clean_title(node.title, limits)
                ),
                summary=_clean_summary(node.summary, limits.max_summary_chars),
                keywords=_clean_keywords(node.keywords, limits),
                evidence=_filter_evidence(
                    node.evidence_chunk_ids, known_chunk_ids, limits.max_evidence_per_topic
                ),
                seed=linked_seed,
                parent=parent,
            )
        )

    cycle = _first_cycle_member(dict(enumerate(entry.parent for entry in entries)))
    if cycle is not None:
        raise CyclicHierarchyError(draft.nodes[cycle].ref)

    claimed = {seed.ref for seed in seed_of.values()}
    for seed in seeds:
        if seed.ref not in claimed:
            entries.append(
                _Entry(
                    title=seed.title,
                    summary=None,
                    keywords=(),
                    evidence=(),
                    seed=seed,
                    parent=None,
                )
            )

    dropped = 0
    for entry in entries:
        if entry.seed is None and not entry.evidence and has_passages:
            entry.survives = False
            dropped += 1

    survivors = sum(1 for entry in entries if entry.survives)
    if survivors == 0:
        raise NoTopicsError(
            "No topic survived validation: none of the proposed topics cited the material."
        )
    if survivors > limits.max_topics:
        raise TopicLimitError(survivors, limits.max_topics)

    return ValidatedTopicTree(
        mode=mode,
        nodes=_arrange(entries, seeds, has_passages, limits, first_root_position),
        dropped_uncited=dropped,
    )


def _claim_seeds(
    draft: TopicDraft, seeds: Sequence[TopicSeed], seed_by_ref: Mapping[str, TopicSeed]
) -> dict[int, TopicSeed]:
    """Which draft node stands for which seed. Explicit links the model made
    (``seed_ref``) win; a seed left over is then matched to an unlinked node with
    the same title. A seed is claimed at most once.
    """
    seed_of: dict[int, TopicSeed] = {}
    claimed: set[str] = set()

    for i, node in enumerate(draft.nodes):
        ref = _blank_to_none(node.seed_ref)
        if ref is not None and ref in seed_by_ref and ref not in claimed:
            seed_of[i] = seed_by_ref[ref]
            claimed.add(ref)

    unclaimed_by_key: dict[str, list[TopicSeed]] = {}
    for seed in seeds:
        if seed.ref not in claimed:
            unclaimed_by_key.setdefault(term_key(seed.title), []).append(seed)

    for i, node in enumerate(draft.nodes):
        if i in seed_of:
            continue
        matches = unclaimed_by_key.get(term_key(node.title))
        if matches:
            seed_of[i] = matches.pop(0)
    return seed_of


def _arrange(
    entries: Sequence[_Entry],
    seeds: Sequence[TopicSeed],
    has_passages: bool,
    limits: TopicLimits,
    first_root_position: int,
) -> tuple[ValidatedTopicNode, ...]:
    """Lay surviving entries out in reading order, assigning depth, position
    and origin. Dropped entries are skipped; their surviving descendants are
    attached to the nearest surviving ancestor.
    """

    def nearest_survivor(index: int | None) -> int | None:
        while index is not None and not entries[index].survives:
            index = entries[index].parent
        return index

    seed_order = {seed.ref: n for n, seed in enumerate(seeds)}
    children: dict[int | None, list[int]] = {}
    for i, entry in enumerate(entries):
        if entry.survives:
            children.setdefault(nearest_survivor(entry.parent), []).append(i)

    # Roots: the user's seeds first (in the order the user gave them), then the
    # model's own roots in the order it proposed them.
    def root_key(i: int) -> tuple[int, int]:
        seed = entries[i].seed
        return (0, seed_order[seed.ref]) if seed is not None else (1, i)

    children[None] = sorted(children.get(None, []), key=root_key)

    # Iterative depth-first walk. Each stack item is
    # (entry index, parent's arranged index, depth, position).
    root_items: list[tuple[int, int | None, int, int]] = []
    next_root = first_root_position
    for i in children[None]:
        seed = entries[i].seed
        if seed is not None:
            position = seed.position  # an existing row: keep where it already is
        else:
            position = next_root
            next_root += 1
        root_items.append((i, None, 0, position))
    stack = list(reversed(root_items))

    arranged: list[ValidatedTopicNode] = []
    while stack:
        i, parent_index, depth, position = stack.pop()
        if depth >= limits.max_depth:
            raise TopicDepthError(
                f"A topic sits at depth {depth}; at most {limits.max_depth} levels are allowed."
            )
        entry = entries[i]
        index = len(arranged)
        arranged.append(
            ValidatedTopicNode(
                index=index,
                parent_index=parent_index,
                title=entry.title,
                summary=entry.summary,
                keywords=entry.keywords,
                origin=_origin(entry, has_passages),
                depth=depth,
                position=position,
                evidence_chunk_ids=entry.evidence,
                seed_ref=entry.seed.ref if entry.seed is not None else None,
            )
        )
        stack.extend(
            reversed([(child, index, depth + 1, n) for n, child in enumerate(children.get(i, []))])
        )
    return tuple(arranged)


def _origin(entry: _Entry, has_passages: bool) -> TopicOrigin:
    if entry.seed is not None:
        return ORIGIN_USER
    if entry.evidence:
        return ORIGIN_SOURCE
    # Only reachable without material: an uncited topic is dropped whenever
    # there was something to cite.
    assert not has_passages
    return ORIGIN_SUGGESTED


# --- rules over stored topics -----------------------------------------------------------


def validate_hierarchy(topics: Sequence[TopicLike], *, max_depth: int | None = None) -> None:
    """Prove a project's stored topics form a consistent forest, or raise.

    ``depth`` and ``position`` are read as the caller stored them and checked,
    never recomputed: every parent must exist, there must be no cycle, a root
    has depth 0 and any other topic exactly its parent's depth plus one, depth
    stays below ``max_depth`` when one is given, and siblings have distinct,
    non-negative positions.
    """
    by_id = {topic.id: topic for topic in topics}

    for topic in topics:
        if topic.parent_id is not None and topic.parent_id not in by_id:
            raise UnknownParentError(str(topic.id), str(topic.parent_id))

    cycle = _first_cycle_member({topic.id: topic.parent_id for topic in topics})
    if cycle is not None:
        raise CyclicHierarchyError(str(cycle))

    taken: set[tuple[UUID | None, int]] = set()
    for topic in topics:
        parent = by_id[topic.parent_id] if topic.parent_id is not None else None
        expected_depth = 0 if parent is None else parent.depth + 1
        if topic.depth != expected_depth:
            raise TopicDepthError(
                f"Topic {topic.id} has depth {topic.depth}; its place in the "
                f"hierarchy makes that {expected_depth}."
            )
        if max_depth is not None and topic.depth >= max_depth:
            raise TopicDepthError(
                f"Topic {topic.id} is at depth {topic.depth}; at most {max_depth} "
                "levels are allowed."
            )
        if topic.position < 0:
            raise TopicOrderError(f"Topic {topic.id} has a negative position.")
        slot = (topic.parent_id, topic.position)
        if slot in taken:
            raise TopicOrderError(
                f"Two topics share position {topic.position} under the same parent."
            )
        taken.add(slot)


def reading_order[T: TopicLike](topics: Sequence[T]) -> list[T]:
    """``topics`` in reading order: depth-first, each topic followed by its
    children, siblings by ``position`` (then id, so ties are stable).

    Raises if the structure cannot be walked - a missing parent or a cycle.
    It does not insist that stored depths are consistent; use
    :func:`validate_hierarchy` for that.
    """
    by_id = {topic.id: topic for topic in topics}
    children: dict[UUID | None, list[T]] = {}
    for topic in topics:
        if topic.parent_id is not None and topic.parent_id not in by_id:
            raise UnknownParentError(str(topic.id), str(topic.parent_id))
        children.setdefault(topic.parent_id, []).append(topic)

    cycle = _first_cycle_member({topic.id: topic.parent_id for topic in topics})
    if cycle is not None:
        raise CyclicHierarchyError(str(cycle))

    for siblings in children.values():
        siblings.sort(key=lambda topic: (topic.position, str(topic.id)))

    ordered: list[T] = []
    stack = list(reversed(children.get(None, [])))
    while stack:
        topic = stack.pop()
        ordered.append(topic)
        stack.extend(reversed(children.get(topic.id, [])))
    return ordered


def accepted_topics[T: TopicLike](topics: Sequence[T]) -> list[T]:
    """The topics that count as accepted, in reading order.

    A topic counts only if it *and every ancestor* is ``accepted``: rejecting
    (or not yet accepting) a parent takes its whole subtree out, so a reviewer
    can drop a section with one decision. This is what later stages should
    build on - not each row's own status in isolation.
    """
    accepted_ids: set[UUID] = set()
    result: list[T] = []
    for topic in reading_order(topics):  # a parent always precedes its children
        parent_counts = topic.parent_id is None or topic.parent_id in accepted_ids
        if topic.status == STATUS_ACCEPTED and parent_counts:
            accepted_ids.add(topic.id)
            result.append(topic)
    return result

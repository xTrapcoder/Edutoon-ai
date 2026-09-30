"""Topic vocabulary and value objects for Phase 5's content understanding.

These live in the domain, not in ``providers`` or ``repositories``, so that
pure domain code (:mod:`edutoon.domain.topic_tree`) can name them without
importing either (rule 1). ``providers.llm`` imports the draft types back the
other way, which the layer order permits - the same arrangement as
:mod:`edutoon.domain.script`.

The vocabulary mirrors the ``project_topics`` CHECK constraints
(``ck_project_topics_origin`` from migration 0009, ``ck_project_topics_status``
from 0003) rather than re-deriving them: the migrations stay the source of
truth for what the database accepts, and these ``Literal`` types are how the
rest of the code names those values.

Three kinds of thing are defined here:

- *Inputs* the rules work from: :class:`SourcePassage` (a slice of the
  uploaded material), :class:`TopicSeed` (a topic the user typed in).
- The model's *raw proposal*: :class:`TopicDraft` / :class:`TopicNodeDraft`.
  Nothing in it is trusted yet - :func:`edutoon.domain.topic_tree.validate_and_build`
  is what turns a draft into something safe to persist.
- The *validated result*: :class:`ValidatedTopicTree` / :class:`ValidatedTopicNode`.

Hierarchy and order are the only relationships a topic has: a parent, a
depth and a position among its siblings. There are deliberately no lateral
links ("related to", "prerequisite of") - reading order is the teaching
sequence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

# --- vocabulary (mirrors the project_topics CHECK constraints) ---------------------

TopicOrigin = Literal["source", "user", "suggested"]
"""Where a topic came from. ``source``: extracted from the uploaded material
and backed by citations. ``user``: typed in by the user. ``suggested``: proposed
by the model with no material behind it - never verified."""

TopicStatus = Literal["proposed", "accepted", "rejected"]
"""The user's review decision. Any status may move to any other: a reviewer is
free to change their mind, so there is no transition graph to enforce."""

InputMode = Literal["pdf", "keywords", "pdf_keywords"]
"""What an extraction was given to work from. Derived from the data present,
never stored: ``pdf`` (parsed material only), ``keywords`` (user topics only),
``pdf_keywords`` (both)."""

ORIGIN_SOURCE: TopicOrigin = "source"
ORIGIN_USER: TopicOrigin = "user"
ORIGIN_SUGGESTED: TopicOrigin = "suggested"

STATUS_PROPOSED: TopicStatus = "proposed"
STATUS_ACCEPTED: TopicStatus = "accepted"
STATUS_REJECTED: TopicStatus = "rejected"

KEYWORD_ONLY_UNSUPPORTED = (
    "Keyword-only projects are not supported yet. Every claim in a video must be checked "
    "against your own source material, and keywords alone give nothing to check against. "
    "Upload a PDF - you can add keywords to guide it."
)
"""What the owner is told when they ask for a keyword-only project (or a topic extraction with
keywords and no parsed material). Temporary: an unverified, AI-knowledge mode may come later as a
separate, clearly labelled mode, but it must never be presented as a verified one."""

MODE_PDF: InputMode = "pdf"
MODE_KEYWORDS: InputMode = "keywords"
MODE_PDF_KEYWORDS: InputMode = "pdf_keywords"

TOPIC_ORIGINS: frozenset[str] = frozenset({ORIGIN_SOURCE, ORIGIN_USER, ORIGIN_SUGGESTED})
TOPIC_STATUSES: frozenset[str] = frozenset({STATUS_PROPOSED, STATUS_ACCEPTED, STATUS_REJECTED})


# --- limits -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TopicLimits:
    """Every bound :func:`~edutoon.domain.topic_tree.validate_and_build`
    enforces, passed in explicitly.

    There are deliberately no defaults. The domain never reads configuration
    (the same discipline as ``filter_and_validate``'s ``words_per_minute``):
    the service builds one of these from settings, so the numbers have exactly
    one home and cannot drift between here and the config.

    ``max_depth`` is a count of *levels*: ``3`` allows depths ``0``, ``1`` and
    ``2``.
    """

    max_topics: int
    max_depth: int
    max_title_chars: int
    max_summary_chars: int
    max_keywords_per_topic: int
    max_keyword_chars: int
    max_evidence_per_topic: int

    def __post_init__(self) -> None:
        for name in (
            "max_topics",
            "max_depth",
            "max_title_chars",
            "max_summary_chars",
            "max_keywords_per_topic",
            "max_keyword_chars",
            "max_evidence_per_topic",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1.")


# --- inputs -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourcePassage:
    """A piece of the uploaded material offered to the model.

    ``chunk_id`` is the stringified ``source_chunks.id`` - what the model is
    told to cite. ``excerpt`` is ``True`` when ``text`` is only the leading part
    of the chunk, cut to fit the input budget.
    """

    chunk_id: str
    text: str
    page_from: int | None = None
    page_to: int | None = None
    excerpt: bool = False


@dataclass(frozen=True, slots=True)
class TopicSeed:
    """A topic the user typed in, offered to the model as something to cover.

    ``ref`` identifies the existing ``project_topics`` row (its stringified
    id). ``position`` is that row's current position among the roots -
    supplied by the caller, never derived - so new root topics can be placed
    after it. A user topic is always a root.
    """

    ref: str
    title: str
    position: int = 0


class TopicLike(Protocol):
    """The fields the hierarchy rules read from a stored topic.

    Structural, so ``repositories.project_topics.ProjectTopicRecord`` satisfies
    it as-is - the domain works on stored rows without importing the
    repository. ``depth`` and ``position`` are whatever the caller stored:
    the rules validate them, they never rewrite them.
    """

    @property
    def id(self) -> UUID: ...
    @property
    def parent_id(self) -> UUID | None: ...
    @property
    def depth(self) -> int: ...
    @property
    def position(self) -> int: ...
    @property
    def status(self) -> str: ...


# --- the model's raw proposal ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TopicNodeDraft:
    """One topic as the model proposed it.

    ``ref`` is a handle the *model* invents so that ``parent_ref`` can point
    at another node in the same draft; it is never persisted. There is no
    ``depth`` or ``position`` here on purpose: the model does not get to say
    where a topic sits, only which topic it belongs under. ``seed_ref`` names
    the user topic (:attr:`TopicSeed.ref`) this node covers, if any.
    ``evidence_chunk_ids`` are the passage ids the model says it drew on.
    """

    ref: str
    title: str
    parent_ref: str | None = None
    summary: str | None = None
    keywords: tuple[str, ...] = ()
    evidence_chunk_ids: tuple[str, ...] = ()
    seed_ref: str | None = None


@dataclass(frozen=True, slots=True)
class TopicDraft:
    nodes: tuple[TopicNodeDraft, ...] = ()


# --- the validated result --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ValidatedTopicNode:
    """A topic that has passed :func:`~edutoon.domain.topic_tree.validate_and_build`.

    ``parent_index`` is the index of the parent within
    :attr:`ValidatedTopicTree.nodes` (always smaller than this node's own
    index, because nodes are in reading order). ``seed_ref`` is set exactly
    when this node *is* an existing user topic: the service updates that row
    (evidence, summary, keywords) instead of inserting a new one, and
    ``position`` is then the row's existing position. Every other node is
    new, and its ``origin`` was decided by the rules - never taken from the
    model.
    """

    index: int
    parent_index: int | None
    title: str
    summary: str | None
    keywords: tuple[str, ...]
    origin: TopicOrigin
    depth: int
    position: int
    evidence_chunk_ids: tuple[UUID, ...]
    seed_ref: str | None = None


@dataclass(frozen=True, slots=True)
class ValidatedTopicTree:
    """The result of validation: ``nodes`` in reading order (a parent always
    precedes its children), the input ``mode`` the extraction ran in, and how
    many model-proposed topics were dropped for citing nothing.
    """

    mode: InputMode
    nodes: tuple[ValidatedTopicNode, ...]
    dropped_uncited: int = 0


# --- passage selection result --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PassageSelection:
    """What :func:`~edutoon.domain.topic_tree.select_passages` chose.

    ``truncated`` is ``True`` whenever the model is *not* seeing the whole
    document - some chunk was left out or cut short - so a caller can say so
    honestly. The counters are what a job result records:

    - ``chunks_total`` / ``chars_total``: the whole document offered;
    - ``chunks_used`` / ``chars_used``: what was actually selected.

    A future windowed strategy can return several of these (one per window)
    without any change to what is stored: nothing here reaches the database.
    """

    passages: tuple[SourcePassage, ...]
    chunks_total: int
    chunks_used: int
    chars_total: int
    chars_used: int
    truncated: bool

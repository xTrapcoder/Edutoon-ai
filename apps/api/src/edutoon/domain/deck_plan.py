"""Turning a slide deck into one continuous lesson, deterministically - pure domain logic (rule 1:
no I/O, no imports of repositories / providers / services).

There is no model anywhere in here. The same deck, the same topics and the same target always give
the same narration, so a lesson can be reproduced, tested and explained: every sentence it speaks is
either a *connective* the generator wrote (a welcome, a hand-off, a summary) or one of the slide's
own statements, and each of those is a claim that is quoted from the slide
(:func:`edutoon.domain.deck.claim_is_quoted`).

The pipeline's stages map onto three functions, each reading only the one before it:

1. :func:`plan_topics` groups the slides into the lesson's topics - by the deck's own section
   headers when it has them, otherwise into balanced runs of neighbouring slides.
2. :func:`extract_statements` reads a slide's structure into the statements it makes (a bullet, a
   numbered step, a table row, a sentence of a paragraph or of the speaker notes). These are the
   claims of the lesson.
3. :func:`plan_lesson` chooses which statements fit the target length - the important ones,
   ranked by :func:`score_statement`, never padding when the deck is short - and writes the
   narration: a welcome, then each topic introduced and linked to the one before, each slide
   spoken as sentences rather than read out as a list, and a closing summary.

What makes it one lesson rather than a run of slides: a topic is introduced by name and tied to the
previous one ("Now that we have seen X, let's move on to Y"), consecutive points are joined with
varying connectives, fragments are spoken as a single sentence, and the lesson opens and closes
by naming what it covers.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal

from edutoon.domain.deck import DeckSlide, clean_text, table_rows_text
from edutoon.domain.deck_text import (
    capitalise_first,
    ensure_sentence,
    join_list,
    lower_first,
    number_word,
    ordinal_word,
    speakable,
    split_sentences,
    word_count,
)

StatementKind = Literal["bullet", "step", "paragraph", "row", "note"]
Role = Literal["title", "section", "agenda", "closing", "content", "empty"]

MAX_CLAIM_CHARS: Final = 500
MIN_STATEMENT_LETTERS: Final = 3
MAX_TITLES_SPOKEN: Final = 6

_AGENDA_TITLES: Final = frozenset(
    {
        "agenda",
        "todays agenda",
        "outline",
        "contents",
        "table of contents",
        "what we will cover",
        "what well cover",
        "in this session",
        "session outline",
    }
)
_CLOSING_TITLES: Final = frozenset(
    {
        "thank you",
        "thanks",
        "thank you very much",
        "thankyou",
        "questions",
        "any questions",
        "qa",
        "q and a",
        "the end",
        "end",
        "contact",
        "contact us",
        "contact details",
        "references",
        "reference",
        "resources",
    }
)
_BOILERPLATE = re.compile(
    r"(?:https?://|www\.|@[a-z0-9-]+\.[a-z]{2,}|©|\ball rights reserved\b|\bconfidential\b"
    r"|\bproprietary\b|^\W*\d+\W*$|^\W*page \d+)",
    re.IGNORECASE,
)
_PRESENTER_CUES = (
    "click",
    "pause",
    "ask the audience",
    "ask them",
    "ask the group",
    "next slide",
    "previous slide",
    "tell the audience",
    "tell them",
    "show the",
    "demo",
    "animation",
    "animate",
    "presenter",
    "speaker note",
    "todo",
    "to do:",
)
_VERB_CUES: Final = frozenset(
    {
        "is", "are", "was", "were", "be", "been", "being", "am", "has", "have", "had", "can",
        "could", "will", "would", "shall", "should", "must", "may", "might", "does", "do", "did",
        "helps", "allows", "enables", "provides", "uses", "makes", "creates", "means", "includes",
        "requires", "shows", "gives", "lets", "needs", "takes", "keeps", "protects", "reduces",
        "improves", "causes", "leads", "ensures", "avoid", "keep", "use", "wear", "check", "make",
        "report", "follow", "always", "never", "not", "to", "stay", "call", "raise", "leave",
        "gather", "tuck", "clean", "put", "take", "read", "ask", "tell", "know", "remember",
        "ensure", "test", "review", "plan", "turn", "press", "wait", "start", "stop", "choose",
        "select",
    }
)  # fmt: skip
_DEFINITION_CUES = (" is a ", " is an ", " refers to ", " means ", " defined as ", " is the ")
_EMPHASIS_CUES = (
    "key",
    "important",
    "must",
    "always",
    "never",
    "remember",
    "essential",
    "critical",
)
_STOPWORDS: Final = frozenset(
    {
        "the", "and", "for", "with", "that", "this", "from", "your", "you", "are", "our", "has",
        "have", "how", "what", "why", "when", "who", "into", "about", "their", "they", "its",
        "not", "but", "all", "any", "can", "will", "than", "then", "such", "more", "most",
    }
)  # fmt: skip

_CONNECTORS: Final = ("Also,", "In addition,", "Next,", "Besides that,")
_LIST_LEADS: Final = (
    "Here we have",
    "This covers",
    "The key points are",
    "The main points here are",
)
_SLIDE_LEADS: Final = (
    "Let's look at {t}.",
    "Now let's talk about {t}.",
    "Next, we'll cover {t}.",
    "Let's turn to {t}.",
    "This brings us to {t}.",
)
_NOTE_LEADS: Final = ("To add to this,", "As a reminder,")
_MIDDLE_TOPIC_LEADS: Final = (
    "Now that we have seen {prev}, let's move on to {t}.",
    "Next, let's look at {t}.",
    "With that in place, let's turn to {t}.",
    "Building on that, let's explore {t}.",
)


class DeckPlanError(ValueError):
    """The deck cannot be made into a lesson. The message is written for the project's owner."""


# --- reading a slide -------------------------------------------------------------------


def _plain(title: str | None) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", (title or "").casefold().replace("&", " and ")).strip()


def title_phrase(title: str) -> str:
    """A slide's title as it is spoken inside a sentence."""
    return speakable(title).rstrip(":").strip()


@dataclass(frozen=True, slots=True)
class Statement:
    """One thing a slide says: the unit of narration and the unit of a claim.

    ``text`` is the slide's own wording, a substring of its flat text, and is what a claim states.
    ``spoken`` is the same content made speakable. ``fragment`` marks a short noun-phrase bullet,
    which is spoken as part of a list rather than as a sentence of its own.
    """

    slide_index: int
    kind: StatementKind
    text: str
    spoken: str
    level: int = 0
    ordinal: int = 0
    score: float = 0.0
    fragment: bool = False

    @property
    def key(self) -> tuple[int, str]:
        return (self.slide_index, self.text)


def slide_role(slide: DeckSlide, *, is_first: bool) -> Role:
    """What a slide is for. Title, section and closing slides shape the lesson; only content
    slides are narrated point by point."""
    if not any(
        character.isalpha() for block in slide.blocks for character in block.text
    ) and not any(
        any(character.isalpha() for cell in row for character in cell)
        for block in slide.blocks
        for row in block.rows
    ):
        if not slide.title:
            return "empty"
        title = _plain(slide.title)
        if title in _CLOSING_TITLES:
            return "closing"
        if title in _AGENDA_TITLES:
            return "agenda"
        return "title" if is_first else "section"
    title = _plain(slide.title)
    if title in _CLOSING_TITLES:
        return "closing"
    if title in _AGENDA_TITLES:
        return "agenda"
    layout = (slide.layout or "").casefold()
    if is_first and ("title slide" in layout or layout == "title"):
        return "title"
    body = [block for block in slide.blocks if block.kind != "notes"]
    if is_first and len(body) <= 1 and all(block.kind == "paragraph" for block in body):
        return "title"
    if "section" in layout and not [b for b in body if b.kind in {"bullet", "numbered", "table"}]:
        return "section" if not slide.notes else "content"
    return "content"


def recurring_texts(slides: Sequence[DeckSlide]) -> frozenset[str]:
    """Wording that repeats across many slides - a footer, a running header - and so says nothing
    about any one of them."""
    counts: dict[str, int] = {}
    for slide in slides:
        seen = {
            _plain(block.text)
            for block in slide.blocks
            if block.kind in {"paragraph", "bullet"} and block.text
        }
        for text in seen:
            counts[text] = counts.get(text, 0) + 1
    floor = max(3, round(len(slides) * 0.3))
    return frozenset(text for text, count in counts.items() if count >= floor and text)


def _is_fragment(text: str, kind: StatementKind) -> bool:
    if kind != "bullet":
        return False
    if text.rstrip().endswith((".", "!", "?")):
        return False
    words = [word.casefold().strip(",;:()") for word in text.split()]
    # Anything with a verb in it - "Report every hazard", "Stay alert" - says something and is
    # spoken as a sentence; a short run of nouns ("Wet floors") is a point in a list.
    if any(word in _VERB_CUES for word in words):
        return False
    return len(words) <= 5


def _is_presenter_cue(sentence: str) -> bool:
    lowered = sentence.casefold()
    return any(lowered.startswith(cue) or f" {cue} " in f" {lowered} " for cue in _PRESENTER_CUES)


def _wanted(text: str, recurring: frozenset[str]) -> bool:
    if sum(character.isalpha() for character in text) < MIN_STATEMENT_LETTERS:
        return False
    return not (_BOILERPLATE.search(text) or _plain(text) in recurring)


def _clip_claim(text: str) -> str:
    if len(text) <= MAX_CLAIM_CHARS:
        return text
    window = text[:MAX_CLAIM_CHARS]
    for separator in ("; ", ", "):
        position = window.rfind(separator)
        if position > MAX_CLAIM_CHARS // 2:
            return window[:position].rstrip()
    return window.rsplit(" ", 1)[0]


def score_statement(
    kind: StatementKind, text: str, *, level: int, ordinal: int, keywords: Sequence[str] = ()
) -> float:
    """How much a statement matters to the lesson, higher is more. Only used to decide what to
    leave out when the deck holds more than the target length can carry; with room for everything
    nothing is left out and the score is never consulted."""
    score = {"bullet": 1.0, "paragraph": 1.0, "step": 1.1, "row": 0.9, "note": 0.6}[kind]
    score -= 0.25 * min(level, 3)
    score -= 0.02 * min(ordinal, 20)
    lowered = f" {text.casefold()} "
    if any(cue in lowered for cue in _DEFINITION_CUES) or ": " in text:
        score += 0.3
    if any(cue in lowered for cue in _EMPHASIS_CUES):
        score += 0.2
    if any(character.isdigit() for character in text):
        score += 0.15
    if any(keyword.casefold() in lowered for keyword in keywords if keyword):
        score += 0.6
    return round(score, 4)


def extract_statements(
    slide: DeckSlide, *, recurring: frozenset[str] = frozenset(), keywords: Sequence[str] = ()
) -> tuple[Statement, ...]:
    """The statements a slide makes, in reading order, boilerplate left out."""
    drafts: list[tuple[StatementKind, str, str, int]] = []
    for block in slide.blocks:
        if block.kind == "bullet":
            drafts.append(("bullet", block.text, speakable(block.text), block.level))
        elif block.kind == "numbered":
            drafts.append(("step", block.text, speakable(block.text), 0))
        elif block.kind == "paragraph":
            for sentence in split_sentences(block.text):
                drafts.append(("paragraph", sentence, speakable(sentence), 0))
        elif block.kind == "table":
            for line, spoken in table_rows_text(block.rows):
                drafts.append(("row", line, speakable(spoken), 0))
        elif block.kind == "notes":
            for sentence in split_sentences(block.text):
                if not _is_presenter_cue(sentence):
                    drafts.append(("note", sentence, speakable(sentence), 0))

    statements: list[Statement] = []
    seen: set[str] = set()
    for kind, text, spoken, level in drafts:
        text = _clip_claim(clean_text(text))
        identity = _plain(text)
        if not _wanted(text, recurring) or identity in seen:
            continue
        seen.add(identity)
        ordinal = len(statements)
        statements.append(
            Statement(
                slide_index=slide.index,
                kind=kind,
                text=text,
                spoken=spoken,
                level=level,
                ordinal=ordinal,
                score=score_statement(kind, text, level=level, ordinal=ordinal, keywords=keywords),
                fragment=_is_fragment(text, kind),
            )
        )
    return tuple(statements)


# --- topics ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TopicGroup:
    title: str
    slide_indexes: tuple[int, ...]
    summary: str = ""
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TopicPlan:
    lesson_title: str
    groups: tuple[TopicGroup, ...]


def lesson_title_of(slides: Sequence[DeckSlide], *, fallback: str) -> str:
    for slide in slides:
        if slide.deck_title and _plain(slide.deck_title) not in {"powerpoint presentation", ""}:
            return slide.deck_title
        break
    for position, slide in enumerate(slides):
        if slide_role(slide, is_first=position == 0) == "title" and slide.title:
            return slide.title
    return fallback


def _weight(slide: DeckSlide, recurring: frozenset[str]) -> int:
    return max(1, sum(word_count(s.text) for s in extract_statements(slide, recurring=recurring)))


def _balanced_runs(
    slides: Sequence[DeckSlide], count: int, weights: Mapping[int, int]
) -> list[list[DeckSlide]]:
    """``slides`` cut into ``count`` consecutive runs of about equal weight, each non-empty."""
    count = max(1, min(count, len(slides)))
    total = sum(weights[slide.index] for slide in slides)
    runs: list[list[DeckSlide]] = [[]]
    consumed = 0
    for position, slide in enumerate(slides):
        remaining_slides = len(slides) - position
        remaining_runs = count - len(runs)
        want = total * len(runs) / count
        if (
            runs[-1]
            and remaining_runs > 0
            and (consumed >= want or remaining_slides <= remaining_runs)
        ):
            runs.append([])
        runs[-1].append(slide)
        consumed += weights[slide.index]
    return runs


def _keywords_of(titles: Sequence[str]) -> tuple[str, ...]:
    found: dict[str, None] = {}
    for title in titles:
        for word in re.findall(r"[A-Za-z][A-Za-z'-]{3,}", title):
            if word.casefold() not in _STOPWORDS:
                found.setdefault(word.casefold(), None)
    return tuple(list(found)[:8])


def _group_of(title: str, slides: Sequence[DeckSlide]) -> TopicGroup:
    first, last = slides[0].index, slides[-1].index
    span = f"Slide {first}" if first == last else f"Slides {first} to {last}"
    named = [s.title for s in slides if s.title]
    summary = f"{span}: " + "; ".join(named[:4]) if named else span
    return TopicGroup(
        title=title[:120],
        slide_indexes=tuple(slide.index for slide in slides),
        summary=summary[:400],
        keywords=_keywords_of(named),
    )


def plan_topics(
    slides: Sequence[DeckSlide],
    *,
    max_topics: int,
    max_slides_per_topic: int = 6,
    fallback_title: str = "Presentation",
) -> TopicPlan:
    """Group a deck's slides into the lesson's topics.

    The deck's section-header slides name the topics when it has any; otherwise neighbouring
    slides are cut into runs of similar size. A section with more slides than
    ``max_slides_per_topic`` is split into parts, and when there would be more than ``max_topics``
    the smallest neighbours are merged. Raises :class:`DeckPlanError` if the deck has nothing to
    narrate, or more than ``max_topics * max_slides_per_topic`` slides of it.
    """
    if max_topics < 1 or max_slides_per_topic < 1:
        raise ValueError("max_topics and max_slides_per_topic must be positive.")
    recurring = recurring_texts(slides)

    buckets: list[tuple[str | None, list[DeckSlide]]] = [(None, [])]
    for position, slide in enumerate(slides):
        role = slide_role(slide, is_first=position == 0)
        if role == "section" and slide.title:
            buckets.append((slide.title, []))
        elif role == "content" and extract_statements(slide, recurring=recurring):
            buckets[-1][1].append(slide)
    buckets = [(name, members) for name, members in buckets if members]
    content = [slide for _, members in buckets for slide in members]
    if not content:
        raise DeckPlanError(
            "This presentation has no slide with text to explain. Add slides with titles, "
            "bullets, tables or speaker notes and upload it again."
        )
    if len(content) > max_topics * max_slides_per_topic:
        raise DeckPlanError(
            f"This presentation has {len(content)} slides with content; a lesson can cover at "
            f"most {max_topics * max_slides_per_topic}. Split it into shorter presentations."
        )
    weights = {slide.index: _weight(slide, recurring) for slide in content}

    named_sections = any(name for name, _ in buckets)
    groups: list[tuple[str | None, list[DeckSlide]]] = []
    if not named_sections:
        wanted = max(-(-len(content) // 3), -(-len(content) // max_slides_per_topic))
        for run in _balanced_runs(content, min(wanted, max_topics), weights):
            groups.append((None, run))
    else:
        for name, members in buckets:
            parts = -(-len(members) // max_slides_per_topic)
            for number, run in enumerate(_balanced_runs(members, parts, weights)):
                groups.append((name if number == 0 else f"{name} (continued)", run))

    while len(groups) > max_topics:
        # Merge the neighbouring pair with the fewest slides that still fits a topic.
        candidates = [
            (len(a[1]) + len(b[1]), position)
            for position, (a, b) in enumerate(zip(groups, groups[1:], strict=False))
            if len(a[1]) + len(b[1]) <= max_slides_per_topic
        ]
        if not candidates:
            raise DeckPlanError("This presentation has too many sections for one lesson.")
        _, position = min(candidates)
        (name_a, run_a), (_, run_b) = groups[position], groups[position + 1]
        groups[position : position + 2] = [(name_a, run_a + run_b)]

    planned: list[TopicGroup] = []
    used: set[str] = set()
    for number, (name, run) in enumerate(groups, start=1):
        title = name or run[0].title or f"Part {number}"
        base, suffix = title, 2
        while _plain(title) in used:
            title = f"{base} ({suffix})"
            suffix += 1
        used.add(_plain(title))
        planned.append(_group_of(title, run))
    return TopicPlan(
        lesson_title=lesson_title_of(slides, fallback=fallback_title), groups=tuple(planned)
    )


# --- the lesson ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlannedSegment:
    """One segment of the script: a welcome, one slide, or the closing summary."""

    kind: Literal["intro", "slide", "outro"]
    narration_text: str
    connective: bool
    topic_index: int | None = None
    slide_index: int | None = None
    statements: tuple[Statement, ...] = ()


@dataclass(frozen=True, slots=True)
class LessonPlan:
    title: str
    segments: tuple[PlannedSegment, ...]
    estimated_words: int
    statements_total: int
    statements_dropped: int
    slides_dropped: int
    topic_titles: tuple[str, ...] = field(default=())


def _spoken_item(statement: Statement) -> str:
    return lower_first(statement.spoken)


def _list_sentence(items: Sequence[tuple[Statement, Sequence[Statement]]], rotation: int) -> str:
    spoken: list[str] = []
    for head, children in items:
        text = _spoken_item(head)
        if children:
            text += ", including " + join_list([_spoken_item(child) for child in children])
        spoken.append(text)
    if len(spoken) == 1:
        return ensure_sentence(f"One point to note is {spoken[0]}")
    return ensure_sentence(f"{_LIST_LEADS[rotation % len(_LIST_LEADS)]} {join_list(spoken)}")


def _steps_sentences(steps: Sequence[Statement]) -> list[str]:
    if len(steps) == 1:
        return [ensure_sentence(steps[0].spoken)]
    sentences = [ensure_sentence(f"There are {number_word(len(steps))} steps")]
    for position, step in enumerate(steps):
        word = "Finally" if position == len(steps) - 1 else capitalise_first(ordinal_word(position))
        sentences.append(ensure_sentence(f"{word}, {_spoken_item(step)}"))
    return sentences


def _body_sentences(statements: Sequence[Statement], rotation: int) -> list[str]:
    """The slide's statements as spoken sentences: fragments gathered into one sentence, steps
    given their order, everything else a sentence of its own with a connective now and then."""
    sentences: list[str] = []
    note_lead_used = False
    position = 0
    while position < len(statements):
        statement = statements[position]
        if statement.fragment:
            items: list[tuple[Statement, list[Statement]]] = []
            while position < len(statements) and statements[position].fragment:
                current = statements[position]
                if current.level >= 1 and items:
                    items[-1][1].append(current)
                else:
                    items.append((current, []))
                position += 1
            sentences.append(_list_sentence(items, rotation + len(sentences)))
            continue
        if statement.kind == "step":
            run: list[Statement] = []
            while position < len(statements) and statements[position].kind == "step":
                run.append(statements[position])
                position += 1
            sentences.extend(_steps_sentences(run))
            continue

        sentence = ensure_sentence(statement.spoken)
        if statement.kind == "note" and not note_lead_used:
            sentence = f"{_NOTE_LEADS[rotation % len(_NOTE_LEADS)]} {lower_first(sentence)}"
            note_lead_used = True
        elif (
            sentences
            and len(sentences) % 2 == 1
            and statement.kind != "row"
            and not sentence.endswith("?")
            and not sentence[0].isdigit()
        ):
            connector = _CONNECTORS[(rotation + len(sentences)) % len(_CONNECTORS)]
            sentence = f"{connector} {lower_first(sentence)}"
        sentences.append(sentence)
        position += 1
    return sentences


def _slide_lead(slide: DeckSlide, rotation: int) -> str | None:
    if not slide.title:
        return None
    phrase = title_phrase(slide.title)
    if not phrase:
        return None
    if phrase.endswith("?"):
        return capitalise_first(phrase)
    return _SLIDE_LEADS[rotation % len(_SLIDE_LEADS)].format(t=phrase)


def _topic_lead(index: int, count: int, title: str, previous: str | None) -> str:
    phrase = title_phrase(title)
    if index == 0:
        return f"Let's begin with {phrase}."
    if index == count - 1 and count > 1:
        return f"Finally, let's look at {phrase}."
    template = _MIDDLE_TOPIC_LEADS[index % len(_MIDDLE_TOPIC_LEADS)]
    return template.format(prev=title_phrase(previous or "that"), t=phrase)


def _titles_sentence(titles: Sequence[str], *, max_words: int = 45) -> str:
    spoken: list[str] = []
    words = 0
    for title in titles[:MAX_TITLES_SPOKEN]:
        phrase = title_phrase(title)
        words += word_count(phrase)
        if spoken and words > max_words:
            break
        spoken.append(phrase)
    return join_list(spoken)


def _intro(lesson_title: str, topic_titles: Sequence[str]) -> str:
    text = f"Welcome. In this lesson, we will learn about {title_phrase(lesson_title)}."
    if len(topic_titles) > 1:
        text += f" We will cover {_titles_sentence(topic_titles)}."
    return text


def _outro(lesson_title: str, topic_titles: Sequence[str]) -> str:
    if len(topic_titles) > 1:
        covered = f"we covered {_titles_sentence(topic_titles)}"
    else:
        covered = f"we looked at {title_phrase(topic_titles[0] if topic_titles else lesson_title)}"
    return f"To summarise, in this lesson {covered}. Thank you for watching."


@dataclass(slots=True)
class _Slot:
    topic_index: int
    slide: DeckSlide
    statements: list[Statement]


def plan_lesson(
    slides: Sequence[DeckSlide],
    topics: Sequence[TopicGroup],
    *,
    lesson_title: str,
    target_ms: int,
    words_per_minute: int,
    allowed: frozenset[tuple[int, str]] | None = None,
    keywords: Sequence[str] = (),
) -> LessonPlan:
    """Write the lesson for ``topics`` (the accepted ones, in order) over ``slides``.

    ``allowed`` limits which statements may be spoken to those that have a verified claim
    (``None`` means all). The lesson is fitted to ``target_ms`` by leaving out the lowest-ranked
    statements, then whole slides, until it fits - but never below one slide in every topic, and
    never by repeating or stretching anything when the deck is shorter than the target.
    """
    if target_ms <= 0 or words_per_minute <= 0:
        raise ValueError("target_ms and words_per_minute must be positive.")
    by_index = {slide.index: slide for slide in slides}
    recurring = recurring_texts(slides)

    slots: list[_Slot] = []
    for topic_index, topic in enumerate(topics):
        for slide_index in topic.slide_indexes:
            slide = by_index.get(slide_index)
            if slide is None:
                continue
            statements = [
                statement
                for statement in extract_statements(slide, recurring=recurring, keywords=keywords)
                if allowed is None or statement.key in allowed
            ]
            if statements:
                slots.append(_Slot(topic_index, slide, statements))
    if not slots:
        raise DeckPlanError("None of the accepted topics has any verified content to explain.")
    total_statements = sum(len(slot.statements) for slot in slots)

    def covered_titles() -> list[str]:
        return [topics[index].title for index in dict.fromkeys(slot.topic_index for slot in slots)]

    def narrate() -> list[str]:
        live = list(dict.fromkeys(slot.topic_index for slot in slots))
        position = {topic_index: place for place, topic_index in enumerate(live)}
        texts: list[str] = []
        previous: int | None = None
        for ordinal, slot in enumerate(slots):
            parts: list[str] = []
            slide_lead = _slide_lead(slot.slide, ordinal)
            if slot.topic_index != previous:
                title = topics[slot.topic_index].title
                before = topics[previous].title if previous is not None else None
                parts.append(_topic_lead(position[slot.topic_index], len(live), title, before))
                # A slide that only repeats the topic's own name needs no second introduction.
                if _plain(slot.slide.title) == _plain(title):
                    slide_lead = None
            if slide_lead:
                parts.append(slide_lead)
            parts.extend(_body_sentences(slot.statements, ordinal))
            texts.append(" ".join(parts))
            previous = slot.topic_index
        return texts

    budget_words = round(target_ms / 60_000 * words_per_minute)

    def total_words() -> int:
        titles = covered_titles()
        framing = word_count(_intro(lesson_title, titles)) + word_count(
            _outro(lesson_title, titles)
        )
        return framing + sum(word_count(text) for text in narrate())

    dropped_statements = 0
    dropped_slides = 0
    while total_words() > budget_words:
        # First leave out the lowest-ranked statement of any slide that has more than one; among
        # equals the latest in the deck goes first, so the opening points of a lesson are kept.
        trimmable = [
            (statement.score, -position, -statement.ordinal)
            for position, slot in enumerate(slots)
            if len(slot.statements) > 1
            for statement in slot.statements
        ]
        if trimmable:
            _, negative_position, negative_ordinal = min(trimmable)
            slot = slots[-negative_position]
            slot.statements[:] = [s for s in slot.statements if s.ordinal != -negative_ordinal]
            dropped_statements += 1
            continue
        # Then whole slides, lowest-ranked first, keeping at least one slide in every topic.
        per_topic: dict[int, int] = {}
        for slot in slots:
            per_topic[slot.topic_index] = per_topic.get(slot.topic_index, 0) + 1
        removable = [
            (max(statement.score for statement in slot.statements), -position)
            for position, slot in enumerate(slots)
            if per_topic[slot.topic_index] > 1
        ]
        if not removable:
            break
        _, negative_position = min(removable)
        removed = slots.pop(-negative_position)
        dropped_statements += len(removed.statements)
        dropped_slides += 1

    titles = covered_titles()
    segments: list[PlannedSegment] = [
        PlannedSegment(kind="intro", narration_text=_intro(lesson_title, titles), connective=True)
    ]
    for slot, text in zip(slots, narrate(), strict=True):
        segments.append(
            PlannedSegment(
                kind="slide",
                narration_text=text,
                connective=False,
                topic_index=slot.topic_index,
                slide_index=slot.slide.index,
                statements=tuple(slot.statements),
            )
        )
    segments.append(
        PlannedSegment(kind="outro", narration_text=_outro(lesson_title, titles), connective=True)
    )
    return LessonPlan(
        title=lesson_title,
        segments=tuple(segments),
        estimated_words=sum(word_count(segment.narration_text) for segment in segments),
        statements_total=total_statements,
        statements_dropped=dropped_statements,
        slides_dropped=dropped_slides,
        topic_titles=tuple(titles),
    )

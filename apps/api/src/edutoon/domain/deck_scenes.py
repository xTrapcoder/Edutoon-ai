"""Scenes for a deck lesson, composed from what the narration actually says - pure domain logic
(rule 1: no I/O, no imports of repositories / providers / services).

The storyboard stage asks for a set of scenes for one script segment. For a deck the scenes are not
invented: each segment is matched back to the slide it narrates (:func:`match_slide`), by finding
which of the slide's statements the narration speaks, and the scenes show exactly those statements.
So a viewer reads on screen what they hear, and every word on screen is, by construction, in the
narration - the provenance rule for scene text (``domain.storyboard_provenance``) holds without a
model to trust.

Matching on the narration rather than re-planning the lesson means it does not matter if the target
length, or an accepted topic, changed since the script was written: the script's own words are the
only input.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from edutoon.domain.deck import DeckSlide
from edutoon.domain.deck_plan import Statement, title_phrase
from edutoon.domain.deck_text import fit_text, split_sentences
from edutoon.domain.storyboard_template import (
    BULLET_MAX_CHARS,
    MAX_BULLETS,
    MIN_BULLETS,
    SUBTITLE_MAX_CHARS,
    TITLE_MAX_CHARS,
)

Role = Literal["intro", "slide", "outro", "other"]

BULLETS_PER_SCENE: Final = 3
RECAP_TITLE: Final = "Recap"

_NON_WORD = re.compile(r"[^a-z0-9]+")


def _norm(text: str) -> str:
    return f" {_NON_WORD.sub(' ', text.casefold()).strip()} "


@dataclass(frozen=True, slots=True)
class SlideMatch:
    slide_index: int
    statements: tuple[Statement, ...]
    """The slide's statements the narration speaks, in the order it speaks them."""


def match_slide(
    narration: str,
    statements_by_slide: Mapping[int, Sequence[Statement]],
    slides: Mapping[int, DeckSlide],
) -> SlideMatch | None:
    """The slide a narration is about, with the statements of it that are spoken - or ``None``
    when it speaks none of any slide's (a welcome, a summary, or wording that was rewritten)."""
    spoken = _norm(narration)
    best: SlideMatch | None = None
    best_score = (0, 0)
    for slide_index, statements in statements_by_slide.items():
        found: list[tuple[int, Statement]] = []
        for statement in statements:
            at = spoken.find(_norm(statement.spoken))
            if at >= 0:
                found.append((at, statement))
        if not found:
            continue
        title = slides[slide_index].title if slide_index in slides else None
        titled = 1 if title and _norm(title_phrase(title)) in spoken else 0
        score = (len(found), titled)
        if score > best_score:
            best_score = score
            best = SlideMatch(slide_index, tuple(s for _, s in sorted(found, key=lambda f: f[0])))
    return best


def _bullet(text: str) -> str:
    return fit_text(text.strip().rstrip(".;:,"), BULLET_MAX_CHARS)


def _groups(count: int, max_scenes: int) -> list[int]:
    """How many bullets go in each of the scenes a slide of ``count`` bullets is shown in: about
    three a scene, never fewer than two (a bullet scene needs two) and never more than five."""
    if count <= MAX_BULLETS and count <= BULLETS_PER_SCENE + 1:
        return [count]
    scenes = max(1, min(max_scenes, count // MIN_BULLETS, -(-count // BULLETS_PER_SCENE)))
    scenes = max(scenes, min(max_scenes, -(-count // MAX_BULLETS)))
    base, extra = divmod(min(count, scenes * MAX_BULLETS), scenes)
    return [base + (1 if position < extra else 0) for position in range(scenes)]


def _text_scenes(
    title: str | None, bullets: Sequence[str], *, max_scenes: int
) -> list[tuple[str, dict[str, object]]]:
    """Bullet scenes (or one title card, for a single point) under ``title``."""
    heading = fit_text(title, TITLE_MAX_CHARS) if title else None
    if len(bullets) < MIN_BULLETS:
        if heading:
            card: dict[str, object] = {"title": heading}
            if bullets:
                card["subtitle"] = fit_text(bullets[0], SUBTITLE_MAX_CHARS)
            return [("title_card", card)]
        return [("title_card", {"title": fit_text(bullets[0], TITLE_MAX_CHARS)})] if bullets else []
    scenes: list[tuple[str, dict[str, object]]] = []
    start = 0
    for size in _groups(len(bullets), max_scenes):
        chunk = list(bullets[start : start + size])
        start += size
        content: dict[str, object] = {"bullets": chunk}
        if heading:
            content["title"] = heading
        scenes.append(("bullet_points", content))
    return scenes


def compose_scenes(
    narration: str,
    *,
    role: Role,
    match: SlideMatch | None,
    slide_title: str | None,
    lesson_title: str,
    topic_titles: Sequence[str],
    max_scenes: int,
) -> list[tuple[str, dict[str, object]]]:
    """The ``(template, content)`` scenes for one segment, at most ``max_scenes`` of them.

    A welcome is a title card naming the lesson and what it covers; a slide shows its spoken
    statements as bullets under its own title; the summary is a recap of the topics; anything
    else falls back to the narration's own first sentences as bullets, which are in the
    narration by definition.
    """
    if max_scenes < 1:
        return []
    if role == "intro":
        card: dict[str, object] = {"title": fit_text(lesson_title, TITLE_MAX_CHARS)}
        if len(topic_titles) > 1:
            card["subtitle"] = fit_text(", ".join(topic_titles[:3]), SUBTITLE_MAX_CHARS)
        return [("title_card", card)]
    if role == "outro":
        if len(topic_titles) >= MIN_BULLETS:
            bullets = [_bullet(title) for title in topic_titles[:MAX_BULLETS]]
            return [("bullet_points", {"title": RECAP_TITLE, "bullets": bullets})]
        return [("title_card", {"title": fit_text(lesson_title, TITLE_MAX_CHARS)})]
    if role == "slide" and match is not None:
        shown = [statement for statement in match.statements if statement.kind != "note"]
        if shown:
            return _text_scenes(
                slide_title, [_bullet(s.text) for s in shown], max_scenes=max_scenes
            )[:max_scenes]
        if slide_title:
            return [("title_card", {"title": fit_text(slide_title, TITLE_MAX_CHARS)})]

    sentences = [_bullet(sentence) for sentence in split_sentences(narration)][:MAX_BULLETS]
    return _text_scenes(None, sentences, max_scenes=max_scenes)[:max_scenes]

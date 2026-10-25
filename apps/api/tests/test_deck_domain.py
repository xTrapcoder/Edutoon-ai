"""The deck engine's pure logic: slide structure, grouping into topics, choosing and wording the
lesson, and composing scenes from what the narration says. No database, no provider.

The properties that matter: the same deck always gives the same lesson; every sentence spoken is
a connective or one of the slide's own statements; the lesson fits the target by leaving things
out, never by padding; and a lesson never speaks a figure or name the slides do not contain.
"""

from __future__ import annotations

import pytest

from edutoon.domain.deck import (
    DeckBlock,
    DeckSlide,
    DeckStructureError,
    claim_is_quoted,
    has_content,
    normalise_slide,
    slide_from_json,
    slide_text,
    slide_to_json,
    table_rows_text,
)
from edutoon.domain.deck_plan import (
    DeckPlanError,
    TopicGroup,
    extract_statements,
    lesson_title_of,
    plan_lesson,
    plan_topics,
    recurring_texts,
    slide_role,
)
from edutoon.domain.deck_scenes import compose_scenes, match_slide
from edutoon.domain.deck_text import fit_text, lower_first, speakable, split_sentences
from edutoon.domain.script import ScriptDraft, ScriptSegmentDraft
from edutoon.domain.script_provenance import GroundingReference, filter_and_validate
from edutoon.domain.storyboard_provenance import resolve_provenance
from edutoon.domain.storyboard_template import validate_scene_content
from edutoon.providers.pptx import extract_deck
from tests.pptx_helpers import (
    SECTION_HEADER,
    TITLE_ONLY,
    TITLE_SLIDE,
    SlideSpec,
    make_deck,
    sample_training_deck,
)

WPM = 150


def bullet(text: str, level: int = 0) -> DeckBlock:
    return DeckBlock(kind="bullet", text=text, level=level)


def slide(
    index: int, title: str | None, *blocks: DeckBlock, layout: str | None = None
) -> DeckSlide:
    return DeckSlide(index=index, title=title, blocks=tuple(blocks), layout=layout)


@pytest.fixture(scope="module")
def sample_slides() -> tuple[DeckSlide, ...]:
    return extract_deck(sample_training_deck()).slides


# --- structure ---------------------------------------------------------------------------


def test_a_slide_round_trips_through_its_stored_structure(sample_slides):
    for original in sample_slides:
        assert slide_from_json(slide_to_json(original)) == original


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        {"v": 99, "index": 1, "blocks": []},
        {"v": 1, "index": 0, "blocks": []},
        {"v": 1, "index": 1, "blocks": "no"},
        {"v": 1, "index": 1, "blocks": [{"kind": "video", "text": "x"}]},
        {"v": 1, "index": 1, "blocks": [{"kind": "bullet", "text": 5}]},
        {"v": 1, "index": 1, "blocks": [{"kind": "bullet", "text": "x", "level": -1}]},
    ],
)
def test_a_structure_that_is_not_one_is_refused(bad):
    with pytest.raises(DeckStructureError):
        slide_from_json(bad)


def test_the_flat_text_numbers_steps_and_marks_notes_and_levels():
    flat = slide_text(
        slide(
            1,
            "Fire",
            bullet("Top"),
            bullet("Nested", 1),
            DeckBlock(kind="numbered", text="Alarm"),
            DeckBlock(kind="numbered", text="Call"),
            DeckBlock(kind="notes", text="Stay calm."),
        )
    )
    assert flat.splitlines() == [
        "Fire",
        "- Top",
        "  - Nested",
        "1. Alarm",
        "2. Call",
        "Notes: Stay calm.",
    ]


def test_every_statement_a_slide_makes_is_quoted_from_its_flat_text(sample_slides):
    for one in sample_slides:
        flat = slide_text(one)
        for statement in extract_statements(one):
            assert claim_is_quoted(statement.text, flat), statement.text


def test_a_claim_not_in_the_slide_is_not_quoted():
    assert not claim_is_quoted("Fire needs oxygen.", "Wear a helmet.")
    assert not claim_is_quoted("", "anything")


def test_a_table_is_written_and_spoken_row_by_row():
    [(line, spoken), *_] = table_rows_text(
        [["Type", "Use it on", "Never use it on"], ["Water", "Paper", "Electrical equipment"]]
    )
    assert line == "Water - Use it on: Paper; Never use it on: Electrical equipment"
    assert spoken == "Water: use it on paper; never use it on electrical equipment"


def test_normalising_drops_empty_blocks_and_stray_whitespace():
    cleaned = normalise_slide(
        slide(
            1,
            "  A ​ title ",
            bullet("  "),
            bullet("Real\u000bpoint"),
            DeckBlock("table", rows=(("", ""),)),
        )
    )
    assert cleaned.title == "A title"
    assert [b.text for b in cleaned.blocks] == ["Real point"]
    assert not has_content(slide(1, None))


# --- parsing a real deck -----------------------------------------------------------------


def test_the_sample_deck_keeps_its_structure_and_skips_hidden_slides(sample_slides):
    assert len(sample_slides) == 12  # the thirteenth slide is hidden
    by_title = {s.title: s for s in sample_slides}
    hazards = by_title["Common Hazards"]
    assert [(b.text, b.level) for b in hazards.blocks if b.kind == "bullet"][:3] == [
        ("Slips, trips and falls", 0),
        ("Wet floors", 1),
        ("Loose cables", 1),
    ]
    assert [b.kind for b in by_title["If You Discover a Fire"].blocks] == ["numbered"] * 4 + [
        "notes"
    ]
    assert by_title["Choosing the Right Extinguisher"].blocks[0].kind == "table"
    assert "Speaking up" in by_title["Our Shared Responsibility"].notes


# --- roles and statements ----------------------------------------------------------------


def test_slides_are_given_the_role_they_play(sample_slides):
    roles = {s.title: slide_role(s, is_first=s.index == 1) for s in sample_slides}
    assert roles["Workplace Safety Induction"] == "title"
    assert roles["Agenda"] == "agenda"
    assert roles["Why Safety Matters"] == "section"
    assert roles["Common Hazards"] == "content"
    assert roles["Thank You"] == "closing"


def test_boilerplate_and_presenter_instructions_are_not_statements():
    one = slide(
        1,
        "Topic",
        bullet("Visit www.example.com for more"),
        bullet("Real point about safety"),
        bullet("Company Confidential"),
        DeckBlock(kind="notes", text="Click to show the chart. Fire needs oxygen to burn."),
    )
    texts = [s.text for s in extract_statements(one)]
    assert texts == ["Real point about safety", "Fire needs oxygen to burn."]


def test_a_footer_repeated_on_many_slides_is_recognised_and_dropped():
    slides = [
        slide(i, f"S{i}", bullet(f"Point {i} here"), bullet("Acme Training 2026"))
        for i in range(1, 7)
    ]
    recurring = recurring_texts(slides)
    assert [s.text for s in extract_statements(slides[0], recurring=recurring)] == ["Point 1 here"]


def test_short_bullets_are_fragments_and_sentences_are_not():
    one = slide(
        1,
        "T",
        bullet("Wet floors"),
        bullet("Keep walkways clear of boxes and equipment."),
        bullet("Report every hazard"),
    )
    assert [s.fragment for s in extract_statements(one)] == [True, False, False]


# --- topics ------------------------------------------------------------------------------


def test_a_decks_section_headers_name_its_topics(sample_slides):
    plan = plan_topics(sample_slides, max_topics=10)
    assert [g.title for g in plan.groups] == ["Why Safety Matters", "In an Emergency"]
    assert plan.lesson_title == "Workplace Safety Induction"
    assert [g.slide_indexes for g in plan.groups] == [(4, 5, 6), (8, 9, 10, 11)]


def test_a_deck_without_sections_is_cut_into_balanced_runs_of_neighbours():
    slides = [
        slide(i, f"Slide {i}", bullet(f"A point about thing {i} and more")) for i in range(1, 10)
    ]
    plan = plan_topics(slides, max_topics=10)
    assert [len(g.slide_indexes) for g in plan.groups] == [3, 3, 3]
    covered = [i for g in plan.groups for i in g.slide_indexes]
    assert covered == list(range(1, 10))  # every slide once, in order


def test_topics_are_capped_and_never_hold_more_slides_than_a_topic_can():
    slides = [
        slide(i, f"S{i}", bullet(f"Distinct statement number {i} here")) for i in range(1, 25)
    ]
    plan = plan_topics(slides, max_topics=4, max_slides_per_topic=6)
    assert len(plan.groups) == 4
    assert all(len(g.slide_indexes) <= 6 for g in plan.groups)
    with pytest.raises(DeckPlanError, match="at most 24"):
        plan_topics(slides + [slide(30, "extra", bullet("One more statement"))], max_topics=4)


def test_a_deck_with_nothing_to_say_is_refused_in_plain_words():
    with pytest.raises(DeckPlanError, match="no slide with text"):
        plan_topics([slide(1, "Only a title", layout="Title Slide")], max_topics=5)


def test_topic_titles_are_unique_and_the_plan_is_deterministic(sample_slides):
    first = plan_topics(sample_slides, max_topics=10)
    assert first == plan_topics(sample_slides, max_topics=10)
    twin = [
        slide(1, "Intro", bullet("First point stands here")),
        slide(2, "Intro", bullet("Second point stands here")),
    ]
    titles = [g.title for g in plan_topics(twin, max_topics=5, max_slides_per_topic=1).groups]
    assert len(set(titles)) == 2


# --- the lesson --------------------------------------------------------------------------


def lesson(slides, target_ms=300_000, **kwargs):
    plan = plan_topics(slides, max_topics=10)
    return plan, plan_lesson(
        slides,
        plan.groups,
        lesson_title=plan.lesson_title,
        target_ms=target_ms,
        words_per_minute=WPM,
        **kwargs,
    )


def test_the_lesson_opens_and_closes_by_naming_what_it_covers(sample_slides):
    _, planned = lesson(sample_slides)
    first, last = planned.segments[0], planned.segments[-1]
    assert (first.kind, first.connective) == ("intro", True)
    assert first.narration_text.startswith(
        "Welcome. In this lesson, we will learn about Workplace Safety Induction."
    )
    assert "Why Safety Matters and In an Emergency" in first.narration_text
    assert last.kind == "outro" and "Thank you for watching" in last.narration_text


def test_topics_are_introduced_and_linked_rather_than_just_started(sample_slides):
    _, planned = lesson(sample_slides)
    spoken = " ".join(s.narration_text for s in planned.segments)
    assert "Let's begin with Why Safety Matters." in spoken
    assert "Finally, let's look at In an Emergency." in spoken
    # No two slides open the same way in a row: the connectives vary.
    openings = [s.narration_text.split(".")[0] for s in planned.segments if s.kind == "slide"]
    assert all(a != b for a, b in zip(openings, openings[1:], strict=False))


def test_fragments_are_spoken_as_one_sentence_and_steps_in_order(sample_slides):
    _, planned = lesson(sample_slides)
    spoken = {s.slide_index: s.narration_text for s in planned.segments if s.kind == "slide"}
    assert (
        "slips, trips and falls, including wet floors and loose cables, electrical faults "
        "and manual handling injuries" in spoken[5]
    )
    assert "There are four steps." in spoken[8]
    assert (
        spoken[8].index("First,") < spoken[8].index("Second,") < spoken[8].index("Finally, gather")
    )


def test_table_rows_are_read_as_comparisons(sample_slides):
    _, planned = lesson(sample_slides)
    row = next(s for s in planned.segments if s.slide_index == 9).narration_text
    assert "Water: use it on paper and wood; never use it on electrical equipment." in row


def test_the_same_deck_always_gives_the_same_lesson(sample_slides):
    assert lesson(sample_slides)[1] == lesson(sample_slides)[1]


def test_a_deck_shorter_than_the_target_is_never_padded(sample_slides):
    _, short_target = lesson(sample_slides, target_ms=60_000)
    _, long_target = lesson(sample_slides, target_ms=600_000)
    _, longer_target = lesson(sample_slides, target_ms=1_800_000)
    assert long_target == longer_target  # past what the deck holds, more time changes nothing
    assert long_target.statements_dropped == 0 and long_target.slides_dropped == 0
    assert short_target.estimated_words < long_target.estimated_words


def test_a_long_deck_is_fitted_to_a_short_target_by_leaving_the_least_important_out(sample_slides):
    _, fitted = lesson(sample_slides, target_ms=90_000)
    assert fitted.statements_dropped > 0
    assert fitted.estimated_words <= round(90_000 / 60_000 * WPM) * 1.2
    kept = [s for seg in fitted.segments for s in seg.statements]
    assert any("112" in s.text for s in kept)  # a statement with a figure outranks plain advice
    # Every topic still has a slide: nothing is cut to nothing.
    assert {seg.topic_index for seg in fitted.segments if seg.kind == "slide"} == {0, 1}


def test_only_statements_with_a_verified_claim_may_be_spoken(sample_slides):
    plan = plan_topics(sample_slides, max_topics=10)
    everything = {s.key for slide_ in sample_slides for s in extract_statements(slide_)}
    allowed = frozenset(k for k in everything if k[0] in {4, 5, 6, 8})
    spoken = plan_lesson(
        sample_slides,
        plan.groups,
        lesson_title="L",
        target_ms=300_000,
        words_per_minute=WPM,
        allowed=allowed,
    )
    assert {seg.slide_index for seg in spoken.segments if seg.kind == "slide"} == {4, 5, 6, 8}
    with pytest.raises(DeckPlanError, match="any verified content"):
        plan_lesson(
            sample_slides,
            plan.groups,
            lesson_title="L",
            target_ms=60_000,
            words_per_minute=WPM,
            allowed=frozenset(),
        )


def test_emphasised_keywords_survive_trimming():
    slides = [
        slide(
            1,
            "Alpha",
            bullet("The widget is blue."),
            bullet("Calibration of the quartz sensor is monthly."),
        ),
        slide(
            2, "Beta", bullet("The gadget is red."), bullet("Rotation of the brass dial is weekly.")
        ),
    ]
    groups = [TopicGroup("One", (1,)), TopicGroup("Two", (2,))]
    plain = plan_lesson(slides, groups, lesson_title="L", target_ms=12_000, words_per_minute=WPM)
    focused = plan_lesson(
        slides,
        groups,
        lesson_title="L",
        target_ms=12_000,
        words_per_minute=WPM,
        keywords=["quartz"],
    )
    kept = lambda p: {s.text for seg in p.segments for s in seg.statements}  # noqa: E731
    assert "Calibration of the quartz sensor is monthly." in kept(focused)
    assert kept(plain) != kept(focused) or "Calibration of the quartz sensor is monthly." in kept(
        plain
    )


def _grounding(slides, plan):
    texts = " ".join(s.text for one in slides for s in extract_statements(one))
    titles = " ".join(one.title or "" for one in slides)
    return GroundingReference(
        topic_texts={i: f"{g.title} {titles} {texts}" for i, g in enumerate(plan.groups)},
        frame_text=" ".join([plan.lesson_title, *(g.title for g in plan.groups)]),
    )


def test_the_lesson_passes_the_same_provenance_rules_as_a_model_written_script(sample_slides):
    plan, planned = lesson(sample_slides)
    segments = [
        ScriptSegmentDraft(
            segment_index=i,
            narration_text=seg.narration_text,
            claim_ids=[
                f"00000000-0000-4000-8000-{n:012d}" for n, _ in enumerate(seg.statements, 1)
            ],
            estimated_duration_ms=0,
            connective=seg.connective,
            topic_id=seg.topic_index,  # any hashable key: the grounding maps by it
        )
        for i, seg in enumerate(planned.segments)
    ]
    ids = {__import__("uuid").UUID(c) for seg in segments for c in seg.claim_ids}
    validated = filter_and_validate(
        ScriptDraft(title="T", segments=segments),
        ids,
        WPM,
        grounding=_grounding(sample_slides, plan),
    )
    assert len(validated.segments) == len(planned.segments)


# --- scenes ------------------------------------------------------------------------------


def composed_scenes(sample_slides):
    plan, planned = lesson(sample_slides)
    statements = {s.index: extract_statements(s) for s in sample_slides}
    slides = {s.index: s for s in sample_slides}
    topic_titles = [g.title for g in plan.groups]
    for seg in planned.segments:
        match = None
        role = seg.kind
        if seg.kind == "slide":
            match = match_slide(seg.narration_text, statements, slides)
            assert match is not None and match.slide_index == seg.slide_index
        scenes = compose_scenes(
            seg.narration_text,
            role=role,
            match=match,
            slide_title=slides[seg.slide_index].title if seg.slide_index else None,
            lesson_title=plan.lesson_title,
            topic_titles=topic_titles,
            max_scenes=4,
        )
        yield seg, scenes


def test_every_scene_meets_its_template_and_the_provenance_rule(sample_slides):
    for seg, scenes in composed_scenes(sample_slides):
        assert 1 <= len(scenes) <= 4
        for template, content in scenes:
            validate_scene_content(template, content)
            resolution = resolve_provenance(template, content, seg.narration_text)
            assert resolution.fallback is None, (seg.narration_text, content)


def test_a_slide_is_shown_as_the_bullets_it_speaks_under_its_own_title(sample_slides):
    by_slide = {seg.slide_index: scenes for seg, scenes in composed_scenes(sample_slides)}
    template, content = by_slide[6][0]
    assert template == "bullet_points"
    assert content["title"] == "Keeping Walkways Clear"
    assert len(content["bullets"]) == 3
    assert content["bullets"][0].startswith("Keep walkways and exits")


def test_a_segment_matches_its_slide_even_when_the_wording_around_it_changes(sample_slides):
    slides = {s.index: s for s in sample_slides}
    statements = {s.index: extract_statements(s) for s in sample_slides}
    spoken = "Totally new introduction. Wear safety shoes in the warehouse. Then something else."
    match = match_slide(spoken, statements, slides)
    assert match is not None and match.slide_index == 10
    assert match_slide("Nothing from any slide is said here at all.", statements, slides) is None


def test_welcome_and_summary_get_their_own_scenes(sample_slides):
    scenes = {seg.kind: s for seg, s in composed_scenes(sample_slides) if seg.kind != "slide"}
    assert scenes["intro"][0][0] == "title_card"
    assert scenes["intro"][0][1]["title"] == "Workplace Safety Induction"
    template, content = scenes["outro"][0]
    assert template == "bullet_points" and content["title"] == "Recap"


# --- text helpers ------------------------------------------------------------------------


def test_speakable_spells_out_symbols_and_keeps_numbers():
    assert (
        speakable("Cost & time (e.g. 50% faster) - fast")
        == "Cost and time, for example 50% faster, fast"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Wet floors", "wet floors"),
        ("CO2 sensors", "CO2 sensors"),
        ("iPhone use", "iPhone use"),
        ("I am", "I am"),
    ],
)
def test_lowering_the_first_word_leaves_acronyms_and_names_alone(text, expected):
    assert lower_first(text) == expected


def test_sentences_are_split_without_breaking_after_an_abbreviation():
    assert split_sentences("Use it, e.g. on paper. Never on metal.") == [
        "Use it, e.g. on paper.",
        "Never on metal.",
    ]


def test_fitting_text_keeps_a_prefix_of_the_original_at_a_natural_break():
    text = "Clean up spills immediately, or put up a warning sign for others"
    fitted = fit_text(text, 40)
    assert text.startswith(fitted.rstrip("…")) and len(fitted) <= 41
    assert fit_text("short", 40) == "short"


def test_lesson_title_prefers_the_decks_own_title():
    assert lesson_title_of([slide(1, "Cover", bullet("x y z"))], fallback="F") == "F"
    titled = DeckSlide(index=1, title="Cover", blocks=(), deck_title="Real Name")
    assert lesson_title_of([titled], fallback="F") == "Real Name"


def test_a_deck_of_only_section_and_title_only_slides_has_no_lesson():
    deck = extract_deck(
        make_deck(
            [
                SlideSpec("Welcome", layout=TITLE_SLIDE, subtitle="Hi"),
                SlideSpec("Part", layout=SECTION_HEADER),
                SlideSpec("End", layout=TITLE_ONLY),
            ]
        )
    )
    with pytest.raises(DeckPlanError):
        plan_topics(deck.slides, max_topics=5)

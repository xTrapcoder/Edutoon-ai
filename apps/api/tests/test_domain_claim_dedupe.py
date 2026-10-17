"""``domain/claim_dedupe.py`` - pure, no fixtures, no DB.

Regression: a pasted-text script said "the Stockton and Darlington Railway opened in 1825" in three
sections and "the Industrial Revolution began in Britain in the late 18th century" in four, because
neighbouring topics held the same claim.
"""

from __future__ import annotations

from edutoon.domain.claim_dedupe import (
    MIN_CLAIMS_PER_TOPIC,
    ClaimRef,
    TopicClaims,
    content_words,
    dedupe_claims_across_topics,
    same_fact,
)

RAILWAY = (
    "The Stockton and Darlington Railway, opened in 1825, "
    "was the first public railway to use steam locomotives."
)
RAILWAY_REWORDED = (
    "The Stockton and Darlington Railway opened in 1825 "
    "as the first public railway to use steam locomotives."
)
COAL = "Britain had large deposits of coal and iron ore close to ports."
CANALS = "Canals carried heavy loads of coal and goods cheaply."
HEAT = "Heat from the sun causes evaporation of ocean water."
HEAT_REWORDED = "Evaporation of ocean water is caused by heat from the sun."


def _topic(topic_id: str, title: str, *claims: tuple[str, str]) -> TopicClaims:
    return TopicClaims(topic_id, title, [ClaimRef(cid, text) for cid, text in claims])


def _ids(result, topic_id):
    return [claim.id for claim in result[topic_id]]


def test_the_same_fact_reworded_is_one_fact():
    assert same_fact(content_words(RAILWAY), content_words(RAILWAY_REWORDED))
    assert same_fact(content_words(HEAT), content_words(HEAT_REWORDED))
    assert same_fact(
        content_words("Steam engines enabled factories to be built anywhere."),
        content_words("Steam engines allowed factories to be built anywhere."),
    )


def test_two_claims_that_only_share_a_subject_are_different_facts():
    assert not same_fact(
        content_words("Subduction leads to deep ocean trenches."),
        content_words("Subduction zones are often found at convergent plate boundaries."),
    )


def test_a_fact_held_by_two_topics_is_kept_once_under_the_topic_whose_title_fits():
    result = dedupe_claims_across_topics(
        [
            _topic(
                "britain",
                "Conditions in Britain",
                ("a", COAL),
                ("b", RAILWAY),
                ("c", "Britain had a stable government and a growing banking system."),
            ),
            _topic(
                "iron",
                "Iron and the railway",
                ("d", RAILWAY_REWORDED),
                ("e", CANALS),
                ("f", "Abraham Darby used coke to smelt iron in 1709."),
            ),
        ]
    )

    # the railway fact fits "Iron and the railway" better than it fits "Conditions in Britain"
    assert _ids(result, "britain") == ["a", "c"]
    assert _ids(result, "iron") == ["d", "e", "f"]


def test_a_fact_with_no_better_home_stays_with_the_earliest_topic():
    result = dedupe_claims_across_topics(
        [
            _topic(
                "one",
                "Alpha",
                ("a", HEAT),
                ("x", "Plants release water vapour through their leaves."),
                ("z", "Vapour rises, cools and condenses into clouds."),
            ),
            _topic(
                "two",
                "Beta",
                ("b", HEAT_REWORDED),
                ("y", "Rain, snow and hail are kinds of precipitation."),
                ("w", "Runoff gathers in streams and rivers."),
            ),
        ]
    )

    assert _ids(result, "one") == ["a", "x", "z"]
    assert _ids(result, "two") == ["y", "w"]


def test_a_topic_is_never_left_with_fewer_than_the_minimum():
    only = "Magma rises to form new crust at divergent boundaries."
    result = dedupe_claims_across_topics(
        [
            _topic(
                "one",
                "Divergent boundaries",
                ("a", only),
                ("x", "Iceland sits on the Mid-Atlantic Ridge."),
            ),
            _topic("two", "Seafloor spreading", ("b", only), ("c", "Magnetic stripes mirror.")),
            _topic("three", "Everything repeated", ("d", only), ("e", only + " Really.")),
        ]
    )

    assert len(result["three"]) >= MIN_CLAIMS_PER_TOPIC
    assert [claim.id for claim in result["three"]] == ["d", "e"]


def test_every_topic_is_present_and_order_within_a_topic_is_kept():
    result = dedupe_claims_across_topics(
        [
            _topic(
                "one",
                "One",
                ("a", "Alpha facts here."),
                ("b", "Bravo facts there."),
                ("c", "Charlie facts everywhere."),
            ),
            _topic("two", "Two", ("d", "Delta facts nowhere."), ("e", "Echo facts somewhere.")),
        ]
    )

    assert set(result) == {"one", "two"}
    assert _ids(result, "one") == ["a", "b", "c"]
    assert _ids(result, "two") == ["d", "e"]


def test_the_answer_does_not_depend_on_anything_but_the_input():
    topics = [
        _topic("one", "Steam power", ("a", RAILWAY), ("b", COAL), ("c", CANALS)),
        _topic("two", "Transport", ("d", RAILWAY_REWORDED), ("e", HEAT), ("f", "Plants.")),
    ]
    assert dedupe_claims_across_topics(topics) == dedupe_claims_across_topics(topics)

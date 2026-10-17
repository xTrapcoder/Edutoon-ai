"""``domain/script_closing.py`` - pure, no fixtures, no DB.

Regression: three of four real scripts ended without a conclusion. The writer's own ("In summary,
the heart and blood vessels work together ...") was removed by the support check as not stated in
any one claim, so a script that does not end in a closing is given a fixed one.
"""

from __future__ import annotations

import pytest

from edutoon.domain.script_closing import MAX_TITLES_NAMED, closing_text, has_closing


@pytest.mark.parametrize(
    "text",
    [
        "In summary, the heart pumps blood.",
        "Blood moves around the body. In conclusion, the heart is a pump.",
        "That brings us to the end of this lesson.",
        "This concludes our lesson on plate tectonics.",
        "Thanks for watching.",
        "To sum up, plates move.",
        "Plates move. That wraps up our look at boundaries. See you next time.",
    ],
)
def test_a_script_that_ends_in_a_closing_is_recognised(text: str) -> None:
    assert has_closing(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Arteries carry blood away from the heart.",
        "In the last section, we learned about valves. Now, arteries.",
        "A famous example of a transform boundary is the San Andreas Fault.",
        # a closing in the middle of a script is not an ending
        "In summary, valves matter. Next, the heartbeat starts in the right atrium. Valves close.",
    ],
)
def test_a_script_that_just_stops_is_not(text: str) -> None:
    assert has_closing(text) is False


def test_the_closing_names_the_topics_in_order() -> None:
    assert closing_text(["Valves", "Blood pressure", "Circulation"]) == (
        "That brings us to the end of this lesson. "
        "We covered Valves, Blood pressure and Circulation."
    )
    assert closing_text(["Valves", "Circulation"]).endswith("We covered Valves and Circulation.")
    assert closing_text(["Valves"]).endswith("We covered Valves.")


def test_many_topics_are_named_by_count_and_first_and_last() -> None:
    titles = [f"Topic {n}" for n in range(1, MAX_TITLES_NAMED + 2)]

    text = closing_text(titles)

    assert "We covered 7 topics, from Topic 1 to Topic 7." in text


def test_repeated_and_blank_titles_are_left_out() -> None:
    assert closing_text(["Valves", "  Valves ", "", "Circulation"]).endswith(
        "We covered Valves and Circulation."
    )
    assert closing_text([]) == "That brings us to the end of this lesson."
    assert closing_text(["   "]) == "That brings us to the end of this lesson."


def test_the_closing_is_itself_recognised_as_a_closing() -> None:
    assert has_closing(closing_text(["Valves", "Circulation"])) is True

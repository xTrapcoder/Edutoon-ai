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


# --- placement: a fact is written under the topic it is about -------------------------------------
#
# Regression, from the real claims of a six-topic pasted-notes run ("Python basics"): the source was
# two passages, so every topic had been extracted from both and each held facts about the whole
# document. The lesson's "Functions" section said what a variable and a dictionary are, "Lists and
# Dictionaries" was left with only "Python is popular with beginners", and "Errors" opened with the
# year Python was released. The old rule scored a topic by how many of its *title* words a claim
# contained, so the subject word "Python" (in every claim, and in the title of the first topic) gave
# the first topic every tie, and "dictionaries" never matched "dictionary".

PYTHON_TOPICS = [
    ("intro", "Introduction to Python", ("Python", "programming", "Guido van Rossum", "1991")),
    ("vars", "Variables and Types", ("variables", "types", "integers", "floats", "strings")),
    ("lists", "Lists and Dictionaries", ("lists", "dictionaries", "items", "keys", "values")),
    ("loops", "Decisions and Loops", ("if", "else", "for", "while", "indentation")),
    ("funcs", "Functions", ("functions", "def", "parameters", "return", "built-in functions")),
    ("errors", "Errors and Exception Handling", ("errors", "exceptions", "traceback", "try")),
]

PYTHON_CLAIMS = {
    "intro": [
        ("i1", "Python is a beginner-friendly programming language."),
        ("i2", "Guido van Rossum created Python in 1991."),
        ("i3", "A Python program is a text file that an interpreter runs line by line."),
        ("i4", "Variables in Python store values, like age = 15."),
        ("i5", "Lists in Python are ordered collections, e.g., [3, 1, 2]."),
        ("i6", "If statements in Python run code only if a condition is true."),
        ("i7", "For loops in Python repeat a block of code for each item in a collection."),
        ("i8", "Functions in Python are defined with the def keyword."),
        ("i9", "Python's print() function displays values on the screen."),
    ],
    "vars": [
        ("v1", "A variable in Python stores a value, like age = 15."),
        ("v2", "Python automatically determines the type of a variable."),
        ("v3", "Integers, floats, strings, and booleans are basic Python types."),
        ("v4", "The type() function reveals the type of a value."),
        ("v5", "A list in Python is an ordered collection of items, e.g., [3, 1, 2]."),
        ("v6", "Items in a list are indexed starting from 0."),
    ],
    "lists": [
        ("l1", "A list in Python holds an ordered collection of items inside square brackets."),
        ("l2", "Items in a list are counted from zero, so the first item is at index 0."),
    ],
    "loops": [
        ("o1", "A variable in Python stores a value which can be of different types."),
        ("o2", "Dictionaries in Python store key-value pairs enclosed in curly brackets."),
        ("o3", "An if statement in Python runs a block of code only when a condition is true."),
        ("o4", "A for loop in Python repeats a block of code for each item in a collection."),
        ("o5", "A while loop in Python repeats a block of code as long as a condition is true."),
        ("o6", "Python uses indentation to define blocks of code, not braces."),
    ],
    "funcs": [
        ("f1", "A variable is a name that refers to a value, for example, age = 15."),
        ("f2", "A dictionary in Python stores pairs of keys and values inside curly brackets."),
        ("f3", "A function in Python is a named block of code that you can run again."),
        ("f4", "Python comes with built-in functions such as len() and print()."),
    ],
    "errors": [
        ("e1", "A variable in Python is a name that refers to a value."),
        ("e2", "A dictionary in Python stores pairs of keys and values inside curly brackets."),
        ("e3", "A try and except block in Python allows a program to catch an exception."),
        ("e4", "Items in a Python list are counted from zero, with the first item at index 0."),
    ],
}


def _python_lesson() -> list[TopicClaims]:
    return [
        TopicClaims(
            topic_id,
            title,
            [ClaimRef(cid, text) for cid, text in PYTHON_CLAIMS[topic_id]],
            keywords=keywords,
        )
        for topic_id, title, keywords in PYTHON_TOPICS
    ]


def test_dictionary_facts_are_written_under_lists_and_dictionaries_not_under_functions():
    result = dedupe_claims_across_topics(_python_lesson())

    assert {"o2", "f2"} & set(_ids(result, "lists"))  # a dictionary fact is where dictionaries are
    assert not {"o2", "f2", "e2"} & set(_ids(result, "funcs"))
    assert not {"o2", "f2", "e2"} & set(_ids(result, "loops"))
    assert not {"o2", "f2", "e2"} & set(_ids(result, "errors"))


def test_the_variable_facts_stay_with_variables_and_the_loop_facts_with_loops():
    result = dedupe_claims_across_topics(_python_lesson())

    assert {"v1", "o1", "f1", "e1"} & set(_ids(result, "vars"))
    assert not {"o1", "f1", "e1"} & set(_ids(result, "loops"))
    assert {"o4", "o5"} <= set(_ids(result, "loops"))
    assert not {"o4", "o5", "i7"} & set(_ids(result, "intro"))


def test_the_subject_word_does_not_hand_every_tie_to_the_topic_named_after_the_subject():
    result = dedupe_claims_across_topics(_python_lesson())

    # "Introduction to Python" used to keep the lists, variables, loops and functions facts because
    # every claim contains "Python"; now it keeps only what is about Python itself.
    assert not {"i4", "i5", "i7", "i8"} & set(_ids(result, "intro"))
    assert {"i1", "i2", "i3"} <= set(_ids(result, "intro"))


def test_a_topic_whose_own_list_lacked_its_subject_receives_it_from_another_topics_list():
    # "Lists and Dictionaries" listed only list facts (its own dictionary claim was not
    # supported), but the dictionary facts verified under other topics belong in its section.
    result = dedupe_claims_across_topics(_python_lesson())

    assert {"o2", "f2"} <= set(_ids(result, "lists"))
    assert {"l1", "l2"} <= set(_ids(result, "lists"))


def test_title_matching_folds_plural_forms():
    result = dedupe_claims_across_topics(
        [
            _topic("a", "Overview", ("x1", "Alpha beta gamma delta."), ("x2", "Epsilon zeta eta.")),
            _topic(
                "b",
                "Dictionaries",
                ("y1", "A dictionary pairs each key with a value."),
                ("y2", "Theta iota kappa."),
            ),
            _topic(
                "c",
                "Other",
                ("z1", "A dictionary pairs each key with a value."),
                ("z2", "Mu."),
                ("z3", "Nu xi omicron."),
            ),
        ]
    )

    assert "y1" in _ids(result, "b")
    assert "z1" not in _ids(result, "c")


# --- paraphrased facts are placed by what they are about, not dumped on the first topic -----------
#
# Regression, from a "beginner" run on an ecosystems chapter: the writer used everyday words, so
# "These tiny living things work hard to break down dead things" shared no title word with any topic
# and stayed with the first topic that held it ("What is an ecosystem?"), which ended up teaching
# decomposers, pyramids of biomass and where animals get food. The topic's summary says what it is
# about ("Decomposers like fungi and bacteria break down dead plants and animals ...").

ECOSYSTEM_TOPICS = [
    (
        "eco",
        "What is an ecosystem?",
        ("ecosystem", "environment"),
        "An ecosystem is a group of living things and their non-living environment, like water.",
    ),
    (
        "energy",
        "Energy in Ecosystems",
        ("energy", "sun", "plants", "producers"),
        "Energy in ecosystems comes from the sun and is used by plants, called producers.",
    ),
    (
        "decomp",
        "Decomposers and Nutrient Cycling",
        ("decomposers", "fungi", "bacteria", "nutrients", "soil"),
        "Decomposers like fungi and bacteria break down dead plants and animals, returning "
        "nutrients to the soil.",
    ),
    (
        "keystone",
        "Keystone Species",
        ("keystone species", "trophic cascade", "Yellowstone National Park"),
        "Keystone species have a big effect on their ecosystem, even though they are not many.",
    ),
]

ECOSYSTEM_CLAIMS = {
    "eco": [
        (
            "e1",
            "An ecosystem is a group of living things and the non-living parts of its environment.",
        ),
        (
            "e2",
            "These tiny living things work hard to break down dead things, like leaves and bones.",
        ),
        ("e3", "Examples of ecosystems are a pond, a forest, and a coral reef."),
    ],
    "energy": [
        ("n1", "Every ecosystem needs energy, which mostly comes from the sun."),
        ("n2", "Plants, algae and some bacteria use sunlight to make food."),
        ("n3", "Plants are living things that make their own food from sunlight."),
    ],
    "decomp": [
        ("d1", "Decomposers like fungi and bacteria help break down dead plants and animals."),
        (
            "d2",
            "When dead things are broken down, the nutrients inside them go back into the soil.",
        ),
    ],
    "keystone": [
        ("k1", "In Yellowstone National Park, wolves were removed in 1926."),
        (
            "k2",
            "Without wolves, the elk population grew and ate many young willow and aspen trees.",
        ),
    ],
}


def test_a_paraphrased_fact_is_placed_by_the_topic_summary_not_left_on_the_first_topic() -> None:
    lesson = [
        TopicClaims(
            topic_id,
            title,
            [ClaimRef(cid, text) for cid, text in ECOSYSTEM_CLAIMS[topic_id]],
            keywords=keywords,
            summary=summary,
        )
        for topic_id, title, keywords, summary in ECOSYSTEM_TOPICS
    ]

    result = dedupe_claims_across_topics(lesson)

    assert "e2" in _ids(result, "decomp")
    assert "e2" not in _ids(result, "eco")

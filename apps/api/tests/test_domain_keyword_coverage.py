"""``domain.keyword_coverage``: is each keyword the owner typed backed by their document?"""

from __future__ import annotations

from uuid import uuid4

from edutoon.domain import keyword_coverage as kc


def _topic(title: str = "Calvin cycle", status: str = "accepted") -> kc.TopicFacts:
    return kc.TopicFacts(topic_id=uuid4(), title=title, status=status)


def test_a_keyword_with_supported_statements_is_covered() -> None:
    topic = _topic()
    item = kc.assess_keyword(
        topic, kc.ClaimCounts(supported=4, total=4), scripted_topic_ids={topic.topic_id}
    )
    assert item.state == "covered" and item.in_script is True
    assert item.message == "Covered by 4 statements checked against your document."


def test_a_keyword_whose_statements_none_could_be_supported_is_not_in_the_document() -> None:
    item = kc.assess_keyword(
        _topic("Quantum computing"), kc.ClaimCounts(supported=0, total=4), scripted_topic_ids=None
    )
    assert item.state == "not_in_document"
    assert "Your document does not support this keyword" in item.message
    assert "none of the 4 statements" in item.message
    assert "cannot appear in the video as something your document says" in item.message


def test_a_keyword_with_no_statements_yet_is_not_checked_rather_than_missing() -> None:
    item = kc.assess_keyword(_topic(), kc.ClaimCounts(), scripted_topic_ids=None)
    assert item.state == "not_checked" and item.in_script is None


def test_a_rejected_keyword_is_left_out_whatever_its_claims() -> None:
    item = kc.assess_keyword(
        _topic(status="rejected"), kc.ClaimCounts(supported=0, total=4), scripted_topic_ids=set()
    )
    assert item.state == "left_out"


def test_partly_supported_keywords_say_how_many_were_not() -> None:
    item = kc.assess_keyword(
        _topic(), kc.ClaimCounts(supported=3, total=4), scripted_topic_ids=None
    )
    assert item.state == "covered"
    assert "3 statements" in item.message and "1 more could not be supported" in item.message


def test_a_covered_keyword_missing_from_the_current_script_says_to_write_it_again() -> None:
    item = kc.assess_keyword(
        _topic(), kc.ClaimCounts(supported=1, total=1), scripted_topic_ids=set()
    )
    assert item.in_script is False
    assert "does not teach it" in item.message
    assert item.message.startswith("Covered by 1 statement checked")


def test_completeness_ignores_left_out_keywords_and_fails_on_any_gap() -> None:
    covered = kc.assess_keyword(_topic(), kc.ClaimCounts(1, 1), scripted_topic_ids=None)
    left_out = kc.assess_keyword(
        _topic(status="rejected"), kc.ClaimCounts(), scripted_topic_ids=None
    )
    missing = kc.assess_keyword(_topic("X"), kc.ClaimCounts(0, 2), scripted_topic_ids=None)
    pending = kc.assess_keyword(_topic("Y"), kc.ClaimCounts(), scripted_topic_ids=None)
    assert kc.is_complete([covered, left_out]) is True
    assert kc.is_complete([covered, missing]) is False
    assert kc.is_complete([covered, pending]) is False
    assert kc.is_complete([]) is True


def test_the_summary_names_the_keywords_the_document_does_not_back() -> None:
    items = [
        kc.assess_keyword(_topic("Calvin cycle"), kc.ClaimCounts(2, 2), scripted_topic_ids=None),
        kc.assess_keyword(
            _topic("Quantum computing"), kc.ClaimCounts(0, 4), scripted_topic_ids=None
        ),
        kc.assess_keyword(_topic("Glycolysis"), kc.ClaimCounts(), scripted_topic_ids=None),
        kc.assess_keyword(_topic("Skipped", "rejected"), kc.ClaimCounts(), scripted_topic_ids=None),
    ]
    assert kc.summary(items) == (
        "1 of 3 keywords covered by your document; not in your document: 'Quantum computing'; "
        "1 not checked yet."
    )
    assert kc.summary([]) is None


def test_assess_uses_empty_counts_for_a_topic_with_no_claims() -> None:
    topic = _topic()
    [item] = kc.assess([topic], {}, scripted_topic_ids=None)
    assert item.state == "not_checked"


# --- a keyword the document never mentions -------------------------------------------------------

CHAPTER = (
    "An ecosystem is a community of living things. Decomposers such as fungi and bacteria return "
    "nutrients to the soil. Only about 10 percent of the energy is stored at the next trophic "
    "level; this is known as the ten percent rule. Wolves are a keystone species. Coral reefs are "
    "ecosystems. A trophic cascade is a chain reaction."
)


def test_missing_terms_names_the_words_the_document_never_uses() -> None:
    assert kc.missing_terms("coral bleaching", [CHAPTER]) == ("bleaching",)
    assert kc.missing_terms("blockchain mining", [CHAPTER]) == ("blockchain", "mining")


def test_a_keyword_whose_words_are_all_in_the_document_has_none_missing() -> None:
    for keyword in ("ten percent rule", "decomposers", "keystone species", "trophic cascades"):
        assert kc.missing_terms(keyword, [CHAPTER]) == (), keyword


def test_framing_words_and_plurals_do_not_make_a_keyword_missing() -> None:
    assert kc.missing_terms("the role of decomposers", [CHAPTER]) == ()
    assert kc.missing_terms("Ecosystem", [CHAPTER]) == ()
    assert kc.missing_terms("decomposer", [CHAPTER]) == ()  # begins the document's "decomposers"


def test_a_short_word_must_match_exactly_not_by_prefix() -> None:
    assert kc.missing_terms("cascade", ["A cascading failure."]) == ("cascade",)


def test_supported_statements_do_not_cover_a_keyword_the_document_never_mentions() -> None:
    topic = kc.TopicFacts(
        topic_id=uuid4(), title="coral bleaching", status="accepted", missing_terms=("bleaching",)
    )
    item = kc.assess_keyword(topic, kc.ClaimCounts(supported=4, total=8), scripted_topic_ids=None)

    assert item.state == "not_in_document"
    assert "never mentions 'bleaching'" in item.message
    assert "cannot appear in the video as something your document says" in item.message


def test_a_rejected_keyword_stays_left_out_even_when_the_document_lacks_it() -> None:
    topic = kc.TopicFacts(
        topic_id=uuid4(), title="blockchain", status="rejected", missing_terms=("blockchain",)
    )
    item = kc.assess_keyword(topic, kc.ClaimCounts(), scripted_topic_ids=None)

    assert item.state == "left_out"

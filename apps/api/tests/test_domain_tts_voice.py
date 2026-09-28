"""``domain.tts_voice`` - which Piper voice narrates which project language,
and the explicit temporary-substitute flag for en-IN (Phase 8).
"""

from __future__ import annotations

import pytest

from edutoon.domain.tts_voice import select_voice

EN_GB_VOICE = "en_GB-cori-medium"
EN_IN_VOICE = "en_GB-cori-medium"  # deliberately the same catalogue id today


def test_en_gb_uses_the_configured_en_gb_voice_and_is_never_a_substitute():
    selection = select_voice(
        "en-GB",
        en_gb_voice_model=EN_GB_VOICE,
        en_in_voice_model=EN_IN_VOICE,
        en_in_is_temporary_substitute=True,
    )
    assert selection.voice_model == EN_GB_VOICE
    assert selection.is_temporary_substitute is False


def test_en_in_uses_the_configured_en_in_voice():
    selection = select_voice(
        "en-IN",
        en_gb_voice_model=EN_GB_VOICE,
        en_in_voice_model=EN_IN_VOICE,
        en_in_is_temporary_substitute=True,
    )
    assert selection.voice_model == EN_IN_VOICE


def test_en_in_is_temporary_substitute_is_read_verbatim_from_the_caller_not_inferred():
    """The flag is a fact about the configured model, stated explicitly - not
    guessed by comparing the two voice ids. A caller that (hypothetically) has a
    genuinely distinct, genuine Indian-English voice configured must still say so
    explicitly rather than have it inferred from the ids differing.
    """
    substitute = select_voice(
        "en-IN",
        en_gb_voice_model=EN_GB_VOICE,
        en_in_voice_model=EN_IN_VOICE,
        en_in_is_temporary_substitute=True,
    )
    assert substitute.is_temporary_substitute is True

    not_substitute = select_voice(
        "en-IN",
        en_gb_voice_model=EN_GB_VOICE,
        en_in_voice_model=EN_IN_VOICE,  # same id as en-GB...
        en_in_is_temporary_substitute=False,  # ...but the caller says it is genuine anyway
    )
    assert not_substitute.is_temporary_substitute is False

    # And the reverse: a distinct id does not by itself make it "not a substitute" -
    # the caller's flag is still what decides.
    distinct_but_flagged = select_voice(
        "en-IN",
        en_gb_voice_model=EN_GB_VOICE,
        en_in_voice_model="some-other-voice-id",
        en_in_is_temporary_substitute=True,
    )
    assert distinct_but_flagged.is_temporary_substitute is True


def test_voice_selection_is_immutable():
    selection = select_voice(
        "en-GB",
        en_gb_voice_model=EN_GB_VOICE,
        en_in_voice_model=EN_IN_VOICE,
        en_in_is_temporary_substitute=True,
    )
    with pytest.raises(AttributeError):
        selection.voice_model = "changed"  # type: ignore[misc]

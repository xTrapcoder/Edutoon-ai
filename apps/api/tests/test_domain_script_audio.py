"""``domain.script_audio`` - pure logic: TTS text chunking, the storage-key
scheme, and the content-hash staleness fingerprint (Phase 8).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from edutoon.domain.script_audio import (
    chunk_narration_for_tts,
    compute_content_hash,
    segment_audio_storage_key,
)

# --- chunk_narration_for_tts ---------------------------------------------------------------


def test_short_text_is_a_single_chunk():
    chunks = chunk_narration_for_tts("Hello there. How are you?", max_chars_per_call=1000)
    assert chunks == ("Hello there. How are you?",)


def test_empty_text_yields_no_chunks():
    assert chunk_narration_for_tts("", max_chars_per_call=100) == ()
    assert chunk_narration_for_tts("   \n\t  ", max_chars_per_call=100) == ()


def test_chunks_never_exceed_the_budget():
    text = " ".join(f"Sentence number {i} is here." for i in range(50))
    chunks = chunk_narration_for_tts(text, max_chars_per_call=80)
    assert all(len(chunk) <= 80 for chunk in chunks)


def test_splits_only_at_sentence_boundaries_when_possible():
    text = "First sentence here. Second sentence here. Third one."
    chunks = chunk_narration_for_tts(text, max_chars_per_call=25)
    assert len(chunks) > 1  # the budget forces at least one split, or this proves nothing
    for chunk in chunks:
        # Every chunk ends on real sentence-ending punctuation - never mid-sentence -
        # and is itself an exact substring of the original text, never a mangled
        # fragment (e.g. split mid-word).
        assert chunk.endswith((".", "!", "?"))
        assert chunk in text


def test_reassembling_chunks_reproduces_the_normalised_text():
    text = "One. Two. Three. Four. Five."
    chunks = chunk_narration_for_tts(text, max_chars_per_call=9)
    assert " ".join(chunks) == text


def test_a_sentence_longer_than_the_budget_is_split_at_word_boundaries_not_mid_word():
    long_sentence = "supercalifragilisticexpialidocious " * 5 + "end."
    chunks = chunk_narration_for_tts(long_sentence.strip(), max_chars_per_call=40)
    for chunk in chunks:
        for word in chunk.split(" "):
            assert word in long_sentence


def test_whitespace_is_normalised_within_and_between_chunks():
    text = "First   sentence.\n\nSecond    sentence here."
    chunks = chunk_narration_for_tts(text, max_chars_per_call=1000)
    assert chunks == ("First sentence. Second sentence here.",)


def test_multiple_chunks_when_the_whole_text_exceeds_one_budget():
    text = "Alpha bravo charlie. Delta echo foxtrot. Golf hotel india."
    chunks = chunk_narration_for_tts(text, max_chars_per_call=25)
    assert len(chunks) > 1


@pytest.mark.parametrize("max_chars_per_call", [0, -1, -100])
def test_a_non_positive_budget_is_rejected(max_chars_per_call):
    with pytest.raises(ValueError, match="positive"):
        chunk_narration_for_tts("Some text.", max_chars_per_call=max_chars_per_call)


# --- compute_content_hash -----------------------------------------------------------------


def _hash(**overrides):
    defaults = {
        "narration_text": "Photosynthesis converts light into energy.",
        "voice_model": "en_GB-cori-medium",
        "engine_version": "1.8.0",
        "sample_rate": 22050,
    }
    return compute_content_hash(**{**defaults, **overrides})


def test_the_same_inputs_always_hash_the_same():
    assert _hash() == _hash()


def test_a_different_narration_text_changes_the_hash():
    assert _hash() != _hash(narration_text="Something else entirely.")


def test_a_different_voice_model_changes_the_hash():
    assert _hash() != _hash(voice_model="en_GB-alba-medium")


def test_a_different_engine_version_changes_the_hash():
    assert _hash() != _hash(engine_version="1.9.0")


def test_a_different_sample_rate_changes_the_hash():
    assert _hash() != _hash(sample_rate=16000)


def test_the_hash_is_a_hex_sha256_digest():
    digest = _hash()
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_fields_cannot_be_confused_with_each_other_via_naive_concatenation():
    """ "ab" + "c" and "a" + "bc" must not hash the same - proves the payload uses
    a real separator, not bare string concatenation."""
    a = compute_content_hash(
        narration_text="ab", voice_model="c", engine_version="x", sample_rate=1
    )
    b = compute_content_hash(
        narration_text="a", voice_model="bc", engine_version="x", sample_rate=1
    )
    assert a != b


# --- segment_audio_storage_key -------------------------------------------------------------


def test_the_key_is_deterministic():
    project_id, script_id, segment_id = uuid4(), uuid4(), uuid4()
    key1 = segment_audio_storage_key(
        project_id=project_id, script_id=script_id, segment_id=segment_id
    )
    key2 = segment_audio_storage_key(
        project_id=project_id, script_id=script_id, segment_id=segment_id
    )
    assert key1 == key2


def test_the_key_is_namespaced_by_project_script_and_segment():
    project_id, script_id, segment_id = uuid4(), uuid4(), uuid4()
    key = segment_audio_storage_key(
        project_id=project_id, script_id=script_id, segment_id=segment_id
    )
    assert str(project_id) in key
    assert str(script_id) in key
    assert str(segment_id) in key
    assert key.endswith("/audio.wav")


def test_a_different_script_never_collides_even_with_the_same_segment_id():
    """A regenerated script's segments have new ids in practice, but even in
    the hypothetical worst case, the script_id in the key alone keeps them apart."""
    project_id, segment_id = uuid4(), uuid4()
    key_a = segment_audio_storage_key(
        project_id=project_id, script_id=uuid4(), segment_id=segment_id
    )
    key_b = segment_audio_storage_key(
        project_id=project_id, script_id=uuid4(), segment_id=segment_id
    )
    assert key_a != key_b

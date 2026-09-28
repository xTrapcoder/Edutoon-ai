"""``providers.tts`` - the Piper wrapper itself (Phase 8). Real local
synthesis throughout, never mocked - this is the one module allowed to
import ``piper`` at all, so there is nothing to fake it with.
"""

from __future__ import annotations

import wave
from pathlib import Path

import pytest

from edutoon.providers import tts

VOICES_DIR = Path(__file__).resolve().parents[1] / "var" / "piper-voices"
VOICE_ID = "en_GB-cori-medium"

pytestmark = pytest.mark.skipif(
    not (VOICES_DIR / f"{VOICE_ID}.onnx").exists(),
    reason=f"Piper voice {VOICE_ID} not downloaded - run `make voices` first",
)


def test_engine_version_is_a_real_version_string():
    version = tts.engine_version()
    assert version
    assert version[0].isdigit()


def test_load_voice_raises_a_clear_error_for_a_missing_voice(tmp_path):
    with pytest.raises(FileNotFoundError, match="en_GB-cori-medium"):
        tts.load_voice(tmp_path, "en_GB-cori-medium")


def test_load_voice_returns_the_same_cached_instance_for_the_same_directory():
    first = tts.load_voice(VOICES_DIR, VOICE_ID)
    second = tts.load_voice(VOICES_DIR, VOICE_ID)
    assert first is second


def test_load_voice_does_not_let_a_cached_hit_mask_a_different_directorys_missing_file(
    tmp_path,
):
    """A real Piper voice already cached from VOICES_DIR must not be silently
    reused when the *same model name* is requested from a directory that
    doesn't actually have it - the exact bug this cache-key design fixes.
    """
    tts.load_voice(VOICES_DIR, VOICE_ID)  # warm the cache for VOICES_DIR

    with pytest.raises(FileNotFoundError):
        tts.load_voice(tmp_path, VOICE_ID)


async def test_synthesize_to_wav_file_produces_a_real_playable_wav(tmp_path):
    wav_path = tmp_path / "out.wav"

    result = await tts.synthesize_to_wav_file(
        voices_dir=VOICES_DIR,
        voice_model=VOICE_ID,
        chunks=("Hello there, this is a test.",),
        wav_path=wav_path,
    )

    assert wav_path.exists()
    assert result.sample_rate == 22050
    assert result.channels == 1
    assert result.sample_width == 2
    assert result.duration_ms > 0
    assert len(result.audio_bytes) > 0

    with wave.open(str(wav_path), "rb") as wav_file:
        real_duration_ms = round(wav_file.getnframes() / wav_file.getframerate() * 1000)
    assert result.duration_ms == real_duration_ms


async def test_multiple_chunks_are_concatenated_into_one_continuous_file(tmp_path):
    wav_path = tmp_path / "multi.wav"

    result = await tts.synthesize_to_wav_file(
        voices_dir=VOICES_DIR,
        voice_model=VOICE_ID,
        chunks=("First chunk of narration.", "Second chunk continues it."),
        wav_path=wav_path,
    )

    single = await tts.synthesize_to_wav_file(
        voices_dir=VOICES_DIR,
        voice_model=VOICE_ID,
        chunks=("First chunk of narration. Second chunk continues it.",),
        wav_path=tmp_path / "single.wav",
    )

    # Chunked vs. single-call synthesis of (nearly) the same text should
    # produce comparable durations - not wildly different lengths, which
    # would suggest a chunk was dropped, corrupted, or double-written.
    assert abs(result.duration_ms - single.duration_ms) < 500


async def test_empty_chunks_are_rejected():
    with pytest.raises(ValueError, match="empty"):
        await tts.synthesize_to_wav_file(
            voices_dir=VOICES_DIR, voice_model=VOICE_ID, chunks=(), wav_path=Path("unused.wav")
        )

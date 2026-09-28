"""Local, open-source narration synthesis with Piper - the only module
allowed to import ``piper`` (rule 3).

Candidates investigated and why Piper alone is used - including the en-IN
substitution ``domain.tts_voice`` names explicitly - are documented in
apps/api/README.md, "Text-to-speech". In short: Piper is local, free, has no
company API key requirement, and its ``en_GB`` voices are genuinely British
English with a clearly commercial-permissive per-voice license; no candidate
investigated offers a genuinely Indian-English voice under a comparably clear
license, so ``en-IN`` narrates with the same voice, flagged as a temporary
substitution rather than silently presented as something it is not.

``PiperVoice.load`` costs real, measured time even for this small a model
(~1-2s on the reference machine - see the README), so a voice is loaded once
per process and cached, never reloaded per segment or per chunk.
:func:`load_voice` returns the same cached instance for repeat calls with the
same ``voice_model``.

Synthesis is synchronous and CPU-bound; :func:`synthesize_to_wav_file` runs it
in a worker thread via ``asyncio.to_thread`` so it never blocks the event
loop - the same discipline ``providers/storage.py`` uses for ``boto3``.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import wave
from dataclasses import dataclass
from pathlib import Path

from piper import PiperVoice

_voice_cache: dict[str, PiperVoice] = {}


@dataclass(frozen=True, slots=True)
class SynthesisResult:
    audio_bytes: bytes
    sample_rate: int
    channels: int
    sample_width: int
    duration_ms: int


def engine_version() -> str:
    """The installed ``piper-tts`` package version - part of the
    content-hash fingerprint (:func:`edutoon.domain.script_audio.compute_content_hash`),
    since a Piper upgrade can change synthesis output for the same input.
    """
    return importlib.metadata.version("piper-tts")


def load_voice(voices_dir: Path, voice_model: str) -> PiperVoice:
    """The cached :class:`PiperVoice` for ``voice_model``, loading it from
    ``{voices_dir}/{voice_model}.onnx`` the first time it is needed.

    Cached by the resolved ``(voices_dir, voice_model)`` pair, not
    ``voice_model`` alone - ``PIPER_VOICES_DIR`` is one fixed setting for a
    process's whole lifetime in production, but keying on the pair keeps a
    cache hit from ever masking a genuinely missing voice file for a
    directory this process hasn't loaded that model from before.

    Raises :class:`FileNotFoundError` with a clear, actionable message if the
    voice files are missing - never a bare ``onnxruntime`` traceback. Voice
    files are downloaded separately (``make voices`` /
    ``scripts/download_piper_voices.py``), never committed to the repository.
    """
    cache_key = f"{voices_dir.resolve()}::{voice_model}"
    if cache_key in _voice_cache:
        return _voice_cache[cache_key]

    model_path = voices_dir / f"{voice_model}.onnx"
    if not model_path.exists():
        raise FileNotFoundError(
            f"Piper voice '{voice_model}' not found at {model_path}. Download it first "
            "with `make voices` (apps/api/scripts/download_piper_voices.py)."
        )
    voice = PiperVoice.load(str(model_path))
    _voice_cache[cache_key] = voice
    return voice


def _synthesize_chunks_sync(voice: PiperVoice, chunks: tuple[str, ...], wav_path: Path) -> None:
    """Synthesise every chunk into one continuous WAV file, in order.

    ``synthesize_wav`` sets the WAV header's format (sample rate/width/
    channels) on the file the first time it is called; a Python ``wave``
    writer refuses to change those after writing has started, so every call
    after the first passes ``set_wav_format=False`` - the chunks are simply
    appended as more audio frames onto the same, single-format file.
    """
    with wave.open(str(wav_path), "wb") as wav_file:
        for index, chunk in enumerate(chunks):
            voice.synthesize_wav(chunk, wav_file, set_wav_format=(index == 0))


async def synthesize_to_wav_file(
    *, voices_dir: Path, voice_model: str, chunks: tuple[str, ...], wav_path: Path
) -> SynthesisResult:
    """Synthesise ``chunks`` in order into one continuous WAV file at
    ``wav_path``, then measure it back by re-reading the *file's own header*
    with the ``wave`` module - never trusting an in-memory sample count or
    the engine's own estimate.

    Raises :class:`ValueError` if ``chunks`` is empty, or
    :class:`FileNotFoundError` if ``voice_model``'s files are missing (see
    :func:`load_voice`).
    """
    if not chunks:
        raise ValueError("chunks must not be empty.")

    voice = load_voice(voices_dir, voice_model)
    await asyncio.to_thread(_synthesize_chunks_sync, voice, chunks, wav_path)

    with wave.open(str(wav_path), "rb") as wav_file:
        frames = wav_file.getnframes()
        sample_rate = wav_file.getframerate()
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()

    duration_ms = round(frames / sample_rate * 1000) if sample_rate else 0
    audio_bytes = await asyncio.to_thread(wav_path.read_bytes)
    return SynthesisResult(
        audio_bytes=audio_bytes,
        sample_rate=sample_rate,
        channels=channels,
        sample_width=sample_width,
        duration_ms=duration_ms,
    )

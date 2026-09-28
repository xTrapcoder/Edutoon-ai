"""Pure logic for turning one script segment's narration into TTS-ready audio
(Phase 8) - no I/O, no provider imports (rule 1).

Piper is invoked once per *chunk*, never once per whole segment:
:func:`chunk_narration_for_tts` splits a segment's ``narration_text`` at
sentence boundaries under a configurable character budget, so a long section
is never sent to the engine as one enormous call. The caller synthesises each
chunk in order and concatenates the resulting audio into a single file per
segment - internal chunks are never exposed as separate script segments or
persisted on their own.

:func:`compute_content_hash` is the "is this audio still valid" fingerprint
(the stale-audio protection Phase 8 requires): whatever changes it - the
narration text, the voice, the engine version, the sample rate - the hash
changes, and a caller compares it against what is stored
(``script_segment_audio.content_hash``) to decide whether existing audio may
still be treated as current, or must be re-synthesised.
"""

from __future__ import annotations

import hashlib
import re
from uuid import UUID

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")
_WHITESPACE = re.compile(r"\s+")


def segment_audio_storage_key(*, project_id: UUID, script_id: UUID, segment_id: UUID) -> str:
    """The deterministic object key one segment's audio is stored at.

    Namespaced by project, script *and* segment (mirrors
    ``domain.source_storage.source_storage_key``'s "per-project, content/identity
    addressed" discipline): a regenerated script's segments have new ids, so its
    audio can never collide with - or be mistaken for - a superseded script's, and
    a re-synthesis of the same segment overwrites this exact key rather than
    accumulating objects.
    """
    return f"projects/{project_id}/scripts/{script_id}/segments/{segment_id}/audio.wav"


def chunk_narration_for_tts(text: str, *, max_chars_per_call: int) -> tuple[str, ...]:
    """Split ``text`` into chunks of at most ``max_chars_per_call`` characters,
    breaking only at sentence boundaries - never mid-sentence - except when a
    single sentence alone exceeds the budget, where it is split at word
    boundaries as a last resort (a chunk may then still exceed the budget by
    itself only if a single *word* does, which is never split further -
    truncating a word would corrupt it, and this is not expected of real
    narration text). Whitespace between/within chunks is normalised to single
    spaces. Empty or whitespace-only input yields no chunks.

    Raises ``ValueError`` for a non-positive ``max_chars_per_call``.
    """
    if max_chars_per_call <= 0:
        raise ValueError("max_chars_per_call must be positive.")

    normalised = _WHITESPACE.sub(" ", text).strip()
    if not normalised:
        return ()

    sentences = [sentence for sentence in _SENTENCE_BOUNDARY.split(normalised) if sentence]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    def _flush() -> None:
        if current:
            chunks.append(" ".join(current))
            current.clear()

    for sentence in sentences:
        if len(sentence) > max_chars_per_call:
            _flush()
            current_len = 0
            chunks.extend(_split_long_sentence(sentence, max_chars_per_call))
            continue

        added_len = len(sentence) + (1 if current else 0)
        if current and current_len + added_len > max_chars_per_call:
            _flush()
            current_len = 0
            added_len = len(sentence)
        current.append(sentence)
        current_len += added_len

    _flush()
    return tuple(chunks)


def _split_long_sentence(sentence: str, max_chars_per_call: int) -> list[str]:
    """A sentence too long for one chunk, split at word boundaries."""
    words = sentence.split(" ")
    parts: list[str] = []
    current: list[str] = []
    current_len = 0
    for word in words:
        added_len = len(word) + (1 if current else 0)
        if current and current_len + added_len > max_chars_per_call:
            parts.append(" ".join(current))
            current = []
            current_len = 0
            added_len = len(word)
        current.append(word)
        current_len += added_len
    if current:
        parts.append(" ".join(current))
    return parts


def compute_content_hash(
    *, narration_text: str, voice_model: str, engine_version: str, sample_rate: int
) -> str:
    """A deterministic fingerprint of everything that determines a segment's
    audio content. Changing any input changes the hash; a caller with
    already-stored audio compares this against the stored
    ``content_hash`` - a mismatch means the text, voice, engine or sample
    rate has moved on since that audio was made, so it is stale and must
    never be played as current.
    """
    payload = "\x1f".join([narration_text, voice_model, engine_version, str(sample_rate)])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

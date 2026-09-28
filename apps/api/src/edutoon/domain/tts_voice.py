"""Which Piper voice narrates which project language (Phase 8) - pure logic,
no I/O (rule 1).

Piper's catalogue has no genuine Indian-English voice (see
apps/api/README.md, "Text-to-speech" - the candidates investigated and why).
Rather than silently narrate an ``en-IN`` project with a generic or
mislabelled voice, :func:`select_voice` returns an explicit
``is_temporary_substitute`` flag whenever the caller says the configured
``en-IN`` voice is standing in for a language it does not genuinely speak -
``services.script_audio`` persists this flag on every audio row it writes,
and the API and frontend surface it, so an en-IN segment's audio is always
labelled for what it actually is, never presented as Indian English.

A genuine Indian-English (or other company/commercial) TTS provider replaces
this module's mapping later without changing anything downstream: every
caller already goes through :func:`select_voice`, never a hand-picked model
id.
"""

from __future__ import annotations

from dataclasses import dataclass

from edutoon.domain.generation_settings import LANGUAGE_EN_IN, Language


@dataclass(frozen=True, slots=True)
class VoiceSelection:
    voice_model: str
    is_temporary_substitute: bool


def select_voice(
    language: Language,
    *,
    en_gb_voice_model: str,
    en_in_voice_model: str,
    en_in_is_temporary_substitute: bool,
) -> VoiceSelection:
    """The voice to synthesise ``language`` with.

    ``en_in_is_temporary_substitute`` is read verbatim, never inferred by
    comparing the two model ids: whether the ``en-IN`` voice genuinely speaks
    Indian English is a fact about the configured model, stated explicitly by
    the caller (``core.config``), not guessed from whether it happens to
    match the ``en-GB`` one.
    """
    if language == LANGUAGE_EN_IN:
        return VoiceSelection(
            voice_model=en_in_voice_model,
            is_temporary_substitute=en_in_is_temporary_substitute,
        )
    return VoiceSelection(voice_model=en_gb_voice_model, is_temporary_substitute=False)

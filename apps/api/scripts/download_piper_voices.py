"""Download the Piper voice files Phase 8 needs into ``PIPER_VOICES_DIR``.

Idempotent: an already-present ``.onnx``/``.onnx.json`` pair is left alone.
Downloads only the *distinct* voice ids the currently configured settings
name (``PIPER_VOICE_EN_GB`` / ``PIPER_VOICE_EN_IN`` - often the same id, since
Piper has no genuine Indian-English voice; see apps/api/README.md,
"Text-to-speech"), from the ``rhasspy/piper-voices`` Hugging Face repository,
laid out ``{language}/{speaker}/{quality}/{voice_id}.onnx[.json]`` there.

Run with ``make voices`` from the repository root, or directly:
``uv run python scripts/download_piper_voices.py``.
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from edutoon.core.config import get_settings  # noqa: E402

_BASE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main"


def _voice_url_path(voice_id: str) -> str:
    """``en_GB-cori-medium`` -> ``en/en_GB/cori/medium/en_GB-cori-medium``."""
    language, rest = voice_id.split("-", 1)
    speaker, quality = rest.rsplit("-", 1)
    locale_family = language.split("_", 1)[0]
    return f"{locale_family}/{language}/{speaker}/{quality}/{voice_id}"


def _download(url: str, dest: Path) -> None:
    print(f"  downloading {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url) as response, open(tmp, "wb") as out_file:  # noqa: S310
        out_file.write(response.read())
    tmp.replace(dest)


def download_voice(voices_dir: Path, voice_id: str) -> None:
    onnx_path = voices_dir / f"{voice_id}.onnx"
    json_path = voices_dir / f"{voice_id}.onnx.json"
    if onnx_path.exists() and json_path.exists():
        print(f"{voice_id}: already present, skipping")
        return

    print(f"{voice_id}:")
    url_path = _voice_url_path(voice_id)
    if not onnx_path.exists():
        _download(f"{_BASE_URL}/{url_path}.onnx", onnx_path)
    if not json_path.exists():
        _download(f"{_BASE_URL}/{url_path}.onnx.json", json_path)


def main() -> None:
    settings = get_settings()
    voices_dir = Path(settings.PIPER_VOICES_DIR)
    voice_ids = sorted({settings.PIPER_VOICE_EN_GB, settings.PIPER_VOICE_EN_IN})
    print(f"Voices directory: {voices_dir.resolve()}")
    for voice_id in voice_ids:
        download_voice(voices_dir, voice_id)
    print("Done.")


if __name__ == "__main__":
    main()

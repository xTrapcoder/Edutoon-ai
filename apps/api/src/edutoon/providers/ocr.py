"""Local text recognition for scanned PDF pages - the only module allowed to import ``rapidocr`` and
``pypdfium2`` (rule 3).

Everything runs on this machine: ``pypdfium2`` draws a page to an image and RapidOCR (ONNX models
that ship inside its wheel - nothing is downloaded, nothing leaves the process) reads it. No page
image or recognised text is sent anywhere or logged.

Recognition is slow compared with reading a text layer (seconds a page on a laptop CPU), so callers
bound how many pages they ask for. The engine is built once, on first use, and shared.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

# A rendered page is never larger than this on its longest side: a hostile page size cannot make a
# huge image, whatever the requested scale.
MAX_RENDER_PIXELS: Final = 3_600

INSTALL_HINT: Final = (
    "Text recognition for scanned PDFs is not installed on this server. Install it with "
    "`uv sync` in apps/api (it needs the free packages rapidocr, onnxruntime and pypdfium2; "
    "no system program and no account are required), then restart the API."
)


class OcrUnavailableError(Exception):
    """The OCR packages are not installed, or the engine could not start."""


class OcrFailedError(Exception):
    """A page could not be rendered or recognised. The message is safe to show: it never contains
    the document's content."""


@dataclass(frozen=True, slots=True)
class OcrPage:
    """One recognised page: its zero-based ``index``, its recognised ``lines`` in reading order and
    the engine's mean confidence (0 to 1) over them. ``lines`` is empty for a page with no text."""

    index: int
    lines: tuple[str, ...]
    mean_confidence: float


_engine: Any = None
_engine_lock = threading.Lock()


def is_available() -> bool:
    """Whether the OCR packages can be imported (the engine itself is built on first use)."""
    try:
        import pypdfium2  # type: ignore[import-untyped]  # noqa: F401
        import rapidocr  # noqa: F401
    except ImportError:
        return False
    return True


def _get_engine() -> Any:
    global _engine
    with _engine_lock:
        if _engine is None:
            try:
                from rapidocr import RapidOCR

                _engine = RapidOCR(params={"Global.log_level": "critical"})
            except Exception as exc:  # noqa: BLE001 - any start-up failure is "unavailable"
                raise OcrUnavailableError(INSTALL_HINT) from exc
        return _engine


def recognise_pages(
    content: bytes,
    indexes: Sequence[int],
    *,
    scale: float,
    min_line_confidence: float,
) -> list[OcrPage]:
    """Recognise the pages at ``indexes`` (zero-based) of the PDF ``content``, in the order given.

    Blocking and CPU-heavy: call it from a worker thread. Lines the engine is less sure of than
    ``min_line_confidence`` are dropped. Raises :class:`OcrUnavailableError` if recognition cannot
    run and :class:`OcrFailedError` if a page cannot be rendered.
    """
    try:
        import numpy
        import pypdfium2
    except ImportError as exc:
        raise OcrUnavailableError(INSTALL_HINT) from exc

    engine = _get_engine()
    try:
        document = pypdfium2.PdfDocument(content)
    except Exception as exc:  # noqa: BLE001 - any failure to open is a failure to render
        raise OcrFailedError("The PDF's pages could not be drawn for text recognition.") from exc

    pages: list[OcrPage] = []
    try:
        for index in indexes:
            try:
                page = document[index]
                width, height = page.get_size()
                longest = max(width, height) or 1.0
                effective = min(scale, MAX_RENDER_PIXELS / longest)
                image = page.render(scale=effective).to_pil().convert("RGB")
            except Exception as exc:  # noqa: BLE001
                raise OcrFailedError(f"Page {index + 1} could not be drawn.") from exc
            try:
                result: Any = engine(numpy.asarray(image))
            except Exception as exc:  # noqa: BLE001
                raise OcrFailedError(f"Text recognition failed on page {index + 1}.") from exc
            texts = list(result.txts or ())
            scores = [float(score) for score in (result.scores or ())]
            kept = [
                (text, score)
                for text, score in zip(texts, scores, strict=False)
                if score >= min_line_confidence
            ]
            mean = sum(score for _, score in kept) / len(kept) if kept else 0.0
            pages.append(
                OcrPage(
                    index=index,
                    lines=tuple(text for text, _ in kept),
                    mean_confidence=mean,
                )
            )
    finally:
        document.close()
    return pages

"""PDF text extraction - the only module allowed to import ``pypdf``
(rule 3).
"""

from __future__ import annotations

import io

from pypdf import PdfReader
from pypdf.errors import PdfReadError


class PdfParseError(Exception):
    """``content`` cannot be read as a PDF at all (corrupted, encrypted,
    truncated, or otherwise malformed).

    Distinct from a well-formed PDF that simply has no extractable text
    (e.g. a scanned image) - that's a valid zero-chunk result, not an
    error.
    """


def extract_pages(content: bytes) -> list[str]:
    """Return each page's extracted text, in page order.

    Raises :class:`PdfParseError` if ``content`` can't be parsed as a PDF -
    callers treat that as a parse failure
    (``uploaded_sources.status = 'failed'``), not a 5xx: the upload already
    passed a `%PDF` magic-byte check, so a failure here means the bytes are
    a genuinely broken PDF, not client error.
    """
    try:
        reader = PdfReader(io.BytesIO(content))
        return [page.extract_text() for page in reader.pages]
    except (PdfReadError, ValueError, KeyError) as exc:
        raise PdfParseError(str(exc)) from exc

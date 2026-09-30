"""PDF text extraction - the only module allowed to import ``pypdf``
(rule 3).
"""

from __future__ import annotations

import io

from pypdf import PdfReader


class PdfParseError(Exception):
    """``content`` cannot be read as a PDF at all (corrupted, encrypted,
    truncated, or otherwise malformed).

    Distinct from a well-formed PDF that simply has no extractable text
    (e.g. a scanned image) - that's a valid zero-chunk result, not an
    error.
    """


class PdfTooManyPagesError(PdfParseError):
    """A well-formed PDF with more pages than the caller allows. Raised *before* any page's text
    is extracted, so a huge document costs a page count, not a full parse. ``page_count`` is
    how many pages it has."""

    def __init__(self, page_count: int, max_pages: int) -> None:
        super().__init__(f"The PDF has {page_count} pages; the limit is {max_pages}.")
        self.page_count = page_count
        self.max_pages = max_pages


def extract_pages(content: bytes, *, max_pages: int | None = None) -> list[str]:
    """Return each page's extracted text, in page order.

    With ``max_pages``, a document over the limit raises :class:`PdfTooManyPagesError` after
    its page count is read and before any text is extracted - the expensive part.

    Raises :class:`PdfParseError` if ``content`` can't be parsed as a PDF -
    callers treat that as a parse failure
    (``uploaded_sources.status = 'failed'``), not a 5xx: the upload already
    passed a `%PDF` magic-byte check, so a failure here means the bytes are
    a genuinely broken PDF, not client error. Any error the parser raises on hostile or
    malformed input is one: the parser's failure modes are many and none of them is the
    caller's to handle.
    """
    try:
        reader = PdfReader(io.BytesIO(content))
        page_count = len(reader.pages)
        if max_pages is not None and page_count > max_pages:
            raise PdfTooManyPagesError(page_count, max_pages)
        return [page.extract_text() for page in reader.pages]
    except PdfParseError:
        raise
    except Exception as exc:  # noqa: BLE001 - see the docstring
        raise PdfParseError(str(exc)) from exc

"""Real, parseable PDFs for tests, built in memory."""

from __future__ import annotations

from fpdf import FPDF


def make_pdf(pages: list[str] | None = None) -> bytes:
    """A real, pypdf-parseable PDF with one page per string in ``pages`` (default: a single page
    of sample text). An empty string produces a genuinely blank page (no extractable text) - used
    to test that the parsing pipeline skips blank pages when chunking.
    """
    texts = pages if pages is not None else ["Test content for parsing."]
    doc = FPDF()
    for text in texts:
        doc.add_page()
        if text:
            doc.set_font("Helvetica", size=12)
            doc.cell(text=text)
    return bytes(doc.output())

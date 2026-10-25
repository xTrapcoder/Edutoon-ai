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


def make_scanned_pdf(pages: list[tuple[str, str]], *, blur: float = 0.8) -> bytes:
    """A PDF whose pages are *pictures* of text (a heading and a paragraph each), with no text
    layer: what a scan looks like. Drawn with Pillow's built-in font, so it needs nothing else."""
    import io
    import textwrap

    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    heading_font = ImageFont.load_default(size=50)
    body_font = ImageFont.load_default(size=34)
    images = []
    for heading, body in pages:
        image = Image.new("L", (1240, 1754), 245)
        draw = ImageDraw.Draw(image)
        draw.text((100, 120), heading, font=heading_font, fill=20)
        y = 240
        for line in textwrap.wrap(body, 58):
            draw.text((100, y), line, font=body_font, fill=30)
            y += 52
        if blur:
            image = image.filter(ImageFilter.GaussianBlur(blur))
        images.append(image.convert("RGB"))
    buffer = io.BytesIO()
    images[0].save(buffer, format="PDF", save_all=True, append_images=images[1:], resolution=150)
    return buffer.getvalue()

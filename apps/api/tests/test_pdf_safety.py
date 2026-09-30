"""PDF resource safety: an oversized upload is refused before it is read, an over-long document
before its text is extracted, and a document with no text is a clear failure, never zero evidence.

* The body-size middleware answers ``413`` from ``Content-Length`` alone, cuts off a chunked body
  that outgrows the limit, and lets everything else through untouched (pure ASGI tests).
* The PDF provider counts pages before it extracts any text and treats whatever the parser raises on
  hostile input as an unreadable file.
* An upload with no extractable text is stored and recorded as ``failed`` - with the reason an owner
  can act on - and is never queued for indexing.
"""

from __future__ import annotations

import json

import pytest

from edutoon.api.middleware import MULTIPART_OVERHEAD_BYTES, BodySizeLimitMiddleware
from edutoon.providers import pdf
from tests.pdf_helpers import make_pdf as _make_pdf

# --- the middleware ------------------------------------------------------------------------


class Recorder:
    """A minimal ASGI app that records whether it ran and how much body it read."""

    def __init__(self) -> None:
        self.called = False
        self.read = 0

    async def __call__(self, scope, receive, send) -> None:
        self.called = True
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            self.read += len(message.get("body", b""))
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def scope(*, length: int | None = None, kind: str = "http") -> dict:
    headers = [(b"host", b"test")]
    if length is not None:
        headers.append((b"content-length", str(length).encode()))
    return {"type": kind, "method": "POST", "path": "/", "headers": headers}


async def run(app, scope_: dict, chunks: list[bytes]) -> tuple[list[dict], int]:
    """Drive ``app`` with a body delivered as ``chunks``; return what it sent and how many chunks
    it asked for."""
    sent: list[dict] = []
    asked = 0
    queue = list(chunks)

    async def receive() -> dict:
        nonlocal asked
        asked += 1
        if not queue:
            return {"type": "http.disconnect"}
        body = queue.pop(0)
        return {"type": "http.request", "body": body, "more_body": bool(queue)}

    async def send(message: dict) -> None:
        sent.append(message)

    await app(scope_, receive, send)
    return sent, asked


def status_of(sent: list[dict]) -> int:
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def body_of(sent: list[dict]) -> dict:
    return json.loads(b"".join(m["body"] for m in sent if m["type"] == "http.response.body"))


async def test_a_body_that_declares_more_than_the_limit_is_refused_without_being_read() -> None:
    inner = Recorder()
    app = BodySizeLimitMiddleware(inner, max_bytes=1000)

    sent, asked = await run(app, scope(length=1001), [b"x" * 10])

    assert status_of(sent) == 413
    assert body_of(sent)["error"]["code"] == "payload_too_large"
    assert inner.called is False and asked == 0  # not a byte of the body was requested


async def test_a_body_within_the_limit_passes_through_untouched() -> None:
    inner = Recorder()
    app = BodySizeLimitMiddleware(inner, max_bytes=1000)

    sent, _ = await run(app, scope(length=1000), [b"x" * 600, b"x" * 400])

    assert status_of(sent) == 200 and inner.read == 1000


async def test_a_chunked_body_with_no_declared_length_is_cut_off_once_it_outgrows_the_limit() -> (
    None
):
    inner = Recorder()
    app = BodySizeLimitMiddleware(inner, max_bytes=1000)

    sent, _ = await run(app, scope(), [b"x" * 600, b"x" * 600, b"x" * 600])

    assert status_of(sent) == 413
    assert [m for m in sent if m["type"] == "http.response.start"].__len__() == 1  # one answer only


async def test_a_chunked_body_within_the_limit_passes() -> None:
    inner = Recorder()
    app = BodySizeLimitMiddleware(inner, max_bytes=1000)

    sent, _ = await run(app, scope(), [b"x" * 500, b"x" * 500])

    assert status_of(sent) == 200 and inner.read == 1000


async def test_a_length_header_that_is_not_a_number_is_left_to_the_streaming_count() -> None:
    inner = Recorder()
    app = BodySizeLimitMiddleware(inner, max_bytes=1000)
    odd = scope()
    odd["headers"].append((b"content-length", b"lots"))

    sent, _ = await run(app, odd, [b"x" * 10])

    assert status_of(sent) == 200


async def test_anything_that_is_not_http_is_passed_straight_through() -> None:
    seen: list[str] = []

    async def inner(scope_, receive, send) -> None:  # noqa: ANN001
        seen.append(scope_["type"])

    app = BodySizeLimitMiddleware(inner, max_bytes=1)

    await app(scope(kind="lifespan"), None, None)  # type: ignore[arg-type]

    assert seen == ["lifespan"]


def test_the_limit_leaves_room_for_multipart_framing_above_the_largest_file() -> None:
    assert MULTIPART_OVERHEAD_BYTES >= 64 * 1024


# --- the PDF provider ----------------------------------------------------------------------


def test_a_document_over_the_page_limit_is_refused_before_any_text_is_extracted(
    monkeypatch,
) -> None:
    from pypdf import PageObject

    def never(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("text was extracted from a document over the page limit")

    monkeypatch.setattr(PageObject, "extract_text", never)
    content = _make_pdf(["one", "two", "three"])

    with pytest.raises(pdf.PdfTooManyPagesError) as caught:
        pdf.extract_pages(content, max_pages=2)

    assert (caught.value.page_count, caught.value.max_pages) == (3, 2)
    assert isinstance(caught.value, pdf.PdfParseError)  # existing handlers still catch it


def test_a_document_within_the_limit_is_extracted_as_before() -> None:
    assert pdf.extract_pages(_make_pdf(["one", "two"]), max_pages=2) == ["one", "two"]
    assert pdf.extract_pages(_make_pdf(["one", "two"])) == ["one", "two"]


@pytest.mark.parametrize("error", [RecursionError("deep"), AttributeError("x"), TypeError("y")])
def test_whatever_the_parser_raises_on_hostile_input_is_an_unreadable_file(
    monkeypatch, error
) -> None:
    def boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise error

    monkeypatch.setattr(pdf, "PdfReader", boom)

    with pytest.raises(pdf.PdfParseError):
        pdf.extract_pages(b"%PDF-1.4 whatever")

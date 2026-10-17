from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.config import get_settings
from edutoon.core.errors import ConflictError, NotFoundError, ValidationError
from edutoon.core.pagination import Page
from edutoon.domain import pasted_text, pdf_pages
from edutoon.domain.deck import has_content, slide_text, slide_to_json
from edutoon.domain.source_storage import source_storage_key
from edutoon.providers import ocr, pdf, pptx
from edutoon.providers.storage import Storage
from edutoon.repositories import uploaded_sources as uploaded_sources_repo
from edutoon.repositories.source_chunks import NewSourceChunk
from edutoon.repositories.uploaded_sources import UploadedSourceRecord as UploadedSourceRecord
from edutoon.services import evidence as evidence_service
from edutoon.services import source_chunks as source_chunks_service
from edutoon.services import spend_guard

PDF_CONTENT_TYPE = "application/pdf"
PPTX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
TEXT_CONTENT_TYPE = "text/plain; charset=utf-8"
TEXT_SOURCE_TYPE = "text"


def detect_kind(content: bytes) -> str | None:
    """What ``content`` actually is - ``"pdf"``, ``"pptx"`` or ``None`` - judged by its bytes, never
    by the name or type the client gave it."""
    if content.startswith(b"%PDF"):
        return "pdf"
    if pptx.looks_like_pptx(content):
        return "pptx"
    return None


async def upload_source(
    session: AsyncSession,
    storage: Storage,
    *,
    project_id: UUID,
    uploader_id: UUID,
    original_filename: str,
    content_type: str,
    content: bytes,
    bucket: str,
    max_pages: int,
    max_slides: int = 100,
) -> UploadedSourceRecord:
    """Store ``content``, record it against ``project_id``, and parse it
    into ``source_chunks`` inline, one chunk per non-blank page. Parsing
    itself stays synchronous (unchanged from before); once it succeeds, a
    ``build_evidence`` job is enqueued (``evidence.enqueue_indexing_job``)
    so a worker can embed those chunks - the source's own row transitions
    straight to ``status="parsed"`` here regardless, since indexing is a
    separate, asynchronous concern from parsing having succeeded.

    The storage key is deterministic (derived from the content's checksum),
    so a genuine re-upload of identical bytes overwrites the same object
    rather than accumulating duplicates - the ``uploaded_sources`` row for
    it is what actually enforces "already uploaded" (rule 4-adjacent: the
    unique index on ``(project_id, checksum_sha256)``), via
    ``raise_conflict_from_integrity_error`` in the repository.

    A PDF that can't be read at all, that exceeds ``max_pages`` (counted *before* any text
    is extracted), or that has no extractable text (a scan, or nothing but page numbers), still
    produces a row - with ``status="failed"`` - rather than failing the
    whole request: the upload (storage + traceability record) succeeded
    even though parsing didn't, and a failed source can't feed anything
    downstream. A source with no text is never queued for indexing and never silently
    "parsed": a project cannot proceed on zero evidence without being told
    (``failure_reason`` in the API says why).

    A ``.pptx`` deck (``content_type`` :data:`PPTX_CONTENT_TYPE`) takes the deck path instead
    (:func:`_upload_deck`): one chunk per slide with its structure, and no indexing job, because
    a deck lesson is written from the slides themselves and needs no embedding provider. The PDF
    path below is unchanged.
    """
    is_deck = content_type == PPTX_CONTENT_TYPE
    if not is_deck:
        # Refuse before anything is stored: an upload that cannot be indexed is not accepted.
        await spend_guard.ensure_can_start_provider_job(session, project_id=project_id)

    checksum = hashlib.sha256(content).hexdigest()
    storage_key = source_storage_key(project_id, checksum)

    await storage.put_object(
        bucket=bucket, key=storage_key, body=content, content_type=content_type
    )

    source = await create_uploaded_source(
        session,
        project_id=project_id,
        original_filename=original_filename,
        storage_bucket=bucket,
        storage_key=storage_key,
        byte_size=len(content),
        checksum_sha256=checksum,
        uploader_id=uploader_id,
        content_type=content_type,
        kind="pptx" if is_deck else "pdf",
    )

    if is_deck:
        return await _upload_deck(
            session, source=source, project_id=project_id, content=content, max_slides=max_slides
        )

    try:
        pages: list[str] = [text or "" for text in pdf.extract_pages(content, max_pages=max_pages)]
    except pdf.PdfTooManyPagesError as too_long:
        return await update_uploaded_source(
            session,
            source_id=source.id,
            project_id=project_id,
            status="failed",
            page_count=too_long.page_count,
            failure_code="too_many_pages",
        )
    except pdf.PdfParseError:
        return await update_uploaded_source(
            session,
            source_id=source.id,
            project_id=project_id,
            status="failed",
            failure_code="unreadable",
        )

    reading = await _read_scanned_pages(pages, content)
    pages = reading.pages

    if not any(character.isalpha() for text in pages for character in text):
        # A scan nothing could be read from, or a document that is only page numbers and symbols:
        # there is nothing to build evidence from. Fail it, accurately, instead of "parsing" zero
        # chunks - and say whether text recognition was tried.
        return await update_uploaded_source(
            session,
            source_id=source.id,
            project_id=project_id,
            status="failed",
            page_count=len(pages),
            failure_code=reading.failure_code or "no_text",
        )

    chunks = [
        NewSourceChunk(
            source_id=source.id,
            project_id=project_id,
            chunk_index=index,
            content=text.strip(),
            page_from=index + 1,
            page_to=index + 1,
        )
        for index, text in enumerate(pages)
        if text.strip()
    ]
    if chunks:
        await source_chunks_service.create_source_chunks(session, chunks)

    parsed = await update_uploaded_source(
        session,
        source_id=source.id,
        project_id=project_id,
        status="parsed",
        page_count=len(pages),
        extraction_method="ocr" if reading.ocr_pages_kept else "text",
        ocr_page_count=reading.ocr_pages_kept,
    )
    await evidence_service.enqueue_indexing_job(session, project_id=project_id, source_id=source.id)
    return parsed


# One document is recognised at a time: OCR keeps every core busy for seconds a page, and two at
# once would each take twice as long without finishing sooner.
_ocr_gate = asyncio.Semaphore(1)

# In a PDF that otherwise has text, pages with none are read by OCR only when they are at least this
# share of it: a text PDF with a few pictures is not worth minutes of recognition, a PDF that is
# a fifth scans has lost a fifth of its content.
_MIXED_OCR_MIN_SHARE = 0.2


@dataclass(frozen=True, slots=True)
class _PageReading:
    """The pages to chunk, how many of them OCR supplied (``None`` if it was not used), and, when no
    usable text came out at all, the reason text recognition could not help."""

    pages: list[str]
    ocr_pages_kept: int | None = None
    failure_code: str | None = None


async def _read_scanned_pages(pages: list[str], content: bytes) -> _PageReading:
    """Fill in the pages whose own text layer is empty by reading them with local OCR.

    Never invents text: a page OCR cannot read confidently stays empty. A PDF made entirely of
    scans needs OCR, so every way it can fail is named (``ocr_unavailable``,
    ``ocr_too_many_pages``, ``ocr_failed``, ``ocr_no_text``); a PDF with text of its own simply
    keeps that text when OCR is skipped or fails.
    """
    candidates = pdf_pages.pages_without_text(pages)
    if not candidates:
        return _PageReading(pages)

    settings = get_settings()
    scanned_only = len(candidates) == len(pages)
    if scanned_only:
        if not settings.OCR_ENABLED or not ocr.is_available():
            return _PageReading(pages, failure_code="ocr_unavailable")
        if len(candidates) > settings.OCR_MAX_PAGES:
            return _PageReading(pages, failure_code="ocr_too_many_pages")
    elif (
        not settings.OCR_ENABLED
        or len(candidates) / len(pages) < _MIXED_OCR_MIN_SHARE
        or len(candidates) > settings.OCR_MAX_PAGES
        or not ocr.is_available()
    ):
        return _PageReading(pages)

    try:
        async with _ocr_gate:
            recognised = await asyncio.to_thread(
                ocr.recognise_pages,
                content,
                candidates,
                scale=settings.OCR_RENDER_SCALE,
                min_line_confidence=pdf_pages.MIN_OCR_CONFIDENCE,
            )
    except ocr.OcrUnavailableError:
        return _PageReading(pages, failure_code="ocr_unavailable" if scanned_only else None)
    except ocr.OcrFailedError:
        return _PageReading(pages, failure_code="ocr_failed" if scanned_only else None)

    filled = list(pages)
    kept = 0
    for page in recognised:
        text = pdf_pages.tidy_ocr_text(page.lines)
        if pdf_pages.page_is_readable(text, page.mean_confidence):
            filled[page.index] = text
            kept += 1
    if kept == 0:
        return _PageReading(pages, failure_code="ocr_no_text" if scanned_only else None)
    return _PageReading(filled, ocr_pages_kept=kept)


async def upload_pasted_text(
    session: AsyncSession,
    storage: Storage,
    *,
    project_id: UUID,
    project_source_type: str,
    uploader_id: UUID,
    title: str | None,
    text: str,
    bucket: str,
) -> UploadedSourceRecord:
    """Record text the owner pasted as a source and cut it into evidence chunks.

    The same promise as a PDF: the chunks are what every claim is later checked against, and the
    ``build_evidence`` job embeds them. Unlike a PDF, bad input is refused *before* anything is
    stored or spent (the text arrives in the request, so a too-short or word-free paste is the
    caller's to fix, not a failed upload to record). Only a project of source type ``text`` takes
    pasted text. Re-sending identical text to the same project is a 409, by the same checksum
    constraint a re-uploaded PDF meets.
    """
    if project_source_type != TEXT_SOURCE_TYPE:
        raise ConflictError("Pasted text can only be added to a project made from pasted text.")

    cleaned = pasted_text.clean(text)
    try:
        pasted_text.validate(cleaned)
    except pasted_text.PastedTextError as problem:
        raise ValidationError(str(problem)) from problem

    await spend_guard.ensure_can_start_provider_job(session, project_id=project_id)

    content = cleaned.encode("utf-8")
    checksum = hashlib.sha256(content).hexdigest()
    storage_key = source_storage_key(project_id, checksum, "txt")
    await storage.put_object(
        bucket=bucket, key=storage_key, body=content, content_type=TEXT_CONTENT_TYPE
    )
    source = await create_uploaded_source(
        session,
        project_id=project_id,
        original_filename=(title or "").strip() or pasted_text.DEFAULT_TITLE,
        storage_bucket=bucket,
        storage_key=storage_key,
        byte_size=len(content),
        checksum_sha256=checksum,
        uploader_id=uploader_id,
        content_type=TEXT_CONTENT_TYPE,
        kind="text",
    )

    chunks = pasted_text.split_chunks(cleaned)
    await source_chunks_service.create_source_chunks(
        session,
        [
            NewSourceChunk(
                source_id=source.id,
                project_id=project_id,
                chunk_index=index,
                content=chunk,
                page_from=None,
                page_to=None,
            )
            for index, chunk in enumerate(chunks)
        ],
    )
    parsed = await update_uploaded_source(
        session,
        source_id=source.id,
        project_id=project_id,
        status="parsed",
        page_count=None,
    )
    await evidence_service.enqueue_indexing_job(session, project_id=project_id, source_id=source.id)
    return parsed


async def _upload_deck(
    session: AsyncSession,
    *,
    source: UploadedSourceRecord,
    project_id: UUID,
    content: bytes,
    max_slides: int,
) -> UploadedSourceRecord:
    """Parse a stored ``.pptx`` into one chunk per slide that has anything to say.

    A deck that cannot be read, has too many slides, or has no text anywhere (every slide a
    picture) is recorded as ``failed`` and never becomes evidence - the same promise a PDF has.
    A slide with no text is left out of the chunks, but the deck keeps its slide numbers: a
    chunk's ``page_from`` is the slide's own number in the deck.
    """
    try:
        deck = pptx.extract_deck(content, max_slides=max_slides)
    except pptx.PptxTooManySlidesError as too_long:
        return await update_uploaded_source(
            session,
            source_id=source.id,
            project_id=project_id,
            status="failed",
            page_count=too_long.slide_count,
        )
    except pptx.PptxParseError:
        return await update_uploaded_source(
            session, source_id=source.id, project_id=project_id, status="failed"
        )

    slides = [slide for slide in deck.slides if has_content(slide)]
    if not slides:
        return await update_uploaded_source(
            session,
            source_id=source.id,
            project_id=project_id,
            status="failed",
            page_count=deck.slide_count,
        )

    await source_chunks_service.create_source_chunks(
        session,
        [
            NewSourceChunk(
                source_id=source.id,
                project_id=project_id,
                chunk_index=index,
                content=slide_text(slide),
                page_from=slide.index,
                page_to=slide.index,
                structure=slide_to_json(slide),
            )
            for index, slide in enumerate(slides)
        ],
    )
    return await update_uploaded_source(
        session,
        source_id=source.id,
        project_id=project_id,
        status="parsed",
        page_count=deck.slide_count,
    )


async def create_uploaded_source(
    session: AsyncSession,
    *,
    project_id: UUID,
    original_filename: str,
    storage_bucket: str,
    storage_key: str,
    byte_size: int,
    checksum_sha256: str,
    uploader_id: UUID | None = None,
    content_type: str = "application/pdf",
    page_count: int | None = None,
    kind: str = "pdf",
) -> UploadedSourceRecord:
    return await uploaded_sources_repo.create(
        session,
        project_id=project_id,
        original_filename=original_filename,
        storage_bucket=storage_bucket,
        storage_key=storage_key,
        byte_size=byte_size,
        checksum_sha256=checksum_sha256,
        uploader_id=uploader_id,
        content_type=content_type,
        page_count=page_count,
        kind=kind,
    )


async def get_uploaded_source_or_404(
    session: AsyncSession, source_id: UUID
) -> UploadedSourceRecord:
    source = await uploaded_sources_repo.get_by_id(session, source_id)
    if source is None:
        raise NotFoundError(f"Uploaded source {source_id} not found.")
    return source


async def get_uploaded_source_for_project(
    session: AsyncSession, *, source_id: UUID, project_id: UUID
) -> UploadedSourceRecord:
    """Ownership-scoped fetch (rule 9): a source from a different project
    looks exactly like one that doesn't exist.
    """
    source = await uploaded_sources_repo.get_by_id_for_project(session, source_id, project_id)
    if source is None:
        raise NotFoundError(f"Uploaded source {source_id} not found.")
    return source


async def update_uploaded_source(
    session: AsyncSession, *, source_id: UUID, project_id: UUID, **fields: object
) -> UploadedSourceRecord:
    source = await uploaded_sources_repo.update(
        session, source_id=source_id, project_id=project_id, **fields
    )
    if source is None:
        raise NotFoundError(f"Uploaded source {source_id} not found.")
    return source


async def get_uploaded_source_by_checksum(
    session: AsyncSession, *, project_id: UUID, checksum_sha256: str
) -> UploadedSourceRecord | None:
    return await uploaded_sources_repo.get_by_checksum(
        session, project_id=project_id, checksum_sha256=checksum_sha256
    )


async def list_uploaded_sources(
    session: AsyncSession, project_id: UUID, *, limit: int = 50, cursor: str | None = None
) -> Page[UploadedSourceRecord]:
    return await uploaded_sources_repo.list_by_project(
        session, project_id, limit=limit, cursor=cursor
    )

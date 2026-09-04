"""``/v1/projects/{project_id}/sources`` - PDF upload onto a project.

``project_id`` ownership is checked first on every route (rule 9: a project
the caller doesn't own looks exactly like one that doesn't exist - 404,
never 403). The upload itself requires an ``Idempotency-Key`` header
(rule 8) and dedupes through ``services/idempotency.py`` exactly like
``routers/projects.py``: a repeat of the same (caller, method, path, key)
replays the original response instead of re-uploading, while a real second
upload of the same file content (a different key, same checksum) is turned
into a 409 by the ``uq_uploaded_sources_project_id_checksum_sha256``
constraint via ``core/errors.py``.
"""

from __future__ import annotations

from typing import Annotated, Any, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, File, Header, Request, UploadFile, status
from fastapi.exceptions import RequestValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.api.dependencies import RedisDep, StorageDep, get_current_user
from edutoon.api.schemas.uploaded_sources import (
    UploadedSourceListResponse,
    UploadedSourceResponse,
)
from edutoon.core.config import get_settings
from edutoon.core.errors import PayloadTooLargeError
from edutoon.core.pagination import DEFAULT_PAGE_SIZE
from edutoon.db.session import get_session
from edutoon.services import idempotency as idempotency_service
from edutoon.services import projects as projects_service
from edutoon.services import uploaded_sources as uploaded_sources_service
from edutoon.services.users import UserRecord

router = APIRouter(prefix="/projects/{project_id}/sources", tags=["uploaded-sources"])

CurrentUser = Annotated[UserRecord, Depends(get_current_user)]
Session = Annotated[AsyncSession, Depends(get_session)]
IdempotencyKey = Annotated[str, Header()]

_ALLOWED_FORM_FIELDS = {"file"}
_PDF_MAGIC_BYTES = b"%PDF"
_ACCEPTED_CONTENT_TYPE = "application/pdf"


def _raise_validation_error(field: str, msg: str, error_type: str = "value_error") -> NoReturn:
    """Multipart bodies aren't Pydantic-validated, so this is the hand-rolled
    equivalent of a field validator - it produces the exact same 422 shape
    FastAPI gives a malformed JSON body (see ``ProjectCreateRequest``),
    rather than the 400 that ``core.errors.ValidationError`` maps to (that
    class is for deeper domain checks, e.g. a pagination ``limit``, not
    "the request itself is malformed").
    """
    raise RequestValidationError(
        [{"loc": ("body", field), "msg": msg, "type": error_type}]
    )


def _reject_unknown_form_fields(field_names: set[str]) -> None:
    """Rule 10's "unknown fields -> 422" for a multipart body: any field
    beyond ``file``.
    """
    unknown = field_names - _ALLOWED_FORM_FIELDS
    if unknown:
        raise RequestValidationError(
            [
                {
                    "loc": ("body", field),
                    "msg": "Extra inputs are not permitted",
                    "type": "extra_forbidden",
                }
                for field in sorted(unknown)
            ]
        )


@router.post("", status_code=status.HTTP_201_CREATED)
async def upload_source(
    request: Request,
    project_id: UUID,
    current_user: CurrentUser,
    session: Session,
    redis: RedisDep,
    storage: StorageDep,
    idempotency_key: IdempotencyKey,
    file: Annotated[UploadFile, File()],
) -> UploadedSourceResponse:
    form = await request.form()
    _reject_unknown_form_fields(set(form.keys()))

    await projects_service.get_project(session, project_id=project_id, owner_id=current_user.id)

    if file.content_type != _ACCEPTED_CONTENT_TYPE:
        _raise_validation_error(
            "file", f"Only {_ACCEPTED_CONTENT_TYPE} uploads are accepted.", "content_type_error"
        )
    content_type = _ACCEPTED_CONTENT_TYPE

    filename = file.filename
    if not filename or not filename.strip():
        _raise_validation_error("file", "A filename is required.", "missing")

    settings = get_settings()
    content = await file.read()
    if len(content) > settings.MAX_UPLOAD_BYTES:
        raise PayloadTooLargeError(
            f"File exceeds the maximum upload size of {settings.MAX_UPLOAD_BYTES} bytes."
        )
    if not content.startswith(_PDF_MAGIC_BYTES):
        _raise_validation_error("file", "File does not look like a PDF.", "value_error")

    async def _execute() -> tuple[int, Any]:
        source = await uploaded_sources_service.upload_source(
            session,
            storage,
            project_id=project_id,
            uploader_id=current_user.id,
            original_filename=filename,
            content_type=content_type,
            content=content,
            bucket=settings.BUCKET_UPLOADS,
        )
        return status.HTTP_201_CREATED, UploadedSourceResponse.from_record(source).model_dump(
            mode="json"
        )

    _, response_body = await idempotency_service.run_with_idempotency(
        redis,
        owner_id=current_user.id,
        method="POST",
        path=request.url.path,
        idempotency_key=idempotency_key,
        handler=_execute,
    )
    return UploadedSourceResponse.model_validate(response_body)


@router.get("")
async def list_sources(
    project_id: UUID,
    current_user: CurrentUser,
    session: Session,
    limit: int = DEFAULT_PAGE_SIZE,
    cursor: str | None = None,
) -> UploadedSourceListResponse:
    await projects_service.get_project(session, project_id=project_id, owner_id=current_user.id)

    page = await uploaded_sources_service.list_uploaded_sources(
        session, project_id, limit=limit, cursor=cursor
    )
    return UploadedSourceListResponse(
        items=[UploadedSourceResponse.from_record(source) for source in page.items],
        next_cursor=page.next_cursor,
    )

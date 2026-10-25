"""The one function that serves a training module's stored video over HTTP.

Both routes that play a module - the administrator's preview
(``GET /v1/admin/training/modules/{id}/video``) and the employee's stream
(``GET /v1/training/modules/{id}/video``) - end in :func:`serve_module_video` and nowhere else.
So there is exactly one implementation of: reading the module's **own** snapshot (never the
render it came from), parsing ``Range``, the ``206`` partial response, the ``416`` for a range
past the end, ``Accept-Ranges`` / ``Content-Range`` / ``Content-Length``, the content type and the
404 for a module with nothing to play. The routes differ only in *who may call* - decided before
this is reached.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from edutoon.core.errors import NotFoundError
from edutoon.domain.training_storage import VIDEO_CONTENT_TYPE
from edutoon.providers.storage import Storage
from edutoon.services import training as training_service


async def serve_module_video(
    session: AsyncSession,
    storage: Storage,
    *,
    module_id: UUID,
    range_header: str | None,
    download: bool = False,
    not_found_message: str | None = None,
) -> Response:
    """Stream the module's stored video, or one byte range of it.

    ``not_found_message`` (optional) replaces the text of *any* 404 raised on the way, so a caller
    that must give one identical answer for every cause (the employee route) can.
    """
    try:
        opened = await training_service.open_module_video(
            session, storage, module_id=module_id, range_header=range_header
        )
    except NotFoundError:
        if not_found_message is None:
            raise
        raise NotFoundError(not_found_message) from None
    if opened.stream is None:
        return Response(
            status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
            headers={"Content-Range": f"bytes */{opened.unsatisfiable_size}"},
        )
    stream = opened.stream
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(stream.content_length)}
    if stream.content_range:
        headers["Content-Range"] = stream.content_range
    if download:
        headers["Content-Disposition"] = f'attachment; filename="module-{module_id}.mp4"'
    return StreamingResponse(
        stream.iter_chunks(),
        status_code=stream.status_code,
        media_type=VIDEO_CONTENT_TYPE,
        headers=headers,
    )

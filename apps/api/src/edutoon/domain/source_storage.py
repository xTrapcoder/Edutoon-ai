"""Where an uploaded source lives in object storage.

An upload is stored under one key, derived from the project it belongs to and the SHA-256 of
its bytes::

    projects/{project_id}/{checksum_sha256}.pdf

Content-addressed, so uploading identical bytes to the same project overwrites the same object
instead of accumulating copies, and per-project, so nothing one project stores can collide with
another's. This module is the single place that says so: the upload writes with
:func:`source_storage_key`, and removing a project's files checks each one with
:func:`belongs_to_project` first, so a deletion can only ever reach objects in that project's own
prefix - never a neighbour's, and never something a stray row happens to point at.
"""

from __future__ import annotations

from uuid import UUID


def project_storage_prefix(project_id: UUID) -> str:
    """The prefix every object belonging to ``project_id`` is stored under."""
    return f"projects/{project_id}/"


def source_storage_key(project_id: UUID, checksum_sha256: str) -> str:
    """The key an uploaded PDF with this checksum is stored at."""
    return f"{project_storage_prefix(project_id)}{checksum_sha256}.pdf"


def belongs_to_project(key: str, project_id: UUID) -> bool:
    """Whether ``key`` is a real object key inside ``project_id``'s own prefix.

    The prefix itself (an empty name after it) is not an object, so it does not qualify, and a
    key that climbs out of the prefix with a ``..`` segment is refused rather than trusted.
    """
    prefix = project_storage_prefix(project_id)
    return key.startswith(prefix) and len(key) > len(prefix) and ".." not in key.split("/")

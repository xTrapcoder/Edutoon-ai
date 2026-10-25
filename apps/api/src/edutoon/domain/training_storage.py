"""Where a published training video lives in object storage.

Publishing copies the project's rendered MP4 to a key the **module** owns::

    training/modules/{module_id}/{publication_id}.mp4

so the module's video is independent of the project: a re-render overwrites the project's own
object (``projects/{project_id}/renders/...``, see ``domain.render.output_storage_key``) and
deleting the project removes only objects under ``projects/{project_id}/`` - neither can touch this
key, because it is outside every project prefix. The ``publication_id`` is fresh for each
publication, so replacing a module's video writes a *new* object, swaps the row, and only then
removes the old one.

Mirrors the ``source_storage`` module: this is the single place that builds a module key, and
:func:`belongs_to_module` is what a clean-up must check before it deletes anything, so a deletion
can only ever reach objects in that module's own prefix - never a project's, never a neighbour's.
The database enforces the same invariant (``ck_training_modules_video_key_is_module_owned``).
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

MODULES_STORAGE_ROOT: Final = "training/modules/"
VIDEO_CONTENT_TYPE: Final = "video/mp4"


def module_storage_prefix(module_id: UUID) -> str:
    """The prefix every object belonging to ``module_id`` is stored under."""
    return f"{MODULES_STORAGE_ROOT}{module_id}/"


def module_video_key(module_id: UUID, publication_id: UUID) -> str:
    """The key one publication of ``module_id``'s video is stored at."""
    return f"{module_storage_prefix(module_id)}{publication_id}.mp4"


def belongs_to_module(key: str, module_id: UUID) -> bool:
    """Whether ``key`` is a real object key inside ``module_id``'s own prefix.

    The prefix itself (an empty name after it) is not an object, and a key that climbs out of the
    prefix with a ``..`` segment is refused rather than trusted.
    """
    prefix = module_storage_prefix(module_id)
    return key.startswith(prefix) and len(key) > len(prefix) and ".." not in key.split("/")

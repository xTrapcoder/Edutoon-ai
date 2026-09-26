"""``domain.source_storage`` - where an upload is stored, and what counts as a project's own file.

Pure: no storage, no database. The deletion that relies on it is tested against real object
storage in ``test_project_deletion_storage.py``.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from edutoon.domain.source_storage import (
    belongs_to_project,
    project_storage_prefix,
    source_storage_key,
)

PROJECT = UUID("018f0000-0000-7000-8000-000000000001")
OTHER = UUID("018f0000-0000-7000-8000-000000000002")
CHECKSUM = "a" * 64


def test_an_upload_is_stored_under_its_project_and_its_checksum() -> None:
    assert source_storage_key(PROJECT, CHECKSUM) == f"projects/{PROJECT}/{CHECKSUM}.pdf"


def test_the_layout_is_the_one_uploads_have_always_used() -> None:
    """Objects already in storage were written with this exact shape; changing it would
    orphan them all. (See ``services.uploaded_sources.upload_source``.)
    """
    assert project_storage_prefix(PROJECT) == "projects/018f0000-0000-7000-8000-000000000001/"
    assert source_storage_key(PROJECT, CHECKSUM).startswith(project_storage_prefix(PROJECT))


def test_an_upload_key_belongs_to_its_own_project_and_no_other() -> None:
    key = source_storage_key(PROJECT, CHECKSUM)

    assert belongs_to_project(key, PROJECT) is True
    assert belongs_to_project(key, OTHER) is False


@pytest.mark.parametrize(
    "key",
    [
        "",
        f"projects/{PROJECT}/",  # the prefix itself is not an object
        f"projects/{PROJECT}",  # the same without its slash
        f"projects/{PROJECT}x/{CHECKSUM}.pdf",  # a different project whose id merely starts alike
        f"{PROJECT}/{CHECKSUM}.pdf",  # not under projects/ at all
        f"other/{CHECKSUM}.pdf",
        f"projects/{PROJECT}/../{OTHER}/{CHECKSUM}.pdf",  # climbs out into a neighbour
        f"projects/{PROJECT}/nested/../../{OTHER}/x.pdf",
    ],
)
def test_a_key_that_is_not_provably_the_projects_own_is_refused(key: str) -> None:
    assert belongs_to_project(key, PROJECT) is False


def test_a_project_id_that_only_shares_a_prefix_with_another_never_matches() -> None:
    a, b = uuid4(), uuid4()
    assert belongs_to_project(source_storage_key(a, CHECKSUM), b) is False

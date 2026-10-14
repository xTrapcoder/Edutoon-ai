"""The rules for when a module's files are removed - pure logic, no database, no storage server.

``services.training.storage_effects`` decides which of two lists to act on depending on whether the
request's commit went through:

* before the commit: only the **new** objects (``created``) are removed; nothing the committed rows
  still name (``obsolete``) is touched;
* after the commit: only ``obsolete`` objects are removed; ``created`` ones are kept;
* a removal that fails never fails the request and is logged with its bucket and key.

The real-database, real-storage versions of these are in ``test_training_committed.py``.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from edutoon.domain.training import reorder_mismatch
from edutoon.domain.training_storage import module_video_key
from edutoon.services import training as training_service
from edutoon.services.training import (
    ModuleObject,
    remove_module_objects,
    storage_effects,
)
from tests.training_helpers import RecordingLog


class FakeStorage:
    def __init__(self, fail_on: set[str] | None = None) -> None:
        self.deleted: list[str] = []
        self.fail_on = fail_on or set()

    async def delete_object(self, *, bucket: str, key: str) -> None:
        if key in self.fail_on:
            raise RuntimeError("storage is down")
        self.deleted.append(key)


def _object(module_id=None) -> ModuleObject:
    module_id = module_id or uuid4()
    return ModuleObject(module_id, "bucket", module_video_key(module_id, uuid4()))


async def _commit_ok() -> None:
    return None


@pytest.fixture
def logs(monkeypatch) -> RecordingLog:
    recording = RecordingLog()
    monkeypatch.setattr(training_service, "log", recording)
    return recording


async def test_a_clean_exit_removes_only_the_obsolete_objects():
    storage = FakeStorage()
    new, old = _object(), _object()

    async with storage_effects(storage) as effects:  # type: ignore[arg-type]
        effects.created.append(new)
        effects.obsolete.append(old)
        await effects.wrap_commit(_commit_ok)()

    assert storage.deleted == [old.key]


async def test_a_failure_before_the_commit_removes_only_the_new_objects():
    storage = FakeStorage()
    new, old = _object(), _object()

    with pytest.raises(RuntimeError, match="database down"):
        async with storage_effects(storage) as effects:  # type: ignore[arg-type]
            effects.created.append(new)
            effects.obsolete.append(old)
            raise RuntimeError("database down")

    assert storage.deleted == [new.key]  # the old file is still named by committed rows


async def test_a_commit_that_raises_counts_as_before_the_commit():
    storage = FakeStorage()
    new, old = _object(), _object()

    async def failing_commit() -> None:
        raise RuntimeError("commit failed")

    with pytest.raises(RuntimeError, match="commit failed"):
        async with storage_effects(storage) as effects:  # type: ignore[arg-type]
            effects.created.append(new)
            effects.obsolete.append(old)
            await effects.wrap_commit(failing_commit)()

    assert storage.deleted == [new.key]
    assert effects.committed is False


async def test_a_failure_after_the_commit_keeps_the_new_objects_and_removes_the_old():
    """E.g. recording the idempotent result in Redis fails after the rows were committed: the
    change is durable, so the new file must stay and the replaced one goes."""
    storage = FakeStorage()
    new, old = _object(), _object()

    with pytest.raises(RuntimeError, match="redis down"):
        async with storage_effects(storage) as effects:  # type: ignore[arg-type]
            effects.created.append(new)
            effects.obsolete.append(old)
            await effects.wrap_commit(_commit_ok)()
            raise RuntimeError("redis down")

    assert storage.deleted == [old.key]


async def test_nothing_is_removed_before_the_block_ends():
    storage = FakeStorage()
    old = _object()

    async with storage_effects(storage) as effects:  # type: ignore[arg-type]
        effects.obsolete.append(old)
        await effects.wrap_commit(_commit_ok)()
        assert storage.deleted == []

    assert storage.deleted == [old.key]


async def test_a_removal_that_fails_is_logged_as_an_orphan_and_does_not_raise(logs):
    old, other = _object(), _object()
    storage = FakeStorage(fail_on={old.key})

    async with storage_effects(storage) as effects:  # type: ignore[arg-type]
        effects.obsolete.extend([old, other])
        await effects.wrap_commit(_commit_ok)()

    assert storage.deleted == [other.key]  # the failure did not stop the next removal
    [entry] = [e for e in logs.entries if e["event"] == "training.object_cleanup_failed"]
    assert entry["orphaned"] is True and entry["key"] == old.key and entry["bucket"] == "bucket"
    assert entry["module_id"] == str(old.module_id)


async def test_an_object_outside_its_modules_prefix_is_never_deleted(logs):
    storage = FakeStorage()
    module_id = uuid4()
    foreign = ModuleObject(module_id, "bucket", f"projects/{uuid4()}/renders/x/horizontal-low.mp4")
    neighbour = ModuleObject(module_id, "bucket", module_video_key(uuid4(), uuid4()))
    climbing = ModuleObject(module_id, "bucket", f"training/modules/{module_id}/../x.mp4")

    await remove_module_objects(
        storage,  # type: ignore[arg-type]
        [foreign, neighbour, climbing],
        reason="test",
    )

    assert storage.deleted == []
    assert [e["event"] for e in logs.entries].count("training.object_cleanup_skipped") == 3


def test_a_reorder_must_be_a_permutation_of_the_current_modules():
    a, b, c = uuid4(), uuid4(), uuid4()

    assert reorder_mismatch([a, b, c], [c, a, b]) is False
    assert reorder_mismatch([a, b, c], [a, b, c]) is False
    assert reorder_mismatch([a, b, c], [a, b]) is True  # one left out
    assert reorder_mismatch([a, b, c], [a, b, c, uuid4()]) is True  # a stranger added
    assert reorder_mismatch([a, b, c], [a, b, uuid4()]) is True  # one swapped for a stranger
    assert reorder_mismatch([a, b, c], [a, a, b]) is True  # a repeat in place of a module
    assert reorder_mismatch([a, b, c], [a, b, c, a]) is True  # every module, then one again
    assert reorder_mismatch([], [a]) is True
    assert reorder_mismatch([], []) is False

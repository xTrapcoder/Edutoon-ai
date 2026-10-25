"""The mutation runner's development-state comparison sees a change that leaves the counts alone.

The runner (``scripts/mutation_check_training.py``) proves the test suite left the development
buckets untouched by comparing a snapshot before and after. A comparison on object counts, or on
keys alone, would miss an object replaced by another, or overwritten in place. These tests pin that
the snapshot covers the whole listing - key, size, ETag and last-modified time - and that
the comparison reports any difference. They run against the session's scratch buckets only.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from edutoon.core.config import get_settings
from edutoon.providers.storage import get_storage_client
from tests.storage_isolation import PROTECTED_DEV_BUCKETS, SCRATCH_BUCKETS

RUNNER = Path(__file__).resolve().parents[1] / "scripts" / "mutation_check_training.py"


@pytest.fixture(scope="module")
def runner():
    spec = importlib.util.spec_from_file_location("mutation_check_training_under_test", RUNNER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BASE = [
    ("a.pdf", 10, '"e1"', "2026-10-14T10:00:00+00:00"),
    ("b.pdf", 20, '"e2"', "2026-10-14T10:00:01+00:00"),
]


@pytest.mark.parametrize(
    "changed",
    [
        [("a.pdf", 11, '"e1"', "2026-10-14T10:00:00+00:00"), BASE[1]],  # size
        [("a.pdf", 10, '"e9"', "2026-10-14T10:00:00+00:00"), BASE[1]],  # content (ETag)
        [("a.pdf", 10, '"e1"', "2026-10-14T10:00:05+00:00"), BASE[1]],  # last-modified only
        [("c.pdf", 10, '"e1"', "2026-10-14T10:00:00+00:00"), BASE[1]],  # a different key
        [BASE[0]],  # one fewer
    ],
)
def test_the_digest_changes_when_any_field_of_any_object_changes(runner, changed):
    assert runner.listing_digest(changed) != runner.listing_digest(BASE)


def test_the_digest_ignores_only_the_order_of_the_listing(runner):
    assert runner.listing_digest(list(reversed(BASE))) == runner.listing_digest(BASE)


def _state(runner, audit=5, **buckets):
    return runner.DevState(audit, buckets)


def test_a_difference_with_equal_counts_is_reported_and_unequal(runner):
    before = _state(runner, uploads=(2, runner.listing_digest(BASE)))
    replaced = [BASE[0], ("z.pdf", 20, '"e2"', "2026-10-14T10:00:01+00:00")]
    after = _state(runner, uploads=(2, runner.listing_digest(replaced)))

    assert before != after
    assert before.differences(after)
    assert "uploads" in before.differences(after)[0]


def test_identical_states_are_equal_with_no_differences(runner):
    one = _state(runner, uploads=(2, runner.listing_digest(BASE)))
    two = _state(runner, uploads=(2, runner.listing_digest(list(reversed(BASE)))))

    assert one == two
    assert one.differences(two) == []


def test_an_audit_log_change_is_reported(runner):
    before = _state(runner, audit=5, uploads=(0, runner.listing_digest([])))
    after = _state(runner, audit=6, uploads=(0, runner.listing_digest([])))

    assert before != after
    assert before.differences(after) == ["audit_logs 5 -> 6"]


async def test_the_live_snapshot_sees_a_replacement_and_an_overwrite_that_keep_the_count(runner):
    """End to end against the real listing (of this session's scratch buckets): swap one object for
    another, and overwrite one in place with the same size - both keep the object count."""
    settings = get_settings()
    assert SCRATCH_BUCKETS["BUCKET_UPLOADS"] == settings.BUCKET_UPLOADS
    bucket = settings.BUCKET_UPLOADS
    client = get_storage_client(
        endpoint_url=settings.STORAGE_ENDPOINT_URL,
        access_key_id=settings.STORAGE_ACCESS_KEY_ID,
        secret_access_key=settings.STORAGE_SECRET_ACCESS_KEY,
    )
    keys = ["snapshot-probe/one.bin", "snapshot-probe/two.bin"]
    try:
        await client.put_object(bucket=bucket, key=keys[0], body=b"aaaa", content_type="text/plain")
        baseline = await runner._dev_state()  # noqa: SLF001

        # replacement: same count, a different key
        await client.delete_object(bucket=bucket, key=keys[0])
        await client.put_object(bucket=bucket, key=keys[1], body=b"aaaa", content_type="text/plain")
        replaced = await runner._dev_state()  # noqa: SLF001
        assert replaced.buckets[bucket][0] == baseline.buckets[bucket][0]
        assert replaced != baseline

        # overwrite in place: same count, same key, same size, different bytes
        await client.put_object(bucket=bucket, key=keys[1], body=b"bbbb", content_type="text/plain")
        overwritten = await runner._dev_state()  # noqa: SLF001
        assert overwritten.buckets[bucket][0] == replaced.buckets[bucket][0]
        assert overwritten != replaced
        assert overwritten.differences(replaced)
    finally:
        for key in keys:
            await client.delete_object(bucket=bucket, key=key)


def test_the_runner_sweeps_only_new_scratch_buckets_never_a_protected_or_foreign_one(runner):
    protected = set(PROTECTED_DEV_BUCKETS)
    present = {
        *protected,
        "edutoon-test-aaaaaaaaaa-uploads",  # new debris: swept
        "edutoon-test-bbbbbbbbbb-uploads",  # existed before the mutant (another session): kept
        "other-bucket",  # not ours
        "edutoon-testing",  # a lookalike without the exact prefix
    }
    before = {"edutoon-test-bbbbbbbbbb-uploads"}

    assert runner.sweepable_scratch_buckets(present, before, protected) == [
        "edutoon-test-aaaaaaaaaa-uploads"
    ]
    # even if a protected bucket were reported as new, and even if it carried the prefix
    hostile = {sorted(protected)[0], "edutoon-test-x"}
    assert runner.sweepable_scratch_buckets(hostile, set(), {"edutoon-test-x"}) == []
    assert runner.sweepable_scratch_buckets(hostile, set(), set()) == ["edutoon-test-x"]

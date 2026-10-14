"""Object-storage isolation for the backend test suite.

**The rule: a test session never touches the development buckets.** The protected buckets are
``edutoon-uploads``, ``edutoon-segments``, ``edutoon-outputs`` and ``edutoon-assets`` (and whatever
``BUCKET_*`` the environment or ``apps/api/.env`` named when the session started). Three things make
that true, each independent of the others:

1. **Scratch buckets, named in the settings.** At import of ``tests/conftest.py`` - before the
   application reads its settings - ``BUCKET_UPLOADS`` / ``_SEGMENTS`` / ``_OUTPUTS`` /
   ``_ASSETS`` are *overwritten* (never ``setdefault``: a value in the shell or ``.env`` must not
   win) with names unique to this test process, ``edutoon-test-<run id>-<purpose>``. Every code
   path that asks the application's settings for a bucket - the upload route, the renderer, the
   audio service, the training service, a test calling ``get_settings()`` - therefore gets a
   scratch bucket. Dedicated buckets were chosen over a key prefix because the storage layer
   addresses ``(bucket, key)`` everywhere and has no notion of a namespace, so a prefix could not
   be enforced in one place, whereas a bucket is the unit the S3 API already isolates, creates and
   deletes atomically.
2. **A guard on every storage client.** ``Storage.__init__`` is wrapped so that every client
   built in the test process - by ``get_storage_client``, by a fixture, by the worker - refuses,
   *before any request is built or sent*, an operation whose ``Bucket`` (or ``CopySource``) is not
   one of this session's scratch buckets. A protected bucket, a hard-coded name, or a bucket
   nobody created fails fast with :class:`ProtectedBucketError`. This covers raw
   ``storage._client`` calls as well.
3. **Create at session start, delete at session end, only our own.** ``pytest_sessionstart``
   creates the four scratch buckets, ``pytest_sessionfinish`` empties and deletes them. Both
   refuse any name that is protected or lacks the scratch prefix, so setup and teardown cannot
   reach a development bucket even by a bug in this file. A session killed hard leaves its
   scratch buckets behind (named ``edutoon-test-*``, harmless); they are *not* swept by later
   sessions, because a session removes only what it created.

``tests/test_storage_isolation.py`` is the regression test for all of the above.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from uuid import uuid4

PROTECTED_DEV_BUCKETS: frozenset[str] = frozenset(
    {"edutoon-uploads", "edutoon-segments", "edutoon-outputs", "edutoon-assets"}
)
SCRATCH_PREFIX = "edutoon-test-"
BUCKET_SETTINGS = {
    "BUCKET_UPLOADS": "uploads",
    "BUCKET_SEGMENTS": "segments",
    "BUCKET_OUTPUTS": "outputs",
    "BUCKET_ASSETS": "assets",
}

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


class ProtectedBucketError(RuntimeError):
    """A test (or the code under test) addressed a bucket that is not this session's scratch one."""


def _bucket_values_named_outside_the_tests() -> set[str]:
    """Whatever ``BUCKET_*`` the shell or ``.env`` said before the tests overwrote it: these are the
    deployment's real buckets and are protected too, whatever they are called."""
    named: set[str] = set()
    for setting in BUCKET_SETTINGS:
        value = os.environ.get(setting)
        if value:
            named.add(value)
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() in BUCKET_SETTINGS and value.strip():
                named.add(value.strip().strip("\"'"))
    return {name for name in named if not name.startswith(SCRATCH_PREFIX)}


# What follows is computed once, at import, *before* the settings are overwritten.
PROTECTED: frozenset[str] = PROTECTED_DEV_BUCKETS | _bucket_values_named_outside_the_tests()
RUN_ID: str = uuid4().hex[:10]
SCRATCH_BUCKETS: dict[str, str] = {
    setting: f"{SCRATCH_PREFIX}{RUN_ID}-{purpose}" for setting, purpose in BUCKET_SETTINGS.items()
}
SCRATCH_NAMES: frozenset[str] = frozenset(SCRATCH_BUCKETS.values())

UPLOADS_BUCKET = SCRATCH_BUCKETS["BUCKET_UPLOADS"]
SEGMENTS_BUCKET = SCRATCH_BUCKETS["BUCKET_SEGMENTS"]
OUTPUTS_BUCKET = SCRATCH_BUCKETS["BUCKET_OUTPUTS"]
ASSETS_BUCKET = SCRATCH_BUCKETS["BUCKET_ASSETS"]


def point_settings_at_scratch_buckets() -> None:
    """Overwrite the four ``BUCKET_*`` settings in the environment. Call before the application
    reads its settings; overwrites unconditionally."""
    for setting, name in SCRATCH_BUCKETS.items():
        os.environ[setting] = name


def _require_scratch(name: str) -> None:
    if name in PROTECTED or not name.startswith(SCRATCH_PREFIX) or name not in SCRATCH_NAMES:
        raise ProtectedBucketError(
            f"refusing to act on bucket {name!r}: only this session's scratch buckets "
            f"({sorted(SCRATCH_NAMES)}) may be created or deleted by the test setup"
        )


# --- the guard ---------------------------------------------------------------------------------


def _buckets_in(params: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    bucket = params.get("Bucket")
    if isinstance(bucket, str):
        found.add(bucket)
    source = params.get("CopySource")
    if isinstance(source, dict) and isinstance(source.get("Bucket"), str):
        found.add(source["Bucket"])
    elif isinstance(source, str):
        found.add(source.lstrip("/").split("/", 1)[0])
    return found


def guard(params: dict[str, Any], **_: Any) -> None:
    """botocore ``before-parameter-build`` handler: only scratch buckets may be addressed."""
    for bucket in _buckets_in(params):
        if bucket not in SCRATCH_NAMES:
            kind = "a PROTECTED development bucket" if bucket in PROTECTED else "an unknown bucket"
            raise ProtectedBucketError(
                f"the test session tried to use {bucket!r}, {kind}; tests may only use their "
                f"scratch buckets {sorted(SCRATCH_NAMES)} (see tests/storage_isolation.py)"
            )


_installed = False


def install_guard() -> None:
    """Wrap ``Storage.__init__`` so every storage client built in this process carries the guard."""
    global _installed
    if _installed:
        return
    from edutoon.providers.storage import Storage

    original_init = Storage.__init__

    def guarded_init(self: Storage, client: Any) -> None:
        original_init(self, client)
        # A stand-in with no botocore event system (``Storage(client=None)`` in tests that never
        # reach storage) has no request to guard; a real or mocked boto client does.
        events = getattr(getattr(client, "meta", None), "events", None)
        if events is not None:
            events.register("before-parameter-build.s3", guard)

    Storage.__init__ = guarded_init  # type: ignore[method-assign]
    _installed = True


# --- the scratch buckets' lifecycle -------------------------------------------------------------


def _admin_client() -> Any:
    from edutoon.core.config import get_settings
    from edutoon.providers.storage import get_storage_client

    settings = get_settings()
    return get_storage_client(
        endpoint_url=settings.STORAGE_ENDPOINT_URL,
        access_key_id=settings.STORAGE_ACCESS_KEY_ID,
        secret_access_key=settings.STORAGE_SECRET_ACCESS_KEY,
    )._client


def create_scratch_buckets() -> None:
    client = _admin_client()
    for name in sorted(SCRATCH_NAMES):
        _require_scratch(name)
        client.create_bucket(Bucket=name)


def delete_scratch_buckets() -> list[str]:
    """Empty and delete this session's scratch buckets. Returns the names that could not be removed
    (reported, never raised: a failed clean-up must not turn a passing run into a failing one)."""
    failed: list[str] = []
    client = _admin_client()
    for name in sorted(SCRATCH_NAMES):
        try:
            _require_scratch(name)
            paginator = client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=name):
                keys = [{"Key": item["Key"]} for item in page.get("Contents", [])]
                if keys:
                    client.delete_objects(Bucket=name, Delete={"Objects": keys, "Quiet": True})
            client.delete_bucket(Bucket=name)
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{name} ({type(exc).__name__}: {str(exc)[:120]})")
    return failed

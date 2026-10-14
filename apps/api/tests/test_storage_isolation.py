"""The test suite cannot reach the development object-storage buckets.

Regression tests for ``tests/storage_isolation.py``. They fail if a test (or the code under test)
can obtain a storage client that is able to address ``edutoon-uploads``, ``edutoon-segments``,
``edutoon-outputs`` or ``edutoon-assets`` (or whatever the deployment's ``.env`` names), if the
settings stop pointing at scratch buckets, if a test hard-codes a development bucket, or if the
session's set-up and tear-down could touch anything but its own scratch buckets.

Nothing here sends a request to a protected bucket: every attempt is refused before a request is
built, and each client under test also carries a ``before-send`` tripwire that would fail the test
loudly (instead of letting the request through) if the guard ever stopped working.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import boto3
import pytest

from edutoon.core.config import get_settings
from edutoon.providers.storage import Storage, get_storage_client
from tests import storage_isolation
from tests.storage_isolation import (
    PROTECTED,
    PROTECTED_DEV_BUCKETS,
    SCRATCH_BUCKETS,
    SCRATCH_NAMES,
    ProtectedBucketError,
)

TESTS = Path(__file__).parent
SCRATCH_BUCKET_UPLOADS = SCRATCH_BUCKETS["BUCKET_UPLOADS"]
PROTECTED_SORTED = sorted(PROTECTED)


class RequestReachedTheWireError(AssertionError):
    """A request was about to be sent: the guard did not stop it."""


def _tripwire(request, **_: object) -> None:
    """Fails the test if a request is about to be *sent* that addresses a bucket that is not one of
    the scratch ones - by its URL (path-style: ``/<bucket>/<key>``) or by a copy-source header.
    Requests to the scratch buckets pass: only those may legitimately go out."""
    from urllib.parse import unquote, urlsplit

    addressed = {unquote(urlsplit(request.url).path).lstrip("/").split("/", 1)[0]}
    source = request.headers.get("x-amz-copy-source")
    if source:
        addressed.add(
            unquote(source if isinstance(source, str) else source.decode())
            .lstrip("/")
            .split("/", 1)[0]
        )
    addressed.discard("")
    escaped = addressed - SCRATCH_NAMES
    if escaped:
        raise RequestReachedTheWireError(f"a request to {sorted(escaped)} was about to be sent")


@pytest.fixture
def storage() -> Storage:
    settings = get_settings()
    client = get_storage_client(
        endpoint_url=settings.STORAGE_ENDPOINT_URL,
        access_key_id=settings.STORAGE_ACCESS_KEY_ID,
        secret_access_key=settings.STORAGE_SECRET_ACCESS_KEY,
    )
    client._client.meta.events.register("before-send.s3", _tripwire)  # noqa: SLF001
    return client


# --- the settings point at scratch buckets -------------------------------------------------------


def test_the_settings_name_this_sessions_scratch_buckets_not_the_development_ones():
    settings = get_settings()
    configured = {
        "BUCKET_UPLOADS": settings.BUCKET_UPLOADS,
        "BUCKET_SEGMENTS": settings.BUCKET_SEGMENTS,
        "BUCKET_OUTPUTS": settings.BUCKET_OUTPUTS,
        "BUCKET_ASSETS": settings.BUCKET_ASSETS,
    }

    assert configured == SCRATCH_BUCKETS
    assert len(set(configured.values())) == 4
    for name in configured.values():
        assert name.startswith(storage_isolation.SCRATCH_PREFIX)
        assert name not in PROTECTED
    assert PROTECTED_DEV_BUCKETS <= PROTECTED
    assert not PROTECTED & SCRATCH_NAMES


def test_the_scratch_buckets_exist_and_work(storage):
    for name in sorted(SCRATCH_NAMES):
        assert storage._client.head_bucket(Bucket=name)  # noqa: SLF001 - proves it was created


# --- every client refuses anything but the scratch buckets ---------------------------------------


@pytest.mark.parametrize("bucket", PROTECTED_SORTED)
@pytest.mark.parametrize(
    "operation",
    [
        "ping",
        "exists",
        "get",
        "size",
        "open",
        "put",
        "delete",
        "download",
        "upload",
        "copy_from",
        "copy_to",
        "raw_list",
        "raw_put",
        "raw_delete",
        "raw_delete_many",
    ],
)
async def test_no_operation_can_address_a_protected_bucket(storage, tmp_path, bucket, operation):
    scratch = SCRATCH_BUCKETS["BUCKET_UPLOADS"]
    source = tmp_path / "file.bin"
    source.write_bytes(b"x")
    calls = {
        "ping": lambda: storage.ping(bucket=bucket),
        "exists": lambda: storage.object_exists(bucket=bucket, key="k"),
        "get": lambda: storage.get_object(bucket=bucket, key="k"),
        "size": lambda: storage.object_size(bucket=bucket, key="k"),
        "open": lambda: storage.open_object(bucket=bucket, key="k"),
        "put": lambda: storage.put_object(
            bucket=bucket, key="k", body=b"x", content_type="text/plain"
        ),
        "delete": lambda: storage.delete_object(bucket=bucket, key="k"),
        "download": lambda: storage.download_file(
            bucket=bucket, key="k", dest=tmp_path / "out.bin"
        ),
        "upload": lambda: storage.upload_file(
            bucket=bucket, key="k", path=source, content_type="text/plain"
        ),
        "copy_from": lambda: storage.copy_object(
            source_bucket=bucket,
            source_key="k",
            dest_bucket=scratch,
            dest_key="k2",
            content_type="text/plain",
        ),
        "copy_to": lambda: storage.copy_object(
            source_bucket=scratch,
            source_key="k",
            dest_bucket=bucket,
            dest_key="k2",
            content_type="text/plain",
        ),
        "raw_list": lambda: _in_thread(lambda: storage._client.list_objects_v2(Bucket=bucket)),
        "raw_put": lambda: _in_thread(
            lambda: storage._client.put_object(Bucket=bucket, Key="k", Body=b"x")
        ),
        "raw_delete": lambda: _in_thread(
            lambda: storage._client.delete_object(Bucket=bucket, Key="k")
        ),
        "raw_delete_many": lambda: _in_thread(
            lambda: storage._client.delete_objects(
                Bucket=bucket, Delete={"Objects": [{"Key": "k"}]}
            )
        ),
    }

    if operation == "copy_to":
        # The managed copy first reads its source, so the (scratch) source has to exist for the
        # attempt to get as far as naming the protected destination.
        await storage.put_object(bucket=scratch, key="k", body=b"x", content_type="text/plain")
    try:
        with pytest.raises(ProtectedBucketError, match=re.escape(bucket)):
            await calls[operation]()
    finally:
        if operation == "copy_to":
            await storage.delete_object(bucket=scratch, key="k")


async def _in_thread(function):
    import asyncio

    return await asyncio.to_thread(function)


@pytest.mark.parametrize(
    "bucket", ["some-other-bucket", "edutoon", "edutoon-test-deadbeef00-uploads"]
)
async def test_a_bucket_that_is_not_this_sessions_scratch_bucket_is_refused_too(storage, bucket):
    """Not only the protected names: an unknown bucket, and another session's scratch bucket, are
    refused - a test may touch exactly the four buckets created for it."""
    with pytest.raises(ProtectedBucketError):
        await storage.object_exists(bucket=bucket, key="k")


async def test_the_scratch_buckets_themselves_are_usable_and_clean_up_after_themselves(storage):
    bucket = SCRATCH_BUCKETS["BUCKET_OUTPUTS"]
    key = "isolation-probe/probe.bin"
    await storage.put_object(bucket=bucket, key=key, body=b"probe", content_type="text/plain")
    try:
        assert await storage.object_exists(bucket=bucket, key=key)
        assert (await storage.get_object(bucket=bucket, key=key)).body == b"probe"
    finally:
        await storage.delete_object(bucket=bucket, key=key)
    assert not await storage.object_exists(bucket=bucket, key=key)


def test_a_storage_built_around_any_boto_client_is_guarded_too():
    """Not just ``get_storage_client``: wrapping a hand-made boto3 client in ``Storage`` (as a
    fixture or script might) installs the guard as well, because it lives in
    ``Storage.__init__``."""
    settings = get_settings()
    raw = boto3.client(
        "s3",
        endpoint_url=settings.STORAGE_ENDPOINT_URL,
        aws_access_key_id=settings.STORAGE_ACCESS_KEY_ID,
        aws_secret_access_key=settings.STORAGE_SECRET_ACCESS_KEY,
    )
    raw.meta.events.register("before-send.s3", _tripwire)
    Storage(raw)

    with pytest.raises(ProtectedBucketError):
        raw.list_objects_v2(Bucket="edutoon-uploads")


def test_the_application_fixtures_hand_out_guarded_clients(role_app):
    with pytest.raises(ProtectedBucketError):
        role_app.state.storage._client.list_objects_v2(Bucket="edutoon-outputs")  # noqa: SLF001


def test_the_guard_reads_both_forms_of_a_copy_source():
    assert storage_isolation._buckets_in(
        {"Bucket": "a", "CopySource": {"Bucket": "b", "Key": "k"}}
    ) == {"a", "b"}  # noqa: SLF001
    assert storage_isolation._buckets_in({"CopySource": "/c/some/key"}) == {"c"}  # noqa: SLF001
    assert storage_isolation._buckets_in({"CopySource": "d/key"}) == {"d"}  # noqa: SLF001
    assert storage_isolation._buckets_in({}) == set()  # noqa: SLF001


# --- no test hard-codes a development bucket ---------------------------------------------------


def test_no_test_source_hard_codes_a_development_bucket_name():
    pattern = re.compile("|".join(re.escape(name) for name in PROTECTED_SORTED))
    allowed = {"storage_isolation.py", "test_storage_isolation.py"}
    offenders = [
        f"{path.name}:{number}: {line.strip()[:80]}"
        for path in sorted(TESTS.glob("*.py"))
        if path.name not in allowed
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if pattern.search(line)
    ]
    assert offenders == []


# --- set-up and tear-down only ever touch this session's own scratch buckets --------------------


@pytest.mark.parametrize("name", PROTECTED_SORTED)
def test_set_up_and_tear_down_refuse_a_protected_bucket(name):
    with pytest.raises(ProtectedBucketError):
        storage_isolation._require_scratch(name)  # noqa: SLF001


@pytest.mark.parametrize("name", ["edutoon-test-deadbeef00-uploads", "other", "edutoon-test-"])
def test_set_up_and_tear_down_refuse_anything_that_is_not_this_sessions_bucket(name):
    with pytest.raises(ProtectedBucketError):
        storage_isolation._require_scratch(name)  # noqa: SLF001


def test_set_up_and_tear_down_accept_this_sessions_own_buckets():
    for name in SCRATCH_NAMES:
        storage_isolation._require_scratch(name)  # noqa: SLF001


def _scratch_bucket_names_on_the_server() -> set[str]:
    settings = get_settings()
    client = boto3.client(
        "s3",
        endpoint_url=settings.STORAGE_ENDPOINT_URL,
        aws_access_key_id=settings.STORAGE_ACCESS_KEY_ID,
        aws_secret_access_key=settings.STORAGE_SECRET_ACCESS_KEY,
    )
    # ``list_buckets`` names no bucket, so it is not something the guard (or a protected bucket)
    # is involved in; only the *names* are read.
    return {
        b["Name"]
        for b in client.list_buckets()["Buckets"]
        if b["Name"].startswith(storage_isolation.SCRATCH_PREFIX)
    }


def test_a_whole_test_session_creates_its_own_scratch_buckets_and_removes_exactly_those():
    """Run a real, separate pytest session in a child process that reports its run id: afterwards
    none of *its* scratch buckets remain, and this session's own four are still there and untouched.
    (Only the child's buckets are looked for, so another test session running at the same moment -
    its buckets coming and going - cannot make this fail.)"""
    assert _scratch_bucket_names_on_the_server() >= SCRATCH_NAMES
    child = (
        "import sys, pytest; from tests import storage_isolation as iso; "
        "print('CHILD_RUN_ID', iso.RUN_ID, flush=True); "
        "sys.exit(pytest.main(['tests/test_health.py', '-q', '-p', 'no:cacheprovider']))"
    )

    result = subprocess.run(
        [sys.executable, "-c", child],
        cwd=TESTS.parent,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "scratch storage buckets left behind" not in output
    match = re.search(r"CHILD_RUN_ID (\w+)", result.stdout)
    assert match, output
    child_id = match.group(1)
    assert child_id not in "".join(SCRATCH_NAMES)  # a different session from this one
    remaining = _scratch_bucket_names_on_the_server()
    assert not {name for name in remaining if child_id in name}
    assert remaining >= SCRATCH_NAMES


# --- a missing scratch-bucket setting cannot quietly become a development bucket ---------------

_WITHOUT_THE_SCRATCH_SETTINGS = """
import sys
from tests import storage_isolation as iso
from edutoon.core.config import Settings
from edutoon.providers.storage import Storage, get_storage_client

iso.install_guard()
mode = sys.argv[1]


def _nothing_may_be_sent(request, **_):
    raise RuntimeError("A REQUEST REACHED THE WIRE: " + request.url)


if mode == "env_file_only":
    # The shell names no BUCKET_*: apps/api/.env (the development buckets) is what is read.
    settings = Settings()
    names = [
        settings.BUCKET_UPLOADS,
        settings.BUCKET_SEGMENTS,
        settings.BUCKET_OUTPUTS,
        settings.BUCKET_ASSETS,
    ]
    assert not set(names) & iso.SCRATCH_NAMES, names
    client = get_storage_client(
        endpoint_url=settings.STORAGE_ENDPOINT_URL,
        access_key_id=settings.STORAGE_ACCESS_KEY_ID,
        secret_access_key=settings.STORAGE_SECRET_ACCESS_KEY,
    )
    client._client.meta.events.register("before-send.s3", _nothing_may_be_sent)
    refused = 0
    for name in names:
        try:
            client._client.list_objects_v2(Bucket=name)
        except iso.ProtectedBucketError:
            refused += 1
    print("REFUSED", refused, "OF", len(names))
else:
    # Neither the shell nor a .env names them: the settings do not load (no built-in default).
    try:
        Settings(_env_file=None)
    except Exception as exc:
        print("SETTINGS_REFUSED", type(exc).__name__)
"""


def _run_without_bucket_settings(mode: str) -> str:
    import os

    env = {key: value for key, value in os.environ.items() if not key.startswith("BUCKET_")}
    result = subprocess.run(
        [sys.executable, "-c", _WITHOUT_THE_SCRATCH_SETTINGS, mode],
        cwd=TESTS.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_missing_scratch_bucket_settings_never_fall_back_to_a_usable_development_bucket():
    """If the overwrite in ``conftest`` were lost, the settings would come from ``.env``, i.e. the
    development buckets. The guard must then refuse every one of them (no request is built), and
    with no ``.env`` either the settings must fail to load rather than default to a real bucket."""
    from_env_file = _run_without_bucket_settings("env_file_only")
    assert f"REFUSED {len(storage_isolation.BUCKET_SETTINGS)} OF" in from_env_file
    nothing = _run_without_bucket_settings("no_env_file")
    assert "SETTINGS_REFUSED" in nothing


@pytest.mark.parametrize(
    "lookalike",
    [
        SCRATCH_BUCKET_UPLOADS + "x",
        SCRATCH_BUCKET_UPLOADS[:-1],
        SCRATCH_BUCKET_UPLOADS.upper(),
        "x" + SCRATCH_BUCKET_UPLOADS,
        SCRATCH_BUCKET_UPLOADS + "-copy",
        "edutoon-test-",
        "edutoon-uploads-",
        "edutoon-uploads2",
    ],
)
async def test_a_lookalike_of_a_scratch_or_protected_name_is_refused_by_exact_comparison(
    storage, lookalike
):
    """Membership in the exact set of four names, not a prefix test: a name that merely starts like
    a scratch bucket (or like a protected one) is refused by both the guard and the set-up check."""
    with pytest.raises(ProtectedBucketError):
        await storage.object_exists(bucket=lookalike, key="k")
    with pytest.raises(ProtectedBucketError):
        storage_isolation._require_scratch(lookalike)  # noqa: SLF001


def test_a_storage_stand_in_without_a_boto_client_is_constructible_and_a_real_one_stays_guarded():
    """``Storage(client=None)`` is what tests that never reach storage build; the guard must not
    break them, and must not weaken for a real client (the other tests in this file prove that)."""
    assert Storage(client=None) is not None

    raw = boto3.client(
        "s3",
        endpoint_url=get_settings().STORAGE_ENDPOINT_URL,
        aws_access_key_id="x",
        aws_secret_access_key="y",
    )
    Storage(raw)
    with pytest.raises(ProtectedBucketError):
        raw.list_objects_v2(Bucket="edutoon-uploads")

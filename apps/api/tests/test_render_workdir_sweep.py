"""Scratch directories a killed render leaves behind are swept up when a worker starts - and
nothing else is ever touched.

A render's scratch (``edutoon-render-<job id>``) holds hundreds of megabytes of chunks and audio and
is removed when the render ends, unless its process is killed. The sweep deletes, so every test
here is about what it must leave alone: a directory that is still being written to, one with the
wrong name, a file, a symbolic link, and anything outside the work directory.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from uuid import uuid4

import pytest

from edutoon.core.config import get_settings
from edutoon.services import render as render_service

HOUR = 3600


@pytest.fixture
def work_dir(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("RENDER_WORK_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def make_dir(root: Path, name: str, *, age_hours: float, with_files: bool = True) -> Path:
    directory = root / name
    directory.mkdir()
    if with_files:
        (directory / "chunk.mp4").write_bytes(b"x" * 1000)
        (directory / "nested").mkdir()
        (directory / "nested" / "narration.wav").write_bytes(b"y" * 1000)
    then = time.time() - age_hours * HOUR
    for path in [*directory.rglob("*"), directory]:
        os.utime(path, (then, then))
    return directory


def render_name() -> str:
    return f"edutoon-render-{uuid4()}"


def test_an_old_abandoned_render_directory_is_removed(work_dir) -> None:
    old = make_dir(work_dir, render_name(), age_hours=12)

    removed = render_service.sweep_abandoned_work_dirs()

    assert removed == [old.name] and not old.exists()


def test_a_directory_still_being_written_to_is_left_alone(work_dir) -> None:
    live = make_dir(work_dir, render_name(), age_hours=12)
    fresh = live / "nested" / "latest-chunk.mp4"
    fresh.write_bytes(b"z")  # a live render wrote something just now, deep inside

    assert render_service.sweep_abandoned_work_dirs() == []
    assert live.exists()


def test_a_recently_started_render_is_left_alone(work_dir) -> None:
    recent = make_dir(work_dir, render_name(), age_hours=0.5)

    assert render_service.sweep_abandoned_work_dirs() == []
    assert recent.exists()


def test_the_quiet_period_is_at_least_twice_the_stale_threshold(work_dir, monkeypatch) -> None:
    monkeypatch.setenv("RENDER_STALE_AFTER_SECONDS", str(5 * HOUR))  # so 10 h of quiet
    get_settings.cache_clear()
    nine_hours = make_dir(work_dir, render_name(), age_hours=9)
    eleven_hours = make_dir(work_dir, render_name(), age_hours=11)

    assert render_service.sweep_abandoned_work_dirs() == [eleven_hours.name]
    assert nine_hours.exists()


def test_only_the_exact_name_is_matched(work_dir) -> None:
    keep = [
        make_dir(work_dir, "important-data", age_hours=99),
        make_dir(work_dir, "edutoon-render-", age_hours=99),
        make_dir(work_dir, "edutoon-render-not-a-uuid", age_hours=99),
        make_dir(work_dir, f"edutoon-render-{uuid4()}-backup", age_hours=99),
        make_dir(work_dir, f"my-edutoon-render-{uuid4()}", age_hours=99),
        make_dir(work_dir, "edutoon-tts-abc123", age_hours=99),
    ]

    assert render_service.sweep_abandoned_work_dirs() == []
    assert all(path.exists() for path in keep)


def test_a_file_with_the_right_name_is_not_removed(work_dir) -> None:
    file = work_dir / render_name()
    file.write_bytes(b"not a directory")
    os.utime(file, (0, 0))

    assert render_service.sweep_abandoned_work_dirs() == []
    assert file.exists()


def test_a_symbolic_link_is_never_followed_or_removed(work_dir, tmp_path_factory) -> None:
    outside = tmp_path_factory.mktemp("precious")
    (outside / "keep.txt").write_text("do not delete")
    link = work_dir / render_name()
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("this system does not allow creating symbolic links")

    assert render_service.sweep_abandoned_work_dirs() == []
    assert (outside / "keep.txt").read_text() == "do not delete"


def test_several_abandoned_directories_are_all_removed_and_only_they(work_dir) -> None:
    a = make_dir(work_dir, render_name(), age_hours=20)
    b = make_dir(work_dir, render_name(), age_hours=40, with_files=False)
    live = make_dir(work_dir, render_name(), age_hours=0.1)
    other = make_dir(work_dir, "unrelated", age_hours=50)

    removed = render_service.sweep_abandoned_work_dirs()

    assert sorted(removed) == sorted([a.name, b.name])
    assert live.exists() and other.exists() and not a.exists() and not b.exists()


def test_nothing_to_sweep_is_not_an_error(work_dir) -> None:
    assert render_service.sweep_abandoned_work_dirs() == []


def test_it_never_raises_even_if_the_work_dir_cannot_be_read(work_dir, monkeypatch) -> None:
    def boom() -> Path:
        raise OSError("the disk went away")

    monkeypatch.setattr(render_service, "_work_dir_root", boom)

    assert render_service.sweep_abandoned_work_dirs() == []


def test_the_worker_sweeps_once_as_it_starts() -> None:
    source = Path(render_service.__file__).parents[1].joinpath("worker", "__main__.py").read_text()
    assert "sweep_abandoned_work_dirs" in source

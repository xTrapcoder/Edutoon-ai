"""``domain/training_storage.py``: a module's video key, and why project clean-up misses it."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from edutoon.domain.render import output_storage_key
from edutoon.domain.source_storage import belongs_to_project, project_storage_prefix
from edutoon.domain.training_storage import (
    MODULES_STORAGE_ROOT,
    belongs_to_module,
    module_storage_prefix,
    module_video_key,
)


def test_a_module_video_key_is_inside_its_modules_own_prefix():
    module, publication = uuid4(), uuid4()

    key = module_video_key(module, publication)

    assert key == f"training/modules/{module}/{publication}.mp4"
    assert key.startswith(module_storage_prefix(module))
    assert belongs_to_module(key, module)


def test_each_publication_gets_its_own_key_so_a_replacement_never_overwrites_the_old_video():
    module = uuid4()

    assert module_video_key(module, uuid4()) != module_video_key(module, uuid4())


def test_a_module_key_belongs_to_no_project_so_project_cleanup_cannot_touch_it():
    module, publication = uuid4(), uuid4()
    key = module_video_key(module, publication)

    for _ in range(20):
        assert not belongs_to_project(key, uuid4())
    assert not key.startswith("projects/")


def test_a_project_render_key_belongs_to_no_module_so_module_cleanup_cannot_touch_it():
    project, script = uuid4(), uuid4()
    render_key = output_storage_key(project, script, "horizontal", "high")

    assert render_key.startswith(project_storage_prefix(project))
    assert not belongs_to_module(render_key, uuid4())
    assert not render_key.startswith(MODULES_STORAGE_ROOT)


def test_a_module_does_not_own_another_modules_objects():
    mine, theirs = uuid4(), uuid4()

    assert not belongs_to_module(module_video_key(theirs, uuid4()), mine)


@pytest.mark.parametrize(
    "suffix",
    ["", "../other/x.mp4", "a/../../x.mp4", "..", "/.."],
)
def test_the_prefix_itself_and_keys_that_climb_out_of_it_are_refused(suffix):
    module = uuid4()

    assert not belongs_to_module(module_storage_prefix(module) + suffix, module)


def test_a_prefix_lookalike_is_not_the_prefix():
    module = UUID(int=1)

    assert not belongs_to_module(f"training/modules/{module}x/video.mp4", module)
    assert not belongs_to_module(f"training/modules/{module}", module)

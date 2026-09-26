"""The generation-settings vocabulary, limits and defaults (pure - no database).

The values themselves are pinned as literals on purpose: they are a product and
API contract (and the constraints in migration 0010 repeat them), so changing one
must be a deliberate edit that fails here first, not a side effect of a refactor.
``test_migration_0010_schema.py`` holds the database to the same constants.
"""

from __future__ import annotations

from edutoon.domain import generation_settings as gs


def test_orientations_are_horizontal_and_vertical() -> None:
    assert {"horizontal", "vertical"} == gs.ORIENTATIONS


def test_en_in_is_a_supported_language() -> None:
    assert "en-IN" in gs.SUPPORTED_LANGUAGES


def test_en_gb_stays_supported_because_every_existing_project_holds_it() -> None:
    assert "en-GB" in gs.SUPPORTED_LANGUAGES


def test_only_the_named_languages_are_supported() -> None:
    assert {"en-GB", "en-IN"} == gs.SUPPORTED_LANGUAGES


def test_the_minimum_target_duration_is_one_minute() -> None:
    assert gs.MIN_TARGET_DURATION_MS == 60_000


def test_the_maximum_target_duration_is_sixty_minutes() -> None:
    assert gs.MAX_TARGET_DURATION_MS == 3_600_000 == 60 * 60 * 1000


def test_the_minimum_is_positive_so_zero_and_negative_durations_are_out_of_range() -> None:
    assert 0 < gs.MIN_TARGET_DURATION_MS < gs.MAX_TARGET_DURATION_MS


def test_the_defaults_are_horizontal_en_in_and_five_minutes() -> None:
    assert gs.DEFAULT_ORIENTATION == "horizontal"
    assert gs.DEFAULT_LANGUAGE == "en-IN"
    assert gs.DEFAULT_TARGET_DURATION_MS == 300_000


def test_every_default_is_itself_valid() -> None:
    assert gs.DEFAULT_ORIENTATION in gs.ORIENTATIONS
    assert gs.DEFAULT_LANGUAGE in gs.SUPPORTED_LANGUAGES
    assert gs.MIN_TARGET_DURATION_MS <= gs.DEFAULT_TARGET_DURATION_MS <= gs.MAX_TARGET_DURATION_MS

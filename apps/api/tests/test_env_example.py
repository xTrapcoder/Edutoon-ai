"""``.env.example`` is the documented contract for configuration. These tests keep
it from drifting away from ``core.config.Settings``: a setting added or renamed in
code, a default changed on one side only, or a real secret pasted into the file.

They read the file and the ``Settings`` class only - no environment, no network.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from pydantic_core import PydanticUndefined

from edutoon.core.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[3] / ".env.example"

# The only values the two provider keys may have in the example file.
KEY_PLACEHOLDERS = {
    "OPENAI_API_KEY": "sk-your-openai-api-key",
    "ANTHROPIC_API_KEY": "sk-ant-your-anthropic-api-key",
}


def _example() -> dict[str, str]:
    """The uncommented ``NAME=value`` lines of the file."""
    values: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([A-Z][A-Z0-9_]*)=(.*)", line.strip())
        if match:
            values[match[1]] = match[2]
    return values


def _defaults() -> dict[str, Any]:
    """Every optional setting and its default."""
    return {
        name: field.default
        for name, field in Settings.model_fields.items()
        if field.default is not PydanticUndefined
    }


def test_the_example_file_is_found_and_not_empty() -> None:
    assert ENV_EXAMPLE.is_file()
    assert len(_example()) >= 20


def test_every_setting_appears_in_the_example_file() -> None:
    missing = sorted(set(Settings.model_fields) - set(_example()))
    assert missing == [], f"add these to .env.example: {missing}"


def test_the_example_file_sets_nothing_the_application_does_not_read() -> None:
    unknown = sorted(set(_example()) - set(Settings.model_fields))
    assert unknown == [], f"not a setting (a typo, or removed from Settings?): {unknown}"


@pytest.mark.parametrize("name", sorted(_defaults()))
def test_an_optional_setting_shows_its_real_default(name: str) -> None:
    assert _example()[name] == str(_defaults()[name])


def test_the_example_file_is_itself_a_valid_configuration() -> None:
    settings = Settings(_env_file=None, **_example())  # type: ignore[arg-type]
    assert settings.ENVIRONMENT == "development"


@pytest.mark.parametrize("name", sorted(KEY_PLACEHOLDERS))
def test_a_provider_key_in_the_example_file_is_only_ever_a_placeholder(name: str) -> None:
    assert _example()[name] == KEY_PLACEHOLDERS[name]

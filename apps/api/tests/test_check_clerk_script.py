"""``scripts/check_clerk.py`` - the helpers behind "are the API and the web app on the same Clerk
instance?". Pure parsing only: nothing here reads the real ``.env`` files or touches the network.
"""

from __future__ import annotations

import base64
import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_clerk.py"
_spec = importlib.util.spec_from_file_location("check_clerk", _SCRIPT)
assert _spec is not None and _spec.loader is not None
check_clerk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_clerk)


def _publishable(host: str, mode: str = "test") -> str:
    return f"pk_{mode}_" + base64.b64encode(f"{host}$".encode()).decode().rstrip("=")


def test_the_frontend_host_is_read_out_of_a_publishable_key() -> None:
    mode, host = check_clerk.frontend_host_from_publishable_key(
        _publishable("well-known-cat-12.clerk.accounts.dev")
    )

    assert (mode, host) == ("test", "well-known-cat-12.clerk.accounts.dev")


def test_live_and_test_keys_are_told_apart() -> None:
    mode, _ = check_clerk.frontend_host_from_publishable_key(
        _publishable("clerk.example.com", "live")
    )

    assert mode == "live"


@pytest.mark.parametrize(
    "key", ["", "not-a-key", "sk_test_abc", "pk_dev_abc", "pk_test_!!!", "pk_test_" + "A" * 3]
)
def test_a_malformed_publishable_key_yields_no_host(key: str) -> None:
    assert check_clerk.frontend_host_from_publishable_key(key)[1] is None


@pytest.mark.parametrize(
    "value",
    [
        "",
        "https://your-app-name-12.clerk.accounts.dev",
        "sk-your-openai-api-key",
        "https://placeholder.invalid",
    ],
)
def test_placeholders_are_recognised(value: str) -> None:
    assert check_clerk.looks_like_placeholder(value) is True


def test_a_real_looking_issuer_is_not_a_placeholder() -> None:
    assert (
        check_clerk.looks_like_placeholder("https://well-known-cat-12.clerk.accounts.dev") is False
    )


def test_env_files_are_parsed_without_evaluating_anything(tmp_path: Path) -> None:
    path = tmp_path / ".env.local"
    path.write_text("# a comment\nA=1\nB = 'two'\n\nnot a pair\nC=\n", encoding="utf-8")

    assert check_clerk.read_env_file(path) == {"A": "1", "B": "two", "C": ""}
    assert check_clerk.read_env_file(tmp_path / "missing") == {}

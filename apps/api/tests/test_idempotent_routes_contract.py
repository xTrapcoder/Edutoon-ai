"""Every mutating route keeps the two guarantees the older routers were hardened for.

The per-router tests (``test_*_api.py``) prove the behaviour of the routes that
exist. This file is the net for the ones that do not exist yet: a new POST / PATCH /
PUT / DELETE route that forgets either guarantee fails here, by name, instead of
shipping.

- **The key is bounded.** Every mutating operation in the app's OpenAPI document
  requires an ``Idempotency-Key`` header of 1 to 255 characters (rule 8). An empty
  key would make all of a caller's requests to a route share one dedupe record, and
  an oversized one reaches the ``jobs`` unique index, which Postgres refuses.
- **The write is durable before it is cached.** Every mutating route function goes
  through ``run_with_idempotency`` and passes ``commit=``: the request-scoped session
  commits only after the response is sent, so without it a failed commit leaves a
  cached success for a write that never happened.

Both read the app's own definition (its OpenAPI document and the routers' source);
nothing here touches a database.
"""

from __future__ import annotations

import ast
import functools
from pathlib import Path
from typing import Any

import pytest

from edutoon.api.dependencies import IDEMPOTENCY_KEY_MAX_CHARS
from edutoon.main import create_app

ROUTERS = Path(__file__).resolve().parents[1] / "src" / "edutoon" / "api" / "routers"
_MUTATING_HTTP_METHODS = {"post", "put", "patch", "delete"}


@functools.cache
def _openapi() -> dict[str, Any]:
    return create_app().openapi()


def _mutating_operations() -> list[tuple[str, str]]:
    return sorted(
        (method.upper(), path)
        for path, operations in _openapi()["paths"].items()
        for method in operations
        if method in _MUTATING_HTTP_METHODS
    )


def _mutating_route_functions() -> list[tuple[str, ast.AsyncFunctionDef]]:
    """``(module name, function)`` for every function a router decorates with a
    mutating HTTP method.
    """
    found: list[tuple[str, ast.AsyncFunctionDef]] = []
    for path in sorted(ROUTERS.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.AsyncFunctionDef) and any(
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr in _MUTATING_HTTP_METHODS
                for decorator in node.decorator_list
            ):
                found.append((path.stem, node))
    return found


def _idempotency_calls(function: ast.AsyncFunctionDef) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Attribute) and node.func.attr == "run_with_idempotency")
            or (isinstance(node.func, ast.Name) and node.func.id == "run_with_idempotency")
        )
    ]


def test_the_routes_are_found() -> None:
    """A guard for the guards: an empty scan would let everything below pass."""
    assert len(_mutating_operations()) >= 10
    assert len(_mutating_route_functions()) >= 10


@pytest.mark.parametrize(("method", "path"), _mutating_operations())
def test_a_mutating_operation_requires_an_idempotency_key_of_one_to_255_characters(
    method: str, path: str
) -> None:
    operation = _openapi()["paths"][path][method.lower()]
    [parameter] = [
        p
        for p in operation.get("parameters", [])
        if p["in"] == "header" and p["name"].lower() == "idempotency-key"
    ]

    assert parameter["required"] is True
    assert parameter["schema"]["minLength"] == 1
    assert parameter["schema"]["maxLength"] == IDEMPOTENCY_KEY_MAX_CHARS == 255


@pytest.mark.parametrize(
    ("module", "function"),
    _mutating_route_functions(),
    ids=lambda value: value if isinstance(value, str) else value.name,
)
def test_a_mutating_route_commits_before_its_result_is_cached(
    module: str, function: ast.AsyncFunctionDef
) -> None:
    calls = _idempotency_calls(function)

    assert calls, f"{module}.{function.name} mutates but never goes through run_with_idempotency"
    for call in calls:
        assert any(keyword.arg == "commit" for keyword in call.keywords), (
            f"{module}.{function.name} does not pass commit= to run_with_idempotency, so its "
            "result is cached before the transaction commits"
        )

"""CLAUDE.md's architecture rules, checked against the source tree itself.

These read the imports of every module under ``src/edutoon`` (they import
nothing and touch no database), so a rule that is broken by a future change fails
here rather than in review:

- rule 1 - domain modules are pure: they import no repository, provider,
  service, database, configuration, API or worker code;
- rule 2 - routers never import repositories or providers (they go through
  services and the ``api.dependencies`` seam);
- rule 3 - no external provider SDK is imported outside the ``providers`` package;
- layering - nothing below the API imports the API or the worker;
- rule 4's neighbour, the append-only ``audit_logs`` table - no code issues an
  UPDATE or DELETE on it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "edutoon"

# The third-party packages that talk to an external provider (rule 3).
PROVIDER_SDKS = frozenset({"anthropic", "openai", "boto3", "botocore", "redis", "pypdf", "jwt"})


def _modules(package: str) -> list[Path]:
    return sorted((SRC / package).rglob("*.py"))


def _all_modules_outside(package: str) -> list[Path]:
    inside = set(_modules(package))
    return [path for path in sorted(SRC.rglob("*.py")) if path not in inside]


def _imported_modules(path: Path) -> set[str]:
    """Every module named by an import statement in ``path``, absolute
    (``edutoon.services.jobs``, ``anthropic``); relative imports are not used in
    this codebase and are reported as ``.`` so they cannot hide a violation.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                found.add(".")
            elif node.module:
                found.add(node.module)
                # ``from edutoon import services`` names a package via its members.
                found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def _imports_of(path: Path, *forbidden_prefixes: str) -> list[str]:
    return sorted(
        module
        for module in _imported_modules(path)
        if any(module == p or module.startswith(f"{p}.") for p in forbidden_prefixes)
    )


def _relative(path: Path) -> str:
    return path.relative_to(SRC).as_posix()


def test_the_source_tree_is_found() -> None:
    """A guard for the guards: an empty scan would let every rule below pass."""
    assert len(_modules("domain")) >= 4
    assert len(_modules("api/routers")) >= 6
    assert len(_modules("providers")) >= 6


@pytest.mark.parametrize("path", _modules("domain"), ids=_relative)
def test_rule_1_domain_modules_are_pure(path: Path) -> None:
    offending = _imports_of(
        path,
        "edutoon.repositories",
        "edutoon.providers",
        "edutoon.services",
        "edutoon.db",
        "edutoon.core",
        "edutoon.api",
        "edutoon.worker",
    )
    assert offending == [], f"{_relative(path)} breaks rule 1 by importing {offending}"


@pytest.mark.parametrize("path", _modules("api/routers"), ids=_relative)
def test_rule_2_routers_never_import_repositories_or_providers(path: Path) -> None:
    offending = _imports_of(path, "edutoon.repositories", "edutoon.providers")
    assert offending == [], f"{_relative(path)} breaks rule 2 by importing {offending}"


@pytest.mark.parametrize("path", _all_modules_outside("providers"), ids=_relative)
def test_rule_3_no_provider_sdk_is_imported_outside_the_providers_package(path: Path) -> None:
    offending = _imports_of(path, *PROVIDER_SDKS)
    assert offending == [], f"{_relative(path)} breaks rule 3 by importing {offending}"


@pytest.mark.parametrize(
    "package", ["domain", "repositories", "providers", "services", "db", "core"]
)
def test_nothing_below_the_api_imports_the_api_or_the_worker(package: str) -> None:
    for path in _modules(package):
        offending = _imports_of(path, "edutoon.api", "edutoon.worker")
        assert offending == [], f"{_relative(path)} imports upward: {offending}"


@pytest.mark.parametrize("package", ["domain", "repositories", "providers"])
def test_domain_repositories_and_providers_do_not_import_services(package: str) -> None:
    for path in _modules(package):
        offending = _imports_of(path, "edutoon.services")
        assert offending == [], f"{_relative(path)} imports upward: {offending}"


def test_no_code_updates_or_deletes_audit_log_rows() -> None:
    """The table is append-only (a database trigger enforces it too)."""
    pattern = re.compile(r"\baudit_logs\s*\.\s*(update|delete)\s*\(")
    offenders = [
        _relative(path)
        for path in sorted(SRC.rglob("*.py"))
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []

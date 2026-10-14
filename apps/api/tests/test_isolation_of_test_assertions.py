"""Tests may only assert about rows they created (Step 3, test-isolation close criteria).

The development database is shared and is never empty: ``test_recovery_with_nothing_to_recover``
looked flaky, and the cause turned out not to be a global assertion at all (its query was already
scoped to the test's own project) but a *module-level clock*: ``NOW = datetime.now(UTC)`` was taken
when the test module was imported, so on a suite that ran longer than the one-hour staleness window
the job it created with ``started_at=NOW`` had become "stale" before the test ran. Both kinds of
mistake are guarded here, statically, for every test module:

* no module-level constant is a wall-clock reading (``datetime.now()``, ``time.time()`` ...);
* a raw SQL statement over ``jobs``, ``scripts``, ``render_outputs``, ``audit_logs`` or
  ``claim_verifications`` (the tables other sessions and earlier runs also write to) carries a
  ``WHERE`` - it never counts, lists, updates or deletes the whole table.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

TESTS = Path(__file__).parent
SHARED_TABLES = ("jobs", "scripts", "render_outputs", "audit_logs", "claim_verifications")
THIS_FILE = Path(__file__).name
# Migration tests run on a database of their own, created for the test and dropped after it.
SCRATCH_DATABASE_MODULES = {path.name for path in TESTS.glob("test_migration_*.py")} | {
    "migration_helpers.py",
    "test_training_committed.py",
    "test_training_enrolments_committed.py",
}

CLOCK_CALL = re.compile(
    r"\b(datetime|date)\s*\.\s*(now|utcnow|today)\s*\(|\btime\s*\.\s*(time|monotonic)\s*\("
)
STATEMENT = re.compile(rf"(?i)\b(from|update|join|into)\s+({'|'.join(SHARED_TABLES)})\b")


def _modules() -> list[Path]:
    return [p for p in sorted(TESTS.glob("*.py")) if p.name != THIS_FILE]


def _module_level_clock_constants(tree: ast.Module, source: str) -> list[str]:
    found = []
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        segment = ast.get_source_segment(source, value) or ""
        if CLOCK_CALL.search(segment):
            names = ", ".join(t.id for t in targets if isinstance(t, ast.Name))
            found.append(f"{names or '?'} = {segment[:60]}")
    return found


@pytest.mark.parametrize("path", _modules(), ids=lambda p: p.name)
def test_no_test_module_reads_the_wall_clock_at_import(path: Path):
    source = path.read_text(encoding="utf-8")
    assert _module_level_clock_constants(ast.parse(source), source) == []


@pytest.mark.parametrize(
    "path",
    [p for p in _modules() if p.name not in SCRATCH_DATABASE_MODULES],
    ids=lambda p: p.name,
)
def test_raw_sql_over_a_shared_table_is_always_scoped_with_a_where(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    unscoped = [
        f"line {node.lineno}: {' '.join(node.value.split())[:90]}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and STATEMENT.search(node.value)
        and re.search(r"(?i)\b(select|update|delete|insert)\b", node.value)
        and not re.search(r"(?i)\bwhere\b|\bvalues\b|\bselect\b.+\bfrom\b.+\bon\b", node.value)
        and not re.search(r"(?i)\bon conflict\b|\breturning\b", node.value)
    ]
    assert unscoped == []


def test_the_checks_themselves_catch_what_they_are_meant_to():
    clocked = "NOW = datetime.now(UTC)\nLATER = NOW\nOK = 5\n"
    assert _module_level_clock_constants(ast.parse(clocked), clocked) == ["NOW = datetime.now(UTC)"]
    inside = "def f():\n    return datetime.now(UTC)\n"
    assert _module_level_clock_constants(ast.parse(inside), inside) == []
    assert STATEMENT.search("SELECT count(*) FROM audit_logs")
    assert STATEMENT.search("delete from jobs")
    assert not STATEMENT.search("SELECT 1 FROM projects")

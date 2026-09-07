"""Unit tests for the isolation-coverage CI scanner.

Each scenario feeds a synthetic diff string plus a synthetic on-disk
layout to build_report, and asserts the coverage decision. No live git
repo required — the diff string bypasses run_diff, and file-existence
checks read from a pytest tmp_path.

Seven scenarios per the 17-05 plan:

1. Go mutation endpoint, no matching test → missing
2. Go mutation endpoint, matching test file → covered
3. Go mutation endpoint with @skip-isolation-test + non-empty reason → skipped
4. Go mutation endpoint with @skip-isolation-test but EMPTY reason → missing
5. Python mutation endpoint, no matching test → missing
6. Python mutation endpoint, matching test file → covered
7. Read endpoint (r.Get / @router.get) → not reported at all
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


# Load the scanner module from a hyphenated filename.
_SPEC = importlib.util.spec_from_file_location(
    "check_isolation_tests",
    Path(__file__).parent / "check-isolation-tests.py",
)
scanner = importlib.util.module_from_spec(_SPEC)  # type: ignore[arg-type]
sys.modules["check_isolation_tests"] = scanner
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(scanner)


# ---------- Diff fixtures ----------


def _diff(file: str, added: list[str], context_lines: list[str] | None = None) -> str:
    """Build a minimal synthetic diff string for one hunk in `file`.

    Only the parts of `git diff` output the scanner reads are included:
    the `+++ b/<file>` header, the `@@` hunk header, and the added lines.
    """
    ctx = context_lines or []
    all_lines = [" " + c for c in ctx] + ["+" + line for line in added]
    body = "\n".join(all_lines)
    header = f"+++ b/{file}\n@@ -1,{len(ctx)} +1,{len(all_lines)} @@\n"
    return header + body


# ---------- Scenarios ----------


def test_go_mutation_endpoint_without_test_is_missing(tmp_path: Path):
    diff = _diff(
        "services/backend/pkg/api/handlers/foo.go",
        ['\tr.Post("/api/foo", h.Foo)'],
    )
    report = scanner.build_report(diff, tmp_path)

    assert not report.passed()
    assert len(report.missing) == 1
    assert report.missing[0].method == "POST"
    assert report.missing[0].path == "/api/foo"
    assert report.missing[0].lang == "go"


def test_go_mutation_endpoint_with_matching_isolation_test_is_covered(tmp_path: Path):
    # Test file that references the path.
    test_dir = tmp_path / "services/backend/pkg/api/handlers"
    test_dir.mkdir(parents=True)
    (test_dir / "foo_isolation_test.go").write_text(
        'package handlers_test\n\n'
        'func TestFooIsolation(t *testing.T) {\n'
        '    // POST /api/foo cross-tenant scenarios\n'
        '    _ = "/api/foo"\n'
        '}\n',
        encoding="utf-8",
    )

    handler_diff = _diff(
        "services/backend/pkg/api/handlers/foo.go",
        ['\tr.Post("/api/foo", h.Foo)'],
    )
    test_diff = _diff(
        "services/backend/pkg/api/handlers/foo_isolation_test.go",
        ['\t_ = "/api/foo"'],
    )
    report = scanner.build_report(handler_diff + "\n" + test_diff, tmp_path)

    assert report.passed()
    assert len(report.covered) == 1
    assert report.covered[0].path == "/api/foo"


def test_go_endpoint_with_skip_marker_and_reason_is_skipped(tmp_path: Path):
    diff = _diff(
        "services/backend/pkg/api/handlers/health.go",
        ['\tr.Post("/health", healthHandler) // @skip-isolation-test: health endpoint carries no tenant data'],
    )
    report = scanner.build_report(diff, tmp_path)

    assert report.passed()
    assert len(report.skipped) == 1
    assert report.skipped[0].reason.startswith("health endpoint carries no tenant data")


def test_go_endpoint_with_skip_marker_and_empty_reason_still_missing(tmp_path: Path):
    diff = _diff(
        "services/backend/pkg/api/handlers/health.go",
        ['\tr.Post("/health", healthHandler) // @skip-isolation-test:'],
    )
    report = scanner.build_report(diff, tmp_path)

    # Empty reason must NOT unlock the skip path — endpoint reports as missing.
    assert not report.passed()
    assert len(report.missing) == 1
    assert report.missing[0].path == "/health"


def test_python_mutation_endpoint_without_test_is_missing(tmp_path: Path):
    diff = _diff(
        "services/workers/api/routes.py",
        ['@router.post("/api/bar")'],
    )
    report = scanner.build_report(diff, tmp_path)

    assert not report.passed()
    assert len(report.missing) == 1
    assert report.missing[0].method == "POST"
    assert report.missing[0].path == "/api/bar"
    assert report.missing[0].lang == "python"


def test_python_mutation_endpoint_with_matching_isolation_test_is_covered(tmp_path: Path):
    test_dir = tmp_path / "services/workers/tests/isolation"
    test_dir.mkdir(parents=True)
    (test_dir / "test_bar_isolation.py").write_text(
        'def test_bar_scoping():\n'
        '    path = "/api/bar"\n'
        '    assert path\n',
        encoding="utf-8",
    )

    handler_diff = _diff(
        "services/workers/api/routes.py",
        ['@router.post("/api/bar")'],
    )
    test_diff = _diff(
        "services/workers/tests/isolation/test_bar_isolation.py",
        ['    path = "/api/bar"'],
    )
    report = scanner.build_report(handler_diff + "\n" + test_diff, tmp_path)

    assert report.passed()
    assert len(report.covered) == 1
    assert report.covered[0].path == "/api/bar"


def test_read_endpoints_are_not_reported(tmp_path: Path):
    # Neither Go GET nor Python GET should be flagged — read endpoints
    # are covered by RLS silent-filter, not by this scanner.
    diff_go = _diff(
        "services/backend/pkg/api/handlers/list.go",
        ['\tr.Get("/api/things", h.List)'],
    )
    diff_py = _diff(
        "services/workers/api/routes.py",
        ['@router.get("/api/other")'],
    )
    report = scanner.build_report(diff_go + "\n" + diff_py, tmp_path)

    assert report.passed()
    assert not report.missing
    assert not report.covered
    assert not report.skipped

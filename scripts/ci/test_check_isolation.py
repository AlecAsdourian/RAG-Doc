"""Unit tests for the isolation-coverage CI scanner.

Each scenario feeds a synthetic diff string plus a synthetic on-disk
layout to build_report, and asserts the coverage decision. No live git
repo required — the diff string bypasses run_diff, and file-existence
checks read from a pytest tmp_path.

The seven original scenarios per the 17-05 plan:

1. Go mutation endpoint, no matching test → missing
2. Go mutation endpoint, matching test file → covered
3. Go mutation endpoint with @skip-isolation-test + non-empty reason → skipped
4. Go mutation endpoint with @skip-isolation-test but EMPTY reason → missing
5. Python mutation endpoint, no matching test → missing
6. Python mutation endpoint, matching test file → covered
7. Read endpoint (r.Get / @router.get) → not reported at all

Plus the multi-line and marker-scoping cases added by PR #11's review, and
the nested-`chi.Route` group added by PR #21's — see the block at the end
of this file.
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


def _write(root: Path, rel: str, lines: list[str]) -> None:
    """Lay `lines` down on disk at `rel`, starting at line 1.

    Route-prefix resolution reads the post-image from disk, so a diff whose
    hunk starts at line 1 needs a file whose lines 1..N are the same ones.
    """
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


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


def test_skip_marker_on_one_endpoint_does_not_leak_to_a_neighbor(tmp_path: Path):
    """Reviewer finding H1 (PR #11): the original hunk-wide scan would
    apply a single skip marker to every endpoint in the hunk. Verify that
    a marker on `/health` does NOT excuse an unrelated `/api/new-mutation`
    added in the same hunk.
    """
    diff = _diff(
        "services/backend/pkg/api/router.go",
        [
            '\tr.Post("/health", healthHandler) // @skip-isolation-test: no tenant data',
            '\tr.Post("/api/new-mutation", h.New)',
        ],
    )
    report = scanner.build_report(diff, tmp_path)

    assert not report.passed(), (
        "the unrelated mutation endpoint must be reported as missing"
    )
    missing_paths = {e.path for e in report.missing}
    skipped_paths = {s.endpoint.path for s in report.skipped}
    assert missing_paths == {"/api/new-mutation"}
    assert skipped_paths == {"/health"}


def test_go_multi_line_route_registration_is_detected(tmp_path: Path):
    """Reviewer finding H2 (PR #11): gofmt wraps long registrations
    across lines. The scanner must still catch them.
    """
    diff = _diff(
        "services/backend/pkg/api/handlers/foo.go",
        [
            '\tr.Post(',
            '\t\t"/api/very-long-endpoint-name-that-forces-a-wrap",',
            '\t\th.Foo,',
            '\t)',
        ],
    )
    report = scanner.build_report(diff, tmp_path)

    assert not report.passed(), "wrapped route registration must not be invisible to the scanner"
    assert len(report.missing) == 1
    e = report.missing[0]
    assert e.method == "POST"
    assert e.path == "/api/very-long-endpoint-name-that-forces-a-wrap"


def test_go_multi_line_handle_func_is_detected(tmp_path: Path):
    """chi.HandleFunc has the same wrapping problem as the shorthand
    method call; both must be caught by the multi-line join.
    """
    diff = _diff(
        "services/backend/pkg/api/handlers/bar.go",
        [
            '\tchi.HandleFunc(',
            '\t\t"POST /api/wrapped-handle-func",',
            '\t\th.Bar,',
            '\t)',
        ],
    )
    report = scanner.build_report(diff, tmp_path)

    assert not report.passed()
    assert len(report.missing) == 1
    e = report.missing[0]
    assert e.method == "POST"
    assert e.path == "/api/wrapped-handle-func"


def test_skip_marker_on_line_above_route_still_applies(tmp_path: Path):
    """A block comment on the line above the route registration is a
    common Go pattern. The per-endpoint window must reach it.
    """
    diff = _diff(
        "services/backend/pkg/api/router.go",
        [
            '\t// @skip-isolation-test: webhook validated by signature only',
            '\tr.Post("/webhooks/vendor", h.Webhook)',
        ],
    )
    report = scanner.build_report(diff, tmp_path)

    assert report.passed(), (
        "a skip marker on the line above the route must apply to that route"
    )
    assert len(report.skipped) == 1
    assert report.skipped[0].endpoint.path == "/webhooks/vendor"


# ---------- Nested chi.Route groups (PR #21 review, finding H1) ----------
#
# Before these, a `r.Route("/x", func(r chi.Router) {` line joined with
# every route inside it — its `(` stays unclosed until the `})` several
# lines later — so the whole block surfaced as ONE logical line, and a
# greedy `.*` in an anchored pattern reported only the LAST registration
# in it. On PR #21 that meant `POST /api/repositories` was never checked
# at all, and the `DELETE` beside it was reported as `/{id}`: a path no
# test can contain, from a line number pointing at the wrong route.


def test_all_mutation_routes_in_a_chi_route_block_are_detected(tmp_path: Path):
    lines = [
        'func routes(r chi.Router) {',
        '\tr.Route("/api/things", func(r chi.Router) {',
        '\t\tr.Get("/", h.List)',
        '\t\tr.Post("/", h.Create)',
        '\t\tr.Get("/{id}", h.Get)',
        '\t\tr.Delete("/{id}", h.Delete)',
        '\t})',
        '}',
    ]
    _write(tmp_path, "services/backend/pkg/api/router.go", lines)
    report = scanner.build_report(
        _diff("services/backend/pkg/api/router.go", lines), tmp_path
    )

    assert {(e.method, e.path) for e in report.missing} == {
        ("POST", "/api/things"),
        ("DELETE", "/api/things/{id}"),
    }, "every mutation route in the block must be reported, with its full path"

    # And at the line it is actually registered on, not the block opener's.
    by_method = {e.method: e for e in report.missing}
    assert by_method["POST"].line == 4
    assert by_method["DELETE"].line == 6


def test_route_prefix_resolves_when_the_block_opener_is_unchanged(tmp_path: Path):
    """The common shape: a PR adds one route to an existing group. The
    `r.Route(...)` opener is a context line, so the prefix is not in the
    diff at all and has to come from the file on disk.
    """
    lines = [
        '\tr.Route("/api/widgets", func(r chi.Router) {',
        '\t\tr.Get("/", h.List)',
        '\t\tr.Delete("/{id}", h.Delete)',
        '\t})',
    ]
    _write(tmp_path, "services/backend/pkg/api/router.go", lines)
    diff = _diff(
        "services/backend/pkg/api/router.go",
        added=['\t\tr.Delete("/{id}", h.Delete)'],
        context_lines=lines[:2],
    )
    report = scanner.build_report(diff, tmp_path)

    assert [e.path for e in report.missing] == ["/api/widgets/{id}"]


def test_parameterised_route_is_covered_by_its_static_prefix(tmp_path: Path):
    """A Go test drives `DELETE /api/widgets/{id}` by building
    `"/api/widgets/" + id`. The literal `{id}` appears nowhere, so
    requiring it would make every parameterised route permanently
    uncoverable.
    """
    router = "services/backend/pkg/api/router.go"
    test = "services/backend/pkg/api/handlers/widgets_isolation_test.go"
    router_lines = [
        '\tr.Route("/api/widgets", func(r chi.Router) {',
        '\t\tr.Delete("/{id}", h.Delete)',
        '\t})',
    ]
    test_line = '\tdo(t, http.MethodDelete, "/api/widgets/"+other.ID, tokenA)'
    _write(tmp_path, router, router_lines)
    _write(tmp_path, test, ["package handlers_test", test_line])

    report = scanner.build_report(
        _diff(router, router_lines) + "\n" + _diff(test, [test_line]), tmp_path
    )

    assert report.passed()
    assert [e.path for e in report.covered] == ["/api/widgets/{id}"]


def test_unresolvable_root_path_is_never_vacuously_covered(tmp_path: Path):
    """`r.Post("/", ...)` with no resolvable prefix used to match every
    file in the repository, because `"/" in text` is true of all of them.
    An endpoint we cannot name is not an endpoint we can call covered.
    """
    test = "pkg/api/thing_isolation_test.go"
    _write(tmp_path, test, ['// exercises "/api/anything"'])

    report = scanner.build_report(
        _diff("pkg/api/mount.go", ['\tr.Post("/", h.Create)'])
        + "\n"
        + _diff(test, ['// exercises "/api/anything"']),
        tmp_path,
    )

    assert not report.passed()
    assert [e.path for e in report.missing] == ["/"]


def test_block_opener_with_a_trailing_comment_still_ends_the_join(tmp_path: Path):
    """`{ // note` puts the brace off end-of-line. If that stops the
    opener being recognised, the whole group collapses back into one
    logical line and its routes go missing again.
    """
    lines = [
        '\tr.Route("/api/things", func(r chi.Router) { // tenant-scoped',
        '\t\tr.Post("/", h.Create)',
        '\t\tr.Delete("/{id}", h.Delete)',
        '\t})',
    ]
    _write(tmp_path, "services/backend/pkg/api/router.go", lines)
    report = scanner.build_report(
        _diff("services/backend/pkg/api/router.go", lines), tmp_path
    )

    assert {(e.method, e.path) for e in report.missing} == {
        ("POST", "/api/things"),
        ("DELETE", "/api/things/{id}"),
    }


def test_every_registration_on_one_logical_line_is_reported(tmp_path: Path):
    """The backstop for any opener shape `_opens_block` does not know.

    When several registrations do end up on one logical line, all of them
    must surface. Reporting only the last is how `POST /api/repositories`
    stayed invisible while the `DELETE` beside it was flagged.
    """
    line = (
        '\tr.Group(func(r chi.Router) { '
        'r.Post("/api/a", h.A); r.Delete("/api/b", h.B) })'
    )
    _write(tmp_path, "services/backend/pkg/api/router.go", [line])
    report = scanner.build_report(
        _diff("services/backend/pkg/api/router.go", [line]), tmp_path
    )

    assert {(e.method, e.path) for e in report.missing} == {
        ("POST", "/api/a"),
        ("DELETE", "/api/b"),
    }


def test_skip_marker_in_a_route_block_binds_to_its_own_route(tmp_path: Path):
    """One marker, two routes in the same group: the marked one is
    skipped and its neighbour is still demanded.
    """
    lines = [
        '\tr.Route("/api/hooks", func(r chi.Router) {',
        '\t\t// @skip-isolation-test: signature-verified, carries no tenant data',
        '\t\tr.Post("/github", h.Webhook)',
        '\t\tr.Delete("/{id}", h.Delete)',
        '\t})',
    ]
    _write(tmp_path, "services/backend/pkg/api/router.go", lines)
    report = scanner.build_report(
        _diff("services/backend/pkg/api/router.go", lines), tmp_path
    )

    assert [s.endpoint.path for s in report.skipped] == ["/api/hooks/github"]
    assert [e.path for e in report.missing] == ["/api/hooks/{id}"]


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

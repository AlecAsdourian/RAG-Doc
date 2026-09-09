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


# ---------- PR #21 second review ----------


def test_middleware_wrapped_routes_are_detected(tmp_path: Path):
    """H1. `r.With(mw).Post(...)` has a `)` before the dot, so a receiver
    pattern requiring an identifier saw nothing at all — on the idiom
    router.go already uses for every timeout-wrapped group. A destructive
    route guarded by an admin check is exactly this shape.
    """
    lines = [
        '\tr.With(middleware.Timeout(60*time.Second)).Route("/api", func(r chi.Router) {',
        '\t\tr.With(requireAdmin).Delete("/organizations/{id}", h.DeleteOrg)',
        '\t\tr.With(requireAdmin).Post("/organizations/{id}/transfer", h.Transfer)',
        '\t})',
    ]
    _write(tmp_path, "services/backend/pkg/api/router.go", lines)
    report = scanner.build_report(
        _diff("services/backend/pkg/api/router.go", lines), tmp_path
    )

    assert {(e.method, e.path) for e in report.missing} == {
        ("DELETE", "/api/organizations/{id}"),
        ("POST", "/api/organizations/{id}/transfer"),
    }


def test_chi_method_and_methodfunc_registrations_are_detected(tmp_path: Path):
    """H1, same class: chi's explicit form takes the method as an argument,
    which the shorthand pattern cannot see."""
    lines = [
        '\tr.Method("POST", "/api/imports", h.Import)',
        '\tr.MethodFunc(http.MethodDelete, "/api/imports/{id}", h.Drop)',
        '\tr.Method("GET", "/api/imports", h.List)',
    ]
    _write(tmp_path, "services/backend/pkg/api/router.go", lines)
    report = scanner.build_report(
        _diff("services/backend/pkg/api/router.go", lines), tmp_path
    )

    assert {(e.method, e.path) for e in report.missing} == {
        ("POST", "/api/imports"),
        ("DELETE", "/api/imports/{id}"),
    }, "GET must stay unreported; both mutation spellings must be caught"


def test_a_new_route_under_a_tested_prefix_is_not_free(tmp_path: Path):
    """H2. Matching only the leading static piece meant every new route
    nested under an already-tested prefix was covered for nothing.
    `/api/repositories/{id}/resync` reduced to `/api/repositories/`, which
    the existing DELETE test already contains.
    """
    router = "services/backend/pkg/api/router.go"
    test = "services/backend/pkg/api/handlers/repositories_isolation_test.go"
    router_lines = [
        '\tr.Route("/api/repositories", func(r chi.Router) {',
        '\t\tr.Post("/{id}/resync", h.Resync)',
        '\t})',
    ]
    existing_test_line = '\tdo(t, http.MethodDelete, "/api/repositories/"+other.ID, tokenA)'
    _write(tmp_path, router, router_lines)
    _write(tmp_path, test, ["package handlers_test", existing_test_line])

    report = scanner.build_report(
        _diff(router, added=[router_lines[1]], context_lines=[router_lines[0]])
        + "\n"
        + _diff(test, [existing_test_line]),
        tmp_path,
    )

    assert not report.passed(), (
        "a new mutation endpoint must not inherit coverage from a sibling route"
    )
    assert [e.path for e in report.missing] == ["/api/repositories/{id}/resync"]


def test_every_static_segment_present_counts_as_covered(tmp_path: Path):
    """The other half of H2: a test that really does drive the route —
    building the id in the middle — must still count."""
    router = "services/backend/pkg/api/router.go"
    test = "services/backend/pkg/api/handlers/repositories_isolation_test.go"
    router_lines = [
        '\tr.Route("/api/repositories", func(r chi.Router) {',
        '\t\tr.Post("/{id}/resync", h.Resync)',
        '\t})',
    ]
    test_line = '\tdo(t, http.MethodPost, "/api/repositories/"+id+"/resync", tokenA)'
    _write(tmp_path, router, router_lines)
    _write(tmp_path, test, ["package handlers_test", test_line])

    report = scanner.build_report(
        _diff(router, router_lines) + "\n" + _diff(test, [test_line]), tmp_path
    )

    assert report.passed()
    assert [e.path for e in report.covered] == ["/api/repositories/{id}/resync"]


def test_braces_inside_literals_and_comments_do_not_move_the_prefix(tmp_path: Path):
    """L1. A `}` in a string, a raw string carrying `{`, and a multi-line
    block comment each desynchronised the brace stack, so routes reported
    a prefix belonging to some other block.
    """
    lines = [
        '\tr.Route("/api/admin", func(r chi.Router) {',
        '\t\tw.Write([]byte("}"))',
        '\t\tconst tmpl = `{"shape":`',
        '\t\t/* payload looks like { id, name',
        '\t\t   across two lines } */',
        '\t\tr.Post("/wipe", h.Wipe)',
        '\t})',
        '\tr.Post("/api/unrelated", h.Other)',
    ]
    _write(tmp_path, "services/backend/pkg/api/router.go", lines)
    report = scanner.build_report(
        _diff("services/backend/pkg/api/router.go", lines), tmp_path
    )

    assert {(e.method, e.path) for e in report.missing} == {
        ("POST", "/api/admin/wipe"),
        ("POST", "/api/unrelated"),
    }, "a route after the block must not inherit its prefix either"


def test_an_apostrophe_in_prose_does_not_swallow_braces(tmp_path: Path):
    """N-H1, the worst of the lot.

    An unbounded rune alternative in the old regex meant an apostrophe in
    an English comment opened a literal that closed at the next apostrophe
    ANYWHERE, blanking every brace between them. `router.go` carries
    fifteen apostrophes inside `//` comments. The consequence was not a
    cosmetic wrong path: an unrelated destructive route registered after
    the block inherited the block's prefix and was matched by the block's
    existing test.
    """
    lines = [
        '\tr.Route("/api/repositories", func(r chi.Router) {',
        "\t\t// Don't add a sync endpoint here; Phase 21 owns the queue.",
        '\t\tr.Get("/", h.List)',
        '\t})',
        "\t// The stream's lifecycle is managed by the handler itself.",
        '\tr.Delete("/{id}", h.WipeEverything)',
    ]
    _write(tmp_path, "services/backend/pkg/api/router.go", lines)
    report = scanner.build_report(
        _diff("services/backend/pkg/api/router.go", lines), tmp_path
    )

    assert [(e.method, e.path) for e in report.missing] == [("DELETE", "/{id}")], (
        "the route after the block must not inherit /api/repositories"
    )


def test_a_block_comment_opener_inside_a_line_comment_is_inert(tmp_path: Path):
    """N-H1's second door: `/*` written inside a `//` comment used to open
    a block comment that ran to the next `*/`, blanking braces on the way.
    """
    lines = [
        '\tr.Route("/api/admin", func(r chi.Router) {  // TODO /* split this up',
        '\t\tr.Post("/wipe", h.Wipe)',
        '\t})',
        '\tr.Post("/public/subscribe", h.Subscribe)',
    ]
    _write(tmp_path, "services/backend/pkg/api/router.go", lines)
    report = scanner.build_report(
        _diff("services/backend/pkg/api/router.go", lines), tmp_path
    )

    assert {(e.method, e.path) for e in report.missing} == {
        ("POST", "/api/admin/wipe"),
        ("POST", "/public/subscribe"),
    }


def test_segments_must_share_one_line_in_order(tmp_path: Path):
    """N-M2. Matching segments independently anywhere in the file left the
    free pass intact in a narrower form — a stray `// TODO: cover /resync`
    was enough to mark the route covered.
    """
    router = "services/backend/pkg/api/router.go"
    test = "services/backend/pkg/api/handlers/repositories_isolation_test.go"
    router_lines = [
        '\tr.Route("/api/repositories", func(r chi.Router) {',
        '\t\tr.Post("/{id}/resync", h.Resync)',
        '\t})',
    ]
    scattered = [
        'package handlers_test',
        '\tdo(t, http.MethodDelete, "/api/repositories/"+other.ID, tokenA)',
        '\t// TODO: cover /resync one day',
    ]
    _write(tmp_path, router, router_lines)
    _write(tmp_path, test, scattered)

    report = scanner.build_report(
        _diff(router, added=[router_lines[1]], context_lines=[router_lines[0]])
        + "\n"
        + _diff(test, scattered[1:]),
        tmp_path,
    )

    assert not report.passed(), (
        "segments scattered across unrelated lines must not count as coverage"
    )


def test_segments_out_of_order_on_one_line_do_not_count(tmp_path: Path):
    """Order is part of the rule, not incidental: a line mentioning the
    tail before the head is not a line that builds this path."""
    router = "services/backend/pkg/api/router.go"
    test = "services/backend/pkg/api/handlers/thing_isolation_test.go"
    router_lines = [
        '\tr.Route("/api/things", func(r chi.Router) {',
        '\t\tr.Post("/{id}/resync", h.Resync)',
        '\t})',
    ]
    backwards = ['package handlers_test', '\t// "/resync" is reached under "/api/things/"']
    _write(tmp_path, router, router_lines)
    _write(tmp_path, test, backwards)

    report = scanner.build_report(
        _diff(router, router_lines) + "\n" + _diff(test, backwards[1:]), tmp_path
    )

    assert not report.passed()
    assert [e.path for e in report.missing] == ["/api/things/{id}/resync"]


def test_a_library_call_with_a_non_path_argument_is_not_a_route(tmp_path: Path):
    """N-L7. Widening the receiver to accept `)` and `]` also matched any
    library call taking a string. A chi path starts with a slash.
    """
    lines = [
        '\tbuckets[0].Delete("tmp")',
        '\tcache.Delete("some-key")',
        '\tr.Post("/api/real-route", h.Real)',
    ]
    _write(tmp_path, "services/backend/pkg/api/router.go", lines)
    report = scanner.build_report(
        _diff("services/backend/pkg/api/router.go", lines), tmp_path
    )

    assert [(e.method, e.path) for e in report.missing] == [("POST", "/api/real-route")]


def test_a_double_slash_route_literal_is_not_read_as_a_comment(tmp_path: Path):
    """L2. `"//v2"` looks like a comment opener, which collapsed the block
    onto one logical line — and one skip marker then covered every route
    in it.
    """
    lines = [
        '\tr.Route("//v2", func(r chi.Router) { // @skip-isolation-test: legacy alias',
        '\t\tr.Post("/wipe", h.Wipe)',
        '\t\tr.Delete("/nuke", h.Nuke)',
        '\t})',
    ]
    _write(tmp_path, "services/backend/pkg/api/router.go", lines)
    report = scanner.build_report(
        _diff("services/backend/pkg/api/router.go", lines), tmp_path
    )

    assert not report.passed(), (
        "a marker on the Route opener must not excuse the routes inside it"
    )
    assert {(e.method, e.path) for e in report.missing} == {
        ("POST", "/v2/wipe"),
        ("DELETE", "/v2/nuke"),
    }


def test_a_route_after_a_closed_block_loses_the_prefix(tmp_path: Path):
    """The prefix stack has to POP. Nothing exercised that before, so a
    stack that never popped passed the whole suite.
    """
    lines = [
        '\tr.Route("/api/things", func(r chi.Router) {',
        '\t\tr.Post("/", h.Create)',
        '\t})',
        '\tr.Post("/webhooks/github", h.Webhook)',
    ]
    _write(tmp_path, "services/backend/pkg/api/router.go", lines)
    report = scanner.build_report(
        _diff("services/backend/pkg/api/router.go", lines), tmp_path
    )

    assert {(e.method, e.path) for e in report.missing} == {
        ("POST", "/api/things"),
        ("POST", "/webhooks/github"),
    }


def test_a_route_sharing_its_groups_line_is_inside_that_group(tmp_path: Path):
    """A route on the same line as its own `Route(` opener is INSIDE it.

    An earlier version recorded the prefix before pushing, on the reasoning
    that such a route sits outside its own group. That is backwards, and it
    left a complete one-line group with no prefix at all — its routes
    reported as `/y` rather than `/x/y`.
    """
    lines = [
        '\tr.Post("/api/first", h.First)',
        '\tr.Route("/api/inline", func(r chi.Router) { r.Post("/second", h.Second) })',
        '\tr.Route("/api/group", func(r chi.Router) {',
        '\t\tr.Delete("/third", h.Third)',
        '\t})',
        '\tr.Post("/api/last", h.Last)',
    ]
    _write(tmp_path, "services/backend/pkg/api/router.go", lines)
    report = scanner.build_report(
        _diff("services/backend/pkg/api/router.go", lines), tmp_path
    )

    assert {(e.method, e.path) for e in report.missing} == {
        ("POST", "/api/first"),
        ("POST", "/api/inline/second"),
        ("DELETE", "/api/group/third"),
        ("POST", "/api/last"),
    }


def test_routes_registered_inside_a_test_file_are_not_reported(tmp_path: Path):
    """Isolation tests stand up their own routers. Scanning them would
    report a test's own scaffolding as an unprotected endpoint."""
    test = "services/backend/pkg/api/handlers/thing_isolation_test.go"
    lines = ['\tr.Post("/api/only-in-a-test", h.Thing)']
    _write(tmp_path, test, lines)
    report = scanner.build_report(_diff(test, lines), tmp_path)

    assert report.passed()
    assert not report.missing and not report.covered and not report.skipped


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

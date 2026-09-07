#!/usr/bin/env python3
"""Scan a git diff for mutation endpoints missing isolation tests.

Runs as a GitHub Action step (see .github/workflows/isolation-check.yml)
and locally as: `python scripts/ci/check-isolation-tests.py --base-ref main`.

Behavior:

- Parse `git diff --unified=3 <base>...<head>` for added lines matching
  a mutation-endpoint pattern (Go: `.Post/Put/Patch/Delete("path", ...)`
  or `HandleFunc("METHOD path", ...)`; Python: `@router.post("path")`
  etc.).
- For each detected endpoint, check whether any test file in the same
  diff (matching `*_isolation_test.go` or `test_*_isolation.py`)
  references the endpoint path string.
- If not covered, look for a `@skip-isolation-test: <reason>` marker on
  the endpoint's line or within its diff hunk. A non-empty reason is
  required.
- Exit 0 if every mutation endpoint is covered or explicitly skipped;
  exit 1 otherwise.

Output: a human-readable report to stdout, or (with --json) a machine
report to stdout and no human noise. The GitHub Action pipes the JSON
into a PR comment.

Deliberately regex-on-diff, not AST-based. It's ~100x faster, portable
across languages, and easier to extend when v2 adds new frameworks
(e.g., MCP endpoints — one line in ENDPOINT_PATTERNS below).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Iterable


# ---------- Pattern registry ----------

# Matches an added line (starts with `+`). Groups: (method, path).
_GO_METHOD_CALL = re.compile(
    r'^\+.*\b(?:[A-Za-z_][A-Za-z0-9_]*)\.(Post|Put|Patch|Delete)\s*\(\s*"([^"]+)"'
)
_GO_HANDLE_FUNC = re.compile(
    r'^\+.*HandleFunc\s*\(\s*"(POST|PUT|PATCH|DELETE)\s+([^"]+)"'
)
_PY_DECORATOR = re.compile(
    r'^\+\s*@\w+\.(post|put|patch|delete)\s*\(\s*[\'"]([^\'"]+)[\'"]'
)

ENDPOINT_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "go": [_GO_METHOD_CALL, _GO_HANDLE_FUNC],
    "python": [_PY_DECORATOR],
}

# `@skip-isolation-test: reason`. The reason must contain at least one
# non-space character; an empty or whitespace-only reason is refused.
SKIP_MARKER = re.compile(r'@skip-isolation-test:\s*(\S[^\n\r*]*?)\s*(?:$|\*/)')

# Test file globs: any file matching these is a candidate for the
# "isolation test in same PR" check.
TEST_FILE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r'(?:^|/)[^/]*_isolation_test\.go$'),
    re.compile(r'(?:^|/)test_[^/]*_isolation\.py$'),
    re.compile(r'(?:^|/)[^/]*_isolation_test\.py$'),
]


# ---------- Data types ----------


@dataclass
class Endpoint:
    method: str
    path: str
    file: str
    line: int
    lang: str


@dataclass
class SkippedEndpoint:
    endpoint: Endpoint
    reason: str


@dataclass
class Report:
    missing: list[Endpoint] = field(default_factory=list)
    skipped: list[SkippedEndpoint] = field(default_factory=list)
    covered: list[Endpoint] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(
            {
                "missing": [asdict(e) for e in self.missing],
                "skipped": [
                    {"endpoint": asdict(s.endpoint), "reason": s.reason}
                    for s in self.skipped
                ],
                "covered": [asdict(e) for e in self.covered],
            },
            indent=2,
        )

    def passed(self) -> bool:
        return not self.missing


# ---------- Diff parsing ----------


@dataclass
class Hunk:
    file: str  # path relative to repo root
    lang: str  # "go" or "python" or "other"
    start_line: int  # first line number in new file (from @@ header)
    lines: list[str]  # raw diff lines including leading + / -


def parse_diff(diff_text: str) -> list[Hunk]:
    """Parse `git diff` output into per-hunk records with new-file line numbers."""
    hunks: list[Hunk] = []
    current_file: str | None = None
    current_lang: str = "other"
    current_hunk: Hunk | None = None
    new_line = 0

    file_header = re.compile(r'^\+\+\+ b/(.+)$')
    hunk_header = re.compile(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@')

    for raw in diff_text.splitlines():
        m = file_header.match(raw)
        if m:
            if current_hunk is not None:
                hunks.append(current_hunk)
                current_hunk = None
            current_file = m.group(1)
            current_lang = _lang_for_path(current_file)
            continue

        m = hunk_header.match(raw)
        if m:
            if current_hunk is not None:
                hunks.append(current_hunk)
            new_line = int(m.group(1))
            current_hunk = Hunk(
                file=current_file or "<unknown>",
                lang=current_lang,
                start_line=new_line,
                lines=[],
            )
            continue

        if current_hunk is None:
            continue

        current_hunk.lines.append(raw)

    if current_hunk is not None:
        hunks.append(current_hunk)

    return hunks


def _lang_for_path(path: str) -> str:
    if path.endswith(".go"):
        return "go"
    if path.endswith(".py"):
        return "python"
    return "other"


def _iter_logical_added_lines(hunk: Hunk) -> Iterable[tuple[int, int, str]]:
    """Yield (new_file_line_no, hunk_idx, logical_content) per added line.

    For Go, consecutive added lines are joined into one logical line while
    the running parenthesis balance is unclosed — so `r.Post(\\n\\t"/x",\\n)`
    surfaces as a single logical event carrying the whole call, and the
    endpoint regex can match a multi-line-wrapped registration. The event's
    reported line number and `hunk_idx` are those of the FIRST line in the
    joined sequence.

    For Python and other languages, each added line is its own event —
    multi-line decorators are exotic enough that the added complexity is
    not worth it.
    """
    line_no = hunk.start_line
    pending_start_no: int | None = None
    pending_start_idx: int | None = None
    pending_content: str | None = None

    def _flush() -> Iterable[tuple[int, int, str]]:
        nonlocal pending_start_no, pending_start_idx, pending_content
        if pending_content is not None:
            yield (
                pending_start_no or 0,
                pending_start_idx or 0,
                pending_content,
            )
            pending_start_no = None
            pending_start_idx = None
            pending_content = None

    for idx, raw in enumerate(hunk.lines):
        if raw.startswith("+++"):
            continue
        if raw.startswith("+"):
            content = raw[1:]
            if pending_content is None:
                pending_content = content
                pending_start_no = line_no
                pending_start_idx = idx
            else:
                # Continuation of a wrapped registration; strip leading
                # indent to keep the joined content readable for the regex.
                pending_content = pending_content + " " + content.lstrip()
            line_no += 1
            # If parens are balanced (Go multi-line join) OR we're not Go,
            # emit this event and reset.
            if hunk.lang != "go" or _parens_balanced(pending_content):
                yield from _flush()
        elif raw.startswith("-"):
            # deleted line — flush any pending join, does not advance line no
            yield from _flush()
        else:
            # context line — flush and advance
            yield from _flush()
            line_no += 1

    yield from _flush()


def _parens_balanced(text: str) -> bool:
    """True if `(` count is <= `)` count. Cheap approximation — good enough
    for detecting an unclosed function call at end of a diff line."""
    return text.count("(") <= text.count(")")


def added_endpoints(hunk: Hunk) -> Iterable[tuple[Endpoint, int]]:
    """Yield (endpoint, hunk_line_idx) for each mutation route in this hunk.

    The `hunk_line_idx` points to the first raw line of the registration
    (so multi-line-wrapped routes report the line where `.Post(` begins,
    not the line where the string literal happens to sit). It is used by
    `endpoint_skip_reason` to scan for a per-endpoint skip marker.
    """
    if hunk.lang == "other":
        return
    for line_no, hunk_idx, content in _iter_logical_added_lines(hunk):
        # `content` has no leading `+`; re-add it so the same patterns
        # (which anchor on `^\+`) match uniformly.
        needle = "+" + content
        for pat in ENDPOINT_PATTERNS[hunk.lang]:
            m = pat.match(needle)
            if m:
                yield (
                    Endpoint(
                        method=m.group(1).upper(),
                        path=m.group(2),
                        file=hunk.file,
                        line=line_no,
                        lang=hunk.lang,
                    ),
                    hunk_idx,
                )
                break


# How many lines *above* the endpoint's registration to scan for a
# block-comment skip marker. Three lines matches the plan's ±3 window.
_SKIP_LOOKBACK = 3


def endpoint_skip_reason(hunk: Hunk, endpoint_idx: int) -> str | None:
    """Return the non-empty @skip-isolation-test reason for this endpoint,
    or None if there isn't one.

    Scans the endpoint's own line first (inline `//` or `#` comment), then
    up to `_SKIP_LOOKBACK` lines above (block comment above the route).
    Bounded per-endpoint so one marker cannot bleed onto unrelated
    endpoints — the lookback halts the moment it encounters a line that
    itself is another route registration, so an endpoint at hunk index 1
    does not inherit the marker from a different endpoint at index 0.
    """
    lo = max(0, endpoint_idx - _SKIP_LOOKBACK)
    patterns = ENDPOINT_PATTERNS.get(hunk.lang, [])
    for idx in range(endpoint_idx, lo - 1, -1):
        raw = hunk.lines[idx]
        if idx != endpoint_idx:
            # A different route registration boundary — do not cross it.
            if any(p.match(raw) for p in patterns):
                return None
        m = SKIP_MARKER.search(raw)
        if m:
            reason = m.group(1).strip()
            if reason:
                return reason
    return None


# ---------- Coverage check ----------


def collect_test_file_paths(diff_text: str) -> set[str]:
    """Return every path in the diff that looks like an isolation-test file."""
    paths: set[str] = set()
    for raw in diff_text.splitlines():
        if raw.startswith("+++ b/"):
            path = raw[len("+++ b/"):]
            for pat in TEST_FILE_PATTERNS:
                if pat.search(path):
                    paths.add(path)
                    break
    return paths


def test_files_reference(
    paths: set[str], endpoint: Endpoint, repo_root: Path
) -> bool:
    """True if any of the given test files contains the endpoint's path."""
    for rel in paths:
        target = repo_root / rel
        if not target.exists():
            # New file in diff that was later deleted, or a rename. Skip.
            continue
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if endpoint.path in text:
            return True
    return False


# ---------- Orchestration ----------


def build_report(diff_text: str, repo_root: Path) -> Report:
    hunks = parse_diff(diff_text)
    test_paths = collect_test_file_paths(diff_text)

    report = Report()

    for hunk in hunks:
        # Skip test files themselves — adding a test that registers a
        # route shouldn't count as an unprotected mutation endpoint.
        if any(p.search(hunk.file) for p in TEST_FILE_PATTERNS):
            continue
        for endpoint, hunk_idx in added_endpoints(hunk):
            reason = endpoint_skip_reason(hunk, hunk_idx)
            if reason is not None:
                report.skipped.append(
                    SkippedEndpoint(endpoint=endpoint, reason=reason)
                )
                continue
            if test_files_reference(test_paths, endpoint, repo_root):
                report.covered.append(endpoint)
                continue
            report.missing.append(endpoint)

    return report


def run_diff(base: str, head: str, repo_root: Path) -> str:
    cmd = [
        "git",
        "-C",
        str(repo_root),
        "diff",
        "--unified=3",
        f"{base}...{head}",
        "--",
        "*.go",
        "*.py",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0 and not result.stdout:
        print(f"git diff failed: {result.stderr}", file=sys.stderr)
        sys.exit(2)
    return result.stdout


def print_human_report(report: Report, verbose: bool) -> None:
    if report.missing:
        print("Isolation test coverage check: FAIL")
        print()
        print("Mutation endpoints added without a matching isolation test:")
        for e in report.missing:
            print(f"  - {e.method} {e.path}  ({e.file}:{e.line})")
        print()
        print(
            "Add a test in `*_isolation_test.go` (Go) or `test_*_isolation.py` "
            "(Python) that references the endpoint path or handler name."
        )
        print(
            "To intentionally skip (rare): add `// @skip-isolation-test: <reason>` "
            "(Go) or `# @skip-isolation-test: <reason>` (Python) on the route line."
        )
        print("See docs/isolation.md for the pattern.")
    else:
        print("Isolation test coverage check: PASS")

    if verbose or report.missing:
        if report.skipped:
            print()
            print("Skipped endpoints:")
            for s in report.skipped:
                e = s.endpoint
                print(
                    f"  - {e.method} {e.path}  ({e.file}:{e.line})  reason: {s.reason}"
                )
    if verbose:
        if report.covered:
            print()
            print("Covered endpoints:")
            for e in report.covered:
                print(f"  - {e.method} {e.path}  ({e.file}:{e.line})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fail if any mutation endpoint added in the diff lacks a "
            "matching isolation test in the same PR."
        )
    )
    parser.add_argument("--base-ref", default="origin/main")
    parser.add_argument("--head-ref", default="HEAD")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON report on stdout instead of the human-readable report.",
    )
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Path to the repository root. Defaults to the current directory.",
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    diff_text = run_diff(args.base_ref, args.head_ref, repo_root)

    report = build_report(diff_text, repo_root)

    if args.json:
        print(report.to_json())
    else:
        print_human_report(report, verbose=args.verbose)

    return 0 if report.passed() else 1


if __name__ == "__main__":
    sys.exit(main())

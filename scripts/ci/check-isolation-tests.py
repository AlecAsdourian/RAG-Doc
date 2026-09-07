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


def added_endpoints(hunk: Hunk) -> Iterable[Endpoint]:
    """Yield endpoints introduced or modified by added lines in this hunk."""
    if hunk.lang == "other":
        return
    line_no = hunk.start_line
    for raw in hunk.lines:
        if raw.startswith("+") and not raw.startswith("+++"):
            for pat in ENDPOINT_PATTERNS[hunk.lang]:
                m = pat.match(raw)
                if m:
                    yield Endpoint(
                        method=m.group(1).upper(),
                        path=m.group(2),
                        file=hunk.file,
                        line=line_no,
                        lang=hunk.lang,
                    )
                    break
            line_no += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            # deleted line: does not advance new-file line number
            pass
        else:
            # context line: advances new-file line number
            line_no += 1


def hunk_skip_reason(hunk: Hunk) -> str | None:
    """Return the first non-empty @skip-isolation-test reason found in the hunk."""
    for raw in hunk.lines:
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
        skip_reason = hunk_skip_reason(hunk)
        for endpoint in added_endpoints(hunk):
            if skip_reason is not None:
                report.skipped.append(
                    SkippedEndpoint(endpoint=endpoint, reason=skip_reason)
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

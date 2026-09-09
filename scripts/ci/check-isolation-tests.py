#!/usr/bin/env python3
"""Scan a git diff for mutation endpoints missing isolation tests.

Runs as a GitHub Action step (see .github/workflows/isolation-check.yml)
and locally as: `python scripts/ci/check-isolation-tests.py --base-ref main`.

Behavior:

- Parse `git diff --unified=3 <base>...<head>` for added lines matching
  a mutation-endpoint pattern (Go: `.Post/Put/Patch/Delete("path", ...)`
  or `HandleFunc("METHOD path", ...)`; Python: `@router.post("path")`
  etc.).
- Resolve each Go endpoint's FULL path by walking the enclosing
  `chi.Route`/`Mount` prefixes in the file on disk, so a route registered
  as `"/{id}"` inside `Route("/api/repositories")` is reported — and
  matched — as `/api/repositories/{id}`.
- For each detected endpoint, check whether any test file in the same
  diff (matching `*_isolation_test.go` or `test_*_isolation.py`)
  references the endpoint's full path, or has one line holding every
  static segment of it in order — since a test builds real ids in place
  of the `{param}` pieces.
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

# Route-registration patterns. Groups: (method, path).
#
# Deliberately NOT anchored on the diff's leading `+`. They are used two
# ways, and an anchor breaks both: scanned across a logical added line
# (whose `+` has already been stripped, and which may carry more than one
# registration), and searched against raw diff lines by
# `endpoint_skip_reason` to find registration boundaries — where an
# unchanged route sitting between a skip marker and an endpoint is every
# bit as much a boundary as an added one.
#
# The receiver is `[\w)\]]`, not an identifier. `r.With(mw).Post("/x", h)`
# has a `)` before the dot, and requiring an identifier there made the
# scanner blind to middleware-wrapped routes — the idiom router.go itself
# uses for every timeout-wrapped group. A destructive route guarded by an
# admin check is exactly the shape most likely to be written that way.
# The path must start with `/`. Widening the receiver to accept `)` and
# `]` also let `buckets[0].Delete("tmp")` and any other library call with
# a string argument read as a route; a chi path always starts with a
# slash, so that one character filters most of it back out.
_GO_METHOD_CALL = re.compile(
    r'[\w)\]]\s*\.\s*(Post|Put|Patch|Delete)\s*\(\s*"(/[^"]*)"'
)
_GO_HANDLE_FUNC = re.compile(
    r'HandleFunc\s*\(\s*"(POST|PUT|PATCH|DELETE)\s+(/[^"]*)"'
)
# `r.Method("POST", "/x", h)` / `r.MethodFunc(http.MethodDelete, "/y", h)`
# — chi's explicit form, invisible to the shorthand pattern above.
_GO_METHOD_STRING = re.compile(
    r'\.\s*Method(?:Func)?\s*\(\s*'
    r'(?:"(POST|PUT|PATCH|DELETE)"|http\.Method(Post|Put|Patch|Delete))'
    r'\s*,\s*"(/[^"]*)"'
)
_PY_DECORATOR = re.compile(
    r'@\w+\.(post|put|patch|delete)\s*\(\s*[\'"]([^\'"]+)[\'"]'
)

ENDPOINT_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "go": [_GO_METHOD_CALL, _GO_HANDLE_FUNC, _GO_METHOD_STRING],
    "python": [_PY_DECORATOR],
}


def _method_and_path(m: re.Match[str]) -> tuple[str, str]:
    """Pull (METHOD, path) out of a match.

    Patterns may carry alternatives that leave some groups None (the two
    spellings of a chi `Method` call), so read the surviving groups
    positionally: method first, path last.
    """
    groups = [g for g in m.groups() if g is not None]
    return groups[0].upper(), groups[-1]

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
    path: str  # full path, including any enclosing chi Route/Mount prefix
    file: str
    line: int
    lang: str
    raw_path: str = ""  # the literal in the registration, before prefixing


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

    A line that OPENS A BLOCK ends the join even though its parentheses are
    unbalanced — see `_opens_block`. Without that, an added
    `r.Route("/x", func(r chi.Router) {` swallows every route in the block,
    because its `(` stays unclosed until the `})` several routes later.

    For Python and other languages, each added line is its own event —
    multi-line decorators are exotic enough that the added complexity is
    not worth it.
    """
    line_no = hunk.start_line
    pending_start_no: int | None = None
    pending_start_idx: int | None = None
    pending_content: str | None = None
    pending_code: str = ""

    def _flush() -> Iterable[tuple[int, int, str]]:
        nonlocal pending_start_no, pending_start_idx, pending_content, pending_code
        if pending_content is not None:
            yield (
                pending_start_no or 0,
                pending_start_idx or 0,
                pending_content,
            )
            pending_start_no = None
            pending_start_idx = None
            pending_content = None
            pending_code = ""

    for idx, raw in enumerate(hunk.lines):
        if raw.startswith("+++"):
            continue
        if raw.startswith("+"):
            content = raw[1:]
            # Parens are counted on the line WITHOUT its comment. A prose
            # comment routinely carries an unbalanced `(`, and one that
            # does used to open a join that swallowed the real routes
            # after it — they were then reported at the comment's line
            # number, under the comment's (wrong) prefix.
            code = _strip_line_comment(content) if hunk.lang == "go" else content
            if pending_content is None:
                pending_content = content
                pending_code = code
                pending_start_no = line_no
                pending_start_idx = idx
            else:
                # Continuation of a wrapped registration; strip leading
                # indent to keep the joined content readable for the regex.
                pending_content = pending_content + " " + content.lstrip()
                pending_code = pending_code + " " + code.lstrip()
            line_no += 1
            # Emit and reset when we're not Go, when the Go parens have
            # closed, or when the line opened a block (whose parens will
            # not close for many lines and must not swallow them).
            if (
                hunk.lang != "go"
                or _parens_balanced(pending_code)
                or _opens_block(pending_code)
            ):
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


def _scan_go(source: str) -> tuple[str, str]:
    """One pass, two views of the file.

    Returns `(code_only, comments_blanked)`:

    * `code_only` blanks strings, runes, raw strings AND comments — used
      for counting braces, where a `}` in any of those is noise.
    * `comments_blanked` blanks only comments, keeping string literals —
      used for reading a route's path, which lives in a literal, while
      still ignoring a commented-out registration.

    Total length is preserved in both, and every `\n` in the source stays
    a `\n`, so offsets into the original still line up.

    That is NOT the same as "line counts are preserved" — an earlier
    version of this docstring claimed it was. `str.splitlines()` also
    breaks on `\v \f \x1c \x1d \x1e \x85    ` and a lone `\r`,
    and those survive inside a literal while being blanked to a space
    outside one, so the two views can disagree about how many lines there
    are. Callers that index by line must therefore `split("\n")`, which is
    what `route_prefixes` does.

    Why a scanner and not a regex alternation. The first version of this
    was one, and it had a hole big enough to hand a destructive route a
    free pass: an unbounded rune alternative `'…'` meant an APOSTROPHE IN
    AN ENGLISH COMMENT opened a literal that ran to the next apostrophe
    anywhere in the file, blanking every brace in between. `router.go`
    carries fifteen apostrophes inside `//` comments, and

        // Don't add a sync endpoint here; Phase 21 owns the queue.
        ...
        // The stream's lifecycle is managed by the handler itself.

    swallowed the `})` between them, so a later top-level route inherited
    the repositories prefix and matched the repositories test. `/*` inside
    a `//` comment did the same through a second door.

    THE INVARIANT TO PRESERVE IS STATEFULNESS, NOT BRANCH ORDER. An
    earlier version of this docstring credited the ordering of the checks
    below, and that is measurably wrong: swapping the literal check above
    the comment checks changes nothing, because the branches can never
    both apply at one index (`//` starts with `/`, a literal with a
    quote). What fixes it is that entering `//` mode is STICKY until the
    newline, so a quote encountered inside a comment is never a delimiter.
    Refactor the branches freely; do not flatten the mode.
    """
    code: list[str] = []      # literals AND comments blanked
    kept: list[str] = []      # only comments blanked
    mode: str | None = None   # None | '"' | "'" | '`' | '//' | '/*'
    i, n = 0, len(source)

    def emit(blank_in_code: str, blank_in_kept: str) -> None:
        code.append(blank_in_code)
        kept.append(blank_in_kept)

    while i < n:
        ch = source[i]

        if mode is None:
            # Order between these branches is arbitrary — they cannot
            # collide at one index. What matters is that the comment modes
            # are STICKY (see the docstring): once inside one, a quote is
            # just a character, so an apostrophe in English prose is inert.
            if source.startswith("//", i):
                mode, i = "//", i + 2
                emit("  ", "  ")
                continue
            if source.startswith("/*", i):
                mode, i = "/*", i + 2
                emit("  ", "  ")
                continue
            if ch in ('"', "'", "`"):
                mode, i = ch, i + 1
                emit(" ", ch)
                continue
            emit(ch, ch)
            i += 1
            continue

        if mode in ("//", "/*"):
            if mode == "//" and ch == "\n":
                mode = None
                emit("\n", "\n")
                i += 1
                continue
            if mode == "/*" and source.startswith("*/", i):
                mode, i = None, i + 2
                emit("  ", "  ")
                continue
            blank = "\n" if ch == "\n" else " "
            emit(blank, blank)
            i += 1
            continue

        # Inside a string, rune or raw string.
        if mode in ('"', "'") and ch == "\\" and i + 1 < n:
            emit("  ", source[i : i + 2])
            i += 2
            continue
        if ch == mode:
            mode = None
            emit(" ", ch)
            i += 1
            continue
        emit("\n" if ch == "\n" else " ", ch)
        i += 1

    return "".join(code), "".join(kept)


def _blank_go_noncode(source: str) -> str:
    return _scan_go(source)[0]


def _strip_line_comment(text: str) -> str:
    """Drop a trailing `//` comment, leaving literals intact.

    Used where the surviving text still has to READ correctly (the
    block-opener check), so this truncates rather than blanking.
    """
    kept = _scan_go(text)[1]
    stripped = kept.rstrip()
    return text[: len(stripped)] if len(stripped) < len(text) else text


# A trailing `func(...) {` — the shape of every chi sub-router opener
# (`r.Route("/x", func(r chi.Router) {`, `r.Group(func(r chi.Router) {`)
# and of an inline handler closure (`r.Post("/x", func(w, r) {`). In both
# cases the registration on this line is complete for our purposes and the
# unclosed `(` belongs to a block, not to a wrapped call.
_GO_BLOCK_OPENER = re.compile(r'func\s*\([^()]*\)\s*\{\s*$')


def _opens_block(text: str) -> bool:
    """True if `text` ends by opening a block — comments discounted, so a
    `{ // note` opener still counts.

    On already-joined content the comment strip can cut too much and this
    returns False, putting the block back on one logical line. That
    degrades to the pre-fix behaviour rather than to a wrong answer,
    because `added_endpoints` scans every match on a logical line.
    """
    return bool(_GO_BLOCK_OPENER.search(_strip_line_comment(text).rstrip()))


# A `chi` sub-router opener whose path prefix applies to every route
# registered inside it.
_GO_ROUTE_PREFIX = re.compile(r'\.\s*(?:Route|Mount)\s*\(\s*"([^"]*)"')

# repo_root is part of the key: the scanner's own test suite builds a
# different tmp_path per test against one module-level cache.
_PREFIX_CACHE: dict[tuple[str, str], dict[int, str]] = {}


def route_prefixes(repo_root: Path, rel_path: str) -> dict[int, str]:
    """Map 1-based line number -> the chi Route/Mount prefix in effect there.

    Read from the file ON DISK rather than reconstructed from the diff,
    because the prefix usually is not in the diff: a PR that adds one route
    inside an existing `r.Route("/api", ...)` block has the opener as a
    context line. Without this, that route reports as `/{id}` — a path no
    test will ever contain, and one that tells a reader nothing in the PR
    comment either.

    Brace counting is a cheap approximation, but not a naive one: string
    literals, rune literals, raw strings and block comments are blanked
    across the whole file first, so `w.Write([]byte("}"))` and a raw
    string holding `{"a":` no longer desynchronise the stack. What is
    still approximate is anything that puts an unbalanced brace in real
    code, which Go does not permit.

    Braces are counted on the blanked text; the Route path is read from
    the ORIGINAL line, since blanking would erase the very literal being
    extracted.
    """
    key = (str(repo_root), rel_path)
    cached = _PREFIX_CACHE.get(key)
    if cached is not None:
        return cached

    prefixes: dict[int, str] = {}
    try:
        source = (repo_root / rel_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        _PREFIX_CACHE[key] = prefixes
        return prefixes

    # split("\n"), NOT splitlines(): the latter also breaks on \v, \f, the
    # separator characters and a lone \r, which survive inside a literal
    # and are blanked outside one — so the two views would disagree about
    # which line a route is on.
    code_lines, kept_lines = (v.split("\n") for v in _scan_go(source))
    stack: list[tuple[int, str]] = []  # (brace depth when opened, prefix)
    depth = 0
    for line_no in range(1, len(code_lines) + 1):
        code = code_lines[line_no - 1]
        line = kept_lines[line_no - 1]

        opened = code.count("{")
        closed = code.count("}")
        route = _GO_ROUTE_PREFIX.search(line)
        base = "".join(p for _, p in stack)

        if route and opened > closed:
            # Opens a group. A route sharing this line is INSIDE it, so
            # the line gets the new prefix too.
            stack.append((depth, route.group(1)))
            prefixes[line_no] = base + route.group(1)
        elif route and opened and opened == closed:
            # A complete one-line group: `r.Route("/x", func(r chi.Router)
            # { r.Post("/y", h) })`. Nothing is pushed, because the block
            # is over by end of line — but its routes still live under it.
            prefixes[line_no] = base + route.group(1)
        else:
            prefixes[line_no] = base

        depth += opened - closed
        while stack and depth <= stack[-1][0]:
            stack.pop()

    _PREFIX_CACHE[key] = prefixes
    return prefixes


def join_route_path(prefix: str, path: str) -> str:
    """Join a chi Route/Mount prefix with the leaf route's own path.

    chi serves `Route("/repositories")` + `Post("/")` at
    `/repositories`, so the trailing slash a bare `/` leaf contributes is
    dropped; duplicate slashes are collapsed.
    """
    joined = re.sub(r'/{2,}', '/', (prefix or "") + (path or ""))
    if len(joined) > 1:
        joined = joined.rstrip("/")
    return joined or "/"


def added_endpoints(hunk: Hunk, repo_root: Path) -> Iterable[tuple[Endpoint, int]]:
    """Yield (endpoint, hunk_line_idx) for each mutation route in this hunk.

    The `hunk_line_idx` points to the first raw line of the registration
    (so multi-line-wrapped routes report the line where `.Post(` begins,
    not the line where the string literal happens to sit). It is used by
    `endpoint_skip_reason` to scan for a per-endpoint skip marker.

    Every match on a logical line is yielded, not just the first. A greedy
    `.*` in an anchored pattern silently reports only the LAST registration
    on a joined line, which is how `POST /api/repositories` went unchecked
    while the `DELETE` beside it was flagged.
    """
    if hunk.lang == "other":
        return
    prefixes = route_prefixes(repo_root, hunk.file) if hunk.lang == "go" else {}
    for line_no, hunk_idx, content in _iter_logical_added_lines(hunk):
        seen: set[tuple[str, str]] = set()
        for pat in ENDPOINT_PATTERNS[hunk.lang]:
            for m in pat.finditer(content):
                method, raw_path = _method_and_path(m)
                if (method, raw_path) in seen:
                    continue
                seen.add((method, raw_path))
                yield (
                    Endpoint(
                        method=method,
                        path=join_route_path(prefixes.get(line_no, ""), raw_path),
                        file=hunk.file,
                        line=line_no,
                        lang=hunk.lang,
                        raw_path=raw_path,
                    ),
                    hunk_idx,
                )


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

    The boundary check is a `search`, so an UNCHANGED route between a
    marker and the endpoint stops the lookback too. A context line is as
    real a route as an added one.
    """
    lo = max(0, endpoint_idx - _SKIP_LOOKBACK)
    patterns = ENDPOINT_PATTERNS.get(hunk.lang, [])
    for idx in range(endpoint_idx, lo - 1, -1):
        raw = hunk.lines[idx]
        if idx != endpoint_idx:
            # A different route registration boundary — do not cross it.
            if any(p.search(raw) for p in patterns):
                return None
            # So is the line that OPENS the enclosing block. A marker on
            # `r.Route("/x", func(r chi.Router) { // @skip…` describes the
            # group, not the routes nested inside it, and letting it reach
            # them is the "one marker, many endpoints" hole this window
            # exists to prevent.
            if hunk.lang == "go" and _opens_block(raw):
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


_PATH_PARAM = re.compile(r'\{[^}]*\}')


def coverage_needles(endpoint: Endpoint) -> list[str]:
    """The literal path is one needle; EVERY static segment is another.

    A Go test drives `DELETE /api/repositories/{id}` by building
    `"/api/repositories/" + id`, so the literal `{id}` appears nowhere and
    demanding it would make parameterised routes permanently uncoverable.
    The answer is to split the path on its parameters and require ALL the
    non-trivial static pieces to appear.

    Requiring only the leading piece — the first version of this — handed
    a free pass to every new route nested under an already-tested prefix.
    `POST /api/repositories/{id}/resync` reduced to `/api/repositories/`,
    which the existing DELETE test already contains, so a brand-new
    mutation endpoint went green with no test at all. Splitting on every
    parameter means `/resync` has to show up too.

    Returns an empty list when nothing non-trivial survives (`/`,
    `/{id}`), so such an endpoint is reported missing rather than matched
    by every file in the repository.

    Known limitation: this is method-blind (ISS-015). A test that only
    exercises `GET /api/things` marks a newly added `POST /api/things` as
    covered. A ratchet against forgetting, not a proof of coverage.
    """
    segments = [s for s in _PATH_PARAM.split(endpoint.path) if len(s) > 1]
    if not segments:
        return []
    return [endpoint.path, *segments]


def _line_has_segments_in_order(line: str, segments: list[str]) -> bool:
    """True if `line` contains every segment, in order, left to right."""
    at = 0
    for seg in segments:
        found = line.find(seg, at)
        if found < 0:
            return False
        at = found + len(seg)
    return True


def test_files_reference(
    paths: set[str], endpoint: Endpoint, repo_root: Path
) -> bool:
    """True if a test file carries the literal path, or builds it.

    "Builds it" means ONE LINE holds every static segment, in order.
    Matching the segments independently anywhere in the file was still a
    free pass: two unrelated comments mentioning `/api/orgs/` and
    `/members/` covered `DELETE /api/orgs/{orgID}/members/{userID}`, and a
    lone `// TODO: cover /resync one day` covered the resync route. A test
    that actually drives the route writes the path in one expression.

    The cost is idiom-sensitivity: `path.Join("/api/things", id, "x")`
    builds the URL without ever holding those pieces adjacently, so it
    reads as uncovered. That direction is the safe one — a false FAIL is
    visible and fixable, a false PASS is silent — and the failure message
    says what to do about it.
    """
    needles = coverage_needles(endpoint)
    if not needles:
        return False
    literal, segments = needles[0], needles[1:]
    for rel in paths:
        target = repo_root / rel
        if not target.exists():
            # New file in diff that was later deleted, or a rename. Skip.
            continue
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if literal in text:
            return True
        if any(_line_has_segments_in_order(ln, segments) for ln in text.splitlines()):
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
        for endpoint, hunk_idx in added_endpoints(hunk, repo_root):
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
            "(Python) that drives the endpoint. It counts when one line holds "
            "every static piece of the path in order — so for "
            "`/api/things/{id}/resync`, a line containing "
            '`\"/api/things/\" + id + \"/resync\"`. Building the path some '
            "other way (path.Join, a helper that splits it up) will not be "
            "recognised; write the literal path in the test, or use the skip "
            "marker below."
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

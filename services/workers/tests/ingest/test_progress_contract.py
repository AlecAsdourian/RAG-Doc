"""The progress contract, pinned to the code that produces it (22.1-03, S5).

`docs/api-ingestion-jobs.md`, "The progress contract", is the authority for
what a job row's `progress` column can hold: its keys, the stage that first
reports each, their types, and every skip reason the fetcher can count. This
file fails when the code and that document disagree, in either direction.

It reads the document's two TABLES, between HTML comment markers added for
the purpose (`<!-- progress-keys -->` and `<!-- skip-reasons -->`), never its
prose. No database, no network: the handler runs with `tests.ingest.fakes`'s
fakes at its edges, through `test_handler._run`, exactly as 22-05's tests
drive it.

WHAT IT DOES NOT PIN (the SUMMARY's "does NOT pin" carries these):
  - a key reported only on a path none of the fixtures below takes;
  - a skip reason produced outside `workers/fetch/archive.py` and
    `workers/fetch/filters.py`, the two files the static scan reads.
"""

from __future__ import annotations

import ast
import os
import re
from typing import Dict, List, Set, Tuple

import workers.fetch.archive as archive_module
import workers.fetch.filters as filters_module
from tests.ingest.fakes import FIXTURE_FILES
from tests.ingest.test_handler import _run
from workers.chunker import SemanticChunker
from workers.ingest import STAGES

DOC = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "docs", "api-ingestion-jobs.md")
)

#: The reasons 22.1-03's plan read from the code on 2026-10-06. The static
#: collector must find AT LEAST these, or "every collected reason is
#: documented" would pass on a collector that finds nothing.
#:
#: ⚠ A PREMISE, NOT A SECOND AUTHORITY. The list of reasons is the doc's
#: `skip-reasons` table; this set exists only to prove the collector works.
#: Renaming or removing a reason changes the code, that table and this set
#: in the same commit (the contract's stability rule).
REASONS_READ_FROM_THE_CODE = {
    # by name (`filters.classify_path`)
    "unsafe_path", "secret", "vendored", "generated", "lockfile", "unsupported",
    # by member type (`archive._member_kind`)
    "symlink", "hardlink", "special", "other",
    # in extraction
    "sparse", "unexpected_top_level", "oversize_file", "refused_by_filter", "unwritable",
    # in the walk
    "link", "binary", "non_utf8",
}


# ---------------------------------------------------------------------
# Reading the document's tables
# ---------------------------------------------------------------------


def _table(marker: str) -> List[List[str]]:
    """The data rows of the markdown table between `<!-- marker -->` and `<!-- /marker -->`.

    Each row is its cells, stripped. The header row and the separator row
    are dropped. Exactly one marked block must exist.
    """
    with open(DOC, encoding="utf-8") as handle:
        text = handle.read()
    pattern = re.compile(
        r"<!-- " + re.escape(marker) + r" -->(.*?)<!-- /" + re.escape(marker) + r" -->", re.S
    )
    blocks = pattern.findall(text)
    assert len(blocks) == 1, f"{DOC} must hold exactly one <!-- {marker} --> block; found {len(blocks)}"
    rows = [line.strip() for line in blocks[0].splitlines() if line.strip().startswith("|")]
    assert len(rows) >= 3, f"the <!-- {marker} --> block holds no table rows"
    cells = [[cell.strip() for cell in row.strip("|").split("|")] for row in rows]
    return cells[2:]  # header, separator


def _name(cell: str) -> str:
    """`` `files_parsed` `` -> `files_parsed`. A cell that is not one code span is a doc bug."""
    match = re.fullmatch(r"`([a-z0-9_]+)`", cell)
    assert match, f"expected a single code span naming a key or a reason, got {cell!r}"
    return match.group(1)


def documented_keys() -> Dict[str, Tuple[str, str]]:
    """key -> (type cell, first-reported stage), from the keys table."""
    keys: Dict[str, Tuple[str, str]] = {}
    for row in _table("progress-keys"):
        key, typ, first = _name(row[0]), row[1], _name(row[2])
        assert key not in keys, f"key {key!r} is documented twice"
        keys[key] = (typ, first)
    return keys


def documented_reasons() -> Set[str]:
    reasons: List[str] = [_name(row[0]) for row in _table("skip-reasons")]
    assert len(reasons) == len(set(reasons)), f"a reason is documented twice: {reasons}"
    return set(reasons)


# ---------------------------------------------------------------------
# Collecting what the code produces
# ---------------------------------------------------------------------


def collect_skip_reasons() -> Set[str]:
    """Every skip reason `archive.py` and `filters.py` can produce, read with `ast`.

    Three shapes, which between them are every way a reason is minted there:
      - a string subscript of a counter named `skipped` (`skipped["link"] += 1`);
      - the second argument of `Verdict(False, ...)`, which the
        verdict-forwarding sites (`skipped[verdict.reason]`) pass through;
      - a string `return` of `_member_kind`, which `skipped[_member_kind(m)]`
        forwards.
    """
    found: Set[str] = set()
    for module in (archive_module, filters_module):
        with open(module.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=module.__file__)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and node.value.id == "skipped"
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
            ):
                found.add(node.slice.value)
            elif isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                if (
                    name == "Verdict"
                    and len(node.args) >= 2
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value is False
                    and isinstance(node.args[1], ast.Constant)
                    and isinstance(node.args[1].value, str)
                ):
                    found.add(node.args[1].value)
            elif isinstance(node, ast.FunctionDef) and node.name == "_member_kind":
                for inner in ast.walk(node):
                    if (
                        isinstance(inner, ast.Return)
                        and isinstance(inner.value, ast.Constant)
                        and isinstance(inner.value.value, str)
                    ):
                        found.add(inner.value.value)
    return found


class _OneFileRaises:
    """The real chunker, except that one file raises (`parse_errors` = 1)."""

    def __init__(self) -> None:
        self._real = SemanticChunker()

    def chunk_file(self, path, content, language):
        if path.endswith("billing.py"):
            raise ValueError("a chunker bug on one file")
        return self._real.chunk_file(path, content, language)


def _runs(tmp_path) -> Dict[str, List[Tuple[str, dict]]]:
    """The fixtures' report sequences: name -> [(stage, progress), ...].

    The happy path (whose tree carries a committed `.env`, so `skipped`
    holds `secret`), one file raising in the chunker, and the chunk cap.
    """
    outcomes = {
        "happy": _run(tmp_path / "happy"),
        "one_file_raises": _run(tmp_path / "flaky", chunker=_OneFileRaises()),
        "chunk_cap": _run(tmp_path / "cap", max_chunks=2),
    }
    assert outcomes["happy"].error is None, repr(outcomes["happy"].error)
    assert outcomes["one_file_raises"].error is None, repr(outcomes["one_file_raises"].error)
    assert outcomes["chunk_cap"].error is not None, "premise: the chunk cap must stop the run"
    assert ".env" in FIXTURE_FILES, "premise: the fixture tree carries a secret-looking file"
    return {name: list(outcome.ctx.reports) for name, outcome in outcomes.items()}


# ---------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------


def test_every_key_the_handler_reports_is_documented_and_first_reported_where_the_doc_says(tmp_path):
    doc = documented_keys()
    runs = _runs(tmp_path)

    reported: Set[str] = set()
    for name, reports in runs.items():
        first_seen: Dict[str, str] = {}
        for stage, progress in reports:
            assert stage in STAGES, f"{name}: stage {stage!r} is not in the vocabulary"
            assert isinstance(progress, dict), f"{name}: the {stage!r} report carried {progress!r}"
            for key in progress:
                first_seen.setdefault(key, stage)
        for key, stage in first_seen.items():
            assert key in doc, f"{name}: the handler reports {key!r}, which the doc does not list"
            assert stage == doc[key][1], (
                f"{name}: {key!r} is first reported at {stage!r}; the doc says {doc[key][1]!r}"
            )
        reported |= set(first_seen)

    assert reported == set(doc), (
        f"documented but never reported by any fixture: {sorted(set(doc) - reported)}; "
        f"reported but undocumented: {sorted(reported - set(doc))}"
    )

    # The first report is `fetch` with `{}`, which is what the doc's `{}` means.
    for name, reports in runs.items():
        assert reports[0] == ("fetch", {}), f"{name}: the first report was {reports[0]!r}"


def test_every_value_has_the_documented_type(tmp_path):
    doc = documented_keys()
    happy = _runs(tmp_path)["happy"]
    stage, last = happy[-1]
    assert stage == "store", "premise: the completed run's last report is `store`"
    assert set(last) == set(doc), "premise: a completed run's last report carries every key"

    for key, value in last.items():
        typ = doc[key][0]
        if key == "skipped":
            assert typ.startswith("object"), f"the doc types `skipped` as {typ!r}"
            assert isinstance(value, dict) and value, f"`skipped` is {value!r}"
            for reason, count in value.items():
                assert isinstance(reason, str), reason
                assert type(count) is int and count >= 0, (reason, count)
        else:
            assert typ == "non-negative integer", f"the doc types {key!r} as {typ!r}"
            assert type(value) is int and value >= 0, (key, value)

    assert last["skipped"].get("secret", 0) >= 1, "premise: the `.env` was counted as `secret`"


def test_every_skip_reason_the_fetcher_can_count_is_documented_and_no_other():
    collected = collect_skip_reasons()

    # ⚠ THE PREMISE FIRST. A collector that finds nothing would make the
    # equality below a comparison of the doc with an empty set, and a
    # collector that finds a subset would let an undocumented reason through.
    # A reason RENAMED or REMOVED in the code lands here too, before the
    # equality: that is a breaking change to the contract, and it changes
    # this list, the doc's table and the code in one commit.
    missing = REASONS_READ_FROM_THE_CODE - collected
    assert not missing, (
        f"the static collector did not find reasons the code is known to produce: "
        f"{sorted(missing)}; either the collector is broken (and nothing below would "
        "mean anything), or a reason was renamed or removed in the code, which is a "
        "breaking change to docs/api-ingestion-jobs.md's skip-reasons table"
    )

    doc = documented_reasons()
    assert collected == doc, (
        f"producible but undocumented: {sorted(collected - doc)}; "
        f"documented but produced by nothing: {sorted(doc - collected)}"
    )

"""`scripts/rag_benchmarks/tripwire.py` (QD1), and `scoring.aggregate`, the one formula (22.2-01).

Hand-made records: no database, no OpenAI. The fixtures compute ranks with
their own plain rule, not scoring.py's, so a record the tripwire accepts was
not built by the code it is judged with.
"""

from __future__ import annotations

import copy
import gzip
import importlib.util
import json
import math
import pathlib
import sys
from typing import List, Optional

import pytest

_RAG = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "rag_benchmarks"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _RAG / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


tripwire = _load("tripwire")
scoring = sys.modules["scoring"]
compare_runs = sys.modules["compare_runs"]

APP_ROLE = {"current_user": "rag_doc_app", "session_user": "scratch", "rolsuper": False, "rolbypassrls": False}
HASH = "0" * 64
TOP_K = 10


def _header(digest: str = "a" * 64) -> dict:
    return {
        "record": "run", "corpus": "toy", "set": "all", "top_k": TOP_K, "boost_config": None,
        "exact_paths": True, "embedding_model": "text-embedding-ada-002", "vector_backend": "pgvector",
        "connections": {"fts": dict(APP_ROLE), "vector": dict(APP_ROLE)},
        "chunk_set_digest": digest, "chunk_rows": 10, "chunker_version": "0123456789abcdef",
    }


def _question(qid: str, set_name: str, file_rank: Optional[int], symbol: Optional[str] = None,
              symbol_rank: Optional[int] = None) -> dict:
    """A record whose final list puts the answer's file at `file_rank` (and the
    answering symbol at `symbol_rank`), ranks written by this fixture's own rule."""
    path = f"pkg/{qid}.py"
    top = [{"chunk_id": f"{qid}-{i}", "file_path": f"pkg/other{i}.py", "breadcrumb": "other"}
           for i in range(1, TOP_K + 1)]
    if file_rank:
        top[file_rank - 1] = {"chunk_id": f"{qid}-f", "file_path": path, "breadcrumb": "unrelated"}
    if symbol_rank:
        top[symbol_rank - 1] = {"chunk_id": f"{qid}-s", "file_path": path, "breadcrumb": f"Mod.{symbol}"}
    first_file = next((i for i, e in enumerate(top, 1) if e["file_path"] == path), None)
    first_symbol = next((i for i, e in enumerate(top, 1)
                         if e["file_path"] == path and e["breadcrumb"] == f"Mod.{symbol}"), None) if symbol else None
    return {
        "record": "question", "id": qid, "set": set_name, "question": f"what is {qid}?", "path": path,
        "symbol": symbol, "file_rank": first_file, "symbol_rank": first_symbol, "top_hit": top[0]["file_path"],
        "error": None, "query_vector_sha256": HASH,
        "trace": {"fts": [], "vector": [], "fused": [], "boosted": [], "top": top},
    }


def _baseline() -> List[dict]:
    """16 tuning questions (one at rank 7), and 3 holdout questions naming a symbol."""
    rows = [_question(f"t{i:02d}", "tuning", 1 if i % 2 else None) for i in range(1, 16)]
    rows.append(_question("t16", "tuning", 7))
    rows += [_question(f"h{i}", "holdout", 2, symbol=f"run{i}", symbol_rank=2) for i in range(1, 4)]
    return rows


def _write(directory: pathlib.Path, prefix: str, header: dict, records: List[dict]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(r) for r in [header, *records]) + "\n"
    if prefix.endswith(".gz"):
        with gzip.open(directory / f"{prefix[:-3]}-toy.jsonl.gz", "wt", encoding="utf-8") as fh:
            fh.write(text)
    else:
        (directory / f"{prefix}-toy.jsonl").write_text(text, encoding="utf-8")


def _run(tmp_path, before, after, before_header=None, after_header=None, capsys=None):
    _write(tmp_path / "b", "before", before_header or _header(), before)
    _write(tmp_path / "a", "after", after_header or _header("b" * 64), after)
    code = tripwire.main(["--before", str(tmp_path / "b"), "--after", str(tmp_path / "a"), "--corpora", "toy"])
    return code, capsys.readouterr().out


def _replace(records: List[dict], qid: str, new: dict) -> List[dict]:
    return [new if r["id"] == qid else r for r in records]


def test_nothing_fell_gives_0(tmp_path, capsys):
    code, out = _run(tmp_path, _baseline(), _baseline(), capsys=capsys)
    assert code == 0, out
    assert "TRIPWIRE: quiet (0 number(s) fell)" in out
    assert "FELL" not in out
    # Every set x level x metric is reported: tuning (file), holdout (file and symbol), 3 metrics each.
    assert out.count("toy        tuning") == 3 and out.count("toy        holdout") == 6


def test_one_mrr_fall_of_0_0011_gives_1(tmp_path, capsys):
    after = _replace(_baseline(), "t16", _question("t16", "tuning", 8))
    code, out = _run(tmp_path, _baseline(), after, capsys=capsys)
    assert code == 1, out
    fell = [line for line in out.splitlines() if line.rstrip().endswith("FELL")]
    assert len(fell) == 1 and "MRR@10" in fell[0] and "-0.0011" in fell[0], fell
    assert "worsened  t16        tuning   file   #7->#8" in out


def test_a_rise_only_gives_0(tmp_path, capsys):
    after = _replace(_baseline(), "t16", _question("t16", "tuning", 1))
    code, out = _run(tmp_path, _baseline(), after, capsys=capsys)
    assert code == 0, out
    assert "rose" in out and "improved  t16" in out


def test_a_fallen_count_is_one_question_fewer(tmp_path, capsys):
    after = _replace(_baseline(), "h1", _question("h1", "holdout", None, symbol="run1", symbol_rank=None))
    code, out = _run(tmp_path, _baseline(), after, capsys=capsys)
    assert code == 1, out
    assert "fell: toy holdout file recall@10" in out and "fell: toy holdout symbol recall@10" in out


def test_gzipped_records_are_read(tmp_path, capsys):
    _write(tmp_path / "b", "before.gz", _header(), _baseline())
    _write(tmp_path / "a", "after.gz", _header(), _baseline())
    code = tripwire.main(["--before", str(tmp_path / "b"), "--after", str(tmp_path / "a"), "--corpora", "toy"])
    assert code == 0, capsys.readouterr().out


def test_equal_digests_are_flagged_as_a_control(tmp_path, capsys):
    code, out = _run(tmp_path, _baseline(), _baseline(), after_header=_header(), capsys=capsys)
    assert code == 0
    assert "identical chunk set" in out
    code, out = _run(tmp_path, _baseline(), _baseline(), capsys=capsys)
    assert "identical chunk set" not in out


def _damage(kind: str):
    """(before records, after records, before header, after header) with one defect."""
    before, after = _baseline(), _baseline()
    bh, ah = _header(), _header("b" * 64)
    target = after[0]
    if kind == "ids":
        after = after[1:]
    elif kind == "text":
        target["question"] = "a different question?"
    elif kind == "rank-edited":
        target["file_rank"] = 3  # the final list still puts it at #1
    elif kind == "superuser":
        ah["connections"]["vector"] = {**APP_ROLE, "current_user": "scratch", "rolsuper": True}
    elif kind == "bypassrls":
        bh["connections"]["fts"] = {**APP_ROLE, "rolbypassrls": True}
    elif kind == "unrecorded":
        del ah["connections"]
    elif kind == "failed-query":
        target["error"] = "Retrieval failed in vector search: RuntimeError: 401"
    elif kind == "hash":
        target["query_vector_sha256"] = "f" * 64
    elif kind == "model":
        ah["embedding_model"] = "text-embedding-3-small"
    elif kind == "top_k":
        ah["top_k"] = 5
    elif kind == "boost_config":
        ah["boost_config"] = {"breadcrumb_match_boost": 1.0}
    elif kind == "exact_paths":
        ah["exact_paths"] = False
    elif kind == "no-digest":
        del bh["chunk_set_digest"]
    elif kind == "set":
        target["set"] = "holdout"
    elif kind == "path":
        target["path"] = "pkg/elsewhere.py"
    elif kind == "symbol":
        named = next(r for r in after if r.get("symbol"))
        named["symbol"] = "another_symbol"
    elif kind == "malformed-top":
        target["trace"]["top"] = None
    return before, after, bh, ah


REFUSALS = {
    "ids": "question ids differ", "text": "question differs", "rank-edited": "not what its final list gives",
    "superuser": "rolsuper=True", "bypassrls": "rolbypassrls=True", "unrecorded": "records no measuring connection",
    "failed-query": "not a measurement", "hash": "query-vector hash differs", "model": "differ in embedding_model",
    "top_k": "differ in top_k", "boost_config": "differ in boost_config", "exact_paths": "differ in exact_paths",
    "no-digest": "no chunk_set_digest",
    # Deviation 3's refusals: a question measured against another target (review B, finding 1).
    "set": "question's set differs", "path": "question's path differs", "symbol": "question's symbol differs",
    # A malformed record is refused, never read as the tripwire firing (review A, finding 7).
    "malformed-top": "final list is not a list of results",
}


def test_input_the_checks_did_not_foresee_exits_2_not_1(tmp_path, capsys):
    """Exit 1 means a number fell; nothing else may produce it (review A, finding 7)."""
    # Equal on both sides, so no header refusal catches it; only the reader can.
    bh, ah = _header(), _header("b" * 64)
    bh["top_k"] = ah["top_k"] = "five"
    code, out = _run(tmp_path, _baseline(), _baseline(), before_header=bh, after_header=ah, capsys=capsys)
    assert code == 2, out
    assert "REFUSED" in out and "TRIPWIRE" not in out


@pytest.mark.parametrize("kind", sorted(REFUSALS))
def test_each_refusal_gives_2_and_compares_nothing(tmp_path, capsys, kind):
    before, after, bh, ah = _damage(kind)
    code, out = _run(tmp_path, before, after, before_header=bh, after_header=ah, capsys=capsys)
    assert code == 2, out
    assert "REFUSED: nothing was compared." in out
    assert REFUSALS[kind] in out, out
    assert "TRIPWIRE" not in out and "MRR@" not in out


def test_the_hash_refusal_names_the_question(tmp_path, capsys):
    before, after, bh, ah = _damage("hash")
    code, out = _run(tmp_path, before, after, before_header=bh, after_header=ah, capsys=capsys)
    assert code == 2 and "t01: the query-vector hash differs" in out


# ---------------------------------------------------------------------------
# scoring.aggregate: one formula, four readers
# ---------------------------------------------------------------------------

RECORDS_2203 = (pathlib.Path(__file__).resolve().parents[3] / ".planning" / "phases"
                / "22-repository-clone-ingestion" / "22-03-records")


def _old_harness_score(ranks):
    """rag_quality_harness._score, copied from 4000acc (test-local reference)."""
    found = [r for r in ranks if r]
    total = len(ranks)
    return {"questions": total, "found": len(found), "rank1": sum(1 for r in found if r == 1),
            "mrr": (sum(1.0 / r for r in found) / total) if total else 0.0}


def _old_compare_runs_score(ranks):
    """compare_runs.score, copied from 4000acc (test-local reference)."""
    found = [r for r in ranks if r]
    total = len(ranks)
    return {
        "questions": total,
        "found": len(found),
        "rank1": sum(1 for r in found if r == 1),
        "mrr": (sum(1.0 / r for r in found) / total) if total else 0.0,
    }


# The old copies and the committed 22-03 summaries added floats with the
# built-in `sum`, whose result changed in Python 3.12 (compensated summation),
# so their MRR can differ from scoring.aggregate's correctly rounded `fsum` in
# the last bits, by interpreter. The counts must match exactly; the MRR within
# this bound (ISS-041). It is tight on both sides:
#   - above the rounding it absorbs: left-to-right summation of n <= 45 terms
#     of at most 1 errs by at most about (n - 1) * 2**-53 * n ~ 2.2e-13 in the
#     sum, so ~5e-15 in the mean; on 22-03's records the measured worst is
#     5.6e-17 (one ULP, mealie file; PR #67, review B). 1e-12 is ~200x the
#     analytical worst;
#   - far below any real difference: a rank step is 0.05 / 45 ~ 1.1e-3 and
#     M2's allowance is 1e-9, so a bound of 1e-12 cannot hide either.
SUM_ROUNDING = 1e-12


def _agrees(new: dict, old: dict) -> bool:
    return ({k: v for k, v in new.items() if k != "mrr"} == {k: v for k, v in old.items() if k != "mrr"}
            and math.isclose(new["mrr"], old["mrr"], rel_tol=0.0, abs_tol=SUM_ROUNDING))


@pytest.mark.parametrize("corpus", ["self", "miniflux", "mealie"])
def test_aggregate_gives_what_both_old_copies_gave_on_22_03s_records(corpus):
    _, records = compare_runs.load_run(RECORDS_2203 / f"pgvector-{corpus}.jsonl.gz")
    sets = {}
    for rec in records.values():
        sets.setdefault(rec["set"], []).append(rec)
    assert len(sets) >= 2
    for set_name, rows in [*sets.items(), ("all", list(records.values()))]:
        for level_ranks in ([r["file_rank"] for r in rows], [r["symbol_rank"] for r in rows if r.get("symbol")]):
            new = scoring.aggregate(level_ranks)
            assert _agrees(new, _old_harness_score(level_ranks)), set_name
            assert _agrees(new, _old_compare_runs_score(level_ranks)), set_name
    summary = json.loads((RECORDS_2203 / f"pgvector-{corpus}.summary.json").read_text(encoding="utf-8"))["summary"]
    rows = list(records.values())
    assert _agrees(scoring.aggregate([r["file_rank"] for r in rows]), summary["file"])
    assert _agrees(scoring.aggregate([r["symbol_rank"] for r in rows if r.get("symbol")]), summary["symbol"])


def test_the_mrr_is_correctly_rounded_on_every_python():
    """The MRR is fsum's, exactly: the same double on 3.11 and 3.12 (ISS-041)."""
    ranks = [1, 3, None, 7, 2, 9, 3, 11, None, 6, 13, 1, 17, 4, 19] * 3
    found = [r for r in ranks if r]
    assert scoring.aggregate(ranks)["mrr"] == math.fsum(1.0 / r for r in found) / len(ranks)
    # Order cannot change a correctly rounded sum.
    assert scoring.aggregate(list(reversed(ranks)))["mrr"] == scoring.aggregate(ranks)["mrr"]


def test_the_aggregate_adds_with_fsum(monkeypatch):
    """Structural, so it fails on every Python if the aggregate goes back to the
    built-in `sum`: on 3.12+ `sum` is compensated and agrees with `fsum` on most
    inputs, so the value test above catches a revert only on 3.11, which CI does
    not run (PR #67, review B, m1)."""
    calls = []
    real_fsum = math.fsum

    def recording_fsum(values):
        values = list(values)
        calls.append(values)
        return real_fsum(values)

    monkeypatch.setattr(scoring.math, "fsum", recording_fsum)
    result = scoring.aggregate([1, None, 4, 2])
    assert calls == [[1.0, 0.25, 0.5]], "the reciprocal ranks are added by math.fsum"
    assert result["mrr"] == real_fsum([1.0, 0.25, 0.5]) / 4


def test_every_reader_calls_the_one_aggregate():
    spec = importlib.util.spec_from_file_location("rag_quality_harness_for_aggregate",
                                                  _RAG.parent / "rag_quality_harness.py")
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)
    assert harness.aggregate is scoring.aggregate
    assert compare_runs.aggregate is scoring.aggregate
    assert tripwire.aggregate is scoring.aggregate
    assert not hasattr(harness, "_score") and not hasattr(compare_runs, "score"), "no copy of the formula is left"


def test_aggregate_counts_a_miss_as_zero():
    assert scoring.aggregate([1, None, 2, None]) == {"questions": 4, "found": 2, "rank1": 1, "mrr": 0.375}
    assert scoring.aggregate([]) == {"questions": 0, "found": 0, "rank1": 0, "mrr": 0.0}


def test_the_fixture_records_are_valid_for_compare_runs_reader(tmp_path):
    """The tripwire reads records with compare_runs.load_run; the fixtures pass it."""
    _write(tmp_path, "before", _header(), _baseline())
    header, questions = compare_runs.load_run(tmp_path / "before-toy.jsonl")
    assert header["chunk_set_digest"] and len(questions) == 19
    assert copy.deepcopy(questions["t16"])["file_rank"] == 7

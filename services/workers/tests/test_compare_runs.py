"""`scripts/rag_benchmarks/compare_runs.py`, the arbiter of the 22-03 equivalence gate.

Hand-made records: one difference of each class the rule names, (a), (b) and
(c), plus one UNEXPLAINED, plus one question that does not differ. The script
must classify all four correctly and exit non-zero. A second pair of records
differs only in one query-vector hash, and the script must refuse it before
comparing anything.

The fixtures compute fusion with their own plain RRF, not the script's, so a
mutation of the script's recomputation (step 0 of the rule) is caught here.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
from typing import Dict, List, Optional, Tuple

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "rag_benchmarks" / "compare_runs.py"
_spec = importlib.util.spec_from_file_location("compare_runs", _SCRIPT)
compare_runs = importlib.util.module_from_spec(_spec)
sys.modules["compare_runs"] = compare_runs
_spec.loader.exec_module(compare_runs)

TOP_K = 2
K = 60


def _leg(items: List[Tuple[str, float]]) -> List[dict]:
    return [
        {"chunk_id": cid, "file_path": f"{cid}.py", "breadcrumb": "", "chunk_type": "function", "score": s}
        for cid, s in items
    ]


def _fuse(fts: List[dict], vector: List[dict]) -> List[dict]:
    """Reciprocal rank fusion written independently of the script (and of rrf_fusion.py)."""
    scores: Dict[str, float] = {}
    sources: Dict[str, List[str]] = {}
    for name, leg in (("fts", fts), ("vector", vector)):
        for rank, entry in enumerate(leg, 1):
            scores[entry["chunk_id"]] = scores.get(entry["chunk_id"], 0.0) + 1.0 / (K + rank)
            sources.setdefault(entry["chunk_id"], []).append(name)
    ordered = sorted(scores, key=lambda cid: scores[cid], reverse=True)
    return [{"chunk_id": cid, "rrf_score": scores[cid], "sources": sources[cid]} for cid in ordered]


def _record(
    qid: str,
    question_set: str,
    fts: List[Tuple[str, float]],
    vector: List[Tuple[str, float]],
    answer: str,
    vector_hash: str = "0" * 64,
    multipliers: Optional[Dict[str, float]] = None,
) -> dict:
    fts_leg, vector_leg = _leg(fts), _leg(vector)
    fused = _fuse(fts_leg, vector_leg)
    boosted = [
        {
            "chunk_id": e["chunk_id"],
            "rrf_score": e["rrf_score"],
            "boost_multiplier": (multipliers or {}).get(e["chunk_id"], 1.0),
            "boosted_score": e["rrf_score"] * (multipliers or {}).get(e["chunk_id"], 1.0),
        }
        for e in fused
    ]
    boosted.sort(key=lambda e: e["boosted_score"], reverse=True)
    top = [
        {"chunk_id": e["chunk_id"], "file_path": f"{e['chunk_id']}.py", "breadcrumb": "", "score": e["boosted_score"]}
        for e in boosted[:TOP_K]
    ]
    file_rank = next((i for i, e in enumerate(top, 1) if e["file_path"] == f"{answer}.py"), None)
    return {
        "record": "question",
        "id": qid,
        "set": question_set,
        "question": f"question {qid}",
        "path": f"{answer}.py",
        "symbol": None,
        "file_rank": file_rank,
        "symbol_rank": None,
        "top_hit": top[0]["file_path"] if top else "(no results)",
        "error": None,
        "query_vector_sha256": vector_hash,
        "trace": {"fts": fts_leg, "vector": vector_leg, "fused": fused, "boosted": boosted, "top": top},
    }


APP_ROLE = {"current_user": "rag_doc_app", "session_user": "isolation", "rolsuper": False, "rolbypassrls": False}
MODEL = "text-embedding-ada-002"


def _header(backend: str) -> dict:
    return {
        "record": "run",
        "corpus": "toy",
        "commit": "harness",
        "repository_id": "repo-1",
        "set": "all",
        "top_k": TOP_K,
        "boost_config": None,
        "exact_paths": True,
        "embedding_model": MODEL,
        "vector_backend": backend,
        "connections": {"fts": dict(APP_ROLE), **({"vector": dict(APP_ROLE)} if backend == "pgvector" else {})},
    }


def _exact(question_ids: Dict[str, List[Tuple[str, float]]], hashes: Dict[str, str]) -> dict:
    return {
        "corpus": "toy",
        "repository_id": "repo-1",
        "model": MODEL,
        "connection": dict(APP_ROLE),
        "questions": {
            qid: {
                "query_vector_sha256": hashes.get(qid, "0" * 64),
                "top": [{"chunk_id": cid, "distance": 1.0 - s} for cid, s in items],
                "tail_ties": [],
                "tail_complete": True,
            }
            for qid, items in question_ids.items()
        },
    }


# Every chunk but D has a Qdrant point; D is a duplicate-content chunk (class (a)).
QDRANT_IDS = {"A", "B", "C", "F"}


def _write(records_dir: pathlib.Path, baseline: List[dict], candidate: List[dict], exact: dict) -> None:
    (records_dir / "baseline-toy.jsonl").write_text(
        "\n".join(json.dumps(r) for r in [_header("qdrant"), *baseline]) + "\n", encoding="utf-8"
    )
    (records_dir / "pgvector-toy.jsonl").write_text(
        "\n".join(json.dumps(r) for r in [_header("pgvector"), *candidate]) + "\n", encoding="utf-8"
    )
    (records_dir / "qdrant_ids-toy.json").write_text(
        json.dumps({"corpus": "toy", "repository_id": "repo-1", "count": len(QDRANT_IDS),
                    "postgres_chunks": len(QDRANT_IDS) + 1, "ids": sorted(QDRANT_IDS)}),
        encoding="utf-8",
    )
    (records_dir / "exact-toy.json").write_text(json.dumps(exact), encoding="utf-8")


def _cases() -> Tuple[List[dict], List[dict], dict]:
    """One question per class, plus one that does not differ."""
    baseline, candidate, exact = [], [], {}

    # (a): D, which has no Qdrant point, enters the pgvector top list and pushes
    # the answer B out of the top-2. Everything else agrees.
    baseline.append(_record("q-a", "tuning", [], [("A", 0.9), ("B", 0.8), ("C", 0.7)], "B"))
    candidate.append(_record("q-a", "tuning", [], [("A", 0.9), ("D", 0.85), ("B", 0.8), ("C", 0.7)], "B"))
    exact["q-a"] = [("A", 0.9), ("D", 0.85), ("B", 0.8), ("C", 0.7)]

    # (b): Qdrant missed B (approximate search); pgvector matches exact search.
    baseline.append(_record("q-b", "holdout", [], [("A", 0.9), ("C", 0.7), ("F", 0.6)], "B"))
    candidate.append(_record("q-b", "holdout", [], [("A", 0.9), ("B", 0.8), ("C", 0.7)], "B"))
    exact["q-b"] = [("A", 0.9), ("B", 0.8), ("C", 0.7)]

    # (c): B and C are tied in the vector leg (identical similarity); the two
    # stores order the tie differently, and the top-2 cut falls between them.
    baseline.append(_record("q-c", "confirm", [("A", 0.5)], [("A", 0.8), ("C", 0.7), ("B", 0.7)], "B"))
    candidate.append(_record("q-c", "confirm", [("A", 0.5)], [("A", 0.8), ("B", 0.7), ("C", 0.7)], "B"))
    exact["q-c"] = [("A", 0.8), ("B", 0.7), ("C", 0.7)]

    # UNEXPLAINED: the pgvector leg ranks C above B with distinguishable scores,
    # against both Qdrant and exact search. No class covers pgvector missing.
    baseline.append(_record("q-u", "tuning", [], [("A", 0.9), ("B", 0.8), ("C", 0.7)], "B"))
    candidate.append(_record("q-u", "tuning", [], [("A", 0.9), ("C", 0.7), ("B", 0.65)], "B"))
    exact["q-u"] = [("A", 0.9), ("B", 0.8), ("C", 0.7)]

    # No difference: must not appear in the table.
    baseline.append(_record("q-same", "tuning", [("B", 0.4)], [("A", 0.9), ("B", 0.8)], "B"))
    candidate.append(_record("q-same", "tuning", [("B", 0.4)], [("A", 0.9), ("B", 0.8)], "B"))
    exact["q-same"] = [("A", 0.9), ("B", 0.8)]

    return baseline, candidate, _exact(exact, {})


def _row(output: str, qid: str) -> str:
    lines = [line for line in output.splitlines() if len(line.split()) > 1 and line.split()[1] == qid]
    assert len(lines) == 1, f"{qid} must appear exactly once in the table:\n{output}"
    return lines[0]


def _class_of(output: str, qid: str) -> str:
    return _row(output, qid).split()[5]


def test_one_difference_of_each_class_is_classified_and_the_gate_fails(tmp_path, capsys):
    baseline, candidate, exact = _cases()
    _write(tmp_path, baseline, candidate, exact)

    code = compare_runs.main(["--records", str(tmp_path), "--corpora", "toy"])
    out = capsys.readouterr().out

    assert code == 1, out
    assert _class_of(out, "q-a") == "a"
    assert _class_of(out, "q-b") == "b"
    assert _class_of(out, "q-c") == "c"
    assert _class_of(out, "q-u") == "UNEXPLAINED"
    assert "q-same" not in out, "a question whose ranks agree must not be listed"
    assert "(a)=1   (b)=1   (c)=1   UNEXPLAINED=1" in out
    assert "VERDICT: FAIL (1 UNEXPLAINED)" in out
    # The ranks on both sides are shown, baseline -> candidate.
    assert "#2->MISS" in _row(out, "q-a")
    assert "MISS->#2" in _row(out, "q-b")


def test_the_gate_passes_with_zero_unexplained(tmp_path, capsys):
    baseline, candidate, exact = _cases()
    keep = {"q-a", "q-b", "q-c", "q-same"}
    baseline = [r for r in baseline if r["id"] in keep]
    candidate = [r for r in candidate if r["id"] in keep]
    _write(tmp_path, baseline, candidate, exact)

    code = compare_runs.main(["--records", str(tmp_path), "--corpora", "toy"])
    out = capsys.readouterr().out

    assert code == 0, out
    assert "UNEXPLAINED=0" in out
    assert "VERDICT: PASS (0 UNEXPLAINED)" in out
    # Aggregates are reported for both sides, and not judged.
    assert "qdrant" in out and "pgvector" in out


def test_a_differing_query_vector_hash_is_refused_before_anything_is_compared(tmp_path, capsys):
    baseline, candidate, exact = _cases()
    for rec in candidate:
        if rec["id"] == "q-a":
            rec["query_vector_sha256"] = "f" * 64
    _write(tmp_path, baseline, candidate, exact)

    code = compare_runs.main(["--records", str(tmp_path), "--corpora", "toy"])
    out = capsys.readouterr().out

    assert code == 2, out
    assert "REFUSED" in out and "q-a" in out and "hash differs" in out
    assert "q-b" not in out and "VERDICT" not in out, "nothing may be compared after a refusal"


def test_a_superuser_measurement_is_refused(tmp_path, capsys):
    baseline, candidate, exact = _cases()
    _write(tmp_path, baseline, candidate, exact)
    path = tmp_path / "pgvector-toy.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    header["connections"]["fts"]["rolsuper"] = True
    path.write_text("\n".join([json.dumps(header), *lines[1:]]) + "\n", encoding="utf-8")

    code = compare_runs.main(["--records", str(tmp_path), "--corpora", "toy"])
    out = capsys.readouterr().out

    assert code == 2, out
    assert "bypasses row-level security" in out and "VERDICT" not in out


def test_a_duplicated_question_record_is_refused(tmp_path, capsys):
    """A second record for the same id must not silently replace the first.

    Reviewer B reproduced the hole: a second `q-a` baseline record that agreed
    with the candidate made the (a) row vanish, exit 0, PASS.
    """
    baseline, candidate, exact = _cases()
    agreeing = next(r for r in candidate if r["id"] == "q-a")
    baseline.append(dict(agreeing))
    _write(tmp_path, baseline, candidate, exact)

    code = compare_runs.main(["--records", str(tmp_path), "--corpora", "toy"])
    out = capsys.readouterr().out

    assert code == 2, out
    assert "REFUSED" in out and "q-a is recorded twice" in out
    assert "VERDICT" not in out


@pytest.mark.parametrize("connections", [None, {}, {"vector": {"current_user": "rag_doc_app", "session_user": "isolation", "rolsuper": False, "rolbypassrls": False}}, {"fts": {"current_user": "rag_doc_app"}}])
def test_a_run_whose_measuring_role_is_unknown_is_refused(tmp_path, capsys, connections):
    """No connections, an empty map, a map without the keyword leg's, or an identity
    that does not state rolsuper and rolbypassrls: the role is unknown, so refused."""
    baseline, candidate, exact = _cases()
    _write(tmp_path, baseline, candidate, exact)
    path = tmp_path / "baseline-toy.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    if connections is None:
        del header["connections"]
    else:
        header["connections"] = connections
    path.write_text("\n".join([json.dumps(header), *lines[1:]]) + "\n", encoding="utf-8")

    code = compare_runs.main(["--records", str(tmp_path), "--corpora", "toy"])
    out = capsys.readouterr().out

    assert code == 2, out
    assert "REFUSED" in out and "VERDICT" not in out
    assert "connection" in out


def test_a_pgvector_run_must_record_the_vector_legs_connection(tmp_path, capsys):
    baseline, candidate, exact = _cases()
    _write(tmp_path, baseline, candidate, exact)
    path = tmp_path / "pgvector-toy.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    assert header["vector_backend"] == "pgvector" and "vector" in header["connections"]
    del header["connections"]["vector"]
    path.write_text("\n".join([json.dumps(header), *lines[1:]]) + "\n", encoding="utf-8")

    code = compare_runs.main(["--records", str(tmp_path), "--corpora", "toy"])
    out = capsys.readouterr().out

    assert code == 2, out
    assert "records no vector connection" in out


# The exact list is the ground truth for class (b), so it is held to the same
# standard as a run (reviewer A, PR #53).


@pytest.mark.parametrize("damage", ["superuser", "no-connection", "other-model", "no-model", "other-repository"])
def test_an_exact_list_that_cannot_be_trusted_is_refused(tmp_path, capsys, damage):
    baseline, candidate, exact = _cases()
    if damage == "superuser":
        exact["connection"]["rolsuper"] = True
    elif damage == "no-connection":
        del exact["connection"]
    elif damage == "other-model":
        exact["model"] = "text-embedding-3-small"
    elif damage == "no-model":
        del exact["model"]
    else:
        exact["repository_id"] = "repo-2"
    _write(tmp_path, baseline, candidate, exact)

    code = compare_runs.main(["--records", str(tmp_path), "--corpora", "toy"])
    out = capsys.readouterr().out

    assert code == 2, out
    assert "REFUSED" in out and "the exact list" in out and "VERDICT" not in out


def test_a_question_with_no_exact_list_is_refused(tmp_path, capsys):
    baseline, candidate, exact = _cases()
    del exact["questions"]["q-b"]
    _write(tmp_path, baseline, candidate, exact)

    code = compare_runs.main(["--records", str(tmp_path), "--corpora", "toy"])
    out = capsys.readouterr().out

    assert code == 2, out
    assert "q-b: no exact list" in out


# A record's ranks must be what its own final list gives (reviewer A, PR #53).


@pytest.mark.parametrize("tamper", ["file_rank", "symbol_rank"])
def test_a_rank_edited_without_its_final_list_is_refused_not_classified(tmp_path, capsys, tamper):
    """Reviewer A's reproduction: a candidate file_rank changed from 1 to 2 with the
    trace untouched came out (c), PASS, exit 0. Now the record is inconsistent
    with itself and the comparison is refused."""
    baseline, candidate, exact = _cases()
    target = next(r for r in candidate if r["id"] == "q-same")
    assert target["file_rank"] == 1
    if tamper == "file_rank":
        target["file_rank"] = 2
    else:
        target["symbol"] = "B"
        for r in baseline:
            if r["id"] == "q-same":
                r["symbol"] = "B"
        target["symbol_rank"] = 1  # the toy breadcrumbs are empty, so the true symbol rank is None
    _write(tmp_path, baseline, candidate, exact)

    code = compare_runs.main(["--records", str(tmp_path), "--corpora", "toy"])
    out = capsys.readouterr().out

    assert code == 2, out
    assert "REFUSED" in out and "q-same" in out and "inconsistent with itself" in out
    assert "VERDICT" not in out and "(c)" not in out


def test_a_real_tied_swap_in_the_final_list_is_class_c(tmp_path, capsys):
    """B and C tie in the vector leg; the stores order them differently, the
    top-2 cut falls between them, and both records' ranks follow from their
    own final lists."""
    baseline, candidate, exact = _cases()
    keep = {"q-c", "q-same"}
    baseline = [r for r in baseline if r["id"] in keep]
    candidate = [r for r in candidate if r["id"] in keep]
    tied_b = next(r for r in baseline if r["id"] == "q-c")
    tied_c = next(r for r in candidate if r["id"] == "q-c")
    assert [e["chunk_id"] for e in tied_b["trace"]["top"]] != [e["chunk_id"] for e in tied_c["trace"]["top"]]
    _write(tmp_path, baseline, candidate, exact)

    code = compare_runs.main(["--records", str(tmp_path), "--corpora", "toy"])
    out = capsys.readouterr().out

    assert code == 0, out
    assert _class_of(out, "q-c") == "c"
    assert "(c)=1   UNEXPLAINED=0" in out


def test_an_untied_swap_in_the_final_list_is_unexplained(tmp_path, capsys):
    """The legs agree, but the candidate's final list has two chunks with
    distinguishable scores in the other order (its ranks consistent with that
    list): no tie explains it."""
    baseline, candidate, exact = _cases()
    keep = {"q-same"}
    baseline = [r for r in baseline if r["id"] in keep]
    candidate = [r for r in candidate if r["id"] in keep]
    target = candidate[0]
    boosted = target["trace"]["boosted"]
    assert boosted[0]["boosted_score"] > boosted[1]["boosted_score"] + 1e-6, "the swap must be untied"
    boosted[0], boosted[1] = boosted[1], boosted[0]
    top = target["trace"]["top"]
    top[0], top[1] = top[1], top[0]
    target["file_rank"] = 2  # what the swapped final list gives (B is now second)
    _write(tmp_path, baseline, candidate, exact)

    code = compare_runs.main(["--records", str(tmp_path), "--corpora", "toy"])
    out = capsys.readouterr().out

    assert code == 1, out
    assert _class_of(out, "q-same") == "UNEXPLAINED"


def test_a_missing_exact_paths_is_assumed_by_corpus_and_said_so(tmp_path, capsys):
    """Records from before PR #53's review carry no exact_paths; the harness's
    rule is assumed, printed, and a wrong assumption is refused by the rank
    recomputation."""
    baseline, candidate, exact = _cases()
    keep = {"q-same"}
    baseline = [r for r in baseline if r["id"] in keep]
    candidate = [r for r in candidate if r["id"] in keep]
    _write(tmp_path, baseline, candidate, exact)
    for name in ("baseline-toy.jsonl", "pgvector-toy.jsonl"):
        path = tmp_path / name
        lines = path.read_text(encoding="utf-8").splitlines()
        header = json.loads(lines[0])
        del header["exact_paths"]
        path.write_text("\n".join([json.dumps(header), *lines[1:]]) + "\n", encoding="utf-8")

    code = compare_runs.main(["--records", str(tmp_path), "--corpora", "toy"])
    out = capsys.readouterr().out

    assert code == 0, out
    assert "note: toy: the baseline header records no exact_paths; assumed True" in out


@pytest.mark.parametrize("shape", ["empty", "missing", "count-mismatch"])
def test_an_empty_or_inconsistent_qdrant_point_set_is_refused(tmp_path, capsys, shape):
    """With no point ids every chunk would count as pointless and class (b) would
    be trivially available (reviewer B saw the fixture's (a) come out (b), exit 0)."""
    baseline, candidate, exact = _cases()
    _write(tmp_path, baseline, candidate, exact)
    path = tmp_path / "qdrant_ids-toy.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    if shape == "empty":
        doc["ids"] = []
    elif shape == "missing":
        del doc["ids"]
    else:
        doc["count"] = len(doc["ids"]) + 1
    path.write_text(json.dumps(doc), encoding="utf-8")

    code = compare_runs.main(["--records", str(tmp_path), "--corpora", "toy"])
    out = capsys.readouterr().out

    assert code == 2, out
    assert "REFUSED" in out and "qdrant_ids-toy.json" in out and "VERDICT" not in out


@pytest.mark.parametrize("damage", ["no-trace", "no-file_rank", "trace-without-boosted", "no-hash"])
def test_an_incomplete_record_is_refused_rather_than_crashing(tmp_path, capsys, damage):
    baseline, candidate, exact = _cases()
    target = next(r for r in candidate if r["id"] == "q-same")
    if damage == "no-trace":
        del target["trace"]
    elif damage == "no-file_rank":
        del target["file_rank"]
    elif damage == "trace-without-boosted":
        del target["trace"]["boosted"]
    else:
        del target["query_vector_sha256"]
    _write(tmp_path, baseline, candidate, exact)

    code = compare_runs.main(["--records", str(tmp_path), "--corpora", "toy"])
    out = capsys.readouterr().out

    assert code == 2, out
    assert "REFUSED" in out and "q-same" in out and "VERDICT" not in out


def test_a_failed_query_is_refused_as_not_a_measurement(tmp_path, capsys):
    baseline, candidate, exact = _cases()
    for rec in baseline:
        if rec["id"] == "q-same":
            rec["error"] = "Retrieval failed in vector search: RuntimeError: 401"
    _write(tmp_path, baseline, candidate, exact)

    code = compare_runs.main(["--records", str(tmp_path), "--corpora", "toy"])
    out = capsys.readouterr().out

    assert code == 2, out
    assert "not a measurement" in out


class TestRankingAgreement:
    """The definition of "agree" from 22-03-equivalence.md, on its own."""

    @staticmethod
    def _r(items, kind="vector"):
        return compare_runs.Ranking(items, kind)

    def test_identical_rankings_agree(self):
        ok, why, _ = compare_runs.rankings_agree(self._r([("A", 0.9), ("B", 0.8)]), self._r([("A", 0.9), ("B", 0.8)]))
        assert ok, why

    def test_tied_chunks_may_be_in_either_order(self):
        ok, _, _ = compare_runs.rankings_agree(
            self._r([("A", 0.9), ("B", 0.7), ("C", 0.7)]), self._r([("A", 0.9), ("C", 0.7), ("B", 0.7)])
        )
        assert ok

    def test_a_tie_within_tolerance_counts_in_either_ranking(self):
        # Tied on one side only (within 1e-5), distinguishable on the other: still a tie.
        ok, _, _ = compare_runs.rankings_agree(
            self._r([("B", 0.700004), ("C", 0.7)]), self._r([("C", 0.70002), ("B", 0.7)])
        )
        assert ok

    def test_distinguishable_chunks_in_the_other_order_do_not_agree(self):
        ok, why, _ = compare_runs.rankings_agree(
            self._r([("A", 0.9), ("B", 0.8), ("C", 0.7)]), self._r([("A", 0.9), ("C", 0.7), ("B", 0.65)])
        )
        assert not ok and "other order" in why

    def test_a_different_chunk_beyond_the_tie_at_the_cut_does_not_agree(self):
        ok, why, _ = compare_runs.rankings_agree(
            self._r([("A", 0.9), ("B", 0.8)]), self._r([("A", 0.9), ("C", 0.5)])
        )
        assert not ok

    def test_the_tie_group_at_the_cut_may_show_different_members_when_the_long_side_is_complete(self):
        short = self._r([("A", 0.9), ("B", 0.7)])
        long = self._r([("A", 0.9), ("C", 0.7), ("B", 0.7), ("D", 0.1)])
        ok, why, cut = compare_runs.prefix_agrees(short, long)
        assert ok, why
        assert cut == {"B", "C"}

    def test_comparable_scores_must_be_tied(self):
        ok, why, _ = compare_runs.rankings_agree(
            self._r([("A", 0.9)], "fts"), self._r([("A", 0.8)], "fts"), scores_comparable=True
        )
        assert not ok and "score differs" in why


def test_the_judge_scores_with_the_harnesss_own_rule():
    """scoring.py is one module: the harness records ranks with it and the judge
    recomputes them with the same object, so the two cannot drift."""
    import importlib.util

    harness_path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "rag_quality_harness.py"
    spec = importlib.util.spec_from_file_location("rag_quality_harness_for_scoring", harness_path)
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)

    assert harness.ranks is compare_runs.ranks
    results = [{"file_path": "pkg/a.go", "breadcrumb": "A.Run"}, {"file_path": "pkg/b.go", "breadcrumb": "B.Run"}]
    assert compare_runs.ranks(True, "pkg/b.go", "Run", results) == (2, 2)
    assert compare_runs.ranks(True, "pkg/b.go", "Other", results) == (2, None)
    assert compare_runs.ranks(False, "pkg/", None, results) == (1, None)
    assert compare_runs.ranks(True, "pkg/", None, results) == (None, None)


def test_rrf_matches_the_fusion_module():
    """The script's recomputation must be rrf_fusion.py's arithmetic and tie order."""
    from workers.retrieval.rrf_fusion import RRFFusion

    fts = [{"chunk_id": c} for c in ("A", "B", "C")]
    vector = [{"chunk_id": c} for c in ("C", "D", "A")]
    expected = [(r["chunk_id"], r["rrf_score"]) for r in RRFFusion().fuse({"fts": fts, "vector": vector})]
    assert compare_runs.rrf(["A", "B", "C"], ["C", "D", "A"]) == expected


# ---------------------------------------------------------------------------
# 22.2-01: QD2's tolerance as a flag, and --no-qdrant
# ---------------------------------------------------------------------------

RECORDS_2203 = (pathlib.Path(__file__).resolve().parents[3] / ".planning" / "phases"
                / "22-repository-clone-ingestion" / "22-03-records")


@pytest.mark.parametrize("extra", [[], ["--vector-tolerance", "1e-5"]])
def test_the_committed_22_03_records_rejudge_byte_identically_at_the_default(capsys, extra):
    code = compare_runs.main(["--records", str(RECORDS_2203), *extra])
    out = capsys.readouterr().out
    committed = (RECORDS_2203 / "compare_runs-rejudged-after-review.txt").read_bytes().decode("utf-8")
    assert code == 0, out
    assert out.replace("\r\n", "\n") == committed.replace("\r\n", "\n")


def _tolerance_pair() -> Tuple[List[dict], List[dict], dict]:
    """B and C at the top-2 cut, their vector scores 5e-6 apart, in the other
    order on the other side: a tie at 1e-5, distinguishable at 2e-6."""
    baseline = [_record("q-t", "tuning", [("A", 0.5)], [("A", 0.8), ("B", 0.700005), ("C", 0.7)], "B")]
    candidate = [_record("q-t", "tuning", [("A", 0.5)], [("A", 0.8), ("C", 0.700005), ("B", 0.7)], "B")]
    exact = _exact({"q-t": [("A", 0.8), ("B", 0.700005), ("C", 0.7)]}, {})
    assert baseline[0]["file_rank"] == 2 and candidate[0]["file_rank"] is None
    return baseline, candidate, exact


def test_the_vector_tolerance_changes_the_class(tmp_path, capsys):
    _write(tmp_path, *_tolerance_pair())

    code = compare_runs.main(["--records", str(tmp_path), "--corpora", "toy", "--vector-tolerance", "2e-6"])
    out = capsys.readouterr().out
    assert code == 1, out
    assert _class_of(out, "q-t") == "UNEXPLAINED"
    assert "vector tolerance: 2e-06 (absolute)" in out

    # A second call in the same process, at the default: nothing leaks from the first.
    code = compare_runs.main(["--records", str(tmp_path), "--corpora", "toy"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert _class_of(out, "q-t") == "c"
    assert "vector tolerance" not in out, "the default output is 22-03's, unchanged"
    assert compare_runs._vector_tolerance == compare_runs.DEFAULT_VECTOR_TOLERANCE


def _write_no_qdrant(records_dir: pathlib.Path, baseline: List[dict], candidate: List[dict], exact: dict) -> None:
    """Two pgvector runs on one database, as recorded after Qdrant's retirement: no point set."""
    for prefix, records in (("run1", baseline), ("run2", candidate)):
        (records_dir / f"{prefix}-toy.jsonl").write_text(
            "\n".join(json.dumps(r) for r in [_header("pgvector"), *records]) + "\n", encoding="utf-8"
        )
    (records_dir / "exact-toy.json").write_text(json.dumps(exact), encoding="utf-8")


NO_QDRANT = ["--corpora", "toy", "--no-qdrant", "--baseline-prefix", "run1", "--candidate-prefix", "run2"]


def test_no_qdrant_disables_class_b(tmp_path, capsys):
    """The baseline's vector leg differs from exact search and the candidate's
    matches it: (b) with a point set; with none, (b) has no meaning."""
    baseline, candidate, exact = _cases()
    keep = {"q-b", "q-same"}
    _write_no_qdrant(tmp_path, [r for r in baseline if r["id"] in keep], [r for r in candidate if r["id"] in keep],
                     exact)

    code = compare_runs.main(["--records", str(tmp_path), *NO_QDRANT])
    out = capsys.readouterr().out

    assert code == 1, out
    assert _class_of(out, "q-b") == "UNEXPLAINED"
    assert "classes (a) and (b) are disabled" in _row(out, "q-b")
    assert out.startswith("mode: --no-qdrant")
    assert "(a)=0   (b)=0   (c)=0   UNEXPLAINED=1" in out


def test_no_qdrant_disables_class_a(tmp_path, capsys):
    """q-a: a chunk entered the candidate's leg ((a) with a point set it is not
    in). q-a0: the baseline's vector leg is empty, which an empty point set
    would make (a) trivially."""
    baseline, candidate, exact = _cases()
    baseline = [r for r in baseline if r["id"] == "q-a"]
    candidate = [r for r in candidate if r["id"] == "q-a"]
    baseline.append(_record("q-a0", "tuning", [("A", 0.5)], [], "B"))
    candidate.append(_record("q-a0", "tuning", [("A", 0.5)], [("B", 0.8)], "B"))
    exact["questions"]["q-a0"] = _exact({"q-a0": [("B", 0.8)]}, {})["questions"]["q-a0"]
    assert baseline[1]["file_rank"] is None and candidate[1]["file_rank"] == 2
    _write_no_qdrant(tmp_path, baseline, candidate, exact)

    code = compare_runs.main(["--records", str(tmp_path), *NO_QDRANT])
    out = capsys.readouterr().out

    assert code == 1, out
    assert _class_of(out, "q-a") == "UNEXPLAINED"
    assert _class_of(out, "q-a0") == "UNEXPLAINED"
    assert "(a)=0" in out


def test_no_qdrant_still_explains_a_tie_at_the_cut_as_c(tmp_path, capsys):
    baseline, candidate, exact = _cases()
    keep = {"q-c", "q-same"}
    _write_no_qdrant(tmp_path, [r for r in baseline if r["id"] in keep], [r for r in candidate if r["id"] in keep],
                     exact)

    code = compare_runs.main(["--records", str(tmp_path), *NO_QDRANT, "--vector-tolerance", "2e-6"])
    out = capsys.readouterr().out

    assert code == 0, out
    assert _class_of(out, "q-c") == "c"
    assert "VERDICT: PASS (0 UNEXPLAINED)" in out
    # The sides are named by their prefixes, and no Qdrant figures are reported.
    assert "run1" in out and "run2" in out and "Qdrant point" not in out


def test_without_no_qdrant_a_missing_point_set_is_still_refused(tmp_path, capsys):
    baseline, candidate, exact = _cases()
    _write_no_qdrant(tmp_path, baseline, candidate, exact)

    code = compare_runs.main(["--records", str(tmp_path), "--corpora", "toy",
                              "--baseline-prefix", "run1", "--candidate-prefix", "run2"])
    out = capsys.readouterr().out

    assert code == 2, out
    assert "REFUSED" in out and "qdrant_ids-toy.json" in out

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


def _header(backend: str) -> dict:
    return {
        "record": "run",
        "corpus": "toy",
        "commit": "harness",
        "repository_id": "repo-1",
        "set": "all",
        "top_k": TOP_K,
        "boost_config": None,
        "vector_backend": backend,
        "connections": {
            "fts": {"current_user": "rag_doc_app", "session_user": "isolation", "rolsuper": False, "rolbypassrls": False}
        },
    }


def _exact(question_ids: Dict[str, List[Tuple[str, float]]], hashes: Dict[str, str]) -> dict:
    return {
        "corpus": "toy",
        "repository_id": "repo-1",
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


def test_rrf_matches_the_fusion_module():
    """The script's recomputation must be rrf_fusion.py's arithmetic and tie order."""
    from workers.retrieval.rrf_fusion import RRFFusion

    fts = [{"chunk_id": c} for c in ("A", "B", "C")]
    vector = [{"chunk_id": c} for c in ("C", "D", "A")]
    expected = [(r["chunk_id"], r["rrf_score"]) for r in RRFFusion().fuse({"fts": fts, "vector": vector})]
    assert compare_runs.rrf(["A", "B", "C"], ["C", "D", "A"]) == expected

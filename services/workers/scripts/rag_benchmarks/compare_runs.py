#!/usr/bin/env python3
"""Judge two recorded harness runs under 22-03-equivalence.md's rule.

This is the arbiter of the storage-move equivalence gate: the verdict is this
script's exit code and its table, never a reading of the records by hand. The
rule and every definition here (tolerances, what "agree" means, the order the
classes are tested in) were committed in
`.planning/phases/22-repository-clone-ingestion/22-03-equivalence.md` BEFORE
the first measurement, and this file implements them.

INPUT. For each corpus, four files written by `rag_quality_harness.py` on ONE
scratch database:

    baseline-<c>.jsonl   --measure --record, on the Qdrant read path
    pgvector-<c>.jsonl   --measure --record, on the pgvector read path
    qdrant_ids-<c>.json  --qdrant-ids: every point Qdrant held (class (a))
    exact-<c>.json       --exact: exact search per question (class (b))

Both runs must have used the same cached query vectors (`--query-vectors`);
every record carries the SHA-256 of the vector it used, and this script
REFUSES to compare anything if a single hash differs.

OUTPUT. Every question whose final file rank or symbol rank differs, with its
class: (a), (b), (c) or UNEXPLAINED. Counts per class. Aggregates under both
runs, reported and not judged. Exit 0 when no question is UNEXPLAINED, 1 when
any is, 2 when the inputs are refused.

USAGE
    compare_runs.py --records DIR [--corpora self miniflux mealie]
                    [--baseline-prefix baseline] [--candidate-prefix pgvector]

Files may be gzip-compressed (`.gz`), which is how the records are committed.
"""

from __future__ import annotations

import argparse
import gzip
import itertools
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from scoring import ranks  # noqa: E402  (the harness's own scoring rule, one module)

RRF_K = 60  # rrf_fusion.py's constant; the recomputation must match it
DEFAULT_CORPORA = ("self", "miniflux", "mealie")
CLASSES = ("a", "b", "c", "UNEXPLAINED")

# 22-03-equivalence.md, "Tolerances". kind -> (mode, value)
TOLERANCES = {
    "vector": ("abs", 1e-5),
    "fts": ("rel", 1e-6),
    "fused": ("abs", 1e-9),
    "boosted": ("abs", 1e-9),
}


def tied(a: float, b: float, kind: str) -> bool:
    mode, tol = TOLERANCES[kind]
    if mode == "abs":
        return abs(a - b) <= tol
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


@dataclass(frozen=True)
class Entry:
    chunk_id: str
    score: float


class Ranking:
    """An ordered list of (chunk id, score); ties are decided by `kind`'s tolerance."""

    def __init__(self, entries: Iterable[Tuple[str, float]], kind: str):
        self.kind = kind
        self.entries: List[Entry] = [Entry(c, float(s)) for c, s in entries]
        self.score_of: Dict[str, float] = {e.chunk_id: e.score for e in self.entries}
        self.position: Dict[str, int] = {e.chunk_id: i for i, e in enumerate(self.entries)}
        if len(self.score_of) != len(self.entries):
            raise ValueError("a ranking must not repeat a chunk id")

    def __len__(self) -> int:
        return len(self.entries)

    @property
    def ids(self) -> Set[str]:
        return set(self.score_of)

    def tied_pair(self, x: str, y: str) -> bool:
        return x in self.score_of and y in self.score_of and tied(
            self.score_of[x], self.score_of[y], self.kind
        )

    def group_around(self, i: int) -> Set[str]:
        """The chunks tied (with this ranking's own scores) with the one at position `i`."""
        anchor = self.entries[i].score
        group = {self.entries[i].chunk_id}
        j = i - 1
        while j >= 0 and tied(self.entries[j].score, anchor, self.kind):
            group.add(self.entries[j].chunk_id)
            j -= 1
        j = i + 1
        while j < len(self.entries) and tied(self.entries[j].score, anchor, self.kind):
            group.add(self.entries[j].chunk_id)
            j += 1
        return group

    def restrict(self, keep: Set[str]) -> "Ranking":
        return Ranking(((e.chunk_id, e.score) for e in self.entries if e.chunk_id in keep), self.kind)


def prefix_agrees(
    short: Ranking,
    long: Ranking,
    scores_comparable: bool = False,
    cut_pool: Optional[Set[str]] = None,
) -> Tuple[bool, str, Set[str]]:
    """Does `short` agree with `long` over `short`'s length?

    Agreement (22-03-equivalence.md, "Rankings"): the same chunk ids; no pair
    of chunks in the other order unless they are tied in one of the two
    rankings; and, when `scores_comparable` (same store, same arithmetic),
    each common chunk's score tied across the two. The tie group at the cut
    may show different members on the two sides only where they are shown to
    be one tie group: by `long` continuing past the cut, by `cut_pool` (the
    exact list's tie tail), or, when both are cut at the same length and the
    scores are comparable, by each side's own last group and the two last
    scores being tied.

    Returns (agrees, why not, the tie group at the cut that was accepted).
    """
    kind = short.kind
    n = len(short)
    if n == 0:
        return (len(long) == 0), ("" if len(long) == 0 else "one side is empty and the other is not"), set()
    head = long.entries[:n]
    head_ids = {e.chunk_id for e in head}
    extras_short = short.ids - head_ids
    extras_head = head_ids - short.ids
    cut_group: Set[str] = set()
    if extras_short or extras_head:
        own_last = short.group_around(n - 1)
        if not extras_short <= own_last:
            return False, "a chunk on one side only is not in that side's tie group at the cut", set()
        if len(long) > n:
            cut_group = long.group_around(n - 1)
            if not (extras_short <= cut_group and extras_head <= cut_group):
                return False, "membership differs beyond the tie group at the cut", set()
        else:
            long_last = long.group_around(n - 1)
            if not extras_head <= long_last:
                return False, "a chunk on one side only is not in that side's tie group at the cut", set()
            by_scores = scores_comparable and tied(short.entries[-1].score, long.entries[-1].score, kind)
            by_pool = cut_pool is not None and extras_short <= cut_pool and extras_head <= cut_pool
            if not (by_scores or by_pool):
                return False, "the chunks at the cut differ and are not shown to be tied", set()
            cut_group = own_last | long_last
    common = short.ids & head_ids
    head_position = {e.chunk_id: i for i, e in enumerate(head)}
    for x, y in itertools.combinations(sorted(common), 2):
        if (short.position[x] < short.position[y]) != (head_position[x] < head_position[y]):
            if not (short.tied_pair(x, y) or long.tied_pair(x, y)):
                return False, f"{x[:8]} and {y[:8]} are in the other order and not tied", set()
    if scores_comparable:
        for x in common:
            if not tied(short.score_of[x], long.score_of[x], kind):
                return False, f"{x[:8]}'s score differs beyond the tolerance", set()
    return True, "", cut_group


def rankings_agree(
    r1: Ranking, r2: Ranking, scores_comparable: bool = False, cut_pool: Optional[Set[str]] = None
) -> Tuple[bool, str, Set[str]]:
    short, long = (r1, r2) if len(r1) <= len(r2) else (r2, r1)
    return prefix_agrees(short, long, scores_comparable, cut_pool)


def rrf(fts_ids: Sequence[str], vector_ids: Sequence[str]) -> List[Tuple[str, float]]:
    """Reciprocal rank fusion exactly as rrf_fusion.py does it.

    Scores accumulate in first-occurrence order (the fts list, then the vector
    list) and the final sort is Python's stable sort, so ties keep that order.
    """
    scores: Dict[str, float] = {}
    for leg in (fts_ids, vector_ids):
        for position, chunk_id in enumerate(leg, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + position)
    return sorted(scores.items(), key=lambda item: item[1], reverse=True)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


def _open_text(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open("r", encoding="utf-8")


def _find(records_dir: Path, name: str) -> Path:
    for candidate in (records_dir / name, records_dir / f"{name}.gz"):
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"{records_dir / name} (or .gz) does not exist")


REQUIRED_QUESTION_KEYS = (
    "id", "set", "path", "symbol", "file_rank", "symbol_rank", "query_vector_sha256", "trace", "error",
)
TRACE_STAGES = ("fts", "vector", "fused", "boosted", "top")


def load_run(path: Path) -> Tuple[dict, Dict[str, dict]]:
    """A --record file: the run header and its question records by id.

    Raises ValueError (which main reports as REFUSED) for a file with no header,
    a question recorded twice (the second could silently replace the first and
    hide a difference), or a record missing what the comparison reads.
    """
    header: Optional[dict] = None
    questions: Dict[str, dict] = {}
    with _open_text(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj.get("record") == "run":
                if header is not None:
                    raise ValueError(f"{path}: two run headers; not one run")
                header = obj
            elif obj.get("record") == "question":
                missing = [key for key in REQUIRED_QUESTION_KEYS if key not in obj]
                if missing:
                    raise ValueError(
                        f"{path}: question record {obj.get('id')!r} lacks {missing}; not a complete measurement"
                    )
                if not obj.get("error"):
                    trace = obj["trace"]
                    stages = [s for s in TRACE_STAGES if not isinstance(trace, dict) or s not in trace]
                    if stages:
                        raise ValueError(f"{path}: question {obj['id']}: the trace lacks {stages}")
                if obj["id"] in questions:
                    raise ValueError(
                        f"{path}: question {obj['id']} is recorded twice; refusing to pick one"
                    )
                questions[obj["id"]] = obj
    if header is None:
        raise ValueError(f"{path}: no run header")
    if not questions:
        raise ValueError(f"{path}: no question records")
    return header, questions


def load_json(path: Path) -> dict:
    with _open_text(path) as fh:
        return json.load(fh)


@dataclass
class CorpusRuns:
    name: str
    baseline_header: dict
    baseline: Dict[str, dict]
    candidate_header: dict
    candidate: Dict[str, dict]
    qdrant_ids: Set[str]
    qdrant_meta: dict
    exact: dict
    notes: List[str]


def exact_paths_of(header: dict, notes: List[str], side: str) -> bool:
    """How the run matched paths. Recorded since PR #53's review; for an older
    record the harness's own rule is assumed and said so, and a wrong
    assumption is refused by the rank recomputation, never passed."""
    if isinstance(header.get("exact_paths"), bool):
        return header["exact_paths"]
    assumed = header.get("corpus") != "self"
    notes.append(
        f"{header.get('corpus')}: the {side} header records no exact_paths; assumed {assumed} "
        "(the harness's rule: self matches by substring, benchmark corpora exactly)"
    )
    return assumed


def refusals(runs: CorpusRuns) -> List[str]:
    """Everything that makes the comparison meaningless. Checked before comparing anything."""
    problems: List[str] = []
    b, c = runs.baseline, runs.candidate
    if set(b) != set(c):
        only_b = sorted(set(b) - set(c))
        only_c = sorted(set(c) - set(b))
        problems.append(
            f"{runs.name}: the runs hold different questions "
            f"(baseline only: {only_b[:5]}; candidate only: {only_c[:5]})"
        )
    exact_questions = runs.exact.get("questions", {})
    for qid in sorted(set(b) & set(c)):
        hb, hc = b[qid].get("query_vector_sha256"), c[qid].get("query_vector_sha256")
        if not hb or not hc or hb != hc:
            problems.append(f"{runs.name}: {qid}: query-vector hash differs ({hb} vs {hc})")
        if qid not in exact_questions:
            problems.append(f"{runs.name}: {qid}: no exact list was recorded for it")
        elif hb and exact_questions[qid].get("query_vector_sha256") != hb:
            problems.append(
                f"{runs.name}: {qid}: the exact list used a different query vector "
                f"({exact_questions[qid].get('query_vector_sha256')})"
            )
        for side, rec in (("baseline", b[qid]), ("candidate", c[qid])):
            if rec.get("error"):
                problems.append(f"{runs.name}: {qid}: the {side} query failed ({rec['error'][:60]}); a failed query is not a measurement")
    for key in ("corpus", "set", "top_k", "boost_config", "repository_id", "embedding_model"):
        if runs.baseline_header.get(key) != runs.candidate_header.get(key):
            problems.append(
                f"{runs.name}: the runs differ in {key}: "
                f"{runs.baseline_header.get(key)!r} vs {runs.candidate_header.get(key)!r}"
            )
    # The recorded ranks must be what the recorded final list gives under the
    # harness's own scoring rule: a rank edited by hand, with the trace left
    # alone, is refused rather than classified (reviewer A, PR #53).
    for side, header, records in (
        ("baseline", runs.baseline_header, b),
        ("candidate", runs.candidate_header, c),
    ):
        exact_paths = exact_paths_of(header, runs.notes, side)
        for qid, rec in sorted(records.items()):
            if rec.get("error"):
                continue
            expected = ranks(exact_paths, rec["path"], rec.get("symbol"), rec["trace"]["top"])
            recorded = (rec["file_rank"], rec.get("symbol_rank"))
            if expected != recorded:
                problems.append(
                    f"{runs.name}: {qid}: the {side} record's ranks {recorded} are not what its final list "
                    f"gives {expected}; the record is inconsistent with itself"
                )
    for side, header in (("baseline", runs.baseline_header), ("candidate", runs.candidate_header)):
        problems.extend(f"{runs.name}: the {side} run {p}" for p in connection_problems(header))
    problems.extend(f"{runs.name}: the exact list {p}" for p in exact_list_problems(runs.exact, runs.baseline_header))
    if not runs.qdrant_ids:
        problems.append(
            f"{runs.name}: the Qdrant point set is empty; class (a) and class (b) cannot be decided"
        )
    if runs.qdrant_meta.get("repository_id") and runs.baseline_header.get("repository_id"):
        if runs.qdrant_meta["repository_id"] != runs.baseline_header["repository_id"]:
            problems.append(f"{runs.name}: the Qdrant point set is for another repository")
    return problems


def exact_list_problems(exact: dict, header: dict) -> List[str]:
    """The exact list is the ground truth for class (b), so it is held to the same
    standard as a run: measured as a role row-level security applies to, with
    the run's model, on the run's repository (reviewer A, PR #53)."""
    problems = []
    identity = exact.get("connection")
    if not isinstance(identity, dict) or not all(
        isinstance(identity.get(key), bool) for key in ("rolsuper", "rolbypassrls")
    ):
        problems.append("does not state the rolsuper and rolbypassrls of the connection that produced it")
    elif identity["rolsuper"] or identity["rolbypassrls"]:
        problems.append(
            f"was produced by {identity.get('current_user')} (rolsuper={identity['rolsuper']}, "
            f"rolbypassrls={identity['rolbypassrls']}), which bypasses row-level security"
        )
    if not exact.get("model") or not header.get("embedding_model"):
        problems.append("or the run does not state its embedding model")
    elif exact["model"] != header["embedding_model"]:
        problems.append(f"was computed for model {exact['model']!r}, the run for {header['embedding_model']!r}")
    if not exact.get("repository_id") or not header.get("repository_id"):
        problems.append("or the run does not state its repository")
    elif exact["repository_id"] != header["repository_id"]:
        problems.append("is for another repository")
    return problems


def connection_problems(header: dict) -> List[str]:
    """Why a run header's measuring connections cannot be trusted, if they cannot.

    A run that records no connection, or one whose identity lacks `rolsuper` or
    `rolbypassrls`, is refused like a superuser run: the role every read ran as
    is unknown, and a superuser bypasses row-level security. A pgvector run must
    record both legs' connections; a Qdrant-era run has only the keyword leg's.
    """
    connections = header.get("connections")
    if not isinstance(connections, dict) or not connections:
        return ["records no measuring connection, so the role its reads ran as is unknown"]
    problems = []
    required = ["fts"] + (["vector"] if header.get("vector_backend") == "pgvector" else [])
    for name in required:
        if name not in connections:
            problems.append(f"records no {name} connection")
    for name, identity in connections.items():
        if not isinstance(identity, dict) or not all(
            isinstance(identity.get(key), bool) for key in ("rolsuper", "rolbypassrls")
        ):
            problems.append(f"'s {name} connection does not state rolsuper and rolbypassrls")
            continue
        if identity["rolsuper"] or identity["rolbypassrls"]:
            problems.append(
                f"'s {name} connection was {identity.get('current_user')} "
                f"(rolsuper={identity['rolsuper']}, rolbypassrls={identity['rolbypassrls']}); "
                "a superuser bypasses row-level security, so that measurement proves nothing"
            )
    return problems


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _legs(record: dict) -> Tuple[Ranking, Ranking]:
    trace = record["trace"]
    return (
        Ranking(((e["chunk_id"], e["score"]) for e in trace["fts"]), "fts"),
        Ranking(((e["chunk_id"], e["score"]) for e in trace["vector"]), "vector"),
    )


def _fused(record: dict) -> Ranking:
    return Ranking(((e["chunk_id"], e["rrf_score"]) for e in record["trace"]["fused"]), "fused")


def _boosted(record: dict) -> Ranking:
    return Ranking(((e["chunk_id"], e["boosted_score"]) for e in record["trace"]["boosted"]), "boosted")


def _top(record: dict) -> Ranking:
    """The final list the ranks were taken from, with its (boosted) scores."""
    return Ranking(((e["chunk_id"], e["score"]) for e in record["trace"]["top"]), "boosted")


def _exact_ranking(exact_entry: dict) -> Ranking:
    """The exact list as similarities (1 - distance), top 50 then its tie tail."""
    rows = list(exact_entry.get("top", [])) + list(exact_entry.get("tail_ties", []))
    return Ranking(((r["chunk_id"], 1.0 - float(r["distance"])) for r in rows), "vector")


def consistency(record: dict, top_k: int) -> Optional[str]:
    """Step 0: fusion, boosts and the cut recomputed from this record's own legs."""
    trace = record["trace"]
    fts, vector = _legs(record)
    expected = Ranking(rrf([e.chunk_id for e in fts.entries], [e.chunk_id for e in vector.entries]), "fused")
    recorded = _fused(record)
    if len(expected) != len(recorded):
        return "the fused list does not hold the legs' chunks"
    ok, why, _ = rankings_agree(expected, recorded, scores_comparable=True)
    if not ok:
        return f"the fused list is not RRF of the recorded legs ({why})"
    boosted = trace["boosted"]
    if {e["chunk_id"] for e in boosted} != recorded.ids:
        return "the boosted list does not hold the fused chunks"
    for e in boosted:
        if abs(e["boosted_score"] - e["rrf_score"] * e["boost_multiplier"]) > TOLERANCES["boosted"][1]:
            return f"{e['chunk_id'][:8]}: boosted score is not rrf_score times the multiplier"
        if not tied(e["rrf_score"], recorded.score_of[e["chunk_id"]], "fused"):
            return f"{e['chunk_id'][:8]}: the boosted list's rrf_score is not the fused list's"
    for first, second in zip(boosted, boosted[1:]):
        if second["boosted_score"] > first["boosted_score"] + TOLERANCES["boosted"][1]:
            return "the boosted list is not in descending order"
    top_ids = [e["chunk_id"] for e in trace["top"]]
    if top_ids != [e["chunk_id"] for e in boosted[:top_k]]:
        return "top is not the boosted list cut at top_k (enrichment dropped or reordered a chunk)"
    return None


def positions_tied(lb: Ranking, lc: Ranking, is_tied_pair: Callable[[str, str], bool]) -> Tuple[bool, str]:
    """Position by position: the same score, and a different chunk only if the two are tied."""
    if len(lb) != len(lc):
        return False, "different lengths"
    for eb, ec in zip(lb.entries, lc.entries):
        if not tied(eb.score, ec.score, lb.kind):
            return False, f"scores differ at {eb.chunk_id[:8]}/{ec.chunk_id[:8]}"
        if eb.chunk_id != ec.chunk_id and not is_tied_pair(eb.chunk_id, ec.chunk_id):
            return False, f"{eb.chunk_id[:8]} and {ec.chunk_id[:8]} swapped without being tied"
    return True, ""


def classify(b: dict, c: dict, qdrant_ids: Set[str], exact_entry: Optional[dict], top_k: int) -> Tuple[str, str]:
    """The class of one differing question, per 22-03-equivalence.md, in its order."""
    # 0. consistency on both sides
    for side, rec in (("baseline", b), ("candidate", c)):
        problem = consistency(rec, top_k)
        if problem:
            return "UNEXPLAINED", f"{side}: {problem}"
    mult_b = {e["chunk_id"]: e["boost_multiplier"] for e in b["trace"]["boosted"]}
    mult_c = {e["chunk_id"]: e["boost_multiplier"] for e in c["trace"]["boosted"]}
    for chunk_id in mult_b.keys() & mult_c.keys():
        if abs(mult_b[chunk_id] - mult_c[chunk_id]) > TOLERANCES["boosted"][1]:
            return "UNEXPLAINED", f"{chunk_id[:8]}'s boost multiplier differs between the runs"

    # 1. keyword legs
    FB, VB = _legs(b)
    FC, VC = _legs(c)
    ok, why, fts_cut = rankings_agree(FB, FC, scores_comparable=True)
    if not ok:
        return "UNEXPLAINED", f"keyword legs differ: {why}"

    E = _exact_ranking(exact_entry) if exact_entry else Ranking([], "vector")
    pool = E.group_around(49) if len(E) >= 50 else set()

    # 2. vector legs agree: only tie order can have moved
    ok, why, vector_cut = rankings_agree(VB, VC, cut_pool=pool)
    if ok:
        cut_groups = [g for g in (fts_cut, vector_cut) if g]

        def is_tied_pair(x: str, y: str) -> bool:
            if any(r.tied_pair(x, y) for r in (FB, FC, VB, VC, _fused(b), _fused(c))):
                return True
            return any(x in g and y in g for g in cut_groups)

        ok_f, why_f = positions_tied(_fused(b), _fused(c), is_tied_pair)
        ok_b, why_b = positions_tied(_boosted(b), _boosted(c), is_tied_pair)
        # (c) needs the final lists themselves to differ, and to differ only by
        # tied chunks crossing: agreeing legs with identical final lists cannot
        # give different ranks, and an untied crossing is no tie (reviewer A).
        TB, TC = _top(b), _top(c)
        if [e.chunk_id for e in TB.entries] == [e.chunk_id for e in TC.entries]:
            return "UNEXPLAINED", "the final lists are identical, yet the ranks differ"
        ok_t, why_t = positions_tied(TB, TC, is_tied_pair)
        if ok_f and ok_b and ok_t:
            return "c", "the legs agree; tied chunks are ordered differently and the top-k cut fell between them"
        return "UNEXPLAINED", (
            "the legs agree but the fused, boosted or final lists differ beyond tied chunks "
            f"({why_f or why_b or why_t})"
        )

    # 3. (a): only chunks with no Qdrant point entered
    stripped = VC.restrict(qdrant_ids)
    entered = len(VC) - len(stripped)
    ok, why, _ = prefix_agrees(stripped, VB, cut_pool=pool)
    if ok and entered:
        return "a", f"{entered} chunk(s) with no Qdrant point entered the pgvector top 50; the rest agrees"

    # 4. (b): pgvector matches exact search, Qdrant did not
    ok_c, why_c, _ = rankings_agree(VC, E)
    ok_q, why_q, _ = rankings_agree(VB, E.restrict(qdrant_ids))
    if ok_c and not ok_q:
        return "b", f"the Qdrant leg differs from exact search ({why_q}); the pgvector leg matches it"

    if not ok_c:
        return "UNEXPLAINED", f"the pgvector leg differs from exact search ({why_c}); vector legs: {why}"
    return "UNEXPLAINED", f"vector legs differ ({why}) and neither (a) nor (b) covers it"


# ---------------------------------------------------------------------------
# Aggregates and the report
# ---------------------------------------------------------------------------


def score(ranks: List[Optional[int]]) -> dict:
    found = [r for r in ranks if r]
    total = len(ranks)
    return {
        "questions": total,
        "found": len(found),
        "rank1": sum(1 for r in found if r == 1),
        "mrr": (sum(1.0 / r for r in found) / total) if total else 0.0,
    }


def aggregates(records: Dict[str, dict]) -> Dict[str, dict]:
    by_set: Dict[str, List[dict]] = {}
    for rec in records.values():
        by_set.setdefault(rec["set"], []).append(rec)
    out = {}
    for set_name, rows in sorted(by_set.items()):
        out[set_name] = {
            "file": score([r["file_rank"] for r in rows]),
            "symbol": score([r["symbol_rank"] for r in rows if r.get("symbol")]),
        }
    return out


def _fmt_rank(rank: Optional[int]) -> str:
    return f"#{rank}" if rank else "MISS"


def compare(runs: CorpusRuns) -> Tuple[List[dict], dict]:
    """Every differing question classified, plus the corpus's informational numbers."""
    top_k = int(runs.baseline_header.get("top_k", 5))
    rows = []
    agreeing_boosted = 0
    max_delta = 0.0
    compared = 0
    for qid in sorted(runs.baseline):
        b, c = runs.baseline[qid], runs.candidate[qid]
        _, VB = _legs(b)
        _, VC = _legs(c)
        for chunk_id in VB.ids & VC.ids:
            max_delta = max(max_delta, abs(VB.score_of[chunk_id] - VC.score_of[chunk_id]))
            compared += 1
        if rankings_agree(_boosted(b), _boosted(c), scores_comparable=True)[0]:
            agreeing_boosted += 1
        differs = (b["file_rank"] != c["file_rank"]) or (b.get("symbol_rank") != c.get("symbol_rank"))
        if not differs:
            continue
        cls, note = classify(b, c, runs.qdrant_ids, runs.exact.get("questions", {}).get(qid), top_k)
        rows.append(
            {
                "corpus": runs.name,
                "id": qid,
                "set": b["set"],
                "file": f"{_fmt_rank(b['file_rank'])}->{_fmt_rank(c['file_rank'])}",
                "symbol": (
                    f"{_fmt_rank(b.get('symbol_rank'))}->{_fmt_rank(c.get('symbol_rank'))}"
                    if b.get("symbol")
                    else "-"
                ),
                "class": cls,
                "note": note,
            }
        )
    info = {
        "questions": len(runs.baseline),
        "boosted_rankings_agree": agreeing_boosted,
        "vector_scores_compared": compared,
        "max_similarity_delta": max_delta,
        "qdrant_points": len(runs.qdrant_ids),
        "postgres_chunks": runs.qdrant_meta.get("postgres_chunks"),
    }
    return rows, info


def load_corpus(records_dir: Path, name: str, baseline_prefix: str, candidate_prefix: str) -> CorpusRuns:
    bh, b = load_run(_find(records_dir, f"{baseline_prefix}-{name}.jsonl"))
    ch, c = load_run(_find(records_dir, f"{candidate_prefix}-{name}.jsonl"))
    qdrant = load_json(_find(records_dir, f"qdrant_ids-{name}.json"))
    ids = qdrant.get("ids")
    if not isinstance(ids, list) or not ids:
        raise ValueError(
            f"qdrant_ids-{name}.json holds no point ids; with an empty set every chunk would count as "
            "having no point and class (b) would be trivially available, so the comparison is refused"
        )
    if "count" in qdrant and qdrant["count"] != len(ids):
        raise ValueError(f"qdrant_ids-{name}.json: count {qdrant['count']} does not match its {len(ids)} ids")
    exact = load_json(_find(records_dir, f"exact-{name}.json"))
    return CorpusRuns(name, bh, b, ch, c, set(ids), qdrant, exact, notes=[])


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--records", type=Path, required=True, help="directory holding the four files per corpus")
    ap.add_argument("--corpora", nargs="+", default=list(DEFAULT_CORPORA))
    ap.add_argument("--baseline-prefix", default="baseline")
    ap.add_argument("--candidate-prefix", default="pgvector")
    args = ap.parse_args(argv)

    all_runs: List[CorpusRuns] = []
    problems: List[str] = []
    for name in args.corpora:
        try:
            runs = load_corpus(args.records, name, args.baseline_prefix, args.candidate_prefix)
        except (FileNotFoundError, ValueError) as exc:
            problems.append(f"{name}: {exc}")
            continue
        problems.extend(refusals(runs))
        all_runs.append(runs)
    if problems:
        print("REFUSED: nothing was compared.")
        for p in problems:
            print(f"  - {p}")
        return 2
    for runs in all_runs:
        for note in runs.notes:
            print(f"note: {note}")

    table: List[dict] = []
    infos: Dict[str, dict] = {}
    for runs in all_runs:
        rows, info = compare(runs)
        table.extend(rows)
        infos[runs.name] = info

    width = {"corpus": 8, "id": 10, "set": 8, "file": 12, "symbol": 12, "class": 11}
    header = " ".join(f"{k:<{w}}" for k, w in width.items()) + " note"
    print(header)
    print("-" * (len(header) + 40))
    for row in table:
        print(" ".join(f"{str(row[k]):<{w}}" for k, w in width.items()) + f" {row['note']}")
    if not table:
        print("(no question differs in file rank or symbol rank)")
    print("-" * (len(header) + 40))
    counts = {cls: sum(1 for r in table if r["class"] == cls) for cls in CLASSES}
    print(
        f"differing questions: {len(table)}   "
        + "   ".join(f"({cls})={n}" if cls != "UNEXPLAINED" else f"UNEXPLAINED={n}" for cls, n in counts.items())
    )

    print("\nAggregates, reported and not judged (recall@k = found/questions, rank-1, MRR):")
    print(f"{'corpus':<9} {'set':<8} {'side':<9} {'file recall':<12} {'file #1':<8} {'file MRR':<9} {'sym recall':<11} {'sym #1':<7} {'sym MRR':<8}")
    for runs in all_runs:
        for side, records in (("qdrant", runs.baseline), ("pgvector", runs.candidate)):
            for set_name, agg in aggregates(records).items():
                f, s = agg["file"], agg["symbol"]
                print(
                    f"{runs.name:<9} {set_name:<8} {side:<9} "
                    f"{f['found']}/{f['questions']:<9} {f['rank1']:<8} {f['mrr']:<9.3f} "
                    f"{s['found']}/{s['questions']:<8} {s['rank1']:<7} {s['mrr']:<8.3f}"
                )

    print("\nFor information only:")
    for name, info in infos.items():
        no_point = (
            info["postgres_chunks"] - info["qdrant_points"] if info["postgres_chunks"] is not None else "?"
        )
        print(
            f"  {name}: {info['boosted_rankings_agree']}/{info['questions']} questions with fully agreeing "
            f"boosted rankings; max |delta similarity| over {info['vector_scores_compared']} chunk scores "
            f"both legs returned = {info['max_similarity_delta']:.2e}; chunks without a Qdrant point: "
            f"{no_point} ({info['postgres_chunks']} in Postgres, {info['qdrant_points']} points)"
        )

    unexplained = counts["UNEXPLAINED"]
    print(f"\nVERDICT: {'PASS' if unexplained == 0 else 'FAIL'} ({unexplained} UNEXPLAINED)")
    return 1 if unexplained else 0


if __name__ == "__main__":
    sys.exit(main())

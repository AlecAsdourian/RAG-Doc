"""`scripts/rag_benchmarks/decide.py`, QD3's judge (22.2-07).

Hand-made records in 22.2-01's header format: no database, no OpenAI. The
fixtures rank with their own plain rule and hash vectors with their own copy
of the formula, so a record the judge accepts was not built by the code it is
judged with. The rules and specs live in a temporary git repository, committed
in the order each test needs (refusal 7 reads that order).

The chunk-shape rule here is a stand-in written for these tests (one file
clause); the real one is 22.2-04's. M2 is the committed
`embedding-model-rule.json`, copied with its protocol.
"""

from __future__ import annotations

import gzip
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import pytest

_RAG = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "rag_benchmarks"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _RAG / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


decide = _load("decide")

ADA, SMALL = "text-embedding-ada-002", "text-embedding-3-small"
CORPORA = ("miniflux", "mealie", "linkwarden")
SET = "shape-model"
N = 15
PREFIX = {"miniflux": "mf", "mealie": "me", "linkwarden": "lw"}
VERSION = {"current": "c" * 16, "candidate": "d" * 16}
DIGEST = {"current": "a" * 64, "candidate": "b" * 64}
ARMS = {
    "current-ada": ("current", ADA),
    "current-3small": ("current", SMALL),
    "candidate-ada": ("candidate", ADA),
    "candidate-3small": ("candidate", SMALL),
}
APP_ROLE = {"current_user": "rag_doc_app", "session_user": "scratch", "rolsuper": False, "rolbypassrls": False}
M2_RULE = _RAG / "embedding-model-rule.json"
M2_PROTOCOL = _RAG / "embedding-model-protocol.md"
CHUNK_RULE = {
    "rule": "chunk-shape",
    "protocol": "chunk-shape-protocol.md",
    "set": SET,
    "corpora": list(CORPORA),
    "top_k": 5,
    "metric": "mrr@5",
    "variable": "chunker",
    "arms": {"current-ada": {"chunker_version": VERSION["current"]},
             "candidate-ada": {"chunker_version": VERSION["candidate"]}},
    "pair": {"baseline": "current-ada", "candidate": "candidate-ada"},
    "clauses": [{"id": "1", "level": "file", "scope": "pooled", "min_delta": 0}],
    "allowance": 1e-9,
}


# ---------------------------------------------------------------------------
# Fixtures: questions, records, vectors, and a repository holding rules and specs
# ---------------------------------------------------------------------------


def questions(corpus: str) -> List[dict]:
    return [{"id": f"{PREFIX[corpus]}-{i:02d}", "set": SET, "question": f"how does part {i} of {corpus} work",
             "path": f"pkg/{corpus}/f{i:02d}.go", "symbol": f"Type{i}.Run", "evidence": "f.go:1"}
            for i in range(1, N + 1)]


def spec(corpus: str) -> dict:
    tuning = {"id": f"{PREFIX[corpus]}-t1", "set": "tuning", "question": "an older question",
              "path": "pkg/old.go", "symbol": "Old"}
    return {"repository": f"https://example.invalid/{corpus}", "commit": "0" * 40,
            "roots": [{"path": ".", "extensions": [".go"], "language": "go"}],
            "questions": [tuning] + questions(corpus)}


def own_hash(vector: Sequence[float]) -> str:
    """The fixture's own copy of the harness's hash: SHA-256 of the JSON float list."""
    return hashlib.sha256(json.dumps(list(vector), separators=(",", ":")).encode("ascii")).hexdigest()


def vector_of(model: str, qid: str) -> List[float]:
    seed = int(hashlib.md5(f"{model}/{qid}".encode()).hexdigest()[:8], 16)
    return [((seed >> shift) & 255) / 255.0 for shift in (0, 8, 16, 24)]


def record(q: dict, arm: str, file_pos: Optional[int], symbol_pos: Optional[int], depth: int = 25,
           tie_at_cut: bool = False) -> dict:
    """A question record whose ranking before the cut puts the answer's file at
    `file_pos` and its symbol at `symbol_pos` (1-based; None for absent), with
    ranks written by this fixture's own rule over the top five."""
    model = ARMS[arm][1]
    ranking = [{"chunk_id": f"{arm}/{q['id']}/{i}", "file_path": f"pkg/elsewhere/o{i}.go",
                "breadcrumb": "Other.x", "chunk_type": "function"} for i in range(1, depth + 1)]
    if file_pos:
        ranking[file_pos - 1].update(file_path=q["path"], breadcrumb="Unrelated.y")
    if symbol_pos:
        ranking[symbol_pos - 1].update(file_path=q["path"], breadcrumb=q["symbol"])
    scores = [1.0 - 0.01 * i for i in range(depth)]
    if tie_at_cut:
        scores[5] = scores[4] - 1e-6
    top = ranking[:5]
    file_rank = next((i for i, e in enumerate(top, 1) if e["file_path"] == q["path"]), None)
    symbol_rank = next((i for i, e in enumerate(top, 1)
                        if e["file_path"] == q["path"] and e["breadcrumb"] == q["symbol"]), None)
    trace = {
        "fts": [],
        "vector": [dict(e, score=s) for e, s in zip(ranking, scores)],
        "fused": [{"chunk_id": e["chunk_id"], "rrf_score": s, "sources": ["vector"]} for e, s in zip(ranking, scores)],
        "boosted": [{"chunk_id": e["chunk_id"], "rrf_score": s, "boost_multiplier": 1.0, "boosted_score": s}
                    for e, s in zip(ranking, scores)],
        "top": [{"chunk_id": e["chunk_id"], "file_path": e["file_path"], "breadcrumb": e["breadcrumb"], "score": s}
                for e, s in zip(top, scores)],
    }
    return {"record": "question", "id": q["id"], "set": SET, "question": q["question"], "path": q["path"],
            "symbol": q["symbol"], "file_rank": file_rank, "symbol_rank": symbol_rank,
            "top_hit": top[0]["file_path"], "error": None,
            "query_vector_sha256": own_hash(vector_of(model, q["id"])), "trace": trace}


def header(corpus: str, arm: str) -> dict:
    chunker, model = ARMS[arm]
    return {"record": "run", "corpus": corpus, "commit": "e" * 40, "corpus_commit": "e" * 40, "set": SET,
            "top_k": 5, "boost_config": None, "exact_paths": True, "harness_commit": "f" * 40,
            "retrieval_code_version": "1" * 16, "corpus_tree_digest": "2" * 64,
            "vector_backend": "pgvector", "embedding_model": model, "chunker_version": VERSION[chunker],
            "chunk_set_digest": DIGEST[chunker], "chunk_rows": 100, "chunk_models": {model: 100},
            "connections": {"fts": dict(APP_ROLE), "vector": dict(APP_ROLE)},
            "database": {"host": "127.0.0.1", "port": "55999", "connection": dict(APP_ROLE)}}


def _git(cwd: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           "-c", "commit.gpgsign=false", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _add_rules(repo: pathlib.Path) -> None:
    rules = repo / "rules"
    rules.mkdir(exist_ok=True)
    shutil.copyfile(M2_RULE, rules / M2_RULE.name)
    shutil.copyfile(M2_PROTOCOL, rules / M2_PROTOCOL.name)
    (rules / "chunk-shape-rule.json").write_text(json.dumps(CHUNK_RULE, indent=1), encoding="utf-8")
    (rules / "chunk-shape-protocol.md").write_text("# a stand-in chunk-shape protocol\n", encoding="utf-8")


def _add_specs(repo: pathlib.Path, with_set: bool = True, where: str = "specs") -> None:
    """The specs; `with_set=False` writes them without the shape-model questions,
    as they stand before a decision set is written (the real specs predate it)."""
    specs = repo / where
    specs.mkdir(exist_ok=True)
    for corpus in CORPORA:
        content = spec(corpus)
        if not with_set:
            content["questions"] = [q for q in content["questions"] if q["set"] != SET]
        (specs / f"{corpus}.json").write_text(json.dumps(content, indent=1), encoding="utf-8")


def require_git(why: str) -> None:
    """Skip where git is absent (python:3.11-slim, ISS-041), but fail on CI,
    where a silent skip of every order and verdict test would keep the job
    green (PR #67, review B, m6)."""
    if shutil.which("git") is None:
        message = f"git is not installed (e.g. python:3.11-slim): {why} (ISS-041)"
        if os.environ.get("CI"):
            pytest.fail(message + "; CI must have git, so this is not skipped there")
        pytest.skip(message)


def make_repo(repo: pathlib.Path, order: str) -> pathlib.Path:
    """A repository with the rules and the specs committed in `order`:
    rule-first, questions-first, together, merged (a branch with the rule,
    then the questions, merged with a merge commit: QD12's protocol PR), or
    moved (questions written under rb/, then the rule, then the specs moved to
    specs/ with `git mv`: review A's I1). The specs exist without the set
    before any of it, as the real ones do."""
    require_git("decide.py reads a rule's order from git, so every judged run needs a repository")
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    # In the repository's own config, so decide.py's calls read files as written.
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "README").write_text("base\n", encoding="utf-8")
    _add_specs(repo, with_set=False, where="rb" if order == "moved" else "specs")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base, with the specs before the decision set")
    if order == "moved":
        _add_specs(repo, where="rb"); _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "questions")
        _add_rules(repo); _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "rule")
        _git(repo, "mv", "rb", "specs"); _git(repo, "commit", "-q", "-m", "move the specs")
    elif order == "rule-first":
        _add_rules(repo); _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "rule")
        _add_specs(repo); _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "questions")
    elif order == "questions-first":
        _add_specs(repo); _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "questions")
        _add_rules(repo); _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "rule")
    elif order == "together":
        _add_rules(repo); _add_specs(repo); _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "squashed")
    elif order == "merged":
        _git(repo, "checkout", "-q", "-b", "protocol")
        _add_rules(repo); _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "rule")
        _add_specs(repo); _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "questions")
        _git(repo, "checkout", "-q", "main")
        (repo / "README").write_text("main moved on\n", encoding="utf-8")
        _git(repo, "commit", "-q", "-am", "main moved on")
        _git(repo, "merge", "-q", "--no-ff", "-m", "merge the protocol", "protocol")
    else:
        raise ValueError(order)
    return repo


class TestRequireGit:
    """Without git, the order and verdict tests skip off CI and fail on CI (review B, m6)."""

    def test_it_skips_off_ci(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: None)
        monkeypatch.delenv("CI", raising=False)
        with pytest.raises(pytest.skip.Exception, match="git is not installed"):
            require_git("a reason")

    def test_it_fails_on_ci(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: None)
        monkeypatch.setenv("CI", "true")
        with pytest.raises(pytest.fail.Exception, match="CI must have git"):
            require_git("a reason")


@pytest.fixture(scope="session")
def template(tmp_path_factory) -> pathlib.Path:
    return make_repo(tmp_path_factory.mktemp("template") / "repo", "rule-first")


Edit = Callable[[dict, Dict[str, dict]], None]


class World:
    """Four arms over three corpora, written as records, with the rules and specs in `repo`.

    `positions[arm][corpus][i]` is question i's (file position, symbol position)
    in the ranking before the cut. The default puts every file at #1 and every
    symbol at #2 on every arm: all deltas 0, so the stand-in chunk-shape rule
    ADOPTs and M2 (which needs +0.03 at symbol level) REJECTs.
    """

    def __init__(self, root: pathlib.Path, repo: pathlib.Path):
        self.root, self.repo = root, repo
        self.records = root / "records"
        self.vector_files = {ADA: root / "vectors-ada.json", SMALL: root / "vectors-3small.json.gz"}
        self.positions = {arm: {c: [(1, 2)] * N for c in CORPORA} for arm in ARMS}
        self.depth = 25
        self.ties: Dict[Tuple[str, str, int], bool] = {}
        self.edits: List[Tuple[str, str, Edit]] = []
        self.vector_edits: List[Callable[[Dict[str, dict]], None]] = []

    def put(self, arm: str, corpus: str, index: int, file_pos: Optional[int], symbol_pos: Optional[int]) -> None:
        row = list(self.positions[arm][corpus])
        row[index] = (file_pos, symbol_pos)
        self.positions[arm][corpus] = row

    def edit(self, arm: str, corpus: str, fn: Edit) -> None:
        self.edits.append((arm, corpus, fn))

    def write(self) -> None:
        self.records.mkdir(exist_ok=True)
        for arm in ARMS:
            for corpus in CORPORA:
                qs = questions(corpus)
                head = header(corpus, arm)
                recs = {q["id"]: record(q, arm, *self.positions[arm][corpus][i], depth=self.depth,
                                        tie_at_cut=self.ties.get((arm, corpus, i), False))
                        for i, q in enumerate(qs)}
                for e_arm, e_corpus, fn in self.edits:
                    if (e_arm, e_corpus) == (arm, corpus):
                        fn(head, recs)
                lines = [json.dumps(head)] + [json.dumps(r) for r in recs.values()]
                path = self.records / f"{arm}-{corpus}.jsonl.gz"
                with gzip.open(path, "wt", encoding="utf-8") as fh:
                    fh.write("\n".join(lines) + "\n")
        for model, path in self.vector_files.items():
            cache = {q["id"]: {"question": q["question"], "model": model, "vector": vector_of(model, q["id"])}
                     for c in CORPORA for q in questions(c)}
            if model == ADA:
                for fn in self.vector_edits:
                    fn(cache)
            text = json.dumps(cache)
            if path.suffix == ".gz":
                with gzip.open(path, "wt", encoding="utf-8") as fh:
                    fh.write(text)
            else:
                path.write_text(text, encoding="utf-8")

    def rules(self) -> List[pathlib.Path]:
        return [self.repo / "rules" / "chunk-shape-rule.json", self.repo / "rules" / M2_RULE.name]

    def argv(self, rules: Optional[Sequence[pathlib.Path]] = None, vectors: Optional[Dict[str, pathlib.Path]] = None
             ) -> List[str]:
        vectors = self.vector_files if vectors is None else vectors
        return (["--rules", *map(str, rules or self.rules()), "--records", str(self.records),
                 "--specs", str(self.repo / "specs"), "--query-vectors"]
                + [f"{m}={p}" for m, p in vectors.items()])

    def run(self, capsys, **kwargs) -> Tuple[int, str]:
        self.write()
        code = decide.main(self.argv(**kwargs))
        return code, capsys.readouterr().out


@pytest.fixture
def world(tmp_path, template) -> World:
    repo = tmp_path / "repo"
    shutil.copytree(template, repo)
    return World(tmp_path, repo)


def refused(code: int, out: str, fragment: str) -> None:
    assert code == 2, out
    assert out.startswith("REFUSED"), out
    assert "VERDICT" not in out, "a refusal gives no verdict"
    assert fragment in out, f"expected {fragment!r} in:\n{out}"


def clause_line(out: str, rule_heading: str, clause_id: str) -> str:
    section = out.split(f"=== RULE {rule_heading}", 1)[1]
    return next(line for line in section.splitlines() if line.strip().startswith(f"clause {clause_id}:"))


# ---------------------------------------------------------------------------
# A clean run: both rules, the clauses printed, the verdicts and the exit code
# ---------------------------------------------------------------------------


class TestTheCleanRun:
    def test_the_default_arms_adopt_the_chunk_shape_and_reject_m2(self, world, capsys):
        code, out = world.run(capsys)
        assert code == 1, out
        assert "VERDICT: ADOPT (chunk-shape)" in out
        assert "VERDICT: REJECT (M2)" in out
        assert "Rules run: chunk-shape ADOPT, M2 REJECT" in out
        # The order of record the check verified, for a protocol to cite (review A, N3).
        rule_commit = _git(world.repo, "log", "--format=%h", "-n1", "--abbrev=12", "--", "rules/" + M2_RULE.name)
        question_commit = _git(world.repo, "log", "--format=%h", "-n1", "--abbrev=12", "--", "specs/miniflux.json")
        order = out.split("Order of record verified", 1)[1].split("=== RULE", 1)[0]
        assert f"M2: {M2_RULE.name} changed in {rule_commit}" in order, order
        assert f"M2: miniflux's shape-model questions arrived in {question_commit}" in order, order
        for clause in ("1", "2", "3"):
            assert clause_line(out, "M2", clause)
        assert "FAILS" in clause_line(out, "M2", "1") and "+0.000000" in clause_line(out, "M2", "1")
        assert "HOLDS" in clause_line(out, "M2", "2")
        line3 = clause_line(out, "M2", "3")
        assert all(f"{c} +0.000000 holds" in line3 for c in CORPORA), line3

    def test_every_rule_adopting_exits_0(self, world, capsys):
        for i in range(3):
            world.put("candidate-3small", "miniflux", i, 1, 1)  # symbol #2 -> #1: +1.5/45 = +0.0333
        code, out = world.run(capsys)
        assert code == 0, out
        assert "VERDICT: ADOPT (M2)" in out

    def test_it_prints_the_aggregates_mrr_at_20_the_table_and_the_ties(self, world, capsys):
        world.put("candidate-3small", "mealie", 0, 8, None)   # the file at #8: a miss at 5, 1/8 at 20
        world.put("candidate-3small", "mealie", 1, 6, 6)      # at #6, tied with #5 at QD2's tolerance
        world.ties[("candidate-3small", "mealie", 1)] = True
        code, out = world.run(capsys)
        assert code == 1, out
        m2 = out.split("=== RULE M2", 1)[1]
        # per corpus and pooled, file and symbol, MRR recall rank-1 at 5
        mealie_file = next(ln for ln in m2.splitlines() if ln.strip().startswith("mealie") and " file " in ln)
        assert "1.0000  15/15  15/15" in mealie_file and "0.8667  13/15  13/15" in mealie_file
        assert "-0.1333" in mealie_file
        assert any(ln.strip().startswith("pooled") and " symbol " in ln for ln in m2.splitlines())
        # MRR@20, from the ranking before the cut: (13 + 1/8 + 1/6) / 15
        at20 = m2.split("MRR@20", 1)[1].split("Per question", 1)[0]
        mealie20 = next(ln for ln in at20.splitlines() if ln.strip().startswith("mealie") and " file " in ln)
        assert f"1.0000 -> {(13 + 1 / 8 + 1 / 6) / 15:.4f}" in mealie20
        # the per-question table
        assert re.search(r"me-01\s+mealie\s+#1 -> MISS\s+#2 -> MISS", m2)
        # the tie at the cut, reported only
        ties = m2.split("tie at the cut", 1)[1]
        assert re.search(r"me-02\s+candidate-3small\s+file #6 is tied with the cut \(positions 5-6\)", ties), ties
        assert "symbol #6 is tied with the cut" in ties

    def test_a_boosted_chunk_found_by_keyword_search_only_is_joined_to_that_leg(self, world, capsys):
        """The answer's chunk came from the keyword leg alone: MRR@20 still scores it."""
        world.put("candidate-3small", "linkwarden", 0, 9, None)

        def keyword_only(h, r):
            trace = r["lw-01"]["trace"]
            entry = next(e for e in trace["vector"] if e["file_path"] == r["lw-01"]["path"])
            trace["vector"].remove(entry)
            trace["fts"].append(entry)
        world.edit("candidate-3small", "linkwarden", keyword_only)
        code, out = world.run(capsys)
        assert code == 1, out
        at20 = out.split("=== RULE M2", 1)[1].split("MRR@20", 1)[1].split("Per question", 1)[0]
        line = next(ln for ln in at20.splitlines() if ln.strip().startswith("linkwarden") and " file " in ln)
        assert f"1.0000 -> {(14 + 1 / 9) / 15:.4f}" in line, line

    def test_mrr_at_20_is_unavailable_when_a_trace_holds_fewer_than_20(self, world, capsys):
        world.depth = 12
        code, out = world.run(capsys)
        assert code == 1, out
        assert "unavailable (fewer than 20 ranked results)" in out.split("MRR@20", 1)[1]

    def test_the_cli_runs_as_a_script(self, world):
        world.write()
        result = subprocess.run([sys.executable, str(_RAG / "decide.py"), *world.argv()],
                                capture_output=True, text=True, encoding="utf-8")
        assert result.returncode == 1, result.stdout + result.stderr
        assert "VERDICT: REJECT (M2)" in result.stdout


# ---------------------------------------------------------------------------
# The verdict boundaries, per clause
# ---------------------------------------------------------------------------


def m2_rule() -> dict:
    return json.loads(M2_RULE.read_text(encoding="utf-8"))


def test_pooled_mrr_at_20_scores_each_corpus_with_its_own_exact_paths(world, capsys):
    """mealie matches paths as substrings here; miniflux, the first corpus, exactly.
    A chunk at #6 whose path contains mealie's expected path counts for mealie
    (1/6), not the answer at #8 (1/8), in mealie's line and in the pool (review A, N1)."""
    for arm in ARMS:
        world.edit(arm, "mealie", lambda h, r: h.update(exact_paths=False))
    world.put("candidate-3small", "mealie", 0, 8, None)

    def near_miss(h, r):
        trace = r["me-01"]["trace"]
        trace["vector"][5]["file_path"] = "vendor/" + r["me-01"]["path"]
    world.edit("candidate-3small", "mealie", near_miss)
    code, out = world.run(capsys)
    assert code == 1, out
    at20 = out.split("=== RULE M2", 1)[1].split("MRR@20", 1)[1].split("Per question", 1)[0]
    mealie = next(ln for ln in at20.splitlines() if ln.strip().startswith("mealie") and " file " in ln)
    pooled = next(ln for ln in at20.splitlines() if ln.strip().startswith("pooled") and " file " in ln)
    assert f"-> {(14 + 1 / 6) / 15:.4f}" in mealie, mealie
    assert f"-> {(44 + 1 / 6) / 45:.4f}" in pooled, pooled


class TestTheBoundaries:
    @pytest.mark.parametrize("clause_id", ["1", "2", "3"])
    def test_exactly_at_the_threshold_minus_the_allowance_holds_and_2e9_past_it_fails(self, clause_id):
        """The production predicate, on M2's committed numbers."""
        rule = m2_rule()
        clause = next(c for c in rule["clauses"] if c["id"] == clause_id)
        at = clause["min_delta"] - rule["allowance"]
        assert decide.holds(at, clause["min_delta"], rule["allowance"])
        assert not decide.holds(at - 2e-9, clause["min_delta"], rule["allowance"])
        assert decide.holds(clause["min_delta"], clause["min_delta"], rule["allowance"])

    def test_clause_1_adopts_at_exactly_plus_0_03_and_rejects_one_step_short(self, world, capsys):
        # baseline (candidate-ada): symbol #5, #2, #5; candidate: #1, #1, #4.
        # +0.8 + 0.5 + 0.05 = 1.35 over 45 questions: exactly +0.03.
        for i, sym in enumerate((5, 2, 5)):
            world.put("candidate-ada", "miniflux", i, 1, sym)
            world.put("current-ada", "miniflux", i, 1, sym)  # the chunk-shape rule reads file level only
        for i, sym in enumerate((1, 1, 4)):
            world.put("candidate-3small", "miniflux", i, 1, sym)
        code, out = world.run(capsys)
        assert code == 0, out
        assert "+0.030000 -> HOLDS" in clause_line(out, "M2", "1")
        world.put("candidate-3small", "miniflux", 2, 1, 5)    # one rank short: 1.30 / 45
        code, out = world.run(capsys)
        assert code == 1, out
        assert "+0.028889 -> FAILS" in clause_line(out, "M2", "1")
        assert "VERDICT: REJECT (M2)" in out

    def test_clause_2_adopts_at_zero_and_rejects_one_step_below(self, world, capsys):
        for i in range(3):
            world.put("candidate-3small", "miniflux", i, 1, 1)   # clause 1 holds: +0.0333
        world.put("candidate-ada", "mealie", 5, 4, None)
        world.put("current-ada", "mealie", 5, 4, None)
        world.put("candidate-3small", "mealie", 5, 4, None)      # file delta exactly 0
        code, out = world.run(capsys)
        assert code == 0, out
        assert "+0.000000 -> HOLDS" in clause_line(out, "M2", "2")
        world.put("candidate-3small", "mealie", 5, 5, None)      # #4 -> #5: -0.05 / 45
        code, out = world.run(capsys)
        assert code == 1, out
        assert "-0.001111 -> FAILS" in clause_line(out, "M2", "2")

    def test_clause_3_adopts_at_exactly_minus_0_11_and_rejects_one_step_past(self, world, capsys):
        # linkwarden falls by 1 + 0.5 + 3 x 0.05 = 1.65 over 15: exactly -0.11.
        # miniflux gains 4 x 0.5 = 2.0, so the pooled file delta stays >= 0;
        # miniflux's symbol #2 -> #1 on 3 questions keeps clause 1 holding.
        base = {"linkwarden": [(1, None), (1, None), (4, None), (4, None), (4, None), (4, None)],
                "miniflux": [(2, None)] * 4}
        cand = {"linkwarden": [(None, None), (2, None), (5, None), (5, None), (5, None), (4, None)],
                "miniflux": [(1, None)] * 4}
        for arm in ("current-ada", "candidate-ada", "current-3small"):
            for corpus, rows in base.items():
                for i, (f, s) in enumerate(rows):
                    world.put(arm, corpus, i, f, s)
        for corpus, rows in cand.items():
            for i, (f, s) in enumerate(rows):
                world.put("candidate-3small", corpus, i, f, s)
        for i in range(4, 7):
            world.put("candidate-3small", "miniflux", i, 1, 1)
        code, out = world.run(capsys)
        assert code == 0, out
        line = clause_line(out, "M2", "3")
        assert "linkwarden -0.110000 holds" in line and "-> HOLDS" in line, line
        world.put("candidate-3small", "linkwarden", 5, 5, None)   # one rank further: -1.70 / 15
        code, out = world.run(capsys)
        assert code == 1, out
        line = clause_line(out, "M2", "3")
        assert "linkwarden -0.113333 FAILS" in line and "-> FAILS" in line, line
        assert "HOLDS" in clause_line(out, "M2", "2")


# ---------------------------------------------------------------------------
# The rule order: the chunk-shape verdict chooses M2's arms
# ---------------------------------------------------------------------------


class TestTheRuleOrder:
    """The two M2 pairs are built to give different verdicts, so judging the
    wrong chunker's arms changes the outcome."""

    def _m2_adopts_on(self, world: World, chunker: str) -> None:
        for i in range(3):
            world.put(f"{chunker}-3small", "miniflux", i, 1, 1)

    def test_chunk_shape_adopt_judges_m2_on_the_candidate_arms(self, world, capsys):
        self._m2_adopts_on(world, "candidate")
        code, out = world.run(capsys)
        assert "VERDICT: ADOPT (chunk-shape)" in out
        assert "baseline candidate-ada (text-embedding-ada-002) vs candidate candidate-3small" in out
        assert "chosen by chunk-shape's ADOPT" in out
        assert "VERDICT: ADOPT (M2)" in out and code == 0, out

    def test_chunk_shape_reject_judges_m2_on_the_current_arms(self, world, capsys):
        self._m2_adopts_on(world, "candidate")        # would adopt, but is not the arm judged
        world.put("candidate-ada", "mealie", 0, 2, 2)  # the candidate chunker loses a file rank
        code, out = world.run(capsys)
        assert "VERDICT: REJECT (chunk-shape)" in out
        assert "baseline current-ada (text-embedding-ada-002) vs candidate current-3small" in out
        assert "chosen by chunk-shape's REJECT" in out
        assert "VERDICT: REJECT (M2)" in out and code == 1, out

    def test_chunk_shape_reject_with_current_arms_that_adopt(self, world, capsys):
        self._m2_adopts_on(world, "current")
        world.put("candidate-ada", "mealie", 0, 2, 2)
        code, out = world.run(capsys)
        assert "VERDICT: REJECT (chunk-shape)" in out
        assert "VERDICT: ADOPT (M2)" in out
        assert code == 1, "the chunk-shape rule rejected, so not every rule adopted"


# ---------------------------------------------------------------------------
# One test per refusal
# ---------------------------------------------------------------------------


def _first(recs: Dict[str, dict]) -> dict:
    return recs[sorted(recs)[0]]


class TestRefusal1QuestionSets:
    def test_a_question_text_that_differs_is_refused(self, world, capsys):
        world.edit("current-3small", "mealie", lambda h, r: _first(r).update(question="reworded"))
        refused(*world.run(capsys), "current-3small-mealie: me-01: the question's question differs from the spec's")

    def test_a_question_id_that_differs_is_refused(self, world, capsys):
        world.edit("candidate-ada", "miniflux", lambda h, r: r.pop(sorted(r)[-1]))
        refused(*world.run(capsys), "candidate-ada-miniflux: the question ids differ from the spec's shape-model set")

    def test_the_arms_question_sets_are_compared_with_each_other_too(self, world, capsys):
        world.edit("current-ada", "linkwarden", lambda h, r: _first(r).update(symbol="Type1.Stop"))
        refused(*world.run(capsys), "linkwarden: current-ada vs candidate-ada: lw-01: the question's symbol differs "
                                    "between the arms")

    def test_a_symbol_clause_over_a_question_with_no_symbol_is_refused(self, world, capsys):
        """M2's symbol MRR is over all the questions, so every one must name a symbol."""
        spec_path = world.repo / "specs" / "miniflux.json"
        changed = json.loads(spec_path.read_text(encoding="utf-8"))
        next(q for q in changed["questions"] if q["id"] == "mf-01").pop("symbol")
        spec_path.write_text(json.dumps(changed), encoding="utf-8")
        _git(world.repo, "commit", "-q", "-am", "mf-01 names no symbol")

        def drop_symbol(h, r):
            r["mf-01"].update(symbol=None, symbol_rank=None)
        for arm in ARMS:
            world.edit(arm, "miniflux", drop_symbol)
        refused(*world.run(capsys), "M2 has a symbol clause, and ['mf-01'] name no symbol")

    @pytest.mark.parametrize("key, value, fragment", [
        ("set", "tuning", "the header's set is 'tuning', not the rule's 'shape-model'"),
        ("top_k", 10, "the header's top_k is 10, not the rule's 5"),
        ("corpus", "mealie", "the header's corpus is 'mealie'"),
    ])
    def test_arms_that_agree_with_each_other_but_not_the_rule_are_refused(self, world, capsys, key, value, fragment):
        for arm in ARMS:
            world.edit(arm, "miniflux", lambda h, r: h.update({key: value}))
        code, out = world.run(capsys)
        refused(code, out, fragment)
        assert "the arms differ" not in out, "every arm agrees, so only the check against the rule can refuse"

    def test_a_spec_with_no_question_in_the_set_is_refused(self, world, capsys):
        # Both rules move, since an after-rule must share its prior rule's set.
        for name in (M2_RULE.name, "chunk-shape-rule.json"):
            rule = json.loads((world.repo / "rules" / name).read_text(encoding="utf-8"))
            rule["set"] = "keyword-leg"
            (world.repo / "rules" / name).write_text(json.dumps(rule), encoding="utf-8")
        _git(world.repo, "commit", "-q", "-am", "both rules on another set")
        refused(*world.run(capsys), "miniflux's spec has no question in set 'keyword-leg'")


class TestRefusal2Ranks:
    def test_a_rank_its_final_list_does_not_give_is_refused(self, world, capsys):
        world.edit("candidate-3small", "miniflux", lambda h, r: _first(r).update(file_rank=2))
        refused(*world.run(capsys), "candidate-3small-miniflux: mf-01: the record's ranks (2, 2) are not what its "
                                    "final list gives (1, 2)")

    def test_a_boosted_chunk_in_neither_leg_is_refused(self, world, capsys):
        def drop(h, r):
            _first(r)["trace"]["vector"].pop()
        world.edit("current-ada", "mealie", drop)
        refused(*world.run(capsys), "current-ada-mealie: me-01: boosted chunk current-ada/me-01/25 is in neither leg")

    def test_a_final_list_longer_than_the_cut_is_refused(self, world, capsys):
        """A sixth result, consistent with the record's own ranks, would still be
        scored by ranks() if allowed (review A, M2)."""
        def longer(h, r):
            top = _first(r)["trace"]["top"]
            top.append({"chunk_id": "extra", "file_path": "pkg/elsewhere/extra.go", "breadcrumb": "Other.x",
                        "score": 0.1})
        world.edit("candidate-ada", "linkwarden", longer)
        refused(*world.run(capsys), "candidate-ada-linkwarden: lw-01: the final list holds 6 results, more than the "
                                    "cut's top_k 5")


class TestRefusal3Connections:
    def test_a_run_not_on_pgvector_is_refused(self, world, capsys):
        """Every arm agrees, so only the backend check can refuse; off pgvector
        the vector leg's connection would not be required (review A, M1)."""
        def keyword_leg_only(h, r):
            h["vector_backend"] = "qdrant"
            del h["connections"]["vector"]
        for arm in ARMS:
            world.edit(arm, "mealie", keyword_leg_only)
        code, out = world.run(capsys)
        refused(code, out, "the run's vector backend is 'qdrant', not 'pgvector'")
        assert "the arms differ" not in out


    @pytest.mark.parametrize("where, change, fragment", [
        ("fts", {"rolsuper": True}, "the run's fts connection was rag_doc_app (rolsuper=True"),
        ("vector", {"rolbypassrls": True}, "the run's vector connection was rag_doc_app (rolsuper=False, rolbypassrls=True"),
        ("database", {"rolsuper": True}, "the run's database connection was rag_doc_app (rolsuper=True"),
    ])
    def test_a_superuser_or_rls_bypassing_connection_is_refused(self, world, capsys, where, change, fragment):
        def edit(h, r):
            target = h["database"]["connection"] if where == "database" else h["connections"][where]
            target.update(change)
        world.edit("current-3small", "linkwarden", edit)
        refused(*world.run(capsys), fragment)

    @pytest.mark.parametrize("drop, fragment", [
        ("connections", "records no measuring connection"),
        ("vector", "records no vector connection"),
        ("database", "records no database connection stating rolsuper and rolbypassrls"),
    ])
    def test_an_unrecorded_connection_is_refused(self, world, capsys, drop, fragment):
        def edit(h, r):
            if drop == "vector":
                del h["connections"]["vector"]
            else:
                del h[drop]
        world.edit("candidate-ada", "miniflux", edit)
        refused(*world.run(capsys), fragment)


class TestRefusal4FailedQuery:
    def test_a_failed_query_is_refused(self, world, capsys):
        world.edit("current-ada", "miniflux", lambda h, r: _first(r).update(error="RetrievalError: vector failed"))
        refused(*world.run(capsys), "current-ada-miniflux: mf-01: the query failed")


class TestRefusal5ArmsDiffer:
    @pytest.mark.parametrize("key, value", [
        ("corpus", "other"), ("commit", "9" * 40), ("set", "tuning"), ("top_k", 10),
        ("boost_config", {"breadcrumb_match_boost": 2.0}), ("exact_paths", False),
        ("harness_commit", "8" * 40), ("vector_backend", "qdrant"),
        ("retrieval_code_version", "9" * 16), ("corpus_tree_digest", "8" * 64),   # review A, I3
    ])
    def test_arms_that_differ_in_a_shared_key_are_refused(self, world, capsys, key, value):
        world.edit("candidate-3small", "mealie", lambda h, r: h.update({key: value}))
        refused(*world.run(capsys), f"mealie: candidate-ada vs candidate-3small: the arms differ in {key}")

    def test_a_model_comparison_with_different_chunk_set_digests_is_refused(self, world, capsys):
        world.edit("current-3small", "linkwarden", lambda h, r: h.update(chunk_set_digest="9" * 64))
        refused(*world.run(capsys), "linkwarden: current-ada vs current-3small: a model comparison needs equal "
                                    "chunk-set digests")

    def test_a_chunk_comparison_across_models_is_refused(self, world, capsys):
        world.edit("candidate-ada", "miniflux", lambda h, r: h.update(embedding_model=SMALL))
        refused(*world.run(capsys), "miniflux: current-ada vs candidate-ada: a chunk comparison needs one model")

    def test_a_chunk_comparison_with_another_query_vector_is_refused(self, world, capsys):
        world.edit("candidate-ada", "mealie", lambda h, r: _first(r).update(query_vector_sha256="f" * 64))
        refused(*world.run(capsys), "mealie: current-ada vs candidate-ada: me-01: a chunk comparison needs the same "
                                    "query vector on both arms")

    def test_an_arm_that_is_not_its_declared_model_is_refused(self, world, capsys):
        world.edit("current-3small", "mealie", lambda h, r: h.update(embedding_model="text-embedding-3-large"))
        refused(*world.run(capsys), "current-3small-mealie: the header's model is 'text-embedding-3-large'; the rule "
                                    "declares 'text-embedding-3-small'")

    def test_an_arm_that_is_not_its_declared_chunker_is_refused(self, world, capsys):
        world.edit("candidate-ada", "linkwarden", lambda h, r: h.update(chunker_version=None))
        refused(*world.run(capsys), "candidate-ada-linkwarden: the header's chunker_version is None; the rule declares "
                                    f"'{VERSION['candidate']}'")

    def test_the_unchosen_pair_is_checked_too(self, world, capsys):
        """The chunk-shape rule adopts, so M2 is judged on the candidate arms;
        the current pair is still refused before anything is compared."""
        world.edit("current-3small", "miniflux", lambda h, r: h.update(boost_config={"x": 1}))
        refused(*world.run(capsys), "miniflux: current-ada vs current-3small: the arms differ in boost_config")


class TestRefusal6QueryVectors:
    def test_a_vector_embedded_with_another_model_is_refused(self, world, capsys):
        world.vector_edits.append(lambda cache: cache["mf-03"].update(model=SMALL))
        refused(*world.run(capsys), f"mf-03: the cached vector was embedded with '{SMALL}', and the run used '{ADA}'")

    def test_a_vector_whose_hash_is_not_the_records_is_refused(self, world, capsys):
        world.vector_edits.append(lambda cache: cache["lw-07"].update(vector=[0.5, 0.5, 0.5, 0.5]))
        refused(*world.run(capsys), "lw-07: the cached vector's hash is not the record's query_vector_sha256")

    def test_a_missing_vector_or_text_is_refused(self, world, capsys):
        world.vector_edits.append(lambda cache: cache.pop("me-04"))
        world.vector_edits.append(lambda cache: cache["me-05"].update(question="another question"))
        code, out = world.run(capsys)
        refused(code, out, "me-04: no cached vector in text-embedding-ada-002's file")
        assert "me-05: the cached vector is for another question text" in out

    def test_a_model_with_no_vectors_file_is_refused(self, world, capsys):
        world.write()
        code = decide.main(world.argv(vectors={ADA: world.vector_files[ADA]}))
        refused(code, capsys.readouterr().out, f"no --query-vectors file for its model '{SMALL}'")

    def test_the_judges_hash_is_the_harness_formula(self):
        vector = [0.1, -0.25, 3e-7, 1.0]
        assert decide.vector_sha256(vector) == own_hash(vector)


class TestRefusal7Order:
    """The rule must be committed, whole, before its questions (read from git)."""

    def _world(self, tmp_path, order: str) -> World:
        return World(tmp_path, make_repo(tmp_path / "repo", order))

    def test_rule_then_questions_passes(self, tmp_path, capsys):
        code, out = self._world(tmp_path, "rule-first").run(capsys)
        assert code == 1 and "VERDICT: REJECT (M2)" in out, out

    def test_a_protocol_pr_merged_with_a_merge_commit_passes(self, tmp_path, capsys):
        """QD12: the rule and the questions reach main through a merge commit."""
        code, out = self._world(tmp_path, "merged").run(capsys)
        assert code == 1 and "VERDICT: REJECT (M2)" in out, out

    def test_questions_then_rule_is_refused(self, tmp_path, capsys):
        code, out = self._world(tmp_path, "questions-first").run(capsys)
        refused(code, out, "is not an ancestor of")
        assert "the rule came after them" in out

    def test_one_commit_adding_both_is_refused(self, tmp_path, capsys):
        code, out = self._world(tmp_path, "together").run(capsys)
        refused(code, out, "changes the rule and adds miniflux's shape-model questions at once")

    def test_specs_moved_after_the_rule_are_refused(self, tmp_path, capsys):
        """Questions, then the rule, then `git mv` of the specs: at the new path
        the move looks like the questions' first commit (review A, I1)."""
        code, out = self._world(tmp_path, "moved").run(capsys)
        refused(code, out, "together with its shape-model questions (a new file or a rename)")

    def test_git_missing_at_run_time_is_named_as_such(self, world, capsys, monkeypatch):
        def no_git(cwd, *args):
            raise FileNotFoundError("git")
        world.write()
        monkeypatch.setattr(decide, "git", no_git)
        refused(decide.main(world.argv()), capsys.readouterr().out,
                f"git is not installed, so the order of the rule {M2_RULE.name} and its questions cannot be read")

    def test_a_rule_changed_after_its_questions_is_refused(self, world, capsys):
        rule_path = world.repo / "rules" / M2_RULE.name
        rule = json.loads(rule_path.read_text(encoding="utf-8"))
        rule["description"] = "edited after the questions"
        rule_path.write_text(json.dumps(rule), encoding="utf-8")
        _git(world.repo, "commit", "-q", "-am", "edit the rule")
        refused(*world.run(capsys), "the rule came after them")

    def test_an_uncommitted_rule_is_refused(self, world, capsys):
        rule_path = world.repo / "rules" / M2_RULE.name
        rule_path.write_text(rule_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        refused(*world.run(capsys), f"the rule {M2_RULE.name} is uncommitted or differs from HEAD")

    def test_uncommitted_questions_are_refused(self, world, capsys):
        spec_path = world.repo / "specs" / "mealie.json"
        spec_path.write_text(spec_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        refused(*world.run(capsys), "mealie's spec mealie.json is uncommitted or differs from HEAD")

    def test_a_rule_outside_any_repository_is_refused(self, world, capsys, tmp_path_factory):
        outside = tmp_path_factory.mktemp("no-git")
        shutil.copyfile(M2_RULE, outside / M2_RULE.name)
        shutil.copyfile(M2_PROTOCOL, outside / M2_PROTOCOL.name)
        rules = [world.repo / "rules" / "chunk-shape-rule.json", outside / M2_RULE.name]
        world.write()
        code = decide.main(world.argv(rules=rules))
        out = capsys.readouterr().out
        assert code == 2 and "VERDICT" not in out, out
        assert "is not in a git checkout" in out or "is not in the rule's repository" in out, out


class TestRefusal8Schema:
    @pytest.mark.parametrize("change, fragment", [
        (lambda r: r.pop("allowance"), "missing ['allowance']"),
        (lambda r: r.update(weights={"x": 1}), "unknown keys ['weights']"),
        (lambda r: r["clauses"][0].update(level="chunk"), "level must be one of"),
        (lambda r: r["clauses"][0].update(scope="median"), "scope must be one of"),
        (lambda r: r["clauses"][0].update(min_delta="0.03"), "min_delta must be a finite number"),
        (lambda r: r["clauses"].append(dict(r["clauses"][0])), "clause ids must be distinct"),
        (lambda r: r.update(metric="mrr@10"), "metric must be 'mrr@5'"),
        (lambda r: r.update(allowance=0.01), "allowance must be a number in [0, 1e-06)"),
        (lambda r: r.update(variable="fusion"), "variable must be one of"),
        (lambda r: r.update(pair={"baseline": "current-ada", "candidate": "current-3small"}),
         "a rule has either pair, or after with arms_by_verdict"),
        (lambda r: r["arms_by_verdict"].pop("REJECT"), "exactly ADOPT and REJECT"),
        (lambda r: r["arms_by_verdict"]["ADOPT"].update(candidate="nowhere"), "names 'nowhere', which is not an arm"),
        (lambda r: r["arms_by_verdict"]["ADOPT"].update(candidate="current-ada"), "declare the same value"),
        (lambda r: r.update(protocol="missing-protocol.md"), "its protocol missing-protocol.md is not beside it"),
        (lambda r: r.update(corpora=[]), "corpora must be a non-empty list"),
    ])
    def test_a_rule_that_fails_the_schema_is_refused(self, world, capsys, change, fragment):
        rule = m2_rule()
        change(rule)
        path = world.root / M2_RULE.name
        path.write_text(json.dumps(rule), encoding="utf-8")
        shutil.copyfile(M2_PROTOCOL, world.root / M2_PROTOCOL.name)
        refused(*world.run(capsys, rules=[world.repo / "rules" / "chunk-shape-rule.json", path]), fragment)

    def test_m2_alone_is_refused_because_its_after_rule_was_not_applied(self, world, capsys):
        refused(*world.run(capsys, rules=[world.repo / "rules" / M2_RULE.name]),
                "after names 'chunk-shape', which is not a rule applied before it")

    def test_m2_before_the_chunk_shape_rule_is_refused(self, world, capsys):
        refused(*world.run(capsys, rules=list(reversed(world.rules()))),
                "after names 'chunk-shape', which is not a rule applied before it")

    def _chunk_rule_beside(self, world, change) -> pathlib.Path:
        rule = json.loads(json.dumps(CHUNK_RULE))
        change(rule)
        path = world.root / "chunk-shape-rule.json"
        path.write_text(json.dumps(rule), encoding="utf-8")
        (world.root / "chunk-shape-protocol.md").write_text("# stand-in\n", encoding="utf-8")
        return path

    def test_a_chunker_variant_no_header_records_is_refused(self, world, capsys):
        """The schema accepts only what a header records (review A, M3)."""
        path = self._chunk_rule_beside(world, lambda r: r["arms"]["candidate-ada"].update(chunker_variant="b"))
        refused(*world.run(capsys, rules=[path, world.repo / "rules" / M2_RULE.name]),
                "arm candidate-ada must declare exactly {\"chunker_version\": <string>}")

    @pytest.mark.parametrize("key, value", [("set", "keyword-leg"), ("corpora", ["miniflux", "mealie"])])
    def test_an_after_rule_on_other_questions_is_refused(self, world, capsys, key, value):
        """Its verdict would choose M2's arms from other questions (review A, N2)."""
        path = self._chunk_rule_beside(world, lambda r: r.update({key: value}))
        refused(*world.run(capsys, rules=[path, world.repo / "rules" / M2_RULE.name]),
                f"after names 'chunk-shape', judged on {key} {value!r}, not this rule's")

    def test_the_committed_m2_rule_passes_the_schema(self):
        assert decide.schema_problems(m2_rule(), M2_RULE) == []


def test_a_malformed_record_is_refused_not_judged(world, capsys):
    world.edit("candidate-3small", "mealie", lambda h, r: _first(r)["trace"].update(boosted=[{"chunk_id": None}]))
    code, out = world.run(capsys)
    assert code == 2 and out.startswith("REFUSED") and "VERDICT" not in out, out
    # Which refusal: the boosted entry has no score, a KeyError the checks did
    # not foresee, refused by main's catch-all (review B, m5 d).
    assert "the inputs could not be judged (KeyError: 'boosted_score')" in out, out


class TestUnreadableInput:
    """Nothing that cannot be read exits 1, REJECT's code (review A, I2)."""

    @staticmethod
    def _truncate(path: pathlib.Path) -> None:
        data = path.read_bytes()
        path.write_bytes(data[: len(data) // 2])

    def test_a_truncated_record_is_refused(self, world, capsys):
        world.write()
        self._truncate(world.records / "candidate-3small-mealie.jsonl.gz")
        refused(decide.main(world.argv()), capsys.readouterr().out, "candidate-3small-mealie: cannot be read (EOFError")

    def test_a_truncated_query_vectors_file_is_refused(self, world, capsys):
        world.write()
        self._truncate(world.vector_files[SMALL])
        refused(decide.main(world.argv()), capsys.readouterr().out,
                f"--query-vectors {SMALL}: {world.vector_files[SMALL]} cannot be read (EOFError)")

    def test_any_unforeseen_exception_exits_2(self, world, capsys, monkeypatch):
        def broken(*args, **kwargs):
            raise RuntimeError("nobody foresaw this")
        world.write()
        monkeypatch.setattr(decide, "report", broken)
        code = decide.main(world.argv())
        out = capsys.readouterr().out
        assert code == 2 and out.startswith("REFUSED: the inputs could not be judged (RuntimeError"), out
        assert "VERDICT" not in out


# ---------------------------------------------------------------------------
# M2's JSON encodes its protocol
# ---------------------------------------------------------------------------


def _section(markdown: str, title: str) -> str:
    match = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", markdown, re.S | re.M)
    assert match, f"no section {title!r}"
    return match.group(1)


class TestM2EncodesItsProtocol:
    """The numbers, levels and scopes in `embedding-model-rule.json` are read
    back from `embedding-model-protocol.md`'s Rule and Precision sections."""

    md = M2_PROTOCOL.read_text(encoding="utf-8")
    rule = m2_rule()

    def _items(self) -> List[str]:
        rule_text = _section(self.md, "Rule")
        items = re.findall(r"^\d\. (.*?)(?=^\d\. |^Both numbers|\Z)", rule_text, re.S | re.M)
        assert len(items) == 3, items
        return [" ".join(i.split()) for i in items]

    def test_the_rule_sections_three_clauses_are_the_jsons(self):
        parsed = []
        t_value = None
        for body in self._items():
            level = "symbol" if "symbol MRR@5" in body else "file" if "file MRR@5" in body else None
            scope = ("pooled" if "over all 45 questions" in body
                     else "each_corpus" if "on each app's 15 questions" in body else None)
            at_least = re.search(r"by at least \*\*([0-9.]+)\*\*", body)
            more_than = re.search(r"by more than \*\*T = ([0-9.]+)\*\*", body)
            if at_least:
                min_delta = float(at_least.group(1))
            elif more_than:
                t_value = float(more_than.group(1))
                min_delta = -t_value
            else:
                assert "is not lower than" in body, body
                min_delta = 0.0
            parsed.append((level, scope, min_delta))
        assert parsed == [(c["level"], c["scope"], c["min_delta"]) for c in self.rule["clauses"]]
        assert parsed == [("symbol", "pooled", 0.03), ("file", "pooled", 0.0), ("file", "each_corpus", -0.11)]
        assert t_value == 0.11
        assert "Symbol MRR is not guarded per app." in self._items()[2]
        assert not any(c["level"] == "symbol" and c["scope"] == "each_corpus" for c in self.rule["clauses"])

    def test_the_precision_sections_comparisons_are_the_jsons_thresholds(self):
        precision = " ".join(_section(self.md, "Precision, fixed now").split())
        allowance = re.search(r"Comparisons allow (\S+), for floating-point rounding only", precision)
        assert allowance and float(allowance.group(1)) == self.rule["allowance"] == 1e-9
        expressions = re.findall(r"is Δ ≥ (.+?)(?:;|\.(?=\s|$))", precision)
        assert len(expressions) == 3, expressions
        thresholds = []
        for expr in expressions:
            expr = expr.replace("−", "-").replace("T", "0.11").replace(" ", "")
            assert re.fullmatch(r"[-0-9.e]+", expr), expr
            thresholds.append(eval(expr, {"__builtins__": {}}))  # noqa: S307 (digits and minus only)
        assert thresholds == [c["min_delta"] - self.rule["allowance"] for c in self.rule["clauses"]]
        assert "**MRR@5**" in precision and self.rule["metric"] == "mrr@5" and self.rule["top_k"] == 5
        assert "Pooled** means over all 45 questions, each weighted equally" in precision

    def test_the_arms_corpora_and_models_are_the_protocols(self):
        test_set = _section(self.md, "Test set")
        assert "miniflux, mealie and linkwarden" in test_set
        assert self.rule["corpora"] == ["miniflux", "mealie", "linkwarden"]
        assert "text-embedding-3-small" in _section(self.md, "Candidate")
        for verdict, chunker in (("ADOPT", "candidate"), ("REJECT", "current")):
            pair = self.rule["arms_by_verdict"][verdict]
            assert pair == {"baseline": f"{chunker}-ada", "candidate": f"{chunker}-3small"}
            assert self.rule["arms"][pair["baseline"]] == "text-embedding-ada-002"
            assert self.rule["arms"][pair["candidate"]] == "text-embedding-3-small"
        arms = " ".join(_section(self.md, "Arms, and the ones this rule is judged on").split())
        assert "judged on the two arms whose chunker the chunk-shape verdict adopts" in arms
        assert self.rule["after"] == "chunk-shape" and self.rule["variable"] == "embedding_model"
        assert self.rule["protocol"] == M2_PROTOCOL.name and self.rule["set"] == "shape-model"

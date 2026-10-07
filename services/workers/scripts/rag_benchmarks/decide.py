#!/usr/bin/env python3
"""QD3's judge: apply committed quality rules, in order, to recorded arms.

A quality decision in Phase 22.2 (the chunk shape, the embedding model, the
keyword leg) is made by a rule committed before its questions were written,
and judged by this script: the verdict is its exit code and its output, never
a reading of the records by hand (22.2-CONTEXT.md QD3; 22.2-07-PLAN.md).
`compare_runs.py` stays 22-03's arbiter and the phase's equivalence tool; it
cannot judge a model decision, since it refuses runs whose models or
query-vector hashes differ, and a model change changes both.

THE RULES are JSON files committed beside their protocol documents, in the
schema `load_rule` checks (any failure is a refusal):

    rule        the id, e.g. "M2" or "chunk-shape"
    protocol    the .md file beside the JSON whose text the JSON encodes
    set         the decision set the rule is judged on, e.g. "shape-model"
    corpora     the corpora whose questions are pooled, e.g. miniflux, mealie, linkwarden
    top_k       the cut, 5
    metric      "mrr@<top_k>": every clause compares MRR at the cut
    variable    "embedding_model" or "chunker": the one thing the arms may differ in
    arms        record prefix -> the variable's declared value on that arm:
                a model name, or {"chunker_version": ...}, checked against
                every record header. (A chunker variant is not declared
                separately: 22.2-01's chunker version folds in the variant
                22.2-04 adds, and no header records a variant on its own. A
                key no header records could only refuse.)
    pair        {"baseline": arm, "candidate": arm}, for a rule applied on its own
    after       (instead of pair) the id of a rule applied first, on the same
                set and corpora; its verdict chooses this rule's arms through
    arms_by_verdict  {"ADOPT": pair, "REJECT": pair}
    clauses     [{id, level: file|symbol, scope: pooled|each_corpus, min_delta}]
    allowance   the float-rounding allowance, 1e-9

A clause holds when delta >= min_delta - allowance, delta being the candidate's
MRR minus the baseline's: over all the rule's questions pooled, each weighted
equally, or on each corpus's questions separately (it holds when every corpus
does). A rule ADOPTs when every clause holds, and REJECTs otherwise.

INPUT. --rules R1.json [R2.json], applied in the order given; --records DIR
holding `<arm>-<corpus>.jsonl` (or `.jsonl.gz`) written by
`rag_quality_harness.py --measure --record`; --specs DIR holding each corpus's
`<corpus>.json` (default: this directory); --query-vectors MODEL=FILE, one per
model the arms ran with (the harness's --query-vectors files, `.gz` accepted).

IT REFUSES (exit 2, no verdict), before comparing anything, when:
  1. the question sets differ, by id or by text (or set, path or symbol),
     across the arms or against the spec's questions of the rule's set; or the
     spec has no question in the set; or a symbol clause meets a question that
     names no symbol (the symbol MRR would not be over all the questions);
  2. a record's ranks are not what its own final list gives under
     `scoring.ranks`, a final list holds more than the cut's `top_k` results,
     or a boosted chunk appears in neither leg (MRR@20 could not be scored);
  3. a measuring connection (either leg, or the header's database connection)
     is a superuser, bypasses RLS, or is unrecorded; or the run's vector
     backend is not pgvector, whose header records both legs;
  4. any query failed;
  5. two compared arms differ in anything but their declared variable:
     corpus, commit, set, top_k, boost_config, exact_paths, harness_commit,
     retrieval_code_version, corpus_tree_digest and vector_backend must match;
     for a model comparison the chunk_set_digest must; for a chunk comparison
     the embedding model and every question's query-vector hash must. An arm
     whose header is not its declaration (the model, or the chunker version),
     or not the rule's set and cut, is refused here too. Every pair a rule
     could be judged on is checked, both of an `after` rule's, so no verdict
     waits on an unchecked arm;
  6. a question's cached vector, in its run's model's file, is missing, has
     another model or another text, or a hash other than the record's
     `query_vector_sha256`;
  7. a rule file was committed after its questions: every commit that changed
     the rule's JSON must be a strict ancestor of every commit that brought a
     question of the set into a corpus spec, read from the checkout's history
     (`main`'s, under QD12). It refuses when the rule or a spec is uncommitted
     or differs from HEAD, when the two are in different repositories, when
     one commit adds both, and when a spec arrives at its path in the same
     commit as its questions (a new file, or a rename: where the questions
     were written first cannot then be read), since the order cannot then be
     proven. Any edit to the rule's JSON after its questions, its description
     included, refuses. A rebased branch rewrites the commits ancestry reads,
     so protocol branches are merged, never rebased (QD12);
  8. a rule fails the schema, two rules share an id, or `after` names a rule
     not applied before it or one on another set or other corpora.
Any input that cannot be read (a truncated `.gz`, malformed JSON, a record
missing what is compared) is refused too, never read as a verdict.

IT PRINTS, per rule, for the pair judged: per corpus and pooled, MRR, recall
and rank-1 at the cut, for file and symbol; MRR@20, reported, from the
`boosted` list (the ranking before the cut, each entry joined to its leg entry
by chunk_id for its path and breadcrumb), or "unavailable" where a question has
fewer than 20 ranked results; the per-question table of ranks per arm; the
questions whose answer sits in a tie at the cut, at QD2's 2e-6, reported only;
every clause, with its value, its threshold and whether it holds; and
`VERDICT: ADOPT` or `VERDICT: REJECT`. It also prints the order of record the
order check verified (each commit that changed the rule, and each that brought
the set's questions into a spec, with its date), for a protocol to cite.

EXIT 0 when every rule run adopted, 1 when any rejected, 2 when refused.
This script adopts nothing: adoption is the plan's code change, after the
verdict.

USAGE
    decide.py --rules chunk-shape-rule.json embedding-model-rule.json \\
              --records DIR --query-vectors text-embedding-ada-002=ada.json.gz \\
              text-embedding-3-small=3small.json.gz [--specs DIR]
"""

from __future__ import annotations

import argparse
import functools
import gzip
import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from chunk_digest import vector_sha256  # noqa: E402  (the harness's hash of a query vector, one definition)
from compare_runs import _find, connection_problems, load_run  # noqa: E402  (one reader of a record)
from scoring import aggregate, ranks  # noqa: E402  (the harness's own scoring rule, one module)

VERDICTS = ("ADOPT", "REJECT")
VARIABLES = ("embedding_model", "chunker")
# What a chunker arm declares: the header's chunker_version, which names the
# variant too (chunk_digest.py). A separate `chunker_variant` would need the
# header to record one first (PR #67, review A, M3).
CHUNKER_KEYS = ("chunker_version",)
LEVELS = ("file", "symbol")
SCOPES = ("pooled", "each_corpus")
RULE_KEYS = {"rule", "protocol", "set", "corpora", "top_k", "metric", "variable", "arms", "clauses", "allowance"}
OPTIONAL_RULE_KEYS = {"pair", "after", "arms_by_verdict", "description"}
CLAUSE_KEYS = {"id", "level", "scope", "min_delta"}
MAX_ALLOWANCE = 1e-6  # the allowance is for float rounding only; a rank step is 0.05/45 ~ 1.1e-3
# Refusal 5: what two compared arms must share, whatever the variable.
# retrieval_code_version names the ranking code even when two arms share a
# HEAD with uncommitted edits; corpus_tree_digest names the source read (22.2-01).
SHARED_HEADER_KEYS = ("corpus", "commit", "set", "top_k", "boost_config", "exact_paths", "harness_commit",
                      "retrieval_code_version", "corpus_tree_digest", "vector_backend")
QUESTION_KEYS = ("question", "set", "path", "symbol")
TIE_TOLERANCE = 2e-6  # QD2, locked 2026-09-29: ties at a rank cut in a decision's report
REPORT_DEPTH = 20     # MRR@20, reported, never judged


class NotInLegs(Exception):
    """A boosted chunk with no leg entry: it has no path or breadcrumb to score."""


class Refused(Exception):
    """Inputs that make a verdict meaningless; main() prints them and exits 2."""

    def __init__(self, problems: Sequence[str]):
        super().__init__("; ".join(problems))
        self.problems = list(problems)


# ---------------------------------------------------------------------------
# The rules (refusal 8)
# ---------------------------------------------------------------------------


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _pair_problems(pair, arms: Mapping, where: str) -> List[str]:
    if not isinstance(pair, dict) or set(pair) != {"baseline", "candidate"}:
        return [f"{where} must be {{\"baseline\": arm, \"candidate\": arm}}"]
    problems = [f"{where}.{side} names {pair[side]!r}, which is not an arm" for side in ("baseline", "candidate")
                if pair[side] not in arms]
    if not problems:
        if pair["baseline"] == pair["candidate"]:
            problems.append(f"{where} compares an arm with itself")
        elif arms[pair["baseline"]] == arms[pair["candidate"]]:
            problems.append(f"{where}'s two arms declare the same value, so the rule would compare nothing")
    return problems


def schema_problems(rule, path: Path) -> List[str]:
    """Everything wrong with a rule file, against the schema above."""
    if not isinstance(rule, dict):
        return [f"{path.name}: a rule is a JSON object"]
    where = path.name
    problems = []
    missing = sorted(RULE_KEYS - set(rule))
    unknown = sorted(set(rule) - RULE_KEYS - OPTIONAL_RULE_KEYS)
    if missing:
        problems.append(f"{where}: missing {missing}")
    if unknown:
        problems.append(f"{where}: unknown keys {unknown}; a new clause type extends the schema, with tests, first")
    if missing:
        return problems
    if not (isinstance(rule["rule"], str) and rule["rule"]):
        problems.append(f"{where}: rule must be a non-empty id")
    protocol = rule["protocol"]
    if not (isinstance(protocol, str) and protocol.endswith(".md") and "/" not in protocol and "\\" not in protocol):
        problems.append(f"{where}: protocol must name the .md file beside the rule")
    elif not (path.parent / protocol).is_file():
        problems.append(f"{where}: its protocol {protocol} is not beside it")
    if not (isinstance(rule["set"], str) and rule["set"]):
        problems.append(f"{where}: set must be a set name")
    corpora = rule["corpora"]
    if not (isinstance(corpora, list) and corpora and all(isinstance(c, str) and c for c in corpora)
            and len(set(corpora)) == len(corpora)):
        problems.append(f"{where}: corpora must be a non-empty list of distinct names")
    top_k = rule["top_k"]
    if not (isinstance(top_k, int) and not isinstance(top_k, bool) and top_k > 0):
        problems.append(f"{where}: top_k must be a positive integer")
    elif rule["metric"] != f"mrr@{top_k}":
        problems.append(f"{where}: metric must be 'mrr@{top_k}', the only metric a clause compares")
    variable = rule["variable"]
    if variable not in VARIABLES:
        problems.append(f"{where}: variable must be one of {VARIABLES}")
    arms = rule["arms"]
    if not (isinstance(arms, dict) and len(arms) >= 2 and all(isinstance(a, str) and a for a in arms)):
        problems.append(f"{where}: arms must map at least two record prefixes to their declared values")
        arms = {}
    for arm, declared in arms.items():
        if variable == "embedding_model" and not (isinstance(declared, str) and declared):
            problems.append(f"{where}: arm {arm} must declare a model name")
        if variable == "chunker" and not (
            isinstance(declared, dict) and declared and set(declared) <= set(CHUNKER_KEYS)
            and all(isinstance(v, str) and v for v in declared.values())
        ):
            problems.append(f"{where}: arm {arm} must declare exactly {{\"chunker_version\": <string>}} "
                            "(no header records a separate variant)")
    has_pair, has_after = "pair" in rule, "after" in rule
    if has_pair == has_after:
        problems.append(f"{where}: a rule has either pair, or after with arms_by_verdict")
    if has_pair:
        problems.extend(_pair_problems(rule["pair"], arms, f"{where}: pair"))
        if "arms_by_verdict" in rule:
            problems.append(f"{where}: arms_by_verdict needs after")
    if has_after:
        if not (isinstance(rule["after"], str) and rule["after"]):
            problems.append(f"{where}: after must name a rule")
        by_verdict = rule.get("arms_by_verdict")
        if not (isinstance(by_verdict, dict) and set(by_verdict) == set(VERDICTS)):
            problems.append(f"{where}: after needs arms_by_verdict with exactly ADOPT and REJECT")
        else:
            for verdict in VERDICTS:
                problems.extend(_pair_problems(by_verdict[verdict], arms, f"{where}: arms_by_verdict.{verdict}"))
    clauses = rule["clauses"]
    if not (isinstance(clauses, list) and clauses):
        problems.append(f"{where}: clauses must be a non-empty list")
        clauses = []
    ids = []
    for clause in clauses:
        if not (isinstance(clause, dict) and set(clause) == CLAUSE_KEYS):
            problems.append(f"{where}: a clause is exactly {sorted(CLAUSE_KEYS)}: {clause!r}")
            continue
        ids.append(clause["id"])
        if not (isinstance(clause["id"], str) and clause["id"]):
            problems.append(f"{where}: a clause id must be a non-empty string")
        if clause["level"] not in LEVELS:
            problems.append(f"{where}: clause {clause['id']}: level must be one of {LEVELS}")
        if clause["scope"] not in SCOPES:
            problems.append(f"{where}: clause {clause['id']}: scope must be one of {SCOPES}")
        if not _is_number(clause["min_delta"]):
            problems.append(f"{where}: clause {clause['id']}: min_delta must be a finite number")
    if len(set(map(str, ids))) != len(ids):
        problems.append(f"{where}: clause ids must be distinct")
    allowance = rule["allowance"]
    if not (_is_number(allowance) and 0 <= allowance < MAX_ALLOWANCE):
        problems.append(f"{where}: allowance must be a number in [0, {MAX_ALLOWANCE:g}): it is for float rounding only")
    return problems


def load_rules(paths: Sequence[Path]) -> List[dict]:
    """The rules, schema-checked, in the order given; `after` must name an earlier one."""
    rules, problems = [], []
    for path in paths:
        try:
            rule = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append(f"{Path(path).name}: cannot be read as JSON ({type(exc).__name__})")
            continue
        found = schema_problems(rule, Path(path))
        if found:
            problems.extend(found)
            continue
        rule["_path"] = Path(path).resolve()
        rules.append(rule)
    seen: Dict[str, dict] = {}
    for rule in rules:
        if rule["rule"] in seen:
            problems.append(f"two rules are named {rule['rule']}")
        if "after" in rule and rule["after"] not in seen:
            problems.append(f"{rule['_path'].name}: after names {rule['after']!r}, which is not a rule applied "
                            f"before it (given: {[r['rule'] for r in rules]})")
        elif "after" in rule:
            # The prior rule's verdict chooses this rule's arms, so it must have
            # been judged on the same questions (PR #67, review A, N2).
            prior = seen[rule["after"]]
            for key in ("set", "corpora"):
                if prior[key] != rule[key]:
                    problems.append(f"{rule['_path'].name}: after names {rule['after']!r}, judged on {key} "
                                    f"{prior[key]!r}, not this rule's {rule[key]!r}")
        seen.setdefault(rule["rule"], rule)
    if problems:
        raise Refused(problems)
    return rules


def pairs_of(rule: dict) -> List[Tuple[Optional[str], dict]]:
    """Every pair the rule could be judged on: (the verdict that chooses it, the pair)."""
    if "pair" in rule:
        return [(None, rule["pair"])]
    return [(verdict, rule["arms_by_verdict"][verdict]) for verdict in VERDICTS]


# ---------------------------------------------------------------------------
# The order of commits (refusal 7)
# ---------------------------------------------------------------------------


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          check=True).stdout.strip()


# A commit never changes, so what it holds is read once per process.
@functools.lru_cache(maxsize=None)
def _blob(cwd: Path, commit: str, name: str) -> Optional[str]:
    try:
        return git(cwd, "rev-parse", f"{commit}:./{name}")
    except subprocess.CalledProcessError:
        return None


@functools.lru_cache(maxsize=None)
def _parents(cwd: Path, commit: str) -> Tuple[str, ...]:
    return tuple(git(cwd, "rev-parse", f"{commit}^@").split())


def _history(cwd: Path, name: str) -> List[str]:
    """Every commit in HEAD's history that touched `name`, merges included."""
    out = git(cwd, "rev-list", "--full-history", "HEAD", "--", name)
    return out.split() if out else []


@functools.lru_cache(maxsize=None)
def _date(cwd: Path, commit: str) -> str:
    return git(cwd, "show", "-s", "--format=%cI", commit)


def _committed_problems(path: Path, what: str) -> List[str]:
    try:
        top = git(path.parent, "rev-parse", "--show-toplevel")
        status = git(path.parent, "status", "--porcelain", "--", path.name)
    except FileNotFoundError:
        return [f"git is not installed, so the order of {what} {path.name} and its questions cannot be read"]
    except (subprocess.CalledProcessError, OSError):
        return [f"{what} {path} is not in a git checkout, so its order cannot be proven"]
    if status:
        return [f"{what} {path.name} is uncommitted or differs from HEAD ({status.split()[0]}), "
                "so its order cannot be proven"]
    if not top:
        return [f"{what} {path} is not in a git checkout"]
    return []


def rule_commits(path: Path) -> List[str]:
    """The commits that changed the rule file: a merge counts only when the
    file differs from every parent (a merge that brings a branch's rule in did
    not write it; the branch's own commit did)."""
    cwd, name = path.parent, path.name
    changed = []
    for commit in _history(cwd, name):
        blob = _blob(cwd, commit, name)
        if all(_blob(cwd, p, name) != blob for p in _parents(cwd, commit)):  # a root commit has none
            changed.append(commit)
    return changed


@functools.lru_cache(maxsize=None)
def _has_set(cwd: Path, commit: str, name: str, set_name: str) -> bool:
    try:
        spec = json.loads(git(cwd, "show", f"{commit}:./{name}"))
    except (subprocess.CalledProcessError, ValueError):
        return False
    questions = spec.get("questions") if isinstance(spec, dict) else None
    return isinstance(questions, list) and any(isinstance(q, dict) and q.get("set") == set_name for q in questions)


def question_commits(spec_path: Path, set_name: str) -> List[str]:
    """The commits that brought a question of `set_name` into the spec: they
    hold one and no parent does."""
    cwd, name = spec_path.parent, spec_path.name
    return [c for c in _history(cwd, name)
            if _has_set(cwd, c, name, set_name) and not any(_has_set(cwd, p, name, set_name)
                                                            for p in _parents(cwd, c))]


def order_problems(rule: dict, spec_paths: Mapping[str, Path]) -> Tuple[List[str], List[str]]:
    """Refusal 7: was the rule committed, whole, before any of its questions?

    Returns (problems, the order of record verified): each commit that changed
    the rule and each that brought the set's questions into a spec, with its
    date, so a protocol can cite the judge's own output (review A, N3)."""
    path = rule["_path"]
    problems = _committed_problems(path, "the rule")
    for corpus, spec in spec_paths.items():
        problems.extend(_committed_problems(spec, f"{corpus}'s spec"))
    if problems:
        return [f"{rule['rule']}: {p}" for p in problems], []
    rule_top = git(path.parent, "rev-parse", "--show-toplevel")
    changes = rule_commits(path)
    if not changes:
        return [f"{rule['rule']}: no commit in HEAD's history adds {path.name}"], []
    record = [f"  {rule['rule']}: {path.name} changed in {c[:12]} ({_date(path.parent, c)})" for c in changes]
    for corpus, spec in spec_paths.items():
        if git(spec.parent, "rev-parse", "--show-toplevel") != rule_top:
            problems.append(f"{rule['rule']}: {corpus}'s spec is not in the rule's repository, so their order "
                            "cannot be read from one history")
            continue
        introduced = question_commits(spec, rule["set"])
        if not introduced:
            problems.append(f"{rule['rule']}: no commit in HEAD's history brings a {rule['set']} question "
                            f"into {corpus}'s spec")
        for q in introduced:
            record.append(f"  {rule['rule']}: {corpus}'s {rule['set']} questions arrived in {q[:12]} "
                          f"({_date(spec.parent, q)})")
            # A spec that arrives at this path in the commit that brings its
            # questions (a new file, or a rename) hides where they were
            # written: a `git mv` after the rule would make the move look like
            # the questions' first commit (review A, I1).
            if not any(_blob(spec.parent, p, spec.name) for p in _parents(spec.parent, q)):
                problems.append(f"{rule['rule']}: commit {q[:12]} brings {corpus}'s spec to {spec.name} together "
                                f"with its {rule['set']} questions (a new file or a rename), so where they were "
                                "written first cannot be read; commit the spec at its path before its questions")
            for r in changes:
                if r == q:
                    problems.append(f"{rule['rule']}: commit {r[:12]} changes the rule and adds {corpus}'s "
                                    f"{rule['set']} questions at once, so the rule cannot be shown to come first")
                    continue
                ancestor = subprocess.run(["git", "merge-base", "--is-ancestor", r, q], cwd=path.parent,
                                          capture_output=True).returncode == 0
                if not ancestor:
                    problems.append(f"{rule['rule']}: the rule's commit {r[:12]} is not an ancestor of {q[:12]}, "
                                    f"which added {corpus}'s {rule['set']} questions: the rule came after them")
    return problems, record


# ---------------------------------------------------------------------------
# The records (refusals 1 to 6)
# ---------------------------------------------------------------------------


def _open_json(path: Path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def spec_questions(spec_path: Path, set_name: str) -> Dict[str, dict]:
    spec = _open_json(spec_path)
    return {q["id"]: q for q in spec.get("questions", []) if q.get("set") == set_name}


def boosted_ranking(record: dict) -> List[dict]:
    """The ranking before the cut: `boosted`, by boosted_score with ties in
    recorded order, each entry joined to its leg entry for path and breadcrumb.
    Raises NotInLegs for a boosted chunk in neither leg."""
    trace = record["trace"]
    legs: Dict[str, dict] = {}
    for leg in ("vector", "fts"):
        for entry in trace[leg]:
            legs.setdefault(entry["chunk_id"], entry)
    ordered = sorted(trace["boosted"], key=lambda e: -float(e["boosted_score"]))
    joined = []
    for entry in ordered:
        leg = legs.get(entry["chunk_id"])
        if leg is None:
            raise NotInLegs(entry["chunk_id"])
        joined.append({"chunk_id": entry["chunk_id"], "file_path": leg.get("file_path"),
                       "breadcrumb": leg.get("breadcrumb"), "boosted_score": float(entry["boosted_score"])})
    return joined


def database_connection_problems(header: dict) -> List[str]:
    database = header.get("database")
    identity = database.get("connection") if isinstance(database, dict) else None
    if not isinstance(identity, dict) or not all(isinstance(identity.get(k), bool)
                                                 for k in ("rolsuper", "rolbypassrls")):
        return ["records no database connection stating rolsuper and rolbypassrls"]
    if identity["rolsuper"] or identity["rolbypassrls"]:
        return [f"database connection was {identity.get('current_user')} (rolsuper={identity['rolsuper']}, "
                f"rolbypassrls={identity['rolbypassrls']}); a superuser bypasses row-level security"]
    return []


def arm_problems(rule: dict, arm: str, corpus: str, run: Tuple[dict, Dict[str, dict]],
                 spec: Dict[str, dict], vectors: Mapping[str, Mapping[str, dict]]) -> List[str]:
    """Refusals 1 to 4 and 6 for one arm on one corpus, and the arm's declaration."""
    header, records = run
    name = f"{arm}-{corpus}"
    problems = []
    # 1: the question set.
    if not spec:
        problems.append(f"{name}: {corpus}'s spec has no question in set {rule['set']!r}")
    if set(records) != set(spec):
        problems.append(f"{name}: the question ids differ from the spec's {rule['set']} set "
                        f"(record only: {sorted(set(records) - set(spec))[:5]}; "
                        f"spec only: {sorted(set(spec) - set(records))[:5]})")
    for qid in sorted(set(records) & set(spec)):
        for key in QUESTION_KEYS:
            if records[qid].get(key) != spec[qid].get(key):
                problems.append(f"{name}: {qid}: the question's {key} differs from the spec's")
    if any(c["level"] == "symbol" for c in rule["clauses"]):
        nameless = sorted(qid for qid, q in spec.items() if not q.get("symbol"))
        if nameless:
            problems.append(f"{name}: {rule['rule']} has a symbol clause, and {nameless[:5]} name no symbol")
    # 5 (the arm against its declaration and the rule).
    declared = rule["arms"][arm]
    if header.get("corpus") != corpus:
        problems.append(f"{name}: the header's corpus is {header.get('corpus')!r}")
    if header.get("set") != rule["set"]:
        problems.append(f"{name}: the header's set is {header.get('set')!r}, not the rule's {rule['set']!r}")
    if header.get("top_k") != rule["top_k"]:
        problems.append(f"{name}: the header's top_k is {header.get('top_k')!r}, not the rule's {rule['top_k']}")
    if rule["variable"] == "embedding_model":
        if header.get("embedding_model") != declared:
            problems.append(f"{name}: the header's model is {header.get('embedding_model')!r}; the rule declares "
                            f"{declared!r}")
    else:
        for key, value in declared.items():
            if header.get(key) != value:
                problems.append(f"{name}: the header's {key} is {header.get(key)!r}; the rule declares {value!r}")
    # 3: the measuring connections. connection_problems asks for the vector
    # leg's identity only on pgvector, so the backend is required first
    # (review A, M1): a decision is measured on what ships.
    if header.get("vector_backend") != "pgvector":
        problems.append(f"{name}: the run's vector backend is {header.get('vector_backend')!r}, not 'pgvector', "
                        "so its vector leg's connection is not required to be recorded")
    for p in connection_problems(header) + database_connection_problems(header):
        if p.startswith("database"):
            p = "'s " + p
        problems.append(f"{name}: the run{p}" if p.startswith("'") else f"{name}: the run {p}")
    # 6: the cached query vectors.
    model = header.get("embedding_model")
    cache = vectors.get(model) if isinstance(model, str) else None
    if cache is None:
        problems.append(f"{name}: no --query-vectors file for its model {model!r}")
    # 2 and 4, and 6 per question.
    exact_paths = header.get("exact_paths")
    if not isinstance(exact_paths, bool):
        problems.append(f"{name}: the header records no exact_paths, so its ranks cannot be recomputed")
    for qid, rec in sorted(records.items()):
        if cache is not None:
            entry = cache.get(qid)
            if not isinstance(entry, dict):
                problems.append(f"{name}: {qid}: no cached vector in {model}'s file")
            elif entry.get("model") != model:
                problems.append(f"{name}: {qid}: the cached vector was embedded with {entry.get('model')!r}, "
                                f"and the run used {model!r}")
            elif entry.get("question") != rec.get("question"):
                problems.append(f"{name}: {qid}: the cached vector is for another question text")
            elif vector_sha256(entry["vector"]) != rec.get("query_vector_sha256"):
                problems.append(f"{name}: {qid}: the cached vector's hash is not the record's "
                                "query_vector_sha256")
        if rec.get("error"):
            problems.append(f"{name}: {qid}: the query failed ({str(rec['error'])[:60]}); "
                            "a failed query is not a measurement")
            continue
        if not isinstance(exact_paths, bool):
            continue
        top = rec["trace"].get("top")
        if not isinstance(top, list) or not all(isinstance(e, dict) for e in top):
            problems.append(f"{name}: {qid}: the final list is not a list of results")
            continue
        if len(top) > rule["top_k"]:
            # ranks() scores the whole list, so a longer one would put a rank
            # past the cut into MRR@k and recall@k (review A, M2).
            problems.append(f"{name}: {qid}: the final list holds {len(top)} results, more than the cut's "
                            f"top_k {rule['top_k']}")
            continue
        expected = ranks(exact_paths, rec["path"], rec.get("symbol"), top)
        if expected != (rec["file_rank"], rec.get("symbol_rank")):
            problems.append(f"{name}: {qid}: the record's ranks {(rec['file_rank'], rec.get('symbol_rank'))} are "
                            f"not what its final list gives {expected}")
        try:
            boosted_ranking(rec)
        except NotInLegs as exc:
            problems.append(f"{name}: {qid}: boosted chunk {exc.args[0]} is in neither leg, so the ranking "
                            "before the cut cannot be scored")
    return problems


def pair_problems(rule: dict, corpus: str, base: str, cand: str,
                  runs: Mapping[Tuple[str, str], Tuple[dict, Dict[str, dict]]]) -> List[str]:
    """Refusal 5 (and 1 across the arms): what the two arms must share."""
    (bh, b), (ch, c) = runs[(base, corpus)], runs[(cand, corpus)]
    name = f"{corpus}: {base} vs {cand}"
    problems = []
    for key in SHARED_HEADER_KEYS:
        if bh.get(key) != ch.get(key):
            problems.append(f"{name}: the arms differ in {key}: {bh.get(key)!r} vs {ch.get(key)!r}")
    if rule["variable"] == "embedding_model":
        if not bh.get("chunk_set_digest") or bh.get("chunk_set_digest") != ch.get("chunk_set_digest"):
            problems.append(f"{name}: a model comparison needs equal chunk-set digests "
                            f"({str(bh.get('chunk_set_digest'))[:16]} vs {str(ch.get('chunk_set_digest'))[:16]}): "
                            "the arms embedded different chunk texts")
    else:
        if bh.get("embedding_model") != ch.get("embedding_model"):
            problems.append(f"{name}: a chunk comparison needs one model ({bh.get('embedding_model')!r} vs "
                            f"{ch.get('embedding_model')!r})")
        for qid in sorted(set(b) & set(c)):
            if b[qid].get("query_vector_sha256") != c[qid].get("query_vector_sha256"):
                problems.append(f"{name}: {qid}: a chunk comparison needs the same query vector on both arms")
    if set(b) != set(c):
        problems.append(f"{name}: the question ids differ between the arms")
    for qid in sorted(set(b) & set(c)):
        for key in QUESTION_KEYS:
            if b[qid].get(key) != c[qid].get(key):
                problems.append(f"{name}: {qid}: the question's {key} differs between the arms")
    return problems


# ---------------------------------------------------------------------------
# The judgement
# ---------------------------------------------------------------------------


def holds(delta: float, min_delta: float, allowance: float) -> bool:
    """A clause holds when delta >= min_delta - allowance (the protocol's Precision)."""
    return delta >= min_delta - allowance


def _level_key(level: str) -> str:
    return "file_rank" if level == "file" else "symbol_rank"


def level_ranks(records: Iterable[dict], level: str) -> List[Optional[int]]:
    key = _level_key(level)
    return [r.get(key) for r in records if level == "file" or r.get("symbol")]


def _ordered(records: Dict[str, dict]) -> List[dict]:
    return [records[qid] for qid in sorted(records)]


def _mrr(records: Iterable[dict], level: str) -> float:
    return aggregate(level_ranks(records, level))["mrr"]


def evaluate(rule: dict, base: Dict[str, Dict[str, dict]], cand: Dict[str, Dict[str, dict]]
             ) -> Tuple[List[str], bool]:
    """Each clause's lines, and whether all hold. `base` and `cand` map corpus -> records."""
    lines, all_hold = [], True
    allowance = rule["allowance"]
    for clause in rule["clauses"]:
        level, threshold = clause["level"], clause["min_delta"]
        head = (f"  clause {clause['id']}: {level} MRR@{rule['top_k']}, {clause['scope']}, "
                f"delta >= {threshold:g} - {allowance:g}")
        if clause["scope"] == "pooled":
            pooled_b = [r for c in rule["corpora"] for r in _ordered(base[c])]
            pooled_c = [r for c in rule["corpora"] for r in _ordered(cand[c])]
            delta = _mrr(pooled_c, level) - _mrr(pooled_b, level)
            ok = holds(delta, threshold, allowance)
            lines.append(f"{head}: pooled delta {delta:+.6f} -> {'HOLDS' if ok else 'FAILS'}")
        else:
            parts = []
            ok = True
            for corpus in rule["corpora"]:
                delta = _mrr(_ordered(cand[corpus]), level) - _mrr(_ordered(base[corpus]), level)
                held = holds(delta, threshold, allowance)
                ok = ok and held
                parts.append(f"{corpus} {delta:+.6f} {'holds' if held else 'FAILS'}")
            lines.append(f"{head}: {'; '.join(parts)} -> {'HOLDS' if ok else 'FAILS'}")
        all_hold = all_hold and ok
    return lines, all_hold


def _fmt_rank(rank: Optional[int]) -> str:
    return f"#{rank}" if rank else "MISS"


def _agg_cells(records: List[dict], level: str, top_k: int) -> Tuple[str, float]:
    agg = aggregate(level_ranks(records, level))
    n = agg["questions"]
    return f"{agg['mrr']:.4f}  {agg['found']:>2}/{n:<2}  {agg['rank1']:>2}/{n:<2}", agg["mrr"]


def mrr_at_depth(records: List[Tuple[dict, bool]], level: str) -> Optional[float]:
    """MRR@20 from the ranking before the cut, or None when a question has
    fewer than 20 ranked results. Each record comes with its own corpus's
    `exact_paths`, so a pool of corpora is scored corpus by corpus (review A, N1)."""
    rrs = []
    for rec, exact_paths in records:
        if level == "symbol" and not rec.get("symbol"):
            continue
        ranking = boosted_ranking(rec)
        if len(ranking) < REPORT_DEPTH:
            return None
        file_rank, symbol_rank = ranks(exact_paths, rec["path"], rec.get("symbol"), ranking[:REPORT_DEPTH])
        rank = file_rank if level == "file" else symbol_rank
        rrs.append(1.0 / rank if rank else 0.0)
    return math.fsum(rrs) / len(rrs) if rrs else 0.0  # correctly rounded, as scoring.aggregate (ISS-041)


def ties_at_cut(rec: dict, top_k: int, exact_paths: bool) -> List[str]:
    """Where the answer sits in a group tied, at QD2's tolerance, with the last result kept."""
    ranking = boosted_ranking(rec)
    if len(ranking) <= top_k:
        return []
    last = ranking[top_k - 1]["boosted_score"]
    group = [i for i, e in enumerate(ranking, 1) if abs(e["boosted_score"] - last) <= TIE_TOLERANCE]
    if max(group) <= top_k:
        return []
    file_rank, symbol_rank = ranks(exact_paths, rec["path"], rec.get("symbol"), ranking)
    out = []
    for level, rank in (("file", file_rank), ("symbol", symbol_rank)):
        if rank in group:
            out.append(f"{level} #{rank} is tied with the cut (positions {min(group)}-{max(group)})")
    return out


def report(rule: dict, base_arm: str, cand_arm: str,
           runs: Mapping[Tuple[str, str], Tuple[dict, Dict[str, dict]]]) -> List[str]:
    """The aggregates, MRR@20, the per-question table and the ties, for one pair."""
    top_k = rule["top_k"]
    lines = [f"  {'corpus':<12} {'level':<7} {'baseline MRR rec rank1':<24} {'candidate MRR rec rank1':<24} delta MRR"]
    groups = [(c, [c]) for c in rule["corpora"]] + [("pooled", list(rule["corpora"]))]
    for label, corpora in groups:
        b = [r for c in corpora for r in _ordered(runs[(base_arm, c)][1])]
        k = [r for c in corpora for r in _ordered(runs[(cand_arm, c)][1])]
        for level in LEVELS:
            cells_b, mrr_b = _agg_cells(b, level, top_k)
            cells_c, mrr_c = _agg_cells(k, level, top_k)
            lines.append(f"  {label:<12} {level:<7} {cells_b:<24} {cells_c:<24} {mrr_c - mrr_b:+.4f}")
    lines.append(f"  (MRR, recall and rank-1 at {top_k}; pooled weights every question equally)")
    lines.append(f"\n  MRR@{REPORT_DEPTH}, reported, never judged (from the ranking before the cut):")
    for label, corpora in groups:
        for level in LEVELS:
            cells = []
            for arm in (base_arm, cand_arm):
                recs = [(r, runs[(arm, c)][0]["exact_paths"]) for c in corpora for r in _ordered(runs[(arm, c)][1])]
                value = mrr_at_depth(recs, level)
                cells.append("unavailable (fewer than 20 ranked results)" if value is None else f"{value:.4f}")
            lines.append(f"  {label:<12} {level:<7} {cells[0]} -> {cells[1]}")
    lines.append(f"\n  Per question ({base_arm} -> {cand_arm}):")
    lines.append(f"  {'id':<14} {'corpus':<12} {'file':<14} symbol")
    ties = []
    for corpus in rule["corpora"]:
        b, k = runs[(base_arm, corpus)][1], runs[(cand_arm, corpus)][1]
        for qid in sorted(b):
            lines.append(f"  {qid:<14} {corpus:<12} "
                         f"{_fmt_rank(b[qid]['file_rank']) + ' -> ' + _fmt_rank(k[qid]['file_rank']):<14} "
                         f"{_fmt_rank(b[qid].get('symbol_rank'))} -> {_fmt_rank(k[qid].get('symbol_rank'))}")
            for arm, rec in ((base_arm, b[qid]), (cand_arm, k[qid])):
                for note in ties_at_cut(rec, top_k, runs[(arm, corpus)][0]["exact_paths"]):
                    ties.append(f"  {qid:<14} {arm:<18} {note}")
    lines.append(f"\n  Answers in a tie at the cut (|score difference| <= {TIE_TOLERANCE:g}), reported only:")
    lines.extend(ties or ["  (none)"])
    return lines


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _vectors_arg(values: Sequence[str]) -> Dict[str, Path]:
    out = {}
    for value in values:
        model, sep, path = value.partition("=")
        if not sep or not model or not path:
            raise Refused([f"--query-vectors takes MODEL=FILE, not {value!r}"])
        if model in out:
            raise Refused([f"--query-vectors names {model} twice"])
        out[model] = Path(path)
    return out


# What reading an input can raise: a missing or unreadable file (OSError,
# gzip.BadGzipFile among them), malformed JSON (ValueError), and a truncated
# `.gz` (EOFError), which is neither (review A, I2).
UNREADABLE = (OSError, ValueError, EOFError)


def gather(args) -> Tuple[List[dict], Dict[Tuple[str, str], Tuple[dict, Dict[str, dict]]], List[str]]:
    """Load and check everything; raise Refused with every problem found.
    Returns the rules, the runs, and the order of record the order check verified."""
    rules = load_rules(args.rules)
    problems: List[str] = []
    vector_files = _vectors_arg(args.query_vectors)
    vectors: Dict[str, Dict[str, dict]] = {}
    for model, path in vector_files.items():
        try:
            vectors[model] = _open_json(path)
        except UNREADABLE as exc:
            problems.append(f"--query-vectors {model}: {path} cannot be read ({type(exc).__name__})")
    runs: Dict[Tuple[str, str], Tuple[dict, Dict[str, dict]]] = {}
    order_record: List[str] = []
    for rule in rules:
        spec_paths = {c: (args.specs / f"{c}.json").resolve() for c in rule["corpora"]}
        missing = [c for c, p in spec_paths.items() if not p.is_file()]
        if missing:
            problems.extend(f"{rule['rule']}: no spec {args.specs / (c + '.json')}" for c in missing)
            continue
        found, record = order_problems(rule, spec_paths)
        problems.extend(found)
        order_record.extend(record)
        arms = sorted({arm for _, pair in pairs_of(rule) for arm in pair.values()})
        for corpus in rule["corpora"]:
            try:
                spec = spec_questions(spec_paths[corpus], rule["set"])
            except UNREADABLE as exc:
                problems.append(f"{corpus}'s spec cannot be read ({type(exc).__name__})")
                continue
            for arm in arms:
                key = (arm, corpus)
                if key not in runs:
                    try:
                        runs[key] = load_run(_find(args.records, f"{arm}-{corpus}.jsonl"))
                    except UNREADABLE as exc:
                        problems.append(f"{arm}-{corpus}: cannot be read ({type(exc).__name__}: {str(exc)[:120]})")
                        continue
                problems.extend(arm_problems(rule, arm, corpus, runs[key], spec, vectors))
            for _, pair in pairs_of(rule):
                if (pair["baseline"], corpus) in runs and (pair["candidate"], corpus) in runs:
                    problems.extend(pair_problems(rule, corpus, pair["baseline"], pair["candidate"], runs))
    if problems:
        raise Refused(list(dict.fromkeys(problems)))
    return rules, runs, list(dict.fromkeys(order_record))


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Exit 0 (every rule adopted), 1 (any rejected) or 2 (refused). Input the
    checks did not foresee is refused too, never read as a verdict: any
    exception at all exits 2, since 1 is REJECT's code and a script reading it
    must never take a crash for a verdict (review A, I2). Nothing is printed
    until every rule has been judged, so a refusal never follows a verdict."""
    try:
        code, lines = _main(argv)
    except Refused as refused:
        print("REFUSED: nothing was compared, and there is no verdict.")
        for problem in refused.problems:
            print(f"  - {problem}")
        return 2
    except Exception as exc:  # noqa: BLE001 (every failure is a refusal, never a verdict)
        print(f"REFUSED: the inputs could not be judged ({type(exc).__name__}: {str(exc)[:160]}).")
        return 2
    print("\n".join(lines))
    return code


def _main(argv: Optional[Sequence[str]] = None) -> Tuple[int, List[str]]:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--rules", type=Path, nargs="+", required=True)
    ap.add_argument("--records", type=Path, required=True)
    ap.add_argument("--specs", type=Path, default=_HERE)
    ap.add_argument("--query-vectors", nargs="+", required=True, metavar="MODEL=FILE")
    args = ap.parse_args(argv)

    rules, runs, order_record = gather(args)
    out: List[str] = ["Order of record verified (every rule change precedes every question's first commit):",
                      *order_record, ""]
    verdicts: Dict[str, str] = {}
    for rule in rules:
        if "pair" in rule:
            pair, why = rule["pair"], "its own pair"
        else:
            prior = verdicts[rule["after"]]
            pair, why = rule["arms_by_verdict"][prior], f"chosen by {rule['after']}'s {prior}"
        base, cand = pair["baseline"], pair["candidate"]
        out.append(f"=== RULE {rule['rule']} ({rule['_path'].name}, protocol {rule['protocol']})")
        out.append(f"  variable {rule['variable']}: baseline {base} ({rule['arms'][base]}) vs candidate {cand} "
                   f"({rule['arms'][cand]}), {why}; set {rule['set']}, corpora {', '.join(rule['corpora'])}")
        out.extend(report(rule, base, cand, runs))
        out.append("\n  Clauses:")
        lines, adopted = evaluate(rule, {c: runs[(base, c)][1] for c in rule["corpora"]},
                                  {c: runs[(cand, c)][1] for c in rule["corpora"]})
        out.extend(lines)
        verdicts[rule["rule"]] = "ADOPT" if adopted else "REJECT"
        out.append(f"VERDICT: {verdicts[rule['rule']]} ({rule['rule']})\n")
    out.append("Rules run: " + ", ".join(f"{r} {v}" for r, v in verdicts.items()))
    return (0 if all(v == "ADOPT" for v in verdicts.values()) else 1), out


if __name__ == "__main__":
    sys.exit(main())

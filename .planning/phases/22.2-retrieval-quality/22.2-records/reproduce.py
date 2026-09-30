#!/usr/bin/env python3
"""Regenerate every file in 22.2-records/ from the committed scripts, and compare with what is committed.

A MEASUREMENT RECORD, not product code. Run it from a clean checkout. It:
  1. checks that each corpus under --corpora is at its pinned commit
     (`fetch_pinned.py`, which only reads HEAD once a corpus is fetched);
  2. runs chunk_census.py, records_analysis.py, consolidate.py, rr_spread.py,
     ts_grammar_checks.py, trace_question.py and environment_check.py into --out;
  3. compares each output with the committed file of the same name: JSON field
     by field, text exactly (line endings normalised);
  4. exits 0 only when nothing differs and nothing is missing.

The census reads this repository's own chunker and `self` corpora from
--census-tree, which must be a checkout of the commit the committed census
records name (0f2e4df), since `services/backend/pkg` has changed since then.

USAGE
    reproduce.py --census-tree <checkout of 0f2e4df> --self-commit <its sha> --corpora <dir> --out <dir>
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]  # 22.2-records -> 22.2-retrieval-quality -> phases -> .planning -> the checkout
RECORDS_2203 = REPO / ".planning" / "phases" / "22-repository-clone-ingestion" / "22-03-records"
CORPORA = ["self-go", "self-py", "self-ts", "miniflux", "mealie", "linkwarden", "ghostfolio-api", "seerr"]


def run(args, out_file=None):
    result = subprocess.run([sys.executable, *map(str, args)], capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise SystemExit(f"FAILED: {' '.join(map(str, args))}\n{result.stdout}\n{result.stderr}")
    if out_file is not None:
        out_file.write_text(result.stdout, encoding="utf-8")
    return result.stdout


def flatten(obj, prefix=""):
    if isinstance(obj, dict):
        items = {}
        for k, v in obj.items():
            items.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
        return items
    if isinstance(obj, list):
        items = {}
        for i, v in enumerate(obj):
            items.update(flatten(v, f"{prefix}[{i}]"))
        return items
    return {prefix: obj}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--census-tree", type=Path, required=True)
    ap.add_argument("--self-commit", required=True)
    ap.add_argument("--corpora", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    workers = a.census_tree / "services" / "workers"

    sys.path.insert(0, str(HERE))
    import chunk_census  # the corpus pins live in its TS_CANDIDATES and the benchmark specs

    pins = {name: (spec.get("dir", name), spec["repository"], spec["commit"])
            for name, spec in chunk_census.TS_CANDIDATES.items()}
    for name in ("miniflux", "mealie"):
        spec = json.loads((workers / "scripts" / "rag_benchmarks" / f"{name}.json").read_text(encoding="utf-8"))
        pins[name] = (name, spec["repository"], spec["commit"])
    print("1. corpus pins")
    for name, (directory, url, sha) in pins.items():
        line = run([HERE / "fetch_pinned.py", a.corpora / directory, url, sha]).strip()
        print(f"   {name}: {line}")

    print("2. regenerate")
    run([HERE / "chunk_census.py", "--workers", workers, "--corpora", a.corpora, "--out", a.out,
         "--self-commit", a.self_commit, "--only", *CORPORA])
    run([HERE / "records_analysis.py", "--records", RECORDS_2203, "--chunks", a.out,
         "--scoring", workers / "scripts" / "rag_benchmarks", "--out", a.out / "records-analysis.json"])
    run([HERE / "consolidate.py", "--raw", HERE / "ts-candidates-raw", "--out", a.out / "ts-candidates.json"])
    run([HERE / "rr_spread.py", "--records", RECORDS_2203], a.out / "rr-spread.txt")
    run([HERE / "ts_grammar_checks.py", "--workers", workers], a.out / "ts-grammar-checks.txt")
    run([HERE / "trace_question.py", "--records", RECORDS_2203, "--corpus", "mealie", "--id", "ml-05"],
        a.out / "ml-05-trace.txt")
    run([HERE / "environment_check.py"], a.out / "environment.txt")
    print("   done")

    print("3. compare with the committed files")
    compared = failures = 0
    names = [f"census-{c}.json" for c in CORPORA] + ["records-analysis.json", "ts-candidates.json"]
    for name in names:
        committed = json.loads((HERE / name).read_text(encoding="utf-8"))
        fresh = json.loads((a.out / name).read_text(encoding="utf-8"))
        b, f = flatten(committed), flatten(fresh)
        differ = sorted(k for k in set(b) & set(f) if b[k] != f[k])
        missing, extra = sorted(set(b) - set(f)), sorted(set(f) - set(b))
        compared += 1
        failures += bool(differ or missing or extra)
        print(f"   {name}: {len(b)} fields; differing {len(differ)}; missing {len(missing)}; extra {len(extra)}")
        for k in (differ + missing + extra)[:5]:
            print(f"      {k}: committed={b.get(k)!r} fresh={f.get(k)!r}")
    for name in ("rr-spread.txt", "ts-grammar-checks.txt", "ml-05-trace.txt", "environment.txt"):
        committed = (HERE / name).read_text(encoding="utf-8").replace("\r\n", "\n")
        fresh = (a.out / name).read_text(encoding="utf-8").replace("\r\n", "\n")
        compared += 1
        same = committed == fresh
        failures += not same
        print(f"   {name}: {'identical' if same else 'DIFFERS'} ({len(committed.splitlines())} lines)")
    print(f"RESULT: {compared} files compared, {failures} differ")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

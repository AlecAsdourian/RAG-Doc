#!/usr/bin/env python3
"""Run the census TOOL and compare its output with the research's census records.

A MEASUREMENT RECORD, not product code (22.2-01 Task 1). It runs
`services/workers/scripts/rag_benchmarks/chunk_census.py` from the tree this
file is in (run it from a clean `git archive` export), with `self` read from
--self-root, and compares each of the eight `census-<c>.json` files in
`22.2-records/` field by field, as `22.2-records/reproduce.py` does:

    differing  a field in both whose value differs     (must be 0)
    missing    a field committed but not produced      (must be 0)
    extra      a field produced but not committed      (listed by name; the tool's additions)

Exit 0 only when no file differs and none misses a field.

USAGE
    reproduce_census.py --self-root <export of 0f2e4df> --self-commit <its sha> --corpora <dir> --out <dir>
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TREE = HERE.parents[3]  # 22.2-01-records -> 22.2-retrieval-quality -> phases -> .planning -> the tree
RECORDS = HERE.parent / "22.2-records"
TOOL = TREE / "services" / "workers" / "scripts" / "rag_benchmarks" / "chunk_census.py"
CORPORA = ["self-go", "self-py", "self-ts", "miniflux", "mealie", "linkwarden", "ghostfolio-api", "seerr"]


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


def new_names(extra, committed_keys):
    """Each extra field collapsed to its shortest name the committed file does not have."""
    names = set()
    for key in extra:
        parts = key.replace("[", ".[").split(".")
        for n in range(1, len(parts) + 1):
            stem = ".".join(parts[:n]).replace(".[", "[")
            if not any(k == stem or k.startswith(stem + ".") or k.startswith(stem + "[") for k in committed_keys):
                names.add(stem)
                break
    return sorted(names)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-root", type=Path, required=True)
    ap.add_argument("--self-commit", required=True)
    ap.add_argument("--corpora", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    cmd = [sys.executable, str(TOOL), "--corpora", str(a.corpora), "--out", str(a.out),
           "--self-root", str(a.self_root), "--self-commit", a.self_commit, "--only", *CORPORA]
    print("1. the tool")
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    for line in result.stdout.splitlines():
        print(f"   {line}")
    if result.returncode != 0:
        print(result.stderr)
        raise SystemExit(f"the census tool exited {result.returncode}")

    print("2. compare with 22.2-records/, field by field")
    failures = 0
    all_new = set()
    for name in CORPORA:
        fname = f"census-{name}.json"
        committed = json.loads((RECORDS / fname).read_text(encoding="utf-8"))
        fresh = json.loads((a.out / fname).read_text(encoding="utf-8"))
        b, f = flatten(committed), flatten(fresh)
        differ = sorted(k for k in set(b) & set(f) if b[k] != f[k])
        missing = sorted(set(b) - set(f))
        extra = sorted(set(f) - set(b))
        names = new_names(extra, set(b))
        all_new.update(names)
        failures += bool(differ or missing)
        print(f"   {fname}: {len(b)} committed fields; differing {len(differ)}; missing {len(missing)}; "
              f"extra {len(extra)} ({', '.join(names)})")
        for k in (differ + missing)[:10]:
            print(f"      {k}: committed={b.get(k)!r} fresh={f.get(k)!r}")
    print(f"   extra fields, by name, over all files: {', '.join(sorted(all_new))}")
    print(f"RESULT: {len(CORPORA)} files compared, {failures} differ or miss a field")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

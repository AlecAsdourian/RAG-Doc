#!/usr/bin/env python3
"""Record `--list-targets` and `--check` for the committed specs (22.2-07 Task 2).

A MEASUREMENT RECORD, not product code. It runs the harness from this tree,
with the interpreter it is run with:

  - `--corpus <c> --list-targets` for miniflux and mealie, into targets.txt:
    the list a blind writer is given (no question text);
  - `--corpus <c> --check` for every committed spec, into check.txt: offline,
    no database, no OpenAI, with validate_spec's independence check on (it runs
    when the spec loads).

USAGE (with the workers venv's python)
    python targets_and_check.py --corpora-dir <where the corpora are fetched>
"""
import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TREE = HERE.parents[3]
WORKERS = TREE / "services" / "workers"
HARNESS = WORKERS / "scripts" / "rag_quality_harness.py"
SPECS = WORKERS / "scripts" / "rag_benchmarks"


def run(args):
    shown = "python scripts/rag_quality_harness.py " + " ".join(args)
    done = subprocess.run([sys.executable, str(HARNESS), *args], cwd=WORKERS, capture_output=True, text=True,
                          encoding="utf-8")
    return [f"$ {shown}", done.stdout.rstrip(), *([done.stderr.rstrip()] if done.stderr.strip() else []),
            f"(exit {done.returncode})", ""]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpora-dir", type=Path, required=True)
    a = ap.parse_args()
    targets = []
    for corpus in ("miniflux", "mealie"):
        targets += run(["--corpus", corpus, "--list-targets"])
    (HERE / "targets.txt").write_text("\n".join(targets), encoding="utf-8")
    specs = sorted(p.stem for p in SPECS.glob("*.json") if not p.stem.endswith("-rule"))
    check = [f"committed specs: {', '.join(specs)}", ""]
    for corpus in specs:
        check += run(["--corpus", corpus, "--corpora-dir", str(a.corpora_dir), "--check"])
    (HERE / "check.txt").write_text("\n".join(check), encoding="utf-8")
    print("\n".join(check))
    return 0


if __name__ == "__main__":
    sys.exit(main())

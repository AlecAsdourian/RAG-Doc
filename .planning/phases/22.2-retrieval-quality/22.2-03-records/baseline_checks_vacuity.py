#!/usr/bin/env python3
"""Show that baseline_checks.py can fail in each case PR #70's review (M1)
found it passing vacuously.

    baseline_checks_vacuity.py <22.2-03-records dir> <scratch dir>

For each mutation, the records are copied into a fresh directory under
<scratch>. The one file is mutated there, and the mutation is proven by
bytes: the copy's sha256 differs from the committed file's, and the mutated
field reads back as intended. Then baseline_checks.py is run on the copy, and
its FAIL lines and exit code are printed. Nothing in the records directory is
written: its files' hashes are compared before and after.
"""
import gzip
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

REC = Path(sys.argv[1]).resolve()
SCRATCH = Path(sys.argv[2]).resolve()
CHECKS = REC / "baseline_checks.py"
JSONL = "baseline-linkwarden.jsonl.gz"
SUMMARY = "baseline-linkwarden.summary.json"


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def read_jsonl(p: Path):
    with gzip.open(p, "rt", encoding="utf-8") as fh:
        return [json.loads(l) for l in fh]


def write_jsonl(p: Path, lines):
    with gzip.open(p, "wt", encoding="utf-8", compresslevel=9) as fh:
        fh.write("".join(json.dumps(l) + "\n" for l in lines))


def m_connections(d: Path):
    lines = read_jsonl(d / JSONL)
    lines[0]["connections"] = {}
    write_jsonl(d / JSONL, lines)
    return [JSONL], read_jsonl(d / JSONL)[0]["connections"] == {}


def m_usage(d: Path):
    for name in ("baseline-usage.txt", "baseline-usage-attempt1-429.txt"):
        (d / name).write_bytes(b"")
    return (["baseline-usage.txt", "baseline-usage-attempt1-429.txt"],
            all((d / n).stat().st_size == 0 for n in ("baseline-usage.txt", "baseline-usage-attempt1-429.txt")))


def m_symbol_rank(d: Path):
    lines = read_jsonl(d / JSONL)
    for r in lines[1:]:
        r["symbol_rank"] = 1
    write_jsonl(d / JSONL, lines)
    s = json.loads((d / SUMMARY).read_text(encoding="utf-8"))
    for r in s["rows"]:
        r["symbol_rank"] = 1
    (d / SUMMARY).write_text(json.dumps(s, indent=2), encoding="utf-8")
    return [JSONL, SUMMARY], all(r["symbol_rank"] == 1 for r in read_jsonl(d / JSONL)[1:])


def m_all_tuning(d: Path):
    lines = read_jsonl(d / JSONL)
    for r in lines[1:]:
        r["set"] = "tuning"
    write_jsonl(d / JSONL, lines)
    return [JSONL], all(r["set"] == "tuning" for r in read_jsonl(d / JSONL)[1:])


def m_top_k(d: Path):
    lines = read_jsonl(d / JSONL)
    lines[0]["top_k"] = 50
    write_jsonl(d / JSONL, lines)
    s = json.loads((d / SUMMARY).read_text(encoding="utf-8"))
    s["top_k"] = 50
    (d / SUMMARY).write_text(json.dumps(s, indent=2), encoding="utf-8")
    return [JSONL, SUMMARY], read_jsonl(d / JSONL)[0]["top_k"] == 50


MUTATIONS = [
    ("connections = {}", m_connections),
    ("both usage files emptied", m_usage),
    ("every symbol_rank set to 1", m_symbol_rank),
    ("every row labelled tuning", m_all_tuning),
    ("top_k = 50", m_top_k),
]


def main() -> int:
    before = {p.name: sha(p) for p in REC.iterdir() if p.is_file()}
    r = subprocess.run([sys.executable, "-B", str(CHECKS), str(REC)], capture_output=True, text=True, encoding="utf-8")
    print(f"== the committed records: exit {r.returncode}; {r.stdout.strip().splitlines()[-1]}")
    caught = 0
    for i, (what, mutate) in enumerate(MUTATIONS, 1):
        d = SCRATCH / f"w2203-vacuity-{i}"
        if d.exists():
            shutil.rmtree(d)
        shutil.copytree(REC, d)
        files, reads_back = mutate(d)
        print(f"== mutation {i}: {what}")
        for f in files:
            print(f"   {f}: committed sha256 {before[f][:16]}, mutated copy {sha(d / f)[:16]}, "
                  f"differs: {sha(d / f) != before[f]}")
        print(f"   the mutated field reads back as intended: {reads_back}")
        r = subprocess.run([sys.executable, "-B", str(CHECKS), str(d)], capture_output=True, text=True,
                           encoding="utf-8")
        fails = [l for l in r.stdout.splitlines() if l.startswith("FAIL")]
        for l in fails:
            print(f"   {l}")
        print(f"   baseline_checks.py exit {r.returncode}; {r.stdout.strip().splitlines()[-1]}")
        caught += r.returncode == 1 and bool(fails)
    after = {p.name: sha(p) for p in REC.iterdir() if p.is_file()}
    print(f"== {caught} of {len(MUTATIONS)} mutations FAIL; the records directory is unchanged: {before == after}")
    return 0 if caught == len(MUTATIONS) and before == after else 1


if __name__ == "__main__":
    sys.exit(main())

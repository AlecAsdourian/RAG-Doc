#!/usr/bin/env python3
"""Apply one mutation to a committed file, run the tests, restore it, and prove each step by bytes.

A MEASUREMENT RECORD, not product code: 22.2-01's script (22.2-01-records/mutate.py),
copied for 22.2-02 with one addition, --occurrences, so a pattern written twice
can be neutered in both places (the mutation rule in 22.2-02-PLAN.md). Run it on a clean, committed tree:

  1. the file's raw bytes (binary mode, no newline translation) are read, and
     `git hash-object` of it must equal HEAD's blob: the tree is clean;
  2. --find is replaced by --replace in those bytes, exactly once (the
     mutation neuters a predicate rather than deleting it);
  3. the mutation is proven to have landed: the new bytes differ from the
     pristine ones, the replacement is present, the original is absent, and
     `git hash-object` no longer equals HEAD's blob;
  4. the tests run, and the failing test ids are recorded;
  5. `git checkout -- <file>` restores it, proven by the raw bytes' SHA-256
     equal to the pristine SHA-256 and `git hash-object` equal to HEAD's blob.

A mutation is KILLED when at least one test fails and every test named by
--expect-fail is among the failures.

USAGE (from anywhere; paths are relative to the tree this file is in)
    mutate.py --label M1 --file services/workers/x.py --find 'a' --replace 'b' \
              --tests tests/test_x.py --expect-fail test_name [--out record.txt]
"""
import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

TREE = Path(__file__).resolve().parents[4]
WORKERS = TREE / "services" / "workers"


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=TREE, capture_output=True, text=True, check=True).stdout.strip()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--file", required=True)
    ap.add_argument("--find", required=True)
    ap.add_argument("--replace", required=True)
    ap.add_argument("--tests", nargs="+", required=True)
    ap.add_argument("--expect-fail", nargs="+", required=True)
    ap.add_argument("--occurrences", type=int, default=1,
                    help="how many times --find must occur; every occurrence is replaced")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    path = TREE / a.file
    lines = []

    def say(text: str = "") -> None:
        print(text)
        lines.append(text)

    head_blob = git("rev-parse", f"HEAD:{a.file}")
    clean = git("hash-object", a.file) == head_blob
    say(f"== {a.label}: {a.file}")
    if not clean:
        raise SystemExit("the file differs from HEAD before mutating; commit first")
    # The pristine bytes are what `git checkout --` writes (22.2-02's addition):
    # a file written by an editor with LF, under core.autocrlf, hashes equal to
    # HEAD but comes back from the restore as CRLF, so its own bytes could never
    # prove the restore. Checking it out first makes pristine == restored a
    # comparison that can succeed, and still fail on a real difference.
    git("checkout", "--", a.file)
    pristine = path.read_bytes()
    say(f"   HEAD {git('rev-parse', '--short=12', 'HEAD')}, blob {head_blob[:12]}; "
        f"pristine sha256 {sha(pristine)[:16]}; hash-object == HEAD blob: {clean}")
    find, replace = a.find.encode("utf-8"), a.replace.encode("utf-8")
    if pristine.count(find) != a.occurrences:
        raise SystemExit(f"--find occurs {pristine.count(find)} times; it must occur exactly {a.occurrences}")
    mutated = pristine.replace(find, replace)
    path.write_bytes(mutated)
    on_disk = path.read_bytes()
    landed = (on_disk == mutated and sha(on_disk) != sha(pristine) and replace in on_disk
              and find not in on_disk and git("hash-object", a.file) != head_blob)
    say(f"   mutation: {a.find!r} -> {a.replace!r}")
    say(f"   landed: {landed} (mutated sha256 {sha(on_disk)[:16]}; replacement present {replace in on_disk}; "
        f"original absent {find not in on_disk}; hash-object != HEAD blob {git('hash-object', a.file) != head_blob})")
    failed = []
    try:
        if not landed:
            raise SystemExit("the mutation did not land")
        result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-rf", "-p", "no:cacheprovider", *a.tests],
                                cwd=WORKERS, capture_output=True, text=True, encoding="utf-8")
        failed = sorted({ln.split(" ", 1)[1].split(" - ")[0] for ln in result.stdout.splitlines()
                         if ln.startswith("FAILED ")})
        summary = [ln for ln in result.stdout.splitlines() if " passed" in ln or " failed" in ln][-1:]
        say(f"   pytest {' '.join(a.tests)}: exit {result.returncode}; {summary[0] if summary else '?'}")
        for f in failed:
            say(f"      FAILED {f}")
    finally:
        git("checkout", "--", a.file)
        restored = path.read_bytes()
        say(f"   restored: sha256 {sha(restored)[:16]} == pristine {sha(restored) == sha(pristine)}; "
            f"hash-object == HEAD blob {git('hash-object', a.file) == head_blob}")
    expected = all(any(e in f for f in failed) for e in a.expect_fail)
    killed = bool(failed) and expected and sha(path.read_bytes()) == sha(pristine)
    say(f"   VERDICT {a.label}: {'KILLED' if killed else 'SURVIVED'} (expected to fail: {', '.join(a.expect_fail)})")
    if a.out:
        with a.out.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n\n")
    return 0 if killed else 1


if __name__ == "__main__":
    sys.exit(main())

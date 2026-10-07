"""Mutation check for the one-hand-back-per-turn rule (22.2-03, 2026-10-06,
after PR #68's review).

Each mutant is applied to audit_writer.py, proven to have landed (its bytes
changed, the new text present once and the old text gone), the test file is
run, and the tool is restored byte for byte. Hashes are of the file's bytes as
they are in the working tree; the script says whether those are LF or CRLF,
since a `core.autocrlf=true` checkout hashes differently from the committed
LF blob.

Usage (from the repository root):

    python .planning/phases/22.2-retrieval-quality/22.2-03-records/mutate_turns.py \\
        .planning/phases/22.2-retrieval-quality/22.2-03-records
"""
import hashlib
import subprocess
import sys
from pathlib import Path

REC = Path(sys.argv[1])
TOOL = REC / "audit_writer.py"
MUTANTS = [
    ("the turn ignored: a hand-back must be the transcript's last tool call again",
     b"is_last=(i == len(uses) - 1 or uses[i + 1][3] != turn))",
     b"is_last=(i == len(uses) - 1))  # MUTANT"),
    ("a tool's result ends a turn as a user message does",
     b"        return not any(isinstance(b, dict) and b.get(\"type\") == \"tool_result\" for b in content)\n",
     b"        return True  # MUTANT\n"),
    ("the 'last of its turn' condition removed",
     b"        if not is_last:\n",
     b"        if False:  # MUTANT\n"),
]


def sha(b):
    return hashlib.sha256(b).hexdigest()


orig = TOOL.read_bytes()
crlf = b"\r\n" in orig
print(f"audit_writer.py: {'CRLF' if crlf else 'LF'} bytes, sha256 {sha(orig)}")
for what, old, new in MUTANTS:
    if crlf:
        old, new = old.replace(b"\n", b"\r\n"), new.replace(b"\n", b"\r\n")
    print(f"== mutant: {what}")
    assert orig.count(old) == 1, f"expected exactly one occurrence, found {orig.count(old)}"
    try:
        TOOL.write_bytes(orig.replace(old, new))
        landed = TOOL.read_bytes()
        assert landed != orig and landed.count(new) == 1 and landed.count(old) == 0
        print(f"landed: sha256 {sha(landed)}")
        r = subprocess.run([sys.executable, "-B", "-m", "pytest", str(REC / "test_audit_writer.py"), "-q",
                            "-p", "no:cacheprovider"], capture_output=True, text=True)
        print("\n".join(l for l in r.stdout.splitlines()
                        if l.startswith("FAILED") or " passed" in l or " failed" in l))
        print(f"pytest exit on the mutant: {r.returncode}")
    finally:
        TOOL.write_bytes(orig)
    back = TOOL.read_bytes()
    print(f"restored: sha256 {sha(back)}; byte for byte: {back == orig}")
    assert back == orig

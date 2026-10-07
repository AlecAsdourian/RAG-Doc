"""Mutation check for the hand-back's "is last" condition (22.2-03, 2026-10-06)."""
import hashlib
import subprocess
import sys
from pathlib import Path

REC = Path(sys.argv[1])  # the 22.2-03-records directory
TOOL = REC / "audit_writer.py"
OLD = b"        if not is_last:\n"
NEW = b"        if False:  # MUTANT: 'is last' condition removed\n"

orig = TOOL.read_bytes()
crlf = b"\r\n" in orig
old, new = (OLD.replace(b"\n", b"\r\n"), NEW.replace(b"\n", b"\r\n")) if crlf else (OLD, NEW)
sha0 = hashlib.sha256(orig).hexdigest()
print(f"audit_writer.py sha256 before: {sha0}")
assert orig.count(old) == 1, f"expected exactly one occurrence, found {orig.count(old)}"
mutant = orig.replace(old, new)
try:
    TOOL.write_bytes(mutant)
    landed = TOOL.read_bytes()
    assert landed != orig and landed.count(new) == 1 and landed.count(old) == 0
    print(f"mutant landed: sha256 {hashlib.sha256(landed).hexdigest()}; 'if not is_last:' -> 'if False:'")
    r = subprocess.run([sys.executable, "-B", "-m", "pytest", str(REC / "test_audit_writer.py"), "-q",
                        "-p", "no:cacheprovider"], capture_output=True, text=True)
    lines = [l for l in r.stdout.splitlines() if l.startswith("FAILED") or " passed" in l or " failed" in l]
    print("\n".join(lines))
    print(f"pytest exit on the mutant: {r.returncode}")
finally:
    TOOL.write_bytes(orig)
sha1 = hashlib.sha256(TOOL.read_bytes()).hexdigest()
print(f"audit_writer.py sha256 after restore: {sha1}; restored byte for byte: {sha1 == sha0}")
assert sha1 == sha0

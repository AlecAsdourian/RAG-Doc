"""Re-run the three committed writer audits under the per-turn hand-back
amendment, and compare each one's per-call lines with its latest block in
writer-audit.txt (22.2-03, after PR #68's review, finding 2). Run from the
repository root; the argument is where to write the new audit blocks.
"""
import subprocess, sys
REC = ".planning/phases/22.2-retrieval-quality/22.2-03-records"
SUB = "C:/Users/Alec/.claude/projects/C--Users-Alec-Desktop-code-testtGSD/d9e9d681-6cd2-4b2a-a813-c593a8957090/subagents/"
ROOT = "C:/Users/Alec/Desktop/code/rag-bench-corpora/linkwarden"
RUNS = [
    ("agent-a3e83f5112ab9df32.jsonl", "writer 1, re-audited under the amended tool (7411291), for the record only; the batch stays discarded",
     "writer 1 (discarded), re-audited under the per-turn amendment, for the record only; the batch stays discarded"),
    ("agent-ab8d3830594a88043.jsonl", "writer 1 (replacement)", "writer 1 (replacement), re-audited under the per-turn amendment"),
    ("agent-aa532d24673600e6a.jsonl", "writer 2", "writer 2, re-audited under the per-turn amendment"),
]
text = open(f"{REC}/writer-audit.txt", encoding="utf-8").read()
blocks = {}
for b in text.split("== ")[1:]:
    label = b.split("\n", 1)[0]
    blocks[label] = b
out = []
for t, old_label, new_label in RUNS:
    r = subprocess.run([sys.executable, "-B", f"{REC}/audit_writer.py", "--root", ROOT, "--transcript", SUB + t,
                        "--label", new_label], capture_output=True, text=True, encoding="utf-8")
    new = r.stdout
    def body(s):
        lines = s.split("\n")
        return [l for l in lines[1:] if l and not l.startswith("note")
                and not l.startswith("######") and not l.startswith("writer ") and not l.startswith("committed ")
                and not l.startswith("---") and not l.startswith("+++") and not l.startswith("@@")
                and not l.startswith("+") and not l.startswith("-") and not l.startswith(" ")]
    old_b = body(blocks[old_label])
    new_b = body(new[3:])
    # compare the report lines that the audit itself produced (header, calls, totals)
    same = [l for l in old_b if not l[0].isdigit() and not l.strip()[:1].isdigit()] == \
           [l for l in new_b if not l[0].isdigit() and not l.strip()[:1].isdigit()]
    calls_old = [l for l in blocks[old_label].split("\n") if l[:4].strip().rstrip(".").isdigit()]
    calls_new = [l for l in new.split("\n") if l[:4].strip().rstrip(".").isdigit()]
    out.append(new)
    print(f"{t} exit {r.returncode}; per-call lines identical to '{old_label}': {calls_old == calls_new} "
          f"({len(calls_new)} calls); header and totals identical: {same}")
open(sys.argv[1], "w", encoding="utf-8", newline="\n").write("\n".join(out))

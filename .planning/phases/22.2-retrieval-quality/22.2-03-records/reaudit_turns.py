"""Re-run the three committed writer audits under the current audit_writer.py
and compare each one's per-call lines with the latest block for the same
transcript in writer-audit.txt (22.2-03, after PR #68's review and re-check).

Run from the repository root:

    python .../22.2-03-records/reaudit_turns.py <out-file> "<label suffix>"

<out-file> receives the new audit blocks; the comparison is printed.
"""
import subprocess, sys
REC = ".planning/phases/22.2-retrieval-quality/22.2-03-records"
SUB = "C:/Users/Alec/.claude/projects/C--Users-Alec-Desktop-code-testtGSD/d9e9d681-6cd2-4b2a-a813-c593a8957090/subagents/"
ROOT = "C:/Users/Alec/Desktop/code/rag-bench-corpora/linkwarden"
RUNS = [
    ("agent-a3e83f5112ab9df32.jsonl", "writer 1 (discarded), {}, for the record only; the batch stays discarded"),
    ("agent-ab8d3830594a88043.jsonl", "writer 1 (replacement), {}"),
    ("agent-aa532d24673600e6a.jsonl", "writer 2, {}"),
]


def call_lines(block):
    return [l for l in block.split("\n") if l[:4].strip().rstrip(".").isdigit()]


def verdict(block):
    return [l for l in block.split("\n") if l.startswith(("calls:", "BATCH:"))]


text = open(f"{REC}/writer-audit.txt", encoding="utf-8").read()
blocks = text.split("\n== ")
out_path, suffix = sys.argv[1], sys.argv[2]
out = []
for t, label in RUNS:
    latest = [b for b in blocks if f"subagents\\{t}" in b.split("\n", 2)[1] or f"subagents/{t}" in b.split("\n", 2)[1]][-1]
    r = subprocess.run([sys.executable, "-B", f"{REC}/audit_writer.py", "--root", ROOT, "--transcript", SUB + t,
                        "--label", label.format(suffix)], capture_output=True, text=True, encoding="utf-8")
    new = r.stdout
    out.append(new)
    print(f"{t}: exit {r.returncode}; per-call lines identical to its latest block ('{latest.split(chr(10), 1)[0]}'): "
          f"{call_lines(latest) == call_lines(new)} ({len(call_lines(new))} calls); "
          f"totals and verdict identical: {verdict(latest) == verdict(new)}")
open(out_path, "w", encoding="utf-8", newline="\n").write("\n".join(out))

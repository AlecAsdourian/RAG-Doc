"""The writers' launch prompts and answers, taken from their transcripts.

22.2-03 Task 1. Two jobs, both read-only on the transcripts:

- `prompts`: each writer's launch prompt (the transcript's first user
  message), its sha256, and a diff against the committed prompt text (the
  "## The prompt" section of `writer-prompt.md`). Writer 1's must equal it;
  writer 2's may differ only inside the avoid-list block.
- `questions`: each writer's JSON array, taken verbatim from the `message`
  input of its transcript's final SubagentHandback call, numbered in the
  order the writer listed them (writer 1 -> lw-01..lw-15, writer 2 ->
  lw-16..lw-30), odd ids tuning, even ids holdout, and written into the spec's
  `questions`. Nothing is reordered, edited or filtered.

Usage (from the repository root):

    python .../22.2-03-records/writer_runs.py prompts W1.jsonl W2.jsonl
    python .../22.2-03-records/writer_runs.py questions W1.jsonl W2.jsonl
"""

import difflib
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROMPT_MD = HERE / "writer-prompt.md"
SPEC = HERE.parents[3] / "services/workers/scripts/rag_benchmarks/linkwarden.json"
AVOID_W1 = "    (none: you are the first writer)"
FIELDS = ["question", "path", "symbol", "evidence"]


def entries(transcript):
    return [json.loads(l) for l in Path(transcript).read_text(encoding="utf-8").splitlines() if l.strip()]


def launch_prompt(transcript):
    for e in entries(transcript):
        if e.get("type") == "user":
            c = (e.get("message") or {}).get("content")
            if isinstance(c, str):
                return c
            return "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    raise SystemExit(f"no launch prompt in {transcript}")


def handback_message(transcript):
    uses = [b for e in entries(transcript) if isinstance((e.get("message") or {}).get("content"), list)
            for b in e["message"]["content"] if isinstance(b, dict) and b.get("type") == "tool_use"]
    last = uses[-1]
    if last.get("name") != "SubagentHandback" or set(last.get("input", {})) != {"message"}:
        raise SystemExit(f"{transcript}: the final tool_use is not a SubagentHandback with only a message")
    return last["input"]["message"]


def committed_prompt():
    text = PROMPT_MD.read_text(encoding="utf-8").replace("\r\n", "\n")
    body = text.split("## The prompt\n", 1)[1].split("\n---\n", 1)[0]
    return body.strip("\n")


def sha(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def prompts(w1, w2):
    ref = committed_prompt()
    print(f"committed prompt (writer-prompt.md, '## The prompt' section): sha256 {sha(ref)}")
    for label, t in (("writer 1 (replacement)", w1), ("writer 2", w2)):
        p = launch_prompt(t)
        same = p.strip("\n") == ref
        print(f"{label}: launch prompt sha256 {sha(p)}; equal to the committed text (outer blank lines aside): {same}")
        if not same:
            diff = list(difflib.unified_diff(ref.split("\n"), p.strip("\n").split("\n"),
                                             "writer-prompt.md", label, lineterm="", n=1))
            print("\n".join(diff))
            removed = [l[1:] for l in diff if l.startswith("-") and not l.startswith("---")]
            added = [l[1:] for l in diff if l.startswith("+") and not l.startswith("+++")]
            avoid_only = removed == [AVOID_W1] and all(l.startswith("    ") and " — " in l for l in added)
            print(f"{label}: the only change is the avoid-list block ({len(added)} pairs in place of "
                  f"'{AVOID_W1.strip()}'): {avoid_only}")
            if avoid_only:
                pairs = [tuple(l.strip().split(" — ")) for l in added]
                print(f"{label}: its avoid-list pairs equal writer 1's (path, symbol) list in order: "
                      f"{pairs == [(q['path'], q['symbol']) for q in json.loads(handback_message(w1))]}")


def questions(w1, w2):
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    if spec["questions"]:
        raise SystemExit("the spec already has questions; refusing to overwrite them")
    out = []
    for t in (w1, w2):
        batch = json.loads(handback_message(t))
        if len(batch) != 15:
            raise SystemExit(f"{t}: {len(batch)} questions, not 15")
        for q in batch:
            if list(q) != FIELDS:
                raise SystemExit(f"{t}: fields {list(q)}, not {FIELDS}")
            n = len(out) + 1
            out.append({"id": f"lw-{n:02d}", "set": "tuning" if n % 2 else "holdout", **q})
    spec["questions"] = out
    SPEC.write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {len(out)} questions to {SPEC.name}")


if __name__ == "__main__":
    cmd, w1, w2 = sys.argv[1:4]
    {"prompts": prompts, "questions": questions}[cmd](w1, w2)

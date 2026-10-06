#!/usr/bin/env python3
"""Run decide.py, as a script, on the test fixtures (22.2-07 Task 1's verify step).

A MEASUREMENT RECORD, not product code. It builds tests/test_decide.py's
fixture world (four arms on miniflux, mealie and linkwarden; the committed
M2 rule and a stand-in chunk-shape rule in a temporary git repository,
committed rule first), then runs
`decide.py --rules chunk-shape-rule.json embedding-model-rule.json ...` three
times, writing each command's output and exit code to decide-fixture-run.txt:

  1. the default arms: chunk-shape ADOPT, M2 REJECT (exit 1);
  2. candidate-3small raised by +0.0333 at symbol level: both ADOPT (exit 0);
  3. one arm's chunk-set digest changed: REFUSED (exit 2).

No database and no OpenAI: the records are hand-made.

USAGE (with the workers venv's python)
    python decide_fixture_run.py
"""
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKERS = HERE.parents[3] / "services" / "workers"
sys.path.insert(0, str(WORKERS / "tests"))
import test_decide as t  # noqa: E402  (the fixtures the tests judge)

DECIDE = WORKERS / "scripts" / "rag_benchmarks" / "decide.py"


def run(world: "t.World", label: str) -> list:
    world.write()
    argv = world.argv()
    done = subprocess.run([sys.executable, str(DECIDE), *argv], capture_output=True, text=True, encoding="utf-8")
    shown = " ".join(a if " " not in a else f'"{a}"' for a in argv).replace(str(world.root), "<tmp>")
    return [f"## {label}", f"$ decide.py {shown}", done.stdout.rstrip(), f"(exit {done.returncode})", ""]


def main() -> int:
    out = []
    with tempfile.TemporaryDirectory(prefix="w2207-decide-") as tmp:
        for i, (label, setup) in enumerate([
            ("1. the default arms", lambda w: None),
            ("2. candidate-3small +0.0333 at symbol level", lambda w: [w.put("candidate-3small", "miniflux", j, 1, 1)
                                                                      for j in range(3)]),
            ("3. a model arm with another chunk-set digest", lambda w: w.edit(
                "candidate-3small", "mealie", lambda h, r: h.update(chunk_set_digest="9" * 64))),
        ]):
            root = Path(tmp) / f"world{i}"
            root.mkdir()
            world = t.World(root, t.make_repo(root / "repo", "rule-first"))
            setup(world)
            out += run(world, label)
    (HERE / "decide-fixture-run.txt").write_text("\n".join(out), encoding="utf-8")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Run one side of 22.2-02's benchmark: ingest, measure and clear self, miniflux and mealie.

A MEASUREMENT RECORD, not product code (22.2-02 Task 3, steps 2-3). For each
corpus, with the harness of the tree given by --workers:

  1. `--clear --ingest`            as the scratch superuser   -> ingest-<side>-<c>.txt
     (the embedding client's INFO lines -- its token usage per batch -- go to
     usage-<side>-<c>.txt: they are the spend's evidence);
  2. `--measure --set all --query-vectors <vecs> --record <side>-<c>.jsonl`
     as rag_doc_app (NOSUPERUSER NOBYPASSRLS)                  -> measure-<side>-<c>.txt;
  3. `--clear`                     as the scratch superuser.

Secrets: OPENAI_API_KEY is read from --env-file and DATABASE_URL from the DSN
files, into the child's environment only; nothing here prints either, and the
key is reported by its length alone. Every path printed into a record is made
relative to the scratch directory (`<scratch>`).

USAGE
    bench_run.py --side before|after --workers <tree>/services/workers --self-root <dir>
                 --self-commit <sha> [--harness-commit <sha>] --corpora-dir <dir>
                 --vectors <vecs.json> --su-dsn-file <f> --app-dsn-file <f>
                 --env-file <.env> --out <records dir> --scratch <dir>
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

CORPORA = ("self", "miniflux", "mealie")

# Runs the harness with the embedding client's INFO lines on stderr, prefixed,
# so they can be split from everything else the harness prints.
SHIM = r"""
import logging, runpy, sys
handler = logging.StreamHandler(sys.stderr)
handler.setFormatter(logging.Formatter("USAGE %(asctime)s %(name)s %(message)s"))
for name in ("workers.embeddings.openai_client",):
    lg = logging.getLogger(name); lg.setLevel(logging.INFO); lg.addHandler(handler); lg.propagate = False
warn = logging.StreamHandler(sys.stderr)
warn.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
root = logging.getLogger(); root.setLevel(logging.WARNING); root.addHandler(warn)
harness = sys.argv[1]
sys.argv = [harness] + sys.argv[2:]
runpy.run_path(harness, run_name="__main__")
"""


def read_env_key(path: Path, name: str) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit(f"{name} is not in {path.name}")


def main() -> int:
    ap = argparse.ArgumentParser()
    for flag in ("--side", "--workers", "--self-root", "--self-commit", "--corpora-dir", "--vectors",
                 "--su-dsn-file", "--app-dsn-file", "--env-file", "--out", "--scratch"):
        ap.add_argument(flag, required=True)
    ap.add_argument("--harness-commit", default=None)
    ap.add_argument("--only", nargs="*", default=list(CORPORA))
    a = ap.parse_args()
    out, scratch = Path(a.out), Path(a.scratch).resolve()
    key = read_env_key(Path(a.env_file), "OPENAI_API_KEY")
    su = Path(a.su_dsn_file).read_text(encoding="utf-8").strip()
    app = Path(a.app_dsn_file).read_text(encoding="utf-8").strip()
    print(f"OPENAI_API_KEY: present, {len(key)} characters (never printed)")

    def clean(text: str) -> str:
        for secret in (key, su, app):
            text = text.replace(secret, "<redacted>")
        return text.replace(str(scratch), "<scratch>").replace(str(scratch).replace("\\", "/"), "<scratch>")

    base = ["--corpora-dir", a.corpora_dir, "--self-root", a.self_root, "--self-commit", a.self_commit]
    if a.harness_commit:
        base += ["--harness-commit", a.harness_commit]
    harness = str(Path(a.workers) / "scripts" / "rag_quality_harness.py")

    def run(corpus: str, args, dsn: str, log: Path, usage: Path = None) -> int:
        env = dict(os.environ, OPENAI_API_KEY=key, DATABASE_URL=dsn, PYTHONIOENCODING="utf-8")
        cmd = [sys.executable, "-c", SHIM, harness, "--corpus", corpus, *base, *args]
        r = subprocess.run(cmd, cwd=a.workers, env=env, capture_output=True, text=True, encoding="utf-8")
        lines = [ln for ln in (r.stdout + r.stderr).splitlines()]
        usage_lines = [ln[len("USAGE "):] for ln in lines if ln.startswith("USAGE ")]
        other = [ln for ln in lines if not ln.startswith("USAGE ")]
        shown = " ".join(x if not x.startswith(str(scratch)) else "<scratch>" + x[len(str(scratch)):]
                         for x in ["rag_quality_harness.py", "--corpus", corpus, *base, *args])
        with log.open("a", encoding="utf-8") as fh:
            fh.write(clean(f"$ {shown}\n" + "\n".join(other) + f"\nexit {r.returncode}\n\n"))
        if usage is not None:
            with usage.open("a", encoding="utf-8") as fh:
                fh.write(clean("\n".join(usage_lines) + ("\n" if usage_lines else "")))
        print(f"  {corpus} {' '.join(args[:2])}: exit {r.returncode}")
        return r.returncode

    for c in a.only:
        print(f"== {a.side} {c}")
        if run(c, ["--clear", "--ingest"], su, out / f"ingest-{a.side}-{c}.txt", out / f"usage-{a.side}-{c}.txt"):
            return 1
        measured = run(c, ["--measure", "--set", "all", "--query-vectors", a.vectors,
                           "--record", str(out / f"{a.side}-{c}.jsonl")],
                       app, out / f"measure-{a.side}-{c}.txt", out / f"usage-{a.side}-{c}.txt")
        if run(c, ["--clear"], su, out / f"clear-{a.side}-{c}.txt"):
            return 1
        if measured:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

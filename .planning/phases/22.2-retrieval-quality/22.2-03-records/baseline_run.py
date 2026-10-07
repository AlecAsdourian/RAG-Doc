#!/usr/bin/env python3
"""22.2-03 Task 2: linkwarden's first ingest and its baseline measurement.

A MEASUREMENT RECORD, not product code. It is adapted from 22.2-02's
bench_run.py, for one corpus and the plan's flags. With this tree's harness:

  1. `--corpus linkwarden --clear --ingest`, as the scratch superuser
     -> ingest.txt.
     The embedding client's INFO lines (its tokens and cost per batch) go to
     usage.txt; they are the spend's evidence.
  2. `--measure --set all --query-vectors <vecs> --record <jsonl>
     --json-out <summary> --vector-tolerance 2e-6`, as rag_doc_app
     (NOSUPERUSER NOBYPASSRLS) -> measure.txt.
     The 30 questions are embedded once and written back to <vecs>.
     2e-6 is QD2's tolerance.

Secrets: OPENAI_API_KEY is read from --env-file and DATABASE_URL from the DSN
files, into the child's environment only. Nothing here prints either; the key
is reported by its length alone. Paths under the scratch directory are
printed as `<scratch>`, and OpenAI organisation ids as `org-<redacted>`.

Changed after PR #70's review (M2, N6, N7): the `org-` redaction, `--prefix`
for the log names (default `baseline-`), the `if secret:` guard, and stderr
merged into stdout so the log keeps time order. The committed logs were
written by the first version, then renamed and redacted by hand; baseline.txt
records the exact commands.

USAGE
    baseline_run.py --workers <tree>/services/workers --corpora-dir <dir> --vectors <vecs.json>
                    --record <jsonl> --json-out <summary.json> --su-dsn-file <f> --app-dsn-file <f>
                    --env-file <.env> --out <dir for the .txt logs> --scratch <dir>
"""
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

VECTOR_TOLERANCE = "2e-6"  # QD2

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
    for flag in ("--workers", "--corpora-dir", "--vectors", "--record", "--json-out", "--su-dsn-file",
                 "--app-dsn-file", "--env-file", "--out", "--scratch"):
        ap.add_argument(flag, required=True)
    ap.add_argument("--step", choices=["ingest", "measure", "both"], default="both")
    ap.add_argument("--prefix", default="baseline-",
                    help="prefix of the log names: <prefix>ingest.txt, <prefix>measure.txt, <prefix>usage.txt")
    a = ap.parse_args()
    out, scratch = Path(a.out), Path(a.scratch).resolve()
    key = read_env_key(Path(a.env_file), "OPENAI_API_KEY")
    su = Path(a.su_dsn_file).read_text(encoding="utf-8").strip()
    app = Path(a.app_dsn_file).read_text(encoding="utf-8").strip()
    print(f"OPENAI_API_KEY: present, {len(key)} characters (never printed)")

    def clean(text: str) -> str:
        for secret in (key, su, app):
            if secret:  # an empty value would interleave the marker between every character
                text = text.replace(secret, "<redacted>")
        for form in (str(scratch), str(scratch).replace("\\", "/")):
            text = text.replace(form, "<scratch>")
        # OpenAI's error messages name the organisation (`org-...`).
        return re.sub(r"org-[A-Za-z0-9]{8,}", "org-<redacted>", text)

    harness = str(Path(a.workers) / "scripts" / "rag_quality_harness.py")
    base = ["--corpus", "linkwarden", "--corpora-dir", a.corpora_dir]

    def run(args, dsn: str, log: Path, usage: Path) -> int:
        env = dict(os.environ, OPENAI_API_KEY=key, DATABASE_URL=dsn, PYTHONIOENCODING="utf-8")
        r = subprocess.run([sys.executable, "-c", SHIM, harness, *base, *args], cwd=a.workers, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
        lines = r.stdout.splitlines()  # stderr merged in, so the lines keep their time order
        usage_lines = [ln[len("USAGE "):] for ln in lines if ln.startswith("USAGE ")]
        other = [ln for ln in lines if not ln.startswith("USAGE ")]
        shown = " ".join(["rag_quality_harness.py", *base, *args])
        with log.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(clean(f"$ {shown}\n" + "\n".join(other) + f"\nexit {r.returncode}\n\n"))
        with usage.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(clean("\n".join(usage_lines) + ("\n" if usage_lines else "")))
        print(f"  {' '.join(args[:2])}: exit {r.returncode}")
        return r.returncode

    usage = out / f"{a.prefix}usage.txt"
    if a.step in ("ingest", "both"):
        if run(["--clear", "--ingest"], su, out / f"{a.prefix}ingest.txt", usage):
            return 1
    if a.step in ("measure", "both"):
        if run(["--measure", "--set", "all", "--query-vectors", a.vectors, "--record", a.record,
                "--json-out", a.json_out, "--vector-tolerance", VECTOR_TOLERANCE],
               app, out / f"{a.prefix}measure.txt", usage):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

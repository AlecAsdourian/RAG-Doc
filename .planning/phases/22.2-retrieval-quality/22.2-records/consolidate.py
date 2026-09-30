#!/usr/bin/env python3
"""Consolidate the per-candidate sizing records into ts-candidates.json.

A MEASUREMENT RECORD, not product code. Reads the per-candidate JSON files
`size_candidates.py` wrote (committed in `ts-candidates-raw/`) and writes the
sorted summary that 22.2-RESEARCH.md R3 cites.

USAGE
    consolidate.py --raw <ts-candidates-raw> --out <ts-candidates.json>
"""
import argparse
import json
from pathlib import Path

METHOD = (
    "GitHub REST API, unauthenticated, 2026-09-29: repos/{r}, commits/{default branch}, "
    "the recursive tree at that sha. Counts .ts/.tsx after excluding vendored directories (node_modules, dist, "
    "build, vendor, third_party, .next, out, coverage), generated files (.d.ts, *.generated.*, __generated__, "
    "/generated/, .gen.ts, .min.js), tests (test, tests, __tests__, e2e, __mocks__, cypress, playwright, "
    "fixtures, mocks, stories directories; *.test, *.spec, *.stories, *.e2e, *.cy files) and migrations/ "
    "directories. Sizes are blob sizes in bytes."
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    out = []
    for f in sorted(a.raw.glob("*__*.json")):
        r = json.loads(f.read_text(encoding="utf-8"))
        c, b = r["counts"], r["bytes"]
        out.append({
            "repo": r["repo"], "branch": r["branch"], "sha": r["sha"], "license": r["license"],
            "archived": r["archived"], "pushed_at": r["pushed_at"],
            "ts_files": c.get("ts", 0), "tsx_files": c.get("tsx", 0),
            "ts_tsx_kib": round((b.get("ts", 0) + b.get("tsx", 0)) / 1024),
            "js_files": c.get("js", 0), "test_files": c.get("test", 0), "generated_files": c.get("generated", 0),
            "migration_files": c.get("migration", 0), "vendored_files": c.get("vendored", 0),
            "top_dirs_by_size": dict(list(r["by_top"].items())[:6]),
        })
    out.sort(key=lambda x: x["ts_tsx_kib"])
    with a.out.open("w", encoding="utf-8") as fh:
        json.dump({"method": METHOD, "candidates": out}, fh, indent=1)
    for x in out:
        print(f"{x['repo']:32s} {x['ts_files'] + x['tsx_files']:5d} files {x['ts_tsx_kib']:6d} KiB  "
              f"ts={x['ts_files']} tsx={x['tsx_files']} license={x['license']} archived={x['archived']}")


if __name__ == "__main__":
    main()

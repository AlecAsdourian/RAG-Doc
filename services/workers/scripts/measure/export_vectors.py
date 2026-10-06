"""Export the real runs' REAL ada-002 vectors before the scratch database goes.

`22.1-05-PLAN.md`, Task 2, step 1. As the superuser, in a READ ONLY
transaction, for each repository named on the command line: every chunk's
embedding, in a fixed order (`file_path, start_line, end_line, id`), as one
`float32` `.npy`. Plus `manifest.json`: per repository its label, source
`full_name` and SHA, the row count, the model(s) the rows carry, and the
array's SHA-256.

NO CONTENT IS EXPORTED: the recall test (Task 3) and the at-cap replay need
vectors only. The export's row count is checked against the database's, and
a mismatch is an error.

The output directory must be OUTSIDE the repository (a `mktemp -d`).

    python scripts/measure/export_vectors.py --state <state.json> [--inside] \
        --out <dir> --repo label=<repository_id>=<full_name>@<sha> [...]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys

import numpy as np
import psycopg2

DIMENSIONS = 1536


def export_one(conn, repository_id: str, out: pathlib.Path, label: str) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*), array_agg(DISTINCT embedding_model) FROM chunks WHERE repository_id = %s",
                    (repository_id,))
        expected, models = cur.fetchone()
    vectors = np.empty((expected, DIMENSIONS), dtype=np.float32)
    n = 0
    with conn.cursor(name=f"export_{label.replace('-', '_')}") as cur:
        cur.itersize = 2000
        cur.execute(
            "SELECT embedding::text FROM chunks WHERE repository_id = %s "
            "ORDER BY file_path, start_line, end_line, id",
            (repository_id,),
        )
        for (text,) in cur:
            vectors[n] = np.array(text[1:-1].split(","), dtype=np.float32)
            n += 1
    if n != expected:
        raise SystemExit(f"{label}: exported {n} rows, the database holds {expected}")
    path = out / f"{label}.npy"
    np.save(path, vectors)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    norms = np.linalg.norm(vectors, axis=1)
    return {"label": label, "file": path.name, "rows": n, "models": sorted(models or []),
            "sha256": digest, "norm_min": float(norms.min()) if n else None,
            "norm_max": float(norms.max()) if n else None}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--state", required=True)
    p.add_argument("--inside", action="store_true")
    p.add_argument("--out", required=True)
    p.add_argument("--repo", action="append", required=True,
                   help="label=repository_id=full_name@sha")
    args = p.parse_args()
    state = json.loads(pathlib.Path(args.state).read_text(encoding="utf-8"))
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    conn = psycopg2.connect(state["super_dsn_inside" if args.inside else "super_dsn"])
    conn.set_session(readonly=True)
    manifest = {"dimensions": DIMENSIONS, "dtype": "float32",
                "order": "file_path, start_line, end_line, id", "repositories": []}
    for spec in args.repo:
        label, repository_id, source = spec.split("=", 2)
        full_name, _, sha = source.partition("@")
        entry = export_one(conn, repository_id, out, label)
        entry.update({"full_name": full_name, "sha": sha, "repository_id": repository_id})
        manifest["repositories"].append(entry)
        print(json.dumps({k: entry[k] for k in ("label", "rows", "models", "sha256")}))
    conn.rollback()
    conn.close()
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())

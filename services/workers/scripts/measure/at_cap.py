"""U6's caps WITHOUT OpenAI: the synthetic archives for 22.1-05's at-cap runs.

`22.1-05-PLAN.md`, Task 2, step 2. Two archives, both served to the REAL
`Worker` through `ingest_timings.py --local-archive` (a `MockTransport`
answers the head resolution and the download), with a replay embedder
(`--replay-vectors`) instead of OpenAI. No key is in the environment.

`build` -- the chunk cap. The large repository's REAL indexable files, copied
under top-level subdirectories `copy00/`, `copy01/`, ... until the archive
holds `--target-chunks` chunks (about 99,000 for the cap; 10,000, 25,000 and
50,000 for the size curve), within 20,000 indexable files and 500 MB. Each
file's chunk count comes from the real chunker, cached. The archive's top
level is the public form `{owner}-{repo}-{sha7}` of a synthetic `full_name`,
so `expected_top_levels_for` accepts it.

⚠ THE COPIES ARE MADE DISTINCT. Copied files would give copied chunk texts:
`_embed` embeds a text once, every copy would share its vector, and
pgvector's HNSW stores identical vectors as one element with several heap
TIDs -- so a store of copies would be CHEAPER than the store of a real
99,000-chunk repository, whose chunks are almost all distinct (django:
46,027 distinct of 48,704, the dry run). So `ingest_timings.py
--tag-copies` appends one comment line naming the copy to every chunk text
under `copyNN/` AFTER the real chunker has produced it (parse time is the
chunker's), and the replay embedder perturbs a vector deterministically on
each pass through the export after the first.

`bomb` -- the disk at the 500 MB cap: `--files` members of just under 1 MB
of random bytes each, named `.py` so the extractor WRITES them (it skips
unsupported names before writing), which the walk then skips as binary.
Random bytes do not compress, so the archive and the expansion are both
about `--files` MB.

    python scripts/measure/at_cap.py build --source <L.tar.gz> --target-chunks 99000 --out <dir>
    python scripts/measure/at_cap.py bomb --files 495 --out <dir>
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import pathlib
import sys
import tarfile
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))  # services/workers

from workers.fetch import filters  # noqa: E402
from workers.fetch.archive import DEFAULT_LIMITS  # noqa: E402

SYNTHETIC_OWNER = "measure"


def synthetic_identity(kind: str, target: int, source_sha: str) -> tuple:
    full_name = f"{SYNTHETIC_OWNER}/{kind}-{target}"
    sha = hashlib.sha1(f"{full_name}@{source_sha}".encode()).hexdigest()
    owner, repo = full_name.split("/")
    return full_name, sha, f"{owner}-{repo}-{sha[:7]}"


def indexable_members(source: pathlib.Path) -> list:
    """The source archive's files the walk would index, as (relpath, bytes)."""
    out = []
    with tarfile.open(source, "r:gz") as tar:
        for member in tar:
            if not member.isreg():
                continue
            rel = member.name.split("/", 1)[1] if "/" in member.name else member.name
            verdict = filters.classify_path(rel)
            if not verdict.indexable or member.size > DEFAULT_LIMITS.max_file_bytes:
                continue
            data = tar.extractfile(member).read()
            if b"\x00" in data:
                continue
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                continue
            if verdict.language == "go" and filters.is_generated_go(text):
                continue
            out.append((rel, data, text, verdict.language))
    out.sort(key=lambda m: m[0])
    return out


def chunk_counts(members: list, cache: pathlib.Path) -> dict:
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    from workers.chunker import SemanticChunker

    chunker = SemanticChunker()
    counts = {}
    started = time.time()
    for rel, _data, text, language in members:
        try:
            counts[rel] = len(chunker.chunk_file(rel, text, language))
        except Exception:  # noqa: BLE001 - the handler skips and counts it too
            counts[rel] = 0
    cache.write_text(json.dumps(counts), encoding="utf-8")
    print(json.dumps({"counted_files": len(counts), "chunks": sum(counts.values()),
                      "seconds": round(time.time() - started, 1)}))
    return counts


def build(args: argparse.Namespace) -> int:
    source = pathlib.Path(args.source)
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    members = indexable_members(source)
    counts = chunk_counts(members, out / f"chunk-counts-{source.stem}.json")
    per_copy = sum(counts.values())
    if per_copy == 0:
        raise SystemExit("the source archive produces no chunks")
    full_name, sha, top = synthetic_identity("cap", args.target_chunks, args.source_sha)
    chosen = []
    total = files = size = 0
    copy = 0
    done = False
    while not done:
        for rel, data, _text, _lang in members:
            n = counts.get(rel, 0)
            if total + n > args.target_chunks:
                continue
            if files + 1 > DEFAULT_LIMITS.max_indexable_files:
                done = True
                break
            if size + len(data) > DEFAULT_LIMITS.max_expanded_bytes - 8 * 1024 * 1024:
                done = True
                break
            chosen.append((f"copy{copy:02d}/{rel}", data))
            total += n
            files += 1
            size += len(data)
            if total >= args.target_chunks - args.tolerance:
                done = True
                break
        copy += 1
        if copy > 99:
            break
    path = out / f"cap-{args.target_chunks}.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        for name, data in chosen:
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            info.mtime = 0
            tar.addfile(info, io.BytesIO(data))
    meta = {"archive": path.name, "full_name": full_name, "sha": sha, "branch": "main",
            "expected_chunks": total, "files": files, "expanded_bytes": size,
            "archive_bytes": path.stat().st_size, "copies_started": copy,
            "source": {"full_name": args.source_full_name, "sha": args.source_sha,
                       "chunks_per_full_copy": per_copy, "indexable_files": len(members)}}
    (out / f"cap-{args.target_chunks}.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(json.dumps(meta))
    return 0


def bomb(args: argparse.Namespace) -> int:
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    full_name, sha, top = synthetic_identity("disk", args.files, "0" * 40)
    path = out / f"disk-{args.files}.tar.gz"
    size = args.member_bytes
    with open(path, "wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=1, mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w|") as tar:
                for i in range(args.files):
                    info = tarfile.TarInfo(f"{top}/blob/{i:04d}.py")
                    info.size = size
                    info.mtime = 0
                    tar.addfile(info, io.BytesIO(os.urandom(size)))
    meta = {"archive": path.name, "full_name": full_name, "sha": sha, "branch": "main",
            "files": args.files, "member_bytes": size, "expanded_bytes": args.files * size,
            "archive_bytes": path.stat().st_size}
    (out / f"disk-{args.files}.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(json.dumps(meta))
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--source", required=True)
    b.add_argument("--source-full-name", required=True)
    b.add_argument("--source-sha", required=True)
    b.add_argument("--target-chunks", type=int, required=True)
    b.add_argument("--tolerance", type=int, default=200)
    b.add_argument("--out", required=True)
    d = sub.add_parser("bomb")
    d.add_argument("--files", type=int, default=495)
    d.add_argument("--member-bytes", type=int, default=1_000_000)
    d.add_argument("--out", required=True)
    args = p.parse_args()
    return build(args) if args.cmd == "build" else bomb(args)


if __name__ == "__main__":
    sys.exit(main())

"""What a record measured: the chunk-set digest and the chunker version (22.2-01).

One module, imported by the census (`chunk_census.py`, offline) and by the
harness (`rag_quality_harness.py`, which prints the offline digest on
`--ingest` and records the database's digest in every run header). Two copies
would let the two drift apart, and the point of the digest is that the two are
compared.

THE CHUNK-SET DIGEST names the rows a run measured. It is SHA-256 over the
sorted multiset of rows

    (file_path, start_line, end_line, chunk_type,
     sha256(content), sha256(breadcrumb), sha256(docstring))

with a missing breadcrumb or docstring hashed as "". The embedded text is the
breadcrumb, then the docstring, then the content
(`EmbeddingGenerator._prepare_text_for_embedding`), so two chunk sets with
equal digests embed identical text under the same generator. Hashing the
content alone would let two arms with different docstrings pass as identical
(22.2-01-PLAN.md; QD8 relies on this). Duplicate rows are kept: it is the
digest of a multiset, so two identical chunks do not digest as one.

The harness takes it over the corpus's rows *with the run's embedding model*,
since one repository can hold the rows of two model arms (22.2-05).

THE CHUNKER VERSION names the code that chunked. It is the first 16 hex
characters of a SHA-256 over

    - the relative path and the bytes of every non-test `.py` file under
      `workers/parser/` and `workers/chunker/`, in path order, with CRLF read as
      LF so that one commit names one version on every platform (a Windows
      checkout converts line endings; Python reads both the same);
    - the installed versions of `tree-sitter` and of each grammar package.

So a grammar bump is a new chunker version, by design. 22.2-04, which makes
either chunker ingestible, extends the version with the variant it selects.
"""

from __future__ import annotations

import hashlib
import json
from importlib import metadata
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence, Tuple

WORKERS_DIR = Path(__file__).resolve().parents[2]  # services/workers
CHUNKER_DIRS = ("workers/parser", "workers/chunker")
# The parser's packages. tiktoken is not here: it counts tokens, it does not chunk.
GRAMMAR_PACKAGES = (
    "tree-sitter",
    "tree-sitter-python",
    "tree-sitter-go",
    "tree-sitter-javascript",
    "tree-sitter-typescript",
)

ChunkRow = Tuple[str, int, int, str, str, str, str]


def _sha(text: Optional[str]) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def chunk_row(
    file_path: str,
    start_line: int,
    end_line: int,
    chunk_type: str,
    content: Optional[str],
    breadcrumb: Optional[str],
    docstring: Optional[str],
) -> ChunkRow:
    """One chunk as the digest sees it. `None` is hashed as ""."""
    return (
        str(file_path),
        int(start_line),
        int(end_line),
        str(chunk_type),
        _sha(content),
        _sha(breadcrumb),
        _sha(docstring),
    )


def row_of_chunk(chunk) -> ChunkRow:
    """A `workers.chunker.models.Chunk`, as the chunker made it."""
    meta = chunk.metadata or {}
    return chunk_row(
        chunk.file_path, chunk.start_line, chunk.end_line, chunk.chunk_type,
        chunk.content, meta.get("breadcrumb"), meta.get("docstring"),
    )


# The database read of the same rows. The breadcrumb and docstring are read from
# `metadata`, where the writer stores the chunker's metadata whole: that is what
# the embedding text was built from. Keyword search and the booster read the
# `breadcrumb` column instead, which the writer copies from the same metadata
# (`postgres_writer.py`, `... .get("breadcrumb") or None`), so the two agree by
# construction; BREADCRUMB_MISMATCH_SQL counts the rows where they do not, and
# the harness refuses to record a header over any such row.
DB_ROWS_SQL = """
    SELECT file_path, start_line, end_line, chunk_type, content,
           metadata->>'breadcrumb', metadata->>'docstring'
    FROM chunks
    WHERE repository_id = %s AND embedding_model = %s
"""

BREADCRUMB_MISMATCH_SQL = """
    SELECT count(*) FROM chunks
    WHERE repository_id = %s AND embedding_model = %s
      AND breadcrumb IS DISTINCT FROM NULLIF(metadata->>'breadcrumb', '')
"""

# The stored vectors themselves, as the identity of one ingest: each row's id and
# the md5 of its vector's text, in id order. Two runs over the same stored
# vectors (one ingest) give one digest; a re-ingest gives new ids, and the
# embedding API does not repeat itself (22.2-01-records/embedding-repeatability.txt),
# so two ingests never do. compare_runs.py --no-qdrant refuses a pair whose
# digests differ: QD2's tolerance is for the same stored vectors only.
STORED_VECTORS_SQL = """
    SELECT md5(coalesce(string_agg(id::text || ':' || md5(embedding::text), ',' ORDER BY id), ''))
    FROM chunks
    WHERE repository_id = %s AND embedding_model = %s
"""


def chunk_set_digest(rows: Iterable[Sequence]) -> str:
    """SHA-256 over the rows sorted, as compact JSON. Duplicates are kept."""
    ordered = sorted(tuple(r) for r in rows)
    payload = json.dumps([list(r) for r in ordered], separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def digest_of_chunks(chunks: Iterable) -> Tuple[str, int]:
    """(digest, rows) of chunks the chunker made."""
    rows = [row_of_chunk(c) for c in chunks]
    return chunk_set_digest(rows), len(rows)


def digest_of_db_rows(records: Iterable[Sequence]) -> Tuple[str, int]:
    """(digest, rows) of `DB_ROWS_SQL`'s result: (file_path, start_line, end_line,
    chunk_type, content, breadcrumb, docstring) per row."""
    rows = [chunk_row(*r) for r in records]
    return chunk_set_digest(rows), len(rows)


def tree_digest(files: Iterable[Sequence]) -> str:
    """SHA-256 over the collected source files (path, sha256 of content), in path order.

    `files` are `collect_files`'s (path, content, language) triples, read with
    universal newlines, so the digest does not depend on the platform's line
    endings. Two records whose corpus_tree_digest is equal read one tree,
    whatever their commits claim (22.2-01, review A3).
    """
    h = hashlib.sha256()
    for path, content, _ in sorted(files, key=lambda f: f[0]):
        h.update(path.encode("utf-8") + b"\0")
        data = content.replace("\r\n", "\n").encode("utf-8")
        h.update(hashlib.sha256(data).hexdigest().encode("ascii") + b"\n")
    return h.hexdigest()


def _is_test_file(path: Path) -> bool:
    return path.name.startswith("test_") or path.name.endswith("_test.py")


def source_files(dirs: Sequence[str], workers_dir: Path = WORKERS_DIR) -> list:
    """The non-test .py files under `dirs`, as (relative posix path, Path)."""
    out = []
    for rel in dirs:
        base = workers_dir / rel
        for p in sorted(base.rglob("*.py")):
            if "__pycache__" in p.parts or _is_test_file(p):
                continue
            out.append((p.relative_to(workers_dir).as_posix(), p))
    return sorted(out)


def chunker_files(workers_dir: Path = WORKERS_DIR) -> list:
    """The files the chunker version hashes, as (relative posix path, Path)."""
    return source_files(CHUNKER_DIRS, workers_dir)


def installed_versions(packages: Sequence[str] = GRAMMAR_PACKAGES) -> Mapping[str, str]:
    return {name: metadata.version(name) for name in packages}


def _code_hash(files, packages: Mapping[str, str]) -> str:
    h = hashlib.sha256()
    for rel, path in files:
        data = path.read_bytes().replace(b"\r\n", b"\n")
        h.update(b"file\0" + rel.encode("utf-8") + b"\0")
        h.update(hashlib.sha256(data).hexdigest().encode("ascii") + b"\n")
    for name, version in sorted(packages.items()):
        h.update(f"package\0{name}\0{version}\n".encode("utf-8"))
    return h.hexdigest()[:16]


def chunker_version(workers_dir: Path = WORKERS_DIR) -> str:
    """The first 16 hex characters of the chunker's code-and-parsers hash."""
    files = chunker_files(workers_dir)
    if not files:
        raise FileNotFoundError(f"no chunker or parser source under {workers_dir}")
    return _code_hash(files, installed_versions())


# The code that retrieves and measures, named in every run header beside the
# harness's commit, so a run from an export (no .git, no commit to read) still
# names the code that measured it (22.2-01, review A8).
RETRIEVAL_DIRS = ("workers/retrieval", "workers/embeddings", "workers/storage", "scripts")


def retrieval_code_version(workers_dir: Path = WORKERS_DIR) -> str:
    """The first 16 hex characters of a hash over RETRIEVAL_DIRS' non-test source."""
    files = source_files(RETRIEVAL_DIRS, workers_dir)
    if not files:
        raise FileNotFoundError(f"no retrieval source under {workers_dir}")
    return _code_hash(files, {})

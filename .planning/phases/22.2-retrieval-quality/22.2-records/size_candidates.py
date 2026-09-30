#!/usr/bin/env python3
"""Size TypeScript corpus candidates through the GitHub REST API (unauthenticated, read-only).

A MEASUREMENT RECORD, not product code. For each candidate: default branch,
head SHA, license, archived, then the recursive tree at that SHA, counting
.ts/.tsx/.js/.jsx files and bytes after the exclusions a benchmark spec would
apply (tests, declarations, generated, vendored, migrations). Output: one JSON
file per candidate (committed in ts-candidates-raw/) plus a TSV line;
consolidate.py turns them into ts-candidates.json.

It asks for each repository's CURRENT default-branch head, so a re-run sizes
newer trees; the SHAs it recorded on 2026-09-29 are in the raw files, and the
tree at a SHA does not change.

USAGE
    size_candidates.py <out-dir> <owner/repo> [<owner/repo> ...]
"""
import json
import re
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from pathlib import PurePosixPath

API = "https://api.github.com"
OUT = sys.argv[1]
CANDIDATES = sys.argv[2:]

VENDORED = {"node_modules", "dist", "build", "vendor", "third_party", ".git", ".next", "out", "coverage"}
TEST_DIR = {"test", "tests", "__tests__", "e2e", "__mocks__", "cypress", "playwright", "fixtures", "__fixtures__", "mocks", "stories", "storybook", ".storybook"}
TEST_FILE = re.compile(r"\.(test|spec|stories|e2e|cy)\.(t|j)sx?$")
GENERATED = re.compile(r"(\.d\.ts$|\.generated\.|__generated__|/generated/|\.gen\.ts$|\.min\.js$)")
MIGRATION_DIR = {"migrations", "migration"}


def get(url):
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "rag-doc-sizing"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        remaining = resp.headers.get("X-RateLimit-Remaining")
        return json.loads(resp.read().decode("utf-8")), remaining


def classify(path):
    p = PurePosixPath(path)
    parts = [x.lower() for x in p.parts]
    ext = p.suffix.lower()
    if ext not in (".ts", ".tsx", ".js", ".jsx", ".mts", ".cts"):
        return None
    if any(x in VENDORED for x in parts[:-1]):
        return "vendored"
    if GENERATED.search(path):
        return "generated"
    if any(x in TEST_DIR for x in parts[:-1]) or TEST_FILE.search(p.name.lower()):
        return "test"
    if any(x in MIGRATION_DIR for x in parts[:-1]):
        return "migration"
    if ext in (".js", ".jsx"):
        return "js"
    return "ts" if ext in (".ts", ".mts", ".cts") else "tsx"


rows = []
for full in CANDIDATES:
    try:
        repo, rem = get(f"{API}/repos/{full}")
        branch = repo["default_branch"]
        ref, rem = get(f"{API}/repos/{full}/commits/{branch}")
        sha = ref["sha"]
        tree, rem = get(f"{API}/repos/{full}/git/trees/{sha}?recursive=1")
    except Exception as exc:  # noqa: BLE001
        print(f"{full}\tERROR {type(exc).__name__}: {exc}", flush=True)
        continue
    counts = Counter()
    sizes = Counter()
    by_top = defaultdict(lambda: Counter())
    for entry in tree.get("tree", []):
        if entry.get("type") != "blob":
            continue
        kind = classify(entry["path"])
        if kind is None:
            continue
        counts[kind] += 1
        sizes[kind] += entry.get("size", 0)
        if kind in ("ts", "tsx"):
            top = "/".join(entry["path"].split("/")[:2]) if "/" in entry["path"] else "."
            by_top[top]["files"] += 1
            by_top[top]["bytes"] += entry.get("size", 0)
    record = {
        "repo": full, "branch": branch, "sha": sha, "license": (repo.get("license") or {}).get("spdx_id"),
        "archived": repo.get("archived"), "pushed_at": repo.get("pushed_at"), "stars": repo.get("stargazers_count"),
        "truncated": tree.get("truncated"), "counts": counts, "bytes": sizes,
        "by_top": {k: dict(v) for k, v in sorted(by_top.items(), key=lambda kv: -kv[1]["bytes"])},
    }
    safe = full.replace("/", "__")
    with open(f"{OUT}/{safe}.json", "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=1, default=dict)
    ts_files = counts["ts"] + counts["tsx"]
    ts_kib = (sizes["ts"] + sizes["tsx"]) / 1024
    print(f"{full}\t{sha[:12]}\t{record['license']}\tarchived={record['archived']}\tpushed={record['pushed_at']}"
          f"\tts={counts['ts']} tsx={counts['tsx']} ({ts_kib:.0f} KiB)\tjs={counts['js']}\ttest={counts['test']}"
          f"\tgen={counts['generated']}\tmig={counts['migration']}\tvend={counts['vendored']}\ttrunc={record['truncated']}"
          f"\trate_remaining={rem}", flush=True)
    time.sleep(0.5)

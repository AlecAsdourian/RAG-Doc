#!/usr/bin/env python3
"""Fetch a public repository at one pinned commit, the way rag_quality_harness.py's do_fetch does.

A MEASUREMENT RECORD, not product code: git init, add the remote, fetch that
one commit at depth 1, check it out detached, and confirm HEAD is the pinned
sha. On a directory already fetched it only reads HEAD, and exits 1 when HEAD
is not the pinned sha, so reproduce.py uses it to check the corpora's pins.

USAGE
    fetch_pinned.py <dest-dir> <https-url> <40-char-sha>
"""
import subprocess
import sys
from pathlib import Path

dest, url, sha = Path(sys.argv[1]), sys.argv[2], sys.argv[3]


def git(*args):
    return subprocess.run(["git", *args], cwd=dest, capture_output=True, text=True, check=True).stdout.strip()


if (dest / ".git").exists():
    head = git("rev-parse", "HEAD")
    print(f"already fetched: {dest.name} at {head}")
    sys.exit(0 if head == sha else 1)
dest.mkdir(parents=True, exist_ok=True)
git("init", "-q")
git("remote", "add", "origin", url)
git("fetch", "-q", "--depth", "1", "origin", sha)
git("checkout", "-q", "--detach", "FETCH_HEAD")
head = git("rev-parse", "HEAD")
print(f"fetched {url} at {head} into {dest.name}")
sys.exit(0 if head == sha else 1)

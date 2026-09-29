"""Which files in a repository are indexed, and which are never even written.

An archive holds only TRACKED files, so `.gitignore` protects nothing
(22-RESEARCH Q8): what needs filtering is tracked content that must not be
indexed. Three kinds, in the order they are checked:

1. **Secret-looking files (U7).** Never sent to OpenAI, never stored, never
   written to the worker's disk. A deny-list by base name, case-insensitive.
   It misses secrets pasted into ordinary source files; the user chose this
   over content scanning for v1 (U7, option A).
2. **Vendored and generated code.** Directories such as `vendor/` and
   `node_modules/`, files such as `*.min.js` and `*_pb2.py`, lockfiles, and
   Go files whose first 20 lines carry the canonical generated-code header.
3. **Everything without a language.** The extension map is the list of
   languages the chunker handles; anything else is skipped and counted.

`classify_path` is applied by the extractor BEFORE a member is written, and
again by the walk over what is on disk, so what gets indexed is judged on
the tree as it exists, not on what the extractor thinks it wrote.

The counts these produce are COUNTS ONLY, never paths: the secret-looking
paths are exactly the ones not to repeat anywhere.
"""

from __future__ import annotations

import os
import re
from fnmatch import fnmatchcase
from typing import NamedTuple, Optional

#: U7's deny-list, by base name, matched case-insensitively.
SECRET_NAMES = frozenset({".env", ".npmrc", ".pypirc", ".netrc"})
SECRET_PATTERNS = (
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "*.tfvars",
    "credentials*.json",
    "service-account*.json",
)

#: A file under any of these directories, at any depth, is vendored.
VENDORED_DIRS = frozenset({"vendor", "node_modules", "dist", "build", ".git", "third_party"})

GENERATED_PATTERNS = ("*.min.js", "*_pb2.py", "*.pb.go")

LOCKFILES = frozenset({"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "go.sum"})

#: Extension -> language. This is the whole of "indexable" at the name level.
LANGUAGES = {
    ".py": "python",
    ".go": "go",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".md": "markdown",
}

#: The canonical Go marker (https://go.dev/s/generatedcode), checked over the
#: first GENERATED_GO_LINES lines only.
GENERATED_GO_HEADER = re.compile(r"^// Code generated .* DO NOT EDIT\.$")
GENERATED_GO_LINES = 20


class Verdict(NamedTuple):
    """What `classify_path` decided. `reason` is the skip-count key."""

    indexable: bool
    reason: str
    language: Optional[str]


def is_secret_name(basename: str) -> bool:
    """True when the base name is on U7's deny-list."""
    lower = basename.lower()
    if lower in SECRET_NAMES:
        return True
    return any(fnmatchcase(lower, pattern) for pattern in SECRET_PATTERNS)


def classify_path(path: str) -> Verdict:
    """Judge a POSIX-style relative path by its name alone.

    Content checks (binary, non-UTF-8, generated Go) are the walk's job,
    because they need the bytes.
    """
    parts = [part for part in path.split("/") if part]
    if not parts:
        return Verdict(False, "unsafe_path", None)
    basename = parts[-1]
    lower = basename.lower()

    if is_secret_name(lower):
        return Verdict(False, "secret", None)
    if any(directory.lower() in VENDORED_DIRS for directory in parts[:-1]):
        return Verdict(False, "vendored", None)
    if any(fnmatchcase(lower, pattern) for pattern in GENERATED_PATTERNS):
        return Verdict(False, "generated", None)
    if lower in LOCKFILES:
        return Verdict(False, "lockfile", None)

    language = LANGUAGES.get(os.path.splitext(lower)[1])
    if language is None:
        return Verdict(False, "unsupported", None)
    return Verdict(True, "indexable", language)


def is_generated_go(text: str) -> bool:
    """True when a Go file's first 20 lines carry the generated-code header."""
    for line in text.splitlines()[:GENERATED_GO_LINES]:
        if GENERATED_GO_HEADER.match(line.rstrip("\r")):
            return True
    return False

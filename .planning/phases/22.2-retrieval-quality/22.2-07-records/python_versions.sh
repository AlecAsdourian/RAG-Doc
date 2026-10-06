#!/usr/bin/env bash
# The suite on Python 3.11 (the worker images, no git) and 3.12 (CI), for ISS-041 (22.2-07).
# A MEASUREMENT RECORD. It exports HEAD with `git archive` into a scratch directory, then for each
# image starts a scratch container (named w2207-py311 / w2207-py312, removed with --rm), installs
# requirements.txt and runs CI's command without the isolation tests (they start their own
# Postgres through Docker, which the container cannot reach): pytest tests/ workers/
# --ignore=tests/isolation. It prints the failures, the skip reasons with their counts, the
# summary and pytest's exit code. On 3.12 git is installed first when the network allows, so
# the git-dependent tests run; on 3.11-slim nothing is added, so they must skip with their reason.
# USAGE: python_versions.sh <scratch dir> <output file>
set -u
scratch="$1"; out="$2"
tree="$(cd "$(dirname "$0")/../../../.." && pwd)"
rm -rf "$scratch/w2207-export" && mkdir -p "$scratch/w2207-export"
git -C "$tree" archive HEAD | tar -x -C "$scratch/w2207-export"
head=$(git -C "$tree" rev-parse --short=12 HEAD)
cat > "$scratch/w2207-export/w2207-inner.sh" <<'INNER'
set -u
python --version
if [ "$1" = "3.12" ]; then
  (apt-get update -qq >/dev/null 2>&1 && apt-get install -y -qq git >/dev/null 2>&1) \
    || echo "git install failed (no network); the git tests skip"
fi
command -v git >/dev/null && git --version || echo "git: not installed"
pip install -q -r requirements.txt >/dev/null 2>&1 || echo "pip install failed"
python -m pytest tests/ workers/ --ignore=tests/isolation -q -p no:cacheprovider -rfEs > /tmp/o.txt 2>&1
rc=$?
grep -E '^(FAILED|ERROR) ' /tmp/o.txt | sed 's/ - .*//'
grep -E '^SKIPPED' /tmp/o.txt | sed -E 's/^SKIPPED \[([0-9]+)\] [^:]+:[0-9]+: /\1 x /' | sort | uniq -c
tail -1 /tmp/o.txt
echo "pytest exit $rc"
INNER
: > "$out"
for v in 3.11 3.12; do
  name="w2207-py${v/./}"
  {
    echo "== python:${v}-slim, HEAD ${head} (git archive), container ${name}"
    MSYS_NO_PATHCONV=1 docker run --rm --name "$name" -v "$scratch/w2207-export:/repo" -w /repo/services/workers \
      "python:${v}-slim" bash /repo/w2207-inner.sh "$v"
    echo
  } >> "$out" 2>&1
done

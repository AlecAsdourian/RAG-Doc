#!/usr/bin/env bash
# ISS-043's evidence: which tests need tiktoken to download its encoding at run time (22.2-07).
# A MEASUREMENT RECORD. It exports HEAD with `git archive`, starts a scratch python:3.12-slim
# container (w2207-tiktoken, removed with --rm), installs requirements.txt with the network, then
# runs `pytest tests/ workers/ --ignore=tests/isolation` with the encoding unreachable:
# TIKTOKEN_CACHE_DIR an empty directory, HTTPS and HTTP sent to a dead proxy, and NO_PROXY for
# localhost so tests that use local sockets are not disturbed (review B's method, PR #67). It
# prints the failing and erroring tests counted per file, the summary and pytest's exit code;
# then the same run with the network, for comparison.
# USAGE: tiktoken_offline.sh <scratch dir> <output file>
set -u
scratch="$1"; out="$2"
tree="$(cd "$(dirname "$0")/../../../.." && pwd)"
rm -rf "$scratch/w2207-tk-export" && mkdir -p "$scratch/w2207-tk-export"
git -C "$tree" archive HEAD | tar -x -C "$scratch/w2207-tk-export"
head=$(git -C "$tree" rev-parse --short=12 HEAD)
cat > "$scratch/w2207-tk-export/w2207-tk-inner.sh" <<'INNER'
set -u
pip install -q -r requirements.txt >/dev/null 2>&1 || echo "pip install failed"
mkdir -p /tmp/empty-tiktoken-cache
echo "-- offline: TIKTOKEN_CACHE_DIR empty, HTTPS/HTTP to a dead proxy, NO_PROXY=localhost,127.0.0.1"
TIKTOKEN_CACHE_DIR=/tmp/empty-tiktoken-cache HTTPS_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 \
  NO_PROXY=localhost,127.0.0.1 python -m pytest tests/ workers/ --ignore=tests/isolation -q -p no:cacheprovider \
  -rfE > /tmp/off.txt 2>&1
rc=$?
grep -E '^(FAILED|ERROR) ' /tmp/off.txt | sed -E 's/^(FAILED|ERROR) ([^:]+)::.*/\1 \2/' | sort | uniq -c
echo "tests needing the download: $(grep -cE '^(FAILED|ERROR) ' /tmp/off.txt)"
echo "download URLs in the tracebacks: $(grep -oE 'openaipublic\.blob\.core\.windows\.net[^ ]*' /tmp/off.txt | sort -u | head -3 | tr '\n' ' ')"
tail -1 /tmp/off.txt
echo "pytest exit $rc"
echo "-- online, the same command"
python -m pytest tests/ workers/ --ignore=tests/isolation -q -p no:cacheprovider > /tmp/on.txt 2>&1
rc=$?
tail -1 /tmp/on.txt
echo "pytest exit $rc"
INNER
{
  echo "== python:3.12-slim, HEAD ${head} (git archive), container w2207-tiktoken"
  MSYS_NO_PATHCONV=1 docker run --rm --name w2207-tiktoken -v "$scratch/w2207-tk-export:/repo" \
    -w /repo/services/workers python:3.12-slim bash /repo/w2207-tk-inner.sh
} > "$out" 2>&1

#!/usr/bin/env bash
# ISS-041 before the fix: tests/test_tripwire.py at the plan's base (5bb8693) on Python 3.11 and
# 3.12, in scratch containers removed with --rm. A MEASUREMENT RECORD (22.2-07).
# USAGE: base_tripwire.sh <git archive of 5bb8693> <output file, appended>
set -u
base="$1"; out="$2"
for v in 3.11 3.12; do
  {
    echo "== base 5bb8693 (before the fix), python:${v}-slim, tests/test_tripwire.py"
    MSYS_NO_PATHCONV=1 docker run --rm --name "w2207-base-py${v/./}" -v "$base:/repo" -w /repo/services/workers \
      "python:${v}-slim" bash -c "pip install -q -r requirements.txt >/dev/null 2>&1; \
      python -m pytest tests/test_tripwire.py -q -p no:cacheprovider -rf 2>&1 | grep -E '^FAILED|passed|failed' | sed 's/ - .*//'"
    echo
  } >> "$out" 2>&1
done

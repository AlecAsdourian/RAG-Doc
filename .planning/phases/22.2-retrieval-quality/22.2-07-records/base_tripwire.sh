#!/usr/bin/env bash
# ISS-041 before the fix: tests/test_tripwire.py at the plan's base (5bb8693) on Python 3.11 and
# 3.12, in scratch containers removed with --rm. A MEASUREMENT RECORD (22.2-07). It prints the
# failing tests, the summary and pytest's own exit code.
# USAGE: base_tripwire.sh <git archive of 5bb8693> <output file, appended>
set -u
base="$1"; out="$2"
cat > "$base/w2207-base-inner.sh" <<'INNER'
pip install -q -r requirements.txt >/dev/null 2>&1 || echo "pip install failed"
python -m pytest tests/test_tripwire.py -q -p no:cacheprovider -rf > /tmp/o.txt 2>&1
rc=$?
grep -E '^FAILED ' /tmp/o.txt | sed 's/ - .*//'
tail -1 /tmp/o.txt
echo "pytest exit $rc"
INNER
for v in 3.11 3.12; do
  {
    echo "== base 5bb8693 (before the fix), python:${v}-slim, tests/test_tripwire.py"
    MSYS_NO_PATHCONV=1 docker run --rm --name "w2207-base-py${v/./}" -v "$base:/repo" -w /repo/services/workers \
      "python:${v}-slim" bash /repo/w2207-base-inner.sh
    echo
  } >> "$out" 2>&1
done

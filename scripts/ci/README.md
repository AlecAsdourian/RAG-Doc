# Isolation-test coverage scanner

`check-isolation-tests.py` fails a PR if it adds a mutation endpoint
(`POST`/`PUT`/`PATCH`/`DELETE`) without a matching isolation test in
the same PR.

See `docs/isolation.md` for the full pattern; this document is
operational only.

## Running locally

From the repo root, against the current branch's diff vs. `main`:

```bash
python scripts/ci/check-isolation-tests.py --base-ref main --verbose
```

Exit code `0` = pass, `1` = missing coverage, `2` = internal error
(e.g., git failed).

For a machine-readable report (used by the GitHub Action to build the
PR comment):

```bash
python scripts/ci/check-isolation-tests.py --base-ref main --json
```

## What counts as coverage

For each detected mutation endpoint added in the diff, the scanner
searches every isolation-test file that was also added or modified in
the same diff (`*_isolation_test.go`, `test_*_isolation.py`,
`*_isolation_test.py`) for a substring match on the endpoint path.

Handler-name matching is intentionally NOT used — path strings are more
stable across refactors and easier to grep manually if the check fails.

## Escape hatch

For legitimately non-tenant-scoped endpoints (health checks, webhooks
that don't touch tenant data), add an inline marker on the route
registration line:

```go
r.Post("/health", healthHandler) // @skip-isolation-test: no tenant data
```

```python
@router.post("/health")  # @skip-isolation-test: no tenant data
```

**The reason MUST be non-empty.** `@skip-isolation-test:` on its own
does not unlock the skip; a whitespace-only reason does not either. The
scanner will still report the endpoint as missing.

## Running the scanner's own test suite

```bash
cd scripts/ci
pytest test_check_isolation.py -v
```

Seven scenarios cover Go and Python mutation endpoints, the two
coverage paths, both skip-marker paths, and read-endpoint exclusion.

## Adding a new endpoint framework

Extend `ENDPOINT_PATTERNS` in `check-isolation-tests.py`. Each pattern
must match on an added line (prefix `^\+`) and capture the method and
path via groups 1 and 2. Add a test scenario to
`test_check_isolation.py` alongside.

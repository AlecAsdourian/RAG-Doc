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

**Commit before running it.** The diff comes from `<base>...HEAD` while
route prefixes are read from the working tree, so uncommitted route
changes leave the two disagreeing about line numbers and the reported
paths come out wrong. CI checks out a clean tree, so this only bites
locally. Also pass the base ref your remote actually uses — `--base-ref`
defaults to `origin/main`, and a clone with a differently-named remote
needs it spelled out.

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

The path it matches on is the **full** path. A Go route registered as
`"/{id}"` inside `r.Route("/api/repositories", ...)` is reported as
`/api/repositories/{id}`; the enclosing `Route`/`Mount` prefixes are
resolved from the file on disk, because the opener is usually a context
line rather than part of the diff.

For a parameterised route, the **static prefix** — everything before the
first `{` — counts too. A test drives `DELETE /api/repositories/{id}` by
building `"/api/repositories/" + id`, so the literal `{id}` appears
nowhere; demanding it would make parameterised routes permanently
uncoverable.

Handler-name matching is intentionally NOT used — path strings are more
stable across refactors and easier to grep manually if the check fails.

Two limits worth knowing:

- **Matching is method-blind.** A test that only exercises
  `GET /api/things` marks a newly added `POST /api/things` as covered.
  This is a ratchet against forgetting, not proof of coverage.
- **An endpoint whose full path resolves to `/` is always reported
  missing**, never covered — `"/"` appears in every file, so matching on
  it would mean nothing. Give the route a real path or an explicit skip
  marker.

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

Eighteen scenarios cover Go and Python mutation endpoints, the two
coverage paths, both skip-marker paths, read-endpoint exclusion,
multi-line registrations, and nested `chi.Route` groups.

## Adding a new endpoint framework

Extend `ENDPOINT_PATTERNS` in `check-isolation-tests.py`. Each pattern
captures the method and path via groups 1 and 2, and must be usable with
`search`/`finditer` anywhere in a line — do **not** anchor it on the
diff's leading `+`. The patterns are applied both to added lines with the
marker already stripped and to raw diff lines when locating route
boundaries. Add a test scenario to `test_check_isolation.py` alongside.

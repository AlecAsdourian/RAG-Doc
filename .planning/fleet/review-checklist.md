# Review checklist

Written 2026-09-29 from the findings that recurred across Phases 21 and 22.
Two uses, same list:

- **Workers** run it on their own branch **before opening the PR**, and say in the
  PR body which items they checked and how. The goal is that the first review
  returns "approve with nits", not "changes requested".
- **Reviewers** use it as the floor, not the ceiling. Anything on it that a PR
  misses is a finding; anything off it is still fair game.

Every phase also has an `NN-ACCEPTANCE.md`: numbered, testable criteria for the
phase. A PR body says which criteria it advances and with what evidence; a review
verdict says whether that evidence holds. Neither is optional.

## Tests: can this test actually fail?

- **Fixtures.** Ask what the fixture cannot distinguish. A single-tenant fixture
  cannot catch a cross-tenant leak. If every fixture already has the property
  being asserted, the assertion is decoration. (21-04: a mutation that relabelled
  `synced` rows survived because no fixture had a synced row.)
- **Mutation checks.** Commit first. Prove the mutation reached the code or the
  database — mutated text present *and* original absent — before believing a
  result. **Neuter a predicate rather than deleting it:** deleting can change a
  statement's arity and fail every test for the wrong reason (21-07, M1a).
- **Migrations under test need a fresh database.** golang-migrate never re-applies
  a recorded version; a reused container silently tests the old schema.
- **Isolation tests connect as a non-superuser.** A superuser bypasses RLS, so a
  passing isolation test on a superuser DSN proves nothing.
- **A test that reads no log record cannot catch a logging bug.**
- **An assertion inside a `finally` replaces the exception propagating out of the
  block** — every real failure inside then reads as the assertion's message.
- **A helper that carries its own copy of a predicate is blind to mutations of the
  original.** Test the production statement, not a lookalike.
- **Race tests race.** Build every request or transaction *before* releasing the
  barrier; give the pool enough connections; run several rounds on a warm pool. A
  kill-test that fires before the thing it kills exists passes half the time.
- **Contract tests, not implementation tests.** Assert what the row or the
  response must contain, not that the code did the step.

## Migrations and schema

- **Foreign keys go inside `CREATE TABLE`.** A key added by `ALTER TABLE` after a
  tenant was set in the file validates against one tenant's rows (ISS-031). The
  seeded gate enforces this; run it early, not last.
- **Never set a sentinel tenant** to get past a failing validation — it makes the
  check see zero rows and pass vacuously.
- **A key over data already there:** lift FORCE for that one statement on every
  table the validation reads (the key's table *and* the referenced table), and
  restore it. The gate asserts FORCE is restored on every RLS table.
- **Verify in the deployment shape** — a `NOSUPERUSER NOBYPASSRLS` owner under
  FORCE RLS — not only as the harness superuser. A backfill can silently write
  nothing under the role we deploy with (21-04, 22-01).
- **Partitioned tables:** RLS on the parent does not reach the partitions. Every
  partition gets RLS, FORCE, the policy and the tenant trigger. TRUNCATE is never
  granted; it bypasses RLS.
- **Every writer supplies the tenant column.** A `BEFORE` trigger cannot fill a
  partition key (`0A000`).
- **The down migration** is an inverse: no stale comments, no `NOT VALID` keys
  left behind unless documented, and up/down/up leaves an identical schema.
- **Migration numbers** used by probes or tests must derive from the newest
  existing version, never be hard-coded.

## Runtime and security

- **Secrets never reach a row, a log, a response or a test failure message.**
  `last_error`, progress fields, `reason` strings and `docker compose config`
  output all go through redaction or are asserted on names only. Check every
  `str(e)`, `%v`, f-string of an exception, and every place a token could sit in
  a URL.
- **Tenant scope on every statement that touches tenant data.** A statement with
  no organization filter on a table with no RLS is cross-tenant by construction.
  Name it as such in `doc.go`, and never let it reach a request handler.
- **Connection options merge, never replace.** Passing `options=` to psycopg2
  replaces the DSN's `options`, which is where the role is set — a silent
  privilege change (21-06).
- **Fences on every write a stale worker could make:** id + owner + state.
  Supersede leaves the lease attached, which is *why* the state predicate is
  needed.
- **Wrong-order writes fail silently, not loudly.** Through an upsert, the wrong
  order raises nothing and loses the work. Test for the missing row, not the
  error.
- **Bounded loops and retries.** A "hand it back" path that consumes no attempt
  can spin forever; a connect with no timeout can hang past every give-up rule.
- **Control characters and encodings.** NUL bytes break psycopg2 writes; a smart
  quote in a source file breaks a cp1252 subprocess. Decode subprocess output as
  UTF-8 explicitly.
- **One flag, one meaning.** "Shut down" and "you lost your lease" are different
  events and need different signals.

## Documentation

- **Measure before claiming.** Mark what was inferred as inferred. A claim of the
  form "X is exactly Y" gets diffed, not read.
- **One authority per list.** The same list in three files becomes three lists.
  Point at the authority; do not restate it.
- **A comment that says a test enforces something must be true.** "Enforced by
  the gate" was false twice in one phase.
- **Stale references:** deleted files, old image names, dead test names.
- **Vocabulary is fixed in one place** (stage names, handler endings, error
  codes) and every other file points at it.

## Process

- `DATABASE_TEST_URL` on a scratch Postgres for every Go run; port 5434 is not
  ours. Never start or write to the compose Postgres or Qdrant.
- Fresh venv from `requirements.txt` for the Python suite; run it as CI does.
- One-sentence commits, conventional prefix, **no trailers of any kind**.
- The PR body maps to `NN-ACCEPTANCE.md` and lists which checklist items were
  run, with the evidence.

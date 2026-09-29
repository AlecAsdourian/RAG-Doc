# Phase 22 — Acceptance criteria

Written 2026-09-29, after 22-01 merged, at the user's request: every phase now
carries numbered, testable criteria that the worker's PR body and the reviewer's
verdict both map to. `22-CONTEXT.md` remains the authority for decisions; this
file is the authority for **what "done" means**.

**How it is used**
- A PR body has a section **"Acceptance criteria"** listing the IDs it advances,
  each with the evidence (a test name, a measurement, a CI log line). "Advances"
  can mean partially; say which part.
- A review verdict has a **"Criteria check"** line per cited ID: holds / does not
  hold / not verifiable, with what was run.
- The phase closes when every criterion is **met with evidence recorded in a
  SUMMARY**, not when the last plan merges.
- A criterion that turns out wrong is corrected here with a dated note, never
  silently met by a weaker reading.

## Criteria

**A1 — pgvector everywhere.** Every Postgres this project starts — both test
harnesses, CI and compose — is `pgvector/pgvector:pg16` pinned by one digest, and
migration 000016 creates the extension so any other image fails loudly.
*Evidence:* the digest in all four places; CI applying `16/u`. *Met by 22-01
(`4a3b16c`).*

**A2 — Migrations are proven in the deployment shape.** The seeded-migration gate
runs in CI: a `NOSUPERUSER NOBYPASSRLS` owner, seeded at 10 and 12, one `up` in one
session. Its tenant audit fails any foreign key validated under a tenant or against
a forced-RLS table with rows; FORCE is asserted restored on every RLS table; no key
is left `NOT VALID`. ISS-031 is closed on that evidence. *Evidence:* the gate's
before/after runs; the audit killing the sentinel and the `ALTER TABLE` forms.
*Met by 22-01; every later migration must keep it green.*

**A3 — Tenant isolation on `chunks` holds by schema, not convention.** All 64
partitions carry RLS, FORCE, the scalar policy and `trg_assert_tenant`. As the app
role under tenant A, addressing tenant B's partition by name reads nothing,
changes nothing and cannot insert. A chunk whose organization disagrees with its
repository's is refused by the composite key (`23503`) **with the tenant trigger
disabled**. TRUNCATE is granted to no role. The drift query runs in CI and
detects manufactured drift, including a chunk citing another tenant's run or
symbol. *Evidence:* `chunks_partition_test.go`, the gate's 000017 assertions, the
drift self-tests. *Met by 22-02 (`68a3af2`).*
*Corrected 2026-09-29, from PR #49's worker note:* "with the trigger disabled"
means `trg_assert_tenant`. With **foreign-key** triggers disabled, or under
`session_replication_role = replica`, a misfiled row is accepted and only the
drift query catches it — the limit is pinned by
`TestChunksPartition_TheSingleColumnKeysCarryNoTenancy` and the replica pin, and
stated in `docs/isolation.md`. The single-column `ingestion_run_id` and
`symbol_id` keys carry no tenancy at all until ISS-036 (22.1-01).

**A4 — Every writer of `chunks` is honest about tenancy and vectors.** Each
supplies `organization_id`, the vector and `embedding_model`. `PostgresWriter`
refuses a chunk with no vector **before writing anything**, and duplicate-content
chunks all receive their vector. *Evidence:* the writer inventory in
`22-02-SUMMARY.md`; the writer isolation tests; the duplicate mutation.
*Advanced by 22-02.*

**A5 — Partition pruning works on the production query shape.** `EXPLAIN` on both
retrieval legs shows `Subplans Removed: 63` under the scalar policy. *Evidence:*
the pruning test; the equivalence gate's plans. *Advanced by 22-02, confirmed by
22-03.*

**A6 — Retrieval runs on pgvector, and rankings did not change.** Both legs run in
Postgres under RLS as the app role, filtered by `embedding_model`, with
`hnsw.iterative_scan` set. The equivalence gate passed **under a rule committed
before any measurement**: every question in every set of every corpus ranks the
same, or the difference is classified (a), (b) or (c); zero UNEXPLAINED; no tuning.
Query vectors were embedded once and cached, and both runs used them (hash
verified). Vector read-backs are decoded as `float32`; comparisons use a
float32-scale tolerance. *Evidence:* `22-03-equivalence.md` committed first; the
`compare_runs.py` output and exit code. *Advanced by 22-03.*
*Qualified 2026-09-29, from PR #53's reviewer A:* what was measured is **"the
storage move, judged by exact search on both sides, changed no ranking on 130
vector-leg questions."** At benchmark size (≤ 2,816 chunks per repository) the
planner served the pgvector leg by exact scans, never HNSW, and the keyword leg
was empty for 120 of 130 questions (ISS-029). Rankings served by HNSW at scale
are **22.1-05's** to prove, not this criterion's. "Read-backs decoded as
`float32`" does not apply to the read path, which reads a float8 distance and no
vector; it applies to the harness's exact-list computation, which uses the same
server-side operator on the same literal. The cross-store score difference was
≤ 6.03e-07 over 6,395 pairs against a pre-committed 1e-5 tolerance; the quality
track should fix its tolerance at about 2e-6 from that measurement, dated, before
its next rule.

**A7 — Qdrant is retired.** No runtime dependency remains: not in
`requirements.txt`, compose, `.env.example`, the frontend, the routes or the
docs (history excepted). The benchmark harness refuses `--ingest` and `--clear`
on compose's ports without `--allow-compose`. *Evidence:* a grep gate; the
harness guard's test. *Advanced by 22-03.*

**A8 — Fetching a repository is safe.** The backend keeps the App key; the worker
receives a one-hour, one-repository, `contents: read` token only while it holds a
live lease, from an internal route whose responses are marked and whose 404 is
byte-identical for every miss. The worker fetches an archive at an exact SHA;
500 MB is enforced on the download **and** the expansion, streaming; 20k files,
1 MB per file and 100k chunks are enforced; secret-looking files are skipped;
path traversal, absolute paths, links, bombs and oversize files are refused, each
guard mutation-checked. No customer code is ever executed. A token never appears
in a log, an error, `last_error`, a logged URL or a test failure (sentinel test).
*Evidence:* the hostile-archive table in `22-04-SUMMARY.md`; the no-leak test.
*Advanced by 22-04.*

**A9 — The worker processes real jobs safely.** `REGISTRY` has `full_ingest` and
`incremental`. Endings follow Phase 21's policy: a cap violation ends `dead` in one
attempt (`Rejected`); a mid-run suspension defers; a mid-run uninstall abandons; a
refused token raises `LeaseLost` and writes nothing. `progress` is cumulative on
every report. The heartbeat's `statement_timeout` is merged into the DSN's
options so its role cannot change. Compose's `workers` sits behind a profile and
never holds the App key. *Evidence:* the endings table and its tests in
`22-05-SUMMARY.md`. *Advanced by 22-05.*

**A10 — One real repository is indexed and searchable end to end.**
`AlecAsdourian/ES-SC-API-Navigator` (approved by the user 2026-09-17) goes
connect → queue → worker → pgvector → `/search`, in a scratch database, with every
process running as `rag_doc_app`, and a search returns the expected file. Compose
is never touched. *Evidence:* the live-proof record in `22-05-SUMMARY.md`.
*Advanced by 22-05; this is the phase's headline.*

**A11 — The phase leaves honest records.** Every SUMMARY states what was measured
and what was inferred; every "does NOT pin" item is carried to the phase-level
summary; the open-issue table is accurate; no file claims a guarantee a test does
not give. *Evidence:* the reviewer's criteria checks on each PR.*

## Not acceptance criteria (out of scope, from `22-CONTEXT.md` Boundaries)

Retrieval-quality changes (chunker, model, ranking — the protocol-gated track);
SCIP tier 2; D4's memory tables; ISS-023; ISS-021; single-statement fusion; SSE;
the final shape of the `feedback` link. A PR that advances one of these is scope
creep unless the user has moved it in.

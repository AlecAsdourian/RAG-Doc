# Phase 21: Ingestion Job Infrastructure — Context

**Written:** 2026-09-10. **Revised the same day after review** — five
correctness bugs in the schema, and two decisions (O1, O2) since confirmed.
See `.planning/v2-substrate/REWORK.md` §6.
**Research:** `21-RESEARCH.md` in this directory
**Also depends on:** `.planning/v2-substrate/DECISIONS.md` (D2 in particular)

## Objective

The bones of ingestion: a durable queue, a job state machine, leases, retries and
a dead-letter path — with nothing actually cloning a repository yet (that is
Phase 22).

The phase exists to make Phase 22 possible, and to close ISS-016 by removing the
thing that caused it rather than patching around it.

---

## The blocker this phase inherits

**ISS-016 — `sync_state` has no lease, so a relink can re-queue a run already in
flight.**

The issue is filed as a queueing bug, but the root cause is a modelling one:
`repositories.sync_state` is a *status column* being used as a *queue*. A status
column has no owner, no lease and no attempt counter, so two writers can each
believe they own the same repository, and whichever finishes last writes the
final state. 20-05's webhook writes go through the same upsert and inherit it.

**This phase does not add a lease to `sync_state`.** It stops using it as a
queue. Once a real work item exists, `sync_state` goes back to being what its
name says — a status for the UI to read — and the race has nowhere to happen.

**ISS-016 is narrowed to the racing half, and that is decision O1.**

The issue also carried a second half — *a `failed` repository cannot be retried
through the public API at all*. That half is **not** in this phase, and the
first draft tried to have it both ways: `21-RESEARCH.md` claimed the retry loop
closed it, this document's Boundaries put API retry out of scope, and
`ISSUES.md` said the whole issue closed when Phase 21 ships. Three files, three
answers.

An issue that half-closes never closes cleanly. So: **ISS-016 is the racing
relink, which this phase genuinely closes.** The API-retry half is filed
separately as **ISS-023** and belongs to whichever phase works the API surface.

The state machine below makes the retry *possible*; exposing it is someone
else's deliverable.

---

## Locked decisions

Do not re-litigate these during execution without cause. The reasoning is in
`21-RESEARCH.md`; the short form is here.

### L1 — The queue is a Postgres table, claimed with `FOR UPDATE SKIP LOCKED`

Not Redis Streams (the roadmap's tentative pick), not pgmq, not Temporal.

Three reasons, in order of weight:

1. **Transactional completion.** D2 puts chunks and vectors in Postgres. A worker
   can therefore write its chunks and mark its job complete in one transaction —
   no state where a job is done but its chunks are missing. A Redis queue would
   make that a dual write across two systems, which is precisely the hazard D2
   exists to delete.
2. **Cross-language for free.** The producer is Go and the consumer is Python.
   A SQL interface is one implementation both speak; every language-native
   library would be two implementations hoping to agree.
3. **Throughput is a non-issue.** One job per connect, one per push — around 0.1
   jobs/second at realistic load, roughly three orders of magnitude below where
   Postgres-as-a-queue degrades.

pgmq was the close call and is documented as such in the research: rejected
because it is a third deploy-target constraint after pgvector and nested
virtualization, and because its message model would force a jobs table beside it
that we would then have to keep in sync.

### L2 — A new `ingestion_jobs` table. `sync_state` becomes a projection.

Three things that were conflated get three homes:

| Concern | Home | Meaning |
|---------|------|---------|
| The work item | **`ingestion_jobs`** (new) | something to do, with a lease and attempts |
| The record of a run | `ingestion_runs` (exists) | what happened; `chunks` already FK to it |
| Status for the UI | `repositories.sync_state` (exists) | **derived** — written by the job, read by the frontend, never a queue |

```sql
CREATE TABLE ingestion_jobs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  -- ⚠ A TWO-HOP DENORMALISATION, MAINTAINED BY TRIGGER. See L5 and
  -- `v2-substrate/DECISIONS.md` D5.
  --
  -- `repositories` has NO `organization_id`; tenancy runs
  -- `repositories.project_id -> projects.organization_id`. So this column is a
  -- copy of something two joins away and can drift from it. Because a worker
  -- uses this value to scope every write it then makes, a drifted row writes
  -- another tenant's data -- which makes it an authorization input, not the
  -- annotation the first draft called it.
  organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  repository_id   UUID NOT NULL REFERENCES repositories(id)  ON DELETE CASCADE,

  -- Set when the run begins, so the job points at its result record.
  ingestion_run_id UUID REFERENCES ingestion_runs(id) ON DELETE SET NULL,

  job_type TEXT NOT NULL CHECK (job_type IN ('full_ingest','incremental')),

  -- FIVE STATES, NOT SIX. `failed` was removed after review (decision O2).
  --
  -- It had no edge back to the claimable set and no place in the partial
  -- unique index below, so a failed job could neither be retried nor prevent
  -- a second live job for the same repository. Rather than add an edge, the
  -- state goes: a failed attempt sets `state='queued'` with `run_after` in
  -- the future and records `last_error`.
  --
  -- Nothing is lost. "This repository is currently failing" is
  -- `state='queued' AND attempts > 0`, which both the admin endpoint and the
  -- `sync_state` projection can read. `dead` remains the only failure
  -- terminal.
  state TEXT NOT NULL CHECK (state IN
    ('queued','running','completed','dead','superseded')),

  -- Lease. Short, extended by heartbeat. See L3.
  lease_owner      TEXT,
  lease_expires_at TIMESTAMPTZ,

  attempts     INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 5,
  run_after    TIMESTAMPTZ NOT NULL DEFAULT NOW(),   -- backoff target

  -- Coarse resumability: skip a clone we already completed on a retry.
  last_stage TEXT,          -- clone|parse|embed|store
  progress   JSONB,         -- files_parsed, chunks_embedded, current_file

  last_error TEXT,
  payload    JSONB,

  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- The claim query sorts by run_after over the claimable set only.
-- Without this it degrades to a full scan as completed rows accumulate.
CREATE INDEX idx_ingestion_jobs_claimable
  ON ingestion_jobs (run_after)
  WHERE state IN ('queued','running');

-- At most one live job per repository. This is the ISS-016 guard, in the
-- schema rather than in application logic.
CREATE UNIQUE INDEX idx_ingestion_jobs_one_live_per_repo
  ON ingestion_jobs (repository_id)
  WHERE state IN ('queued','running');
```

### L3 — Short lease, extended by heartbeat

Lease **5 minutes**, heartbeat **every 60 seconds**. An ingest takes minutes; a
lease as long as the worst-case job would strand a repository for that long when
a worker dies. Short-lease-plus-heartbeat bounds recovery at 5 minutes regardless
of job length.

Deliberately the same interval as 20-05's `abandonedProcessingAfter` for webhook
deliveries. Keep them consistent; if one changes, say why.

**Reclaim increments `attempts`.** A job that repeatedly kills its worker must
eventually dead-letter rather than loop forever. Easy to omit, painful to
diagnose.

### L4 — Supersede, don't race. (This is the ISS-016 fix.)

When a relink changes a repository's `installation_id`, **in this order**:

1. **Mark any `queued` or `running` job for that repository `superseded`.**
2. **Then** enqueue the new job.
3. A running worker checks its own `state` at each heartbeat and aborts
   cooperatively if it has been superseded.

**⚠ The first draft had steps 1 and 2 the other way round, and that was a
deterministic bug, not a race.** `CREATE UNIQUE INDEX` is not deferrable, so
inserting a `queued` job while the old one is still `queued`/`running` violates
the partial unique index immediately — raising 23505 in *exactly* the ISS-016
case this decision exists to fix. Worse in bulk: an
`installation_repositories.added` event re-queues N repositories in one
statement and would fail wholesale rather than per row.

Both statements belong in **one transaction**, so a crash between them cannot
leave a repository with its old job superseded and no new one to replace it.

The in-flight run holds a token for an App that was just uninstalled and will
fail regardless — the point is that it fails *promptly and knowingly* rather
than racing a new run to write the final state.

The partial unique index in L2 makes "two live jobs for one repository"
unrepresentable, so this is enforced by the schema and not only by the code path
that remembers to do it.

### L5 — `ingestion_jobs` carries no RLS, deliberately

A worker claims a job **before** it knows the tenant — `organization_id` is on
the row it is trying to claim. Scoping the claim by the answer is circular.

**⚠ The first draft cited the wrong half of migration `000012`.** That
migration contains two patterns and they are not interchangeable:

| Pattern | What it is | Fits us? |
|---------|-----------|----------|
| `github_webhook_deliveries` | no RLS, `organization_id` a nullable **annotation** never used to authorize | **No** — ours *is* used to authorize |
| `github_installation_tenants` | no RLS, **trigger-maintained mirror**, the value *is* an authorization input | **Yes** |

The first draft cited the first and copied its "annotation" language. But a
worker uses `ingestion_jobs.organization_id` to scope every write it then makes,
so a drifted value writes another tenant's data. That is the second pattern,
and `github_installation_tenants` was built trigger-maintained *precisely
because drift was representable*.

**So: no RLS on the queue (the claim is genuinely pre-tenant), and
`organization_id` maintained by a trigger on `repositories`** following
`sync_github_installation_tenant` — schema-qualified body,
`SET search_path = public, pg_temp`, a DELETE branch, and a stale-key delete
before the upsert. See `v2-substrate/DECISIONS.md` **D5**, which makes this one
rule for every denormalised tenancy column rather than a decision taken twice.

**⚠ The consequence, which must not be discovered later:** 21-03's
`GET /api/admin/jobs/:id` is a request handler reading a table with no RLS. It
**must** filter by `organization_id` explicitly — the database will not do it —
and the CI isolation gate **will not catch a mistake**, because that gate scans
mutation endpoints and this is a `GET`. This needs a deliberately written
isolation test, not one inherited from the ratchet.

### L6 — One job per repository ingestion

Clone, parse, embed and store are stages *inside* one job, recorded in
`last_stage`, not separate queue entries.

Fanning out per-file or per-chunk would put a single large repository at tens of
thousands of jobs, which is the only way our load reaches the range where
Postgres-as-a-queue hurts. It would also fight the embedding batching that exists
for cost control.

Cost: a late failure re-runs the job. `last_stage` gives coarse resumability
(skip a completed clone). Stage-level resumability is out of scope.

---

## Essential deliverables

- `ingestion_jobs` with the schema above, plus both indexes.
- A **Go producer** in the backend: enqueue, and supersede-on-relink.
- A **Python consumer** in the workers: claim, heartbeat, complete, fail, and
  cooperative abort on supersede.
- Exponential backoff with jitter; dead-letter at `max_attempts`.
- `repositories.sync_state` rewritten as a projection of job state, with the
  relink path no longer setting it to enqueue work.
- `GET /api/admin/jobs/:id`, tenant-scoped **explicitly**, with its own isolation
  test.
- An integration test covering enqueue → claim → heartbeat → complete, plus
  lease expiry → reclaim → retry → dead-letter.
- **A concurrency test that actually races.** Per `feedback_measure_before_claiming`
  and the 20-04 lesson: release N workers through a barrier against one claimable
  job and assert exactly one wins, repeated over several rounds on a warm pool. A
  single cold-pool round proves nothing.

---

## Boundaries

**In scope:** the queue, the state machine, leases, retries, dead-letter, the
admin endpoint, and retiring `sync_state` as a queue.

**Not in scope:**
- Cloning, parsing, embedding or storing anything — Phase 22.
- The SSE progress endpoint — 22-04. This phase provides the `progress` column it
  will read.
- pgvector, partitioning, `symbols`, `symbol_edges` — those are D1–D3/D5 and land in
  Phase 22's migrations. **This phase must not assume they exist yet.**
- Retrying an exhausted (`dead`) repository through the public API — now
  **ISS-023**, split out of ISS-016 by decision O1. The state machine makes it
  possible; exposing it belongs to whichever phase works the API surface. All
  three documents now agree on this, which the first draft did not.

---

## Corrections to the roadmap sketch

The ROADMAP entry for Phase 21 was written before D2 and before the
cross-language constraint was examined. Three corrections:

1. **"Redis Streams is the tentative choice"** — rejected. See L1. The roadmap's
   reasoning ("already have Redis, at-least-once, consumer groups") is all true
   and is outweighed by transactional completion once D2 moves the vectors into
   Postgres.
2. **21-02's "`ingestion_jobs` table (or extend `ingestion_runs` — decided during
   planning)"** — decided: a new table. `ingestion_runs` is the record of what
   happened and `chunks` already FK to it; overloading it with queue state would
   repeat the `sync_state` mistake one table over.
3. **"Research: Likely (infra decision)"** — done, in `21-RESEARCH.md`.

---

## Open questions for execution

- **Worker identity for `lease_owner`.** Hostname plus PID is the obvious choice
  and is wrong under container restarts that reuse both. A UUID generated at
  worker start is safer. Settle in 21-01.
- **Does the Go producer enqueue in the same transaction as the repository
  write?** It should — that is half the argument for L1 — but `POST
  /api/repositories` currently runs two transactions (20-03), and the enqueue
  belongs in the second. Confirm when wiring, do not assume.
- **Backoff ceiling.** Five attempts with uncapped exponential backoff puts the
  last retry a long way out. Pick a cap in 21-02 and write it down.
- **Pruning.** `ingestion_jobs` grows forever, like `github_webhook_deliveries`.
  Same treatment: a documented `DELETE` for terminal rows older than N days, with
  Phase 24 owning the scheduling. Note it in the migration rather than leaving it
  to be discovered.

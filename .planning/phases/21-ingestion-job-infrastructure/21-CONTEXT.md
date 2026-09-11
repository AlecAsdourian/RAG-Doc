# Phase 21: Ingestion Job Infrastructure — Context

**Written:** 2026-09-10. **Revised the same day after review** — five
correctness bugs in the schema, and two decisions (O1, O2) since confirmed.
See `.planning/v2-substrate/REWORK.md` §6.
**Research:** `21-RESEARCH.md` in this directory

**Verification record.** The schema, trigger, claim query, sweeper, fenced write
and enqueue upsert have been transcribed out of this document and executed
against PostgreSQL 17. Round 3 added the cases the earlier checks missed:

| | |
|---|---|
| W1 | the composite FK makes a mismatched tenant **unrepresentable**, not merely rejected |
| W2 | the enqueue upsert parses — the three shorter forms do not |
| W3 | a **bulk** enqueue racing a live job handles every row: 1 flagged, 2 inserted, all 3 repositories live |
| W4 | the conditional clear reports a rerun; naive `RETURNING` yields `false` and would drop it |
| W5 | complete-then-re-enqueue in that order raises no 23505 |
| W6 | a retry reuses its `ingestion_runs` row instead of erroring on attempt 2 |

W3 is the case that silently lost two of three repositories while reporting
success, and W6 is an error that had been raised in two prior reviews without
being addressed.
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
3. **Enqueue rate is not a factor**, though it is not the axis that binds. One
   job per connect, one per push. Because a job holds a worker for minutes, the
   real constraint is *concurrency*, not enqueue rate — see the research doc's
   throughput section, which was rewritten after review.

**⚠ The pgmq rejection was re-argued after review, and this section previously
carried two reasons that are both withdrawn.** It said pgmq is "a third
deploy-target constraint" — false: pgmq ships a documented pure-SQL install path
requiring no extension support. And it said pgmq's message model "would force a
jobs table beside it that we would have to keep in sync — a dual-write problem
inside one database", which drains the term this document runs on. Two tables in
one transaction is a schema, not a dual write.

**The surviving argument is sharper than either, and it is specific to ISS-016.**

The guard that makes this phase work is a partial unique index —
`(repository_id) WHERE state IN ('queued','running')` — which makes "two live
jobs for one repository" unrepresentable rather than merely unlikely. **That
guard cannot be expressed over pgmq at all:**

**a pgmq row has no state column, so a partial index has nothing to be partial
over.** Its columns are `msg_id, read_ct, enqueued_at, last_read_at, vt, message,
headers` — "queued" and "running" are not values there, they are inferences from
`vt` against `now()`. A predicate cannot be written over a distinction the
schema does not make.

**⚠ An earlier revision led with a different claim — that the index would sit on
"extension-owned `pgmq.q_*` tables that do not survive `drop_queue` or an
extension upgrade" — and it is false.** pgmq 1.6.0 deliberately *detached* queue
tables from extension membership (the 1.5.2→1.6.0 migration runs
`ALTER EXTENSION pgmq DROP TABLE`), precisely so queue data survives dump and
restore. Only "does not survive `drop_queue`" is true, and trivially so.

That is the **third** pgmq rejection reason to fail verification in three
rounds — after "it is an extension, so it constrains our deploy target" and "a
jobs table beside it is a dual write". The decision has survived each time, but
the pattern is reaching for reasons rather than testing them, and it is recorded
here so the next reader weighs the surviving argument on its own merit.

Three smaller things compound it: pgmq has no supersede primitive (L4 needs
one), message bodies are immutable where 22-04's SSE endpoint needs a mutable
`progress` column, and a visibility timeout is a timer rather than a lease — so
there is no owner identity to fence terminal writes against, which L3 now
requires.

pgmq would supply the one piece we can write in a few dozen lines of SQL, and
cost us the four that matter. **Keep the table.**

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

  -- ⚠ A TWO-HOP DENORMALISATION, GUARDED BY A COMPOSITE FOREIGN KEY. See L5.
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
  --
  -- ⚠ A RETRY REUSES THIS ROW; it does not create a second one.
  -- `ingestion_runs` carries `UNIQUE (repository_id, commit_sha)`, so attempt 2
  -- inserting a fresh run for the same commit raises 23505 — a determinate
  -- error on this phase's core path, raised in two reviews before it was
  -- addressed. The worker therefore resolves the run rather than inserting it:
  --
  --   INSERT INTO ingestion_runs (repository_id, commit_sha, ...)
  --   VALUES ($1, $2, ...)
  --   ON CONFLICT (repository_id, commit_sha) DO UPDATE
  --     SET started_at = NOW()
  --   RETURNING id;
  --
  -- which is correct for a retry (same commit, same run) and for a superseded
  -- run's replacement (same commit, run reopened). A job for a DIFFERENT commit
  -- gets its own row, which is the normal case.
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

  -- Set when a push arrives while this job is already live. The worker
  -- re-queues once on completion and clears it. See L7.
  needs_rerun BOOLEAN NOT NULL DEFAULT FALSE,

  last_error TEXT,

  -- ⚠ DOES NOT CARRY CREDENTIALS OR AN INSTALLATION ID.
  --
  -- The worker resolves the repository's CURRENT installation when it claims
  -- the job, not when the job was enqueued. Two reconnects racing produce one
  -- job (L8), and if that job had snapshotted the loser's installation the
  -- winner's newer credentials would be silently lost. Reading at claim time
  -- makes the dedup safe: whichever request won, the job picks up current
  -- state.
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

-- ⚠ COMPOSITE FOREIGN KEY: a mismatched tenant is unrepresentable, not merely
-- rejected. This supersedes the BEFORE INSERT trigger's mismatch check (L5),
-- which remains only for the clearer error message.
--
-- IT REQUIRES `repositories.organization_id`, which does not exist today --
-- tenancy there runs `repositories.project_id -> projects.organization_id`. So
-- this is a THIRD denormalisation under D5's rule, on an existing table:
--
--   ALTER TABLE repositories ADD COLUMN organization_id UUID;
--   -- backfilled from projects, then trigger-maintained per D5
--   ALTER TABLE repositories ADD CONSTRAINT repositories_id_org_key
--     UNIQUE (id, organization_id);
--
-- Two things fall out of it that are worth having anyway: `repositories`' RLS
-- policy can drop from a two-hop EXISTS join to scalar equality, and the column
-- never changes, because D5 forbids cross-organisation re-parenting.
ALTER TABLE ingestion_jobs
  ADD CONSTRAINT ingestion_jobs_repo_tenant_fk
  FOREIGN KEY (repository_id, organization_id)
  REFERENCES repositories (id, organization_id) ON DELETE CASCADE;
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

**⚠ Every terminal write is fenced on the lease.** A worker may only write
`completed`, `queued`-after-failure or `dead` for a job it still owns:

```sql
... WHERE id = $1 AND lease_owner = $2 AND state = 'running'
```

Without the fence, two bugs review found both fire. A worker whose lease expired
and was reclaimed elsewhere still writes its result, clobbering the new
attempt's. And a **superseded** worker (L4) writes `state='queued'` on its way
out — which re-enters the partial unique index and collides with the replacement
job that superseded it, raising 23505 on exactly the ISS-016 path. L4 itself
says the superseded run "will fail regardless", so that collision is guaranteed,
not occasional.

With the fence, a superseded or reclaimed worker's write matches zero rows. It
logs and exits. One `WHERE` clause closes both.

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
`sync_github_installation_tenant`'s conventions: schema-qualified body,
`SET search_path = public, pg_temp`.

**⚠ It is a `BEFORE INSERT` trigger on `ingestion_jobs`, not an `AFTER` mirror
on `repositories`.** An earlier revision said "maintained by a trigger on
`repositories`", which review correctly rejected: an `AFTER` trigger on the
*source* table cannot validate an `organization_id` a producer supplied on a
*different* table at insert time. The check has to sit where the value arrives.

```sql
CREATE OR REPLACE FUNCTION ingestion_jobs_fix_tenant() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE real_org UUID;
BEGIN
  SELECT p.organization_id INTO real_org
  FROM public.repositories r
  JOIN public.projects p ON p.id = r.project_id
  WHERE r.id = NEW.repository_id;

  IF real_org IS NULL THEN
    RAISE EXCEPTION 'repository % does not exist', NEW.repository_id;
  END IF;
  IF NEW.organization_id IS DISTINCT FROM real_org THEN
    RAISE EXCEPTION 'organization_id % does not match repository % (owner %)',
      NEW.organization_id, NEW.repository_id, real_org;
  END IF;
  RETURN NEW;
END; $$;

CREATE TRIGGER trg_ingestion_jobs_tenant
  BEFORE INSERT OR UPDATE OF organization_id, repository_id ON ingestion_jobs
  FOR EACH ROW EXECUTE FUNCTION ingestion_jobs_fix_tenant();
```

Rejecting rather than silently correcting, because a producer that supplies the
wrong tenant has a bug worth surfacing.

**The composite foreign key in L2 is what makes a mismatch unrepresentable.**
This trigger now exists for the error message, not the guarantee — a bare FK
violation names a constraint, not the problem. Keep both; say which does what.

**⚠ OPEN, deliberately not closed: drift on re-parent.** This trigger validates
at INSERT and on UPDATE of its own columns. It is blind to the row's tenancy
changing *underneath* it — a repository moved to a project in another
organisation would leave every existing job carrying the old `organization_id`.

Today that cannot happen: D5 forbids cross-organisation re-parenting, and
migration `000008`'s `FOR ALL` policy on `repositories` already refuses it. So
this is latent, not live.

It is recorded rather than resolved because the answer depends on whether
same-organisation project moves ever become a feature — if projects stay
org-permanent, nothing more is needed; if they don't, the FK needs
`ON UPDATE CASCADE` and the trigger needs a re-parent branch. **Phase 22 should
revisit this rather than inherit it silently.**

**This rule is stated here in full rather than by reference.** It is the same
rule as `DECISIONS.md` D5, but D5 lives on an unmerged PR — and the point of
retargeting this PR to `main` was that it should stand alone. Note the
duplication so the two stay in step.

**⚠ The consequence, which must not be discovered later:** 21-03's
`GET /api/admin/jobs/:id` is a request handler reading a table with no RLS. It
**must** filter by `organization_id` explicitly — the database will not do it —
and the CI isolation gate **will not catch a mistake**, because that gate scans
mutation endpoints and this is a `GET`. This needs a deliberately written
isolation test, not one inherited from the ratchet.

### L7 — A push against a live job sets `needs_rerun`

A repository ingest takes minutes, and L2's partial unique index allows only one
live job per repository. A push arriving in that window therefore has nowhere to
go — and since people push repeatedly, this is close to all steady-state volume,
not an edge case.

**Decision: ONE enqueue statement, used by every producer** — push, relink and
bulk `installation_repositories.added` alike.

```sql
INSERT INTO ingestion_jobs (organization_id, repository_id, job_type, state)
VALUES ($1, $2, $3, 'queued')
ON CONFLICT (repository_id) WHERE state IN ('queued','running')
DO UPDATE SET needs_rerun = TRUE, updated_at = NOW()
RETURNING id, (xmax <> 0) AS was_existing;
```

**⚠ The inference clause is not optional and three shorter forms all fail.**
`ON CONFLICT DO UPDATE` without a target raises `42601`; `ON CONFLICT
(repository_id)` raises `42P10`, because arbiter inference will not select a
**partial** index unless the predicate is repeated. Only the form above works.
An earlier revision of this section stated the first of those, which cannot be
executed — measured in review.

**Why one statement for all three producers.** An earlier revision had the relink
path catch `23505` and return success (L8). That is fine for two reconnects of
*one* repository, and wrong for a bulk add: three repositories racing a relink
left **two of the three never queued while the handler reported success**. The
per-row upsert has no such case — each row either enqueues or flags, and
`was_existing` tells the caller which.

**Reading the flag.** `RETURNING needs_rerun` after clearing it returns the
**new** value, so the worker sees `false` and drops the rerun. `RETURNING OLD.*`
is PostgreSQL 18 and errors on 17. Clear it conditionally instead and let the
row count carry the answer:

```sql
UPDATE ingestion_jobs SET needs_rerun = FALSE, updated_at = NOW()
WHERE id = $1 AND lease_owner = $2 AND needs_rerun
RETURNING id;   -- a row here means "there was a rerun to do"
```

Then enqueue the follow-up **after** the current job leaves the live set — the
completion write and the re-enqueue in that order, in one transaction. The
reverse order raises `23505` against the partial unique index, which is the
identical defect L4 was written to fix.

**A push for a repository whose job is `dead` is accepted loss.** `dead` is
outside the live set, so the upsert above inserts a fresh job rather than
flagging — which is the desired behaviour. But a push arriving *between* the
final failure and the sweep may flag a row that is about to become `dead`, and
that flag is then never acted on. Stated rather than engineered around: the next
push re-queues, and ISS-023 owns an explicit retry.

Rejected: a second queued job (needs a second live state, weakening the guard)
and accepting the loss unconditionally (today's behaviour, and the reason
ISS-016 exists).

### L8 — Concurrent enqueues resolve to one job, and that is correct

Two reconnects landing together both try to enqueue; one hits 23505 on the
partial unique index.

**Decision: the L7 upsert handles it; no error is raised to catch.** Each row
either inserts or flags `needs_rerun`, and `was_existing` distinguishes the two.

**⚠ The earlier "catch 23505 and return success" is withdrawn, and the reason is
worth keeping.** It rested on "both callers asked for the same thing, so one job
satisfies both" — true for two reconnects of *one* repository, false the moment
a statement touches several. A bulk `installation_repositories.added` for three
repositories racing a relink left **two of the three unqueued while the handler
returned success**: silent loss, reported as a win. Measured in review.

The general lesson: a dedup argument that holds per row does not survive being
applied to a set.

**What makes that safe is the claim-time credential read** (see `payload` in
L2). If the job snapshotted an installation at enqueue, the loser's newer
credentials would be lost and the dedup would be silently wrong. Because the
worker resolves the repository's current installation when it claims, whichever
request won the race, the job runs with the latest state.

An advisory lock was considered and rejected as machinery for a case where both
callers wanted the same outcome.

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
- **The sweeper** (L3 / research): moves exhausted jobs to `dead` from both
  `queued` and `running`. Omitted from this list in an earlier revision despite
  being the only thing that reaches a cleanly-failed job.
- **Lease fencing on every terminal write** (L3) — `AND lease_owner = $2 AND
  state = 'running'`.
- **The `BEFORE INSERT` tenant trigger and the composite FK** (L2 / L5),
  including `repositories.organization_id` and its backfill.
- **The single per-row enqueue upsert** (L7), shared by push, relink and bulk.
- An integration test covering enqueue → claim → heartbeat → complete, plus
  lease expiry → reclaim → retry → dead-letter.
- A test that a **bulk** enqueue racing a relink queues *every* row — the case
  that silently lost two of three before L8 was rewritten.
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

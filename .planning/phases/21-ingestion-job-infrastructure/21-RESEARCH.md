# Phase 21: Ingestion Job Infrastructure — Research

**Researched:** 2026-09-10
**Domain:** Durable job queue for a Go producer and a Python consumer
**Confidence:** HIGH on the recommendation, which turns on our constraints
rather than on a close call between technologies. **LOW on the throughput
figures as originally written** — review found several were misattributed or
inverted. Corrected below, and the Sources list now distinguishes pages that
were fetched from pages that were merely surfaced by a search.

<research_summary>
## Summary

The roadmap's tentative pick was Redis Streams, on the reasoning that we already
run Redis and it gives at-least-once delivery with consumer groups. **Two things
changed since that was written**, and together they invert the answer.

**First, the cross-language constraint is sharper than it looks.** The producer
is Go and the consumer is Python. That eliminates every language-native library
in this space — River (Go), Celery/RQ/Arq (Python), Oban (Elixir),
graphile-worker (Node) — not because they are bad, but because we would be
running two different implementations of the same protocol and hoping they
agree. What survives is: something with clients in both languages (Redis,
RabbitMQ, Temporal), or something whose interface is SQL, which both languages
already speak.

**Second, decision D2 moved the vectors into Postgres.** The argument that won
D2 — one transaction instead of two non-transactional writes about to be wired
into a *retrying* queue — applies one layer up. If the queue is in Postgres, a
worker can write its chunks and mark its job complete in the same transaction.
If the queue is in Redis, that is a dual-write across two systems, which is
exactly the hazard D2 exists to delete. Re-introducing it in the phase that
builds the retry loop would be inconsistent.

**Our enqueue rate is not a factor**, though review corrected which axis
matters. One job per repository connect and one per push, with sub-steps kept
*inside* a job rather than fanned out — single-digit jobs per second at peak.
But because a job holds a worker for minutes, the binding constraint is
**concurrency**, not enqueue rate, and that figure is nearer the guidance than
the first draft claimed. See the throughput section.

**Primary recommendation:** a single `ingestion_jobs` table in Postgres, claimed
with `FOR UPDATE SKIP LOCKED`, carrying its own lease, attempt counter and state
machine. Not Redis Streams, not pgmq, not Temporal. Reasoning for each rejection
below.
</research_summary>

---

## Options, against our actual constraints

| Option | Cross-language | Transactional with chunk writes | Lease built in | Operational cost | Verdict |
|--------|----------------|--------------------------------|----------------|------------------|---------|
| **Postgres table + `SKIP LOCKED`** | ✅ both speak SQL | ✅ | ✋ we write it | none — already have Postgres | **Chosen** |
| pgmq | ✅ SQL API | ✅ | ✅ visibility timeout | none — **pure-SQL install path exists** | Close second — see below |
| Redis Streams | ✅ clients both | ❌ **different system** | ✅ PEL + `XAUTOCLAIM` | already running | Rejected |
| Temporal | ✅ Go + Python SDKs | ❌ | ✅ | a stateful cluster | Rejected |
| RabbitMQ / SQS | ✅ | ❌ | ✅ | a broker, or a cloud dependency | Rejected |

### Why not Redis Streams, despite already running Redis

Redis Streams is a good fit on paper: consumer groups, a per-consumer Pending
Entries List, and `XAUTOCLAIM` (Redis 6.2+) to atomically reassign entries idle
past a threshold — a working lease model.

Two objections, and the first is decisive.

1. **It is not in the same transaction as the work.** The worker writes chunks to
   Postgres and then `XACK`s to Redis. A crash between those two leaves the
   chunks written and the job un-acked, so it is reprocessed. This is the
   identical shape to the Postgres/Qdrant dual write that D2 removes. Building
   the retry machinery *on top of* a dual write is the wrong order.

2. **It changes what Redis is for.** Today Redis holds the semantic cache and
   the OAuth state store — both reconstructible. Making it the durable system of
   record for ingestion means its persistence configuration becomes a
   correctness dependency, and ISS-018 already records that a Redis outage at
   startup disables installs until restart. Widening Redis's blast radius while
   we still have that open is a poor trade.

### Why not pgmq, which is the genuinely close call

[pgmq](https://github.com/pgmq/pgmq) is a Postgres extension giving SQS-style
queues through SQL functions. It is a good piece of software and it solves our
cross-language problem the same way we propose to: the interface is SQL, so Go
and Python both just run queries. Its visibility timeout *is* a lease, which is
exactly what ISS-016 asks for, and it benchmarks at **over 11,000 messages per
second on a 2-CPU container** — though that figure has **no first-party
source**: the page it is attributed to is gone (`legacy.tembo.io` does not
resolve) and it survives only in uncited third-party writeups. Treat it as
folklore; the decision does not rest on it.

**⚠ Both original reasons were defective. Review was right, and the decision
survives on one argument rather than two.**

**Reason 1 was factually wrong and is withdrawn.** It claimed pgmq is an
extension and therefore a third deploy-target constraint after pgvector and
nested virtualization. Verified against the pgmq README: there is a documented
**pure-SQL install path** — *"use psql to install PGMQ's objects directly into
the pgmq schema in Postgres. Use this method if you are running someplace that
does not natively support the PGMQ Extension."* No `shared_preload_libraries`,
no `pg_partman` dependency, no background worker. It would have constrained
nothing.

(The nested-virtualization constraint it invoked has since been withdrawn too,
for unrelated reasons — see `v2-substrate/DECISIONS.md` K3.)

**Reason 2 was overstated and is narrowed.** Calling two tables written in one
Postgres transaction a "dual write" drains the term this entire document runs
on. A dual write is two systems with no shared transaction; two tables in one
transaction is just a schema. That was rhetorical inflation and it does not
survive.

**What actually stands.** We need the queue row to be a **first-class queryable
entity**, not an opaque JSONB message: a state machine, an attempt counter,
foreign keys to `repositories` and `ingestion_runs`, progress fields that
22-04's SSE endpoint reads, and a row 21-03's `GET /api/admin/jobs/:id` can
select directly. With pgmq we would keep a jobs table alongside it and maintain
the correspondence between them — more moving parts than the forty-line claim
query it replaces, for a queue whose hard guarantees Postgres provides either
way.

**Reviewed and upheld.** The invitation to flip was taken up in the third
review, which verified the surviving claims against pgmq's source: no supersede
primitive (removal is by `msg_id`; the conditional read filter is experimental
and cannot see in-flight messages), immutable message bodies in the supported
API, and no owner identity to fence on. The decisive one is sharper than what
was written here: **a pgmq row has no state column** (`msg_id, read_ct,
enqueued_at, last_read_at, vt, message, headers`), so ISS-016's partial unique
index has nothing to be partial over. See `21-CONTEXT.md` L1.

One claim did **not** survive and is withdrawn: that the queue tables are
extension-owned. pgmq 1.6.0 detached them deliberately.

### Why not Temporal

Temporal solves multi-step durable workflows, which is genuinely what Phase 22's
clone → parse → embed → store is. The argument against it here is ours, not a
citation: it is a stateful distributed system to operate, or a paid cloud
dependency and a new vendor, and four sequential steps inside one job does not
earn either.

*(An earlier revision presented a direct quotation — "if your job is just a few
background steps … the platform feels bigger than the problem" — as "the
consensus in the literature". It came from a search summary, was traced to a
vendor post comparing Temporal alternatives, and is withdrawn rather than
re-attributed.)*

Four sequential steps inside one job, at single-digit jobs per second, does not
justify that. Revisit if the pipeline grows genuine fan-out with independent
failure and compensation semantics.

---

## Throughput: why this is not a close call

**⚠ This section was substantially wrong and has been rewritten.** Review
checked every figure against the cited pages. What follows is what survived.

**The DBOS citation was inverted — used to argue the opposite of its thesis.**
The first draft cited *Making Postgres queues scale* as evidence of a ~1,000
jobs/sec ceiling. Fetched and read: that number describes **a bug they fixed**
(serialization failures from an over-strict isolation level), and the article's
actual conclusion is that Postgres queues reach **30,000 workflow executions per
second** after three optimisations — `SKIP LOCKED`, isolation tuning, and
selective indexing.

That is the strongest possible evidence *for* this decision, and the first draft
turned it into evidence against.

**Two figures could not be found in any cited page and are withdrawn:** the
"within ~8% of a dedicated broker, p99 85ms vs 34ms" comparison, and the
memorable *"if your answer to 'how do I scale this' is 'shard the queue table,'
you have already outgrown it"* tripwire. Both came out of a search summary, not
a source. They may well be real and quoted somewhere; they are not cited here
until someone opens the page they are in.

**What is left, attributed:**

| Source | Claim |
|--------|-------|
| DBOS, *Making Postgres queues scale* (**fetched**) | 30k workflows/sec achievable; ~1,000/sec was a fixed bug, not a limit |
| Microsoft, *Potential consequences of using Postgres as a job queue* | **withdrawn.** Quoted here in the wrong direction from a page nobody opened; a later reviewer who fetched it reports the sense is the opposite. Nothing in this document rests on it. |

The honest summary is therefore the opposite of the first draft's: **Postgres as
a queue scales further than claimed on the axis we were measuring, and our real
constraint is worker concurrency rather than enqueue rate.**

**Our load:** one job per repository connect, one per push webhook. A thousand
active customer repositories pushing ten times a day each is ~10,000 jobs/day —
about **0.1 jobs per second**. Peak bursts (an organization connecting fifty
repositories at once) are still trivial.

**⚠ That arithmetic is right and answers the wrong question.** Review caught it.
L6 puts a whole repository ingest — minutes of clone, parse, embed — behind a
*single* queue entry. So the binding constraint is not enqueue rate but
**concurrency**: how many jobs are simultaneously `running`, each holding a
worker and a lease.

**⚠ The arithmetic that was here applied a full-ingest duration to push jobs,
and leaned on a source this document files as unread.** Both are corrected.

A *full* ingest takes minutes; an *incremental* push re-index touches only
changed files and is far shorter. Multiplying 10,000 daily pushes by a ten-minute
full-ingest duration mixes the two and produces a concurrency figure with no
referent. The honest position is that **we do not yet know the incremental
duration**, because nothing ingests end to end — so worker-pool sizing is an
open input for 21-01, to be measured in Phase 22 rather than asserted here.

The earlier revision also quoted Microsoft's guidance as putting contention
trouble *"under ~100 concurrent workers"*. That page is listed in this
document's own Sources under **not opened**, and the rule stated there is that
nothing in the body may rest on such an entry. A later reviewer who did fetch it
reports the sense is the opposite — that under 100 workers Postgres is *fine*.
Either way the claim comes out of the body until someone fetches it.

**What survives, and it is enough:** concurrency rather than enqueue rate is the
axis that binds, because a job holds a worker for the length of an ingest. The
first draft's "three orders of magnitude of headroom" was measuring enqueue rate
and is withdrawn.

**This holds only because of the granularity decision below.** Fanning out
per-file or per-chunk jobs would put a single large repository at tens of
thousands of jobs and change the analysis entirely.

---

## Job granularity — a decision that keeps us off the cliff

**One job per repository ingestion.** Clone, parse, embed and store are *stages
inside* one job, tracked in a progress column, not separate queue entries.

Reasons:

- Embedding is already batched (`EmbeddingGenerator.generate_embeddings_batch`),
  so per-chunk jobs would fight the batching that exists for cost control.
- 22-04's SSE progress endpoint reports per repository, so a per-repository job
  is the natural thing to report on.
- It keeps job volume far below any level at which Postgres-as-a-queue
  struggles. (An earlier revision said "three orders of magnitude", a figure
  this document withdraws in the throughput section above. The direction holds;
  the magnitude was never sourced.)

The cost is that a failure late in a long job re-runs the whole job. Mitigated by
recording the last completed stage on the job row, so a retry can skip a
completed clone. Full stage-level resumability is deliberately out of scope.

---

## Patterns to use

### The claim query

```sql
UPDATE ingestion_jobs SET
  state             = 'running',
  lease_owner       = $1,
  lease_expires_at  = NOW() + $2::interval,
  attempts          = attempts + 1,
  updated_at        = NOW()
WHERE id = (
  SELECT id FROM ingestion_jobs
  WHERE attempts < max_attempts        -- ⚠ applies to BOTH branches below
    AND (
         (state = 'queued'  AND run_after <= NOW())
      OR (state = 'running'                              -- reclaim abandoned
          AND (lease_expires_at IS NULL                  -- ⚠ see below
               OR lease_expires_at < NOW()))
    )
  ORDER BY run_after
  FOR UPDATE SKIP LOCKED
  LIMIT 1
)
RETURNING *;
```

**Two corrections from review, both of which would have shipped.**

**1. `attempts < max_attempts` was missing, so poison jobs loop forever.** The
original had no attempt guard on the reclaim branch. A job that reliably kills
its worker is reclaimed, kills the next worker, is reclaimed again — and never
reaches `dead`, because the transition to `dead` was to be written by the
worker, which is the thing that does not survive. Dead-lettering that depends on
the worker surviving is not dead-lettering. This was **pitfall #3 in this very
document**, and the query below it did not implement it.

The guard alone is not enough either: it stops the job being re-claimed but
leaves it sitting in `running` forever, still occupying the unique index. So a
**sweeper** is also required, run on the same schedule as the heartbeat:

```sql
UPDATE ingestion_jobs
SET state = 'dead', updated_at = NOW()
WHERE attempts >= max_attempts
  AND (
        state = 'queued'                       -- ⚠ clean-failure path
     OR (state = 'running'                     -- crash path
         AND (lease_expires_at IS NULL OR lease_expires_at < NOW()))
  );
```

**⚠ The `state = 'queued'` branch was missing in the first revision, and its
absence recreated the very bug two other fixes had just closed.** A worker that
fails *cleanly* on its last attempt writes `state='queued'` — that is what O2's
collapse says to do. The sweeper filtered `state='running'`, so it could not see
that row; the claim query skipped it on `attempts < max_attempts`; and it sat in
`queued` holding the partial unique index forever, blocking every future job for
that repository. The **common** failure path, reconstructed out of the fixes for
the crash path and the state collapse.

Belt and braces, the worker also writes `dead` **directly** when it fails on its
final attempt, fenced on its lease, rather than writing `queued` and waiting for
a sweep:

```sql
UPDATE ingestion_jobs
SET state = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'queued' END,
    run_after = NOW() + $3::interval,
    last_error = $4,
    lease_owner = NULL, lease_expires_at = NULL,
    updated_at = NOW()
WHERE id = $1 AND lease_owner = $2 AND state = 'running';
```

The sweeper is then the backstop for workers that die before running it, which
is the only case it should ever fire on.

**2. `lease_expires_at IS NULL` stranded a job permanently.** `NULL < NOW()`
evaluates to NULL, not true — so a `running` row with a null lease matched
neither branch. It was invisible to every claim, while still occupying the
partial unique index and therefore blocking every future job for that
repository, silently and forever. A null lease is reachable from any partial
write or manual intervention.

Parenthesise the branches explicitly. `AND` binds tighter than `OR`, so the
intended grouping happens to be the default — and relying on that is how the
next person introduces a bug. With the `attempts` guard added the parentheses
are now load-bearing rather than merely defensive.

### Heartbeat, not a long lease

An ingest takes minutes. Setting the lease to the worst-case job duration means a
crashed worker's repository is stuck for that long. Instead: a **short lease
(5 minutes) extended by a heartbeat every minute**. A dead worker's job is
reclaimable in ≤5 minutes regardless of how long the job would have taken.

This is the same shape as 20-05's `abandonedProcessingAfter = "5 minutes"` for
webhook deliveries — worth keeping the two consistent, and worth reusing the
reasoning rather than re-deriving it.

### ⚠ What the transaction actually buys

The precise claim, because it is easy to overstate:

- The worker claims in a short transaction, does the slow work **outside** any
  transaction (cloning and embedding involve network calls; holding a Postgres
  transaction open for minutes causes bloat and holds connections), then writes
  chunks and marks the job complete **in one transaction**.
- That final transaction is the win: chunks and completion commit together or
  not at all. There is no state where a job is marked done but its chunks are
  missing.
- **It does not make the work exactly-once.** A crash mid-embedding still
  re-runs the job from the start. Chunk writes must therefore be idempotent
  anyway — delete-by-run-id then insert, or upsert. The transaction removes
  *torn* writes, not *duplicated work*.

### Retry and dead-letter

- A failed attempt sets `state = 'queued'` with
  `run_after = NOW() + backoff(attempts)`, jittered, exponential, capped, and
  records `last_error`.
- `attempts >= max_attempts` → `state = 'dead'`, written by the sweeper above.
  Terminal.

**There is no `failed` state** — decision O2, taken after review. It had no edge
back to the claimable set and no place in the partial unique index, so a failed
job could neither be retried nor prevent a second live job for the same
repository. Collapsing it removes a state and its transitions instead of adding
an edge, and loses nothing: "currently failing" is
`state = 'queued' AND attempts > 0`.

**And this does not close the second half of ISS-016.** The original text
claimed it did, which contradicted this phase's own Boundaries section. That
half — retrying a `failed` repository through the public API — is now **ISS-023**
and belongs to whichever phase works the API surface. See decision O1.

---

## ⚠ Isolation: the queue table cannot have RLS, and that has a consequence

A worker claims jobs **before** it knows which tenant a job belongs to — the
`organization_id` is on the row it is trying to claim. That is the same
circularity 20-05 hit with webhook deliveries, and it has the same answer:
`ingestion_jobs` carries no RLS, and `organization_id` on it is data the worker
uses to open a tenant-scoped transaction for the actual writes.

Migration `000012`'s comment block is the precedent and the reasoning transfers
directly; the new table should carry an equivalent comment rather than leaving
the next reader to wonder whether RLS was forgotten.

**The consequence to watch:** 21-03's `GET /api/admin/jobs/:id` is a request
handler reading a table with no RLS. It **must** filter by `organization_id`
explicitly — nothing in the database will do it. And the CI isolation gate will
not catch a mistake here, because that gate scans *mutation* endpoints
(`POST/PUT/PATCH/DELETE`) and this is a `GET`. This needs an explicit isolation
test, written deliberately, not inherited from the ratchet.

---

## Don't hand-roll

- **Backoff with jitter** — use the existing pattern from `tenacity` (already a
  dependency per Phase 12's research) rather than writing a sleep loop.
- **The distributed parts** — durability, MVCC, `SKIP LOCKED` semantics — are
  Postgres's, not ours. What we write is a claim query and a state machine, both
  of which we need anyway for the admin endpoint. That is the line: hand-rolling
  a *vector index* or a *semantic cache* would be foolish; hand-rolling a
  forty-line claim query over a database that provides the hard guarantees is
  not the same act.

---

## Common pitfalls

1. **Using a status column as a queue.** This is ISS-016, already live. A status
   column has no lease and no owner, so two writers can believe they own the same
   row. Fixed by separating the work item from the status.
2. **A lease as long as the job.** Makes crash recovery as slow as the worst-case
   job. Use a short lease plus heartbeat.
3. **Forgetting that reclaim is a retry.** A reclaimed job increments `attempts`,
   so a job that repeatedly kills its worker eventually dead-letters instead of
   looping forever. Easy to omit and painful to diagnose.
4. **`ORDER BY` without an index.** The claim query sorts by `run_after`; without
   a partial index on the claimable set it degrades into a full scan as dead rows
   accumulate.
5. **Assuming `SKIP LOCKED` gives ordering.** It does not. Concurrent workers
   will process out of order. Fine for us — jobs are per-repository and
   independent — but it must not be assumed anywhere.

---

## Sources

**The first version of this list cited pages nobody had opened.** WebSearch
returns a synthesized summary across results; the URLs it surfaced were listed
as though each backed the claim beside it. The consequence was the inverted DBOS
citation above and two quotes that exist in no cited page.

Sources are now split, and the distinction is load bearing: **Verified** means
the page was fetched and the specific claim confirmed. Everything else is a
lead, and nothing in the body may rest on it.

### Verified — fetched, claim confirmed

- **[Making Postgres queues scale (DBOS)](https://www.dbos.dev/blog/making-postgres-queues-scale)**
  — fetched 2026-09-10. Confirmed: the ~1,000/sec figure is a **fixed bug**, not
  a ceiling; the article's thesis is 30k workflows/sec via `SKIP LOCKED`,
  isolation tuning and selective indexing. *The first draft cited this
  backwards.*
- **[pgmq](https://github.com/pgmq/pgmq)** — fetched 2026-09-10. Confirmed: a
  documented **pure-SQL install path** (*"use psql to install PGMQ's objects
  directly into the pgmq schema … if you are running someplace that does not
  natively support the PGMQ Extension"*), no `shared_preload_libraries`, no
  `pg_partman` dependency, no background worker. *This withdraws the first
  draft's primary reason for rejecting pgmq.*

### Surfaced by search, not opened — leads only

- [You don't need a job queue — Postgres already has SKIP LOCKED (Prisma)](https://www.prisma.io/blog/you-dont-need-a-job-queue-postgres-already-has-skip-locked)
- [Potential consequences of using Postgres as a job queue (Microsoft)](https://techcommunity.microsoft.com/blog/adforpostgresql/potential-consequences-of-using-postgres-as-a-job-queue/4514332)
  — **nothing in this document rests on this page.** An earlier revision quoted a
  "~100 concurrent workers" threshold from it, in the wrong direction, without
  opening it. The claim has been removed from the body rather than re-quoted.
  Fetch it before 21-01 sizes the worker pool.
- [PGMQ: a self-regulating queue (Tembo)](https://legacy.tembo.io/blog/pgmq-self-regulating-queue/)
  — the 11k msg/sec benchmark is attributed here and was **not** found on this
  page by review; treat the number as unverified.
- [XAUTOCLAIM for auto-reassignment in Redis Streams](https://oneuptime.com/blog/post/2026-03-31-redis-xautoclaim-auto-reassignment/view)
- [XPENDING — Redis docs](https://redis.io/docs/latest/commands/xpending/)
- [Reliable data processing: queues and workflows (Temporal)](https://temporal.io/blog/reliable-data-processing-queues-workflows)
- [Durable execution: what Temporal and Conductor solve that queues can't](https://www.javacodegeeks.com/2026/05/durable-execution-what-temporal-and-conductor-are-solving-that-queues-cant.html)

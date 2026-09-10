# Phase 21: Ingestion Job Infrastructure — Research

**Researched:** 2026-09-10
**Domain:** Durable job queue for a Go producer and a Python consumer
**Confidence:** HIGH on the throughput numbers and the constraints; HIGH on the
recommendation, which turns on our constraints rather than on a close call
between technologies.

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

**Our throughput is not a factor.** One job per repository connect and one per
push, with sub-steps kept *inside* a job rather than fanned out. That is single-
digit jobs per second at absolute peak and realistically far less — roughly
three orders of magnitude below where Postgres-as-a-queue starts to hurt.

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
| pgmq (extension) | ✅ SQL API | ✅ | ✅ visibility timeout | an extension to install | Close second — see below |
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
second on a 2-CPU container** — about a thousand times our need.

Rejected for two specific reasons, neither of which is "not invented here":

1. **It is an extension, and that is a deploy-target constraint.** Phase 24 has
   to pick a host that supports it. R-E already added one such constraint
   (nested virtualization for Firecracker) and D2 adds another (pgvector).
   Spending a third on something we can write in about forty lines narrows the
   deployment choice for little gain.

2. **Its data model is a message, ours is a job.** pgmq stores opaque JSONB with
   read/delete/archive semantics. We need a state machine (`queued → running →
   completed | failed | dead | superseded`), an attempt counter, foreign keys to
   `repositories` and `ingestion_runs`, progress fields for the SSE endpoint, and
   a row the admin endpoint in 21-03 can read directly. We would end up with a
   jobs table *beside* pgmq and have to keep the two in step — a dual-write
   problem again, this time inside one database.

If our throughput were two orders of magnitude higher, or if we did not need the
job row to be a first-class queryable entity, pgmq would win.

### Why not Temporal

Temporal solves multi-step durable workflows, which is genuinely what Phase 22's
clone → parse → embed → store is. But the consensus in the literature is blunt
about the fit: *"if your job is just a few background steps, a cron task, or a
simple webhook chain, the platform feels bigger than the problem."* It is a
stateful distributed system to operate, or a paid cloud dependency and a new
vendor.

Four sequential steps inside one job, at single-digit jobs per second, does not
justify that. Revisit if the pipeline grows genuine fan-out with independent
failure and compensation semantics.

---

## Throughput: why this is not a close call

Published guidance on Postgres-as-a-queue converges:

| Load | Behaviour |
|------|-----------|
| < 1,000 jobs/**minute** | Within ~8% of a dedicated broker on throughput. p99 85ms vs 34ms — irrelevant for background work. |
| ~1,000 jobs/**second** | Serialization failures dominate dequeues without careful locking. |
| > a few thousand/second | Vacuum pressure on the jobs table becomes its own operational problem. Use a real broker. |

The stated tripwire is memorable: *"if your answer to 'how do I scale this' is
'shard the queue table,' you have already outgrown it."*

**Our load:** one job per repository connect, one per push webhook. A thousand
active customer repositories pushing ten times a day each is ~10,000 jobs/day —
about **0.1 jobs per second**. Peak bursts (an organization connecting fifty
repositories at once) are still trivial.

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
- It keeps job volume three orders of magnitude below where Postgres hurts.

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
  WHERE (state = 'queued'  AND run_after <= NOW())
     OR (state = 'running' AND lease_expires_at < NOW())   -- reclaim abandoned
  ORDER BY run_after
  FOR UPDATE SKIP LOCKED
  LIMIT 1
)
RETURNING *;
```

The `OR` branch is the lease recovery: a worker that died holding a job has it
reclaimed once the lease lapses. Parenthesise both branches explicitly — `AND`
binds tighter than `OR`, so the intended grouping happens to be the default, and
relying on that is how the next person introduces a bug.

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

- `run_after = NOW() + backoff(attempts)` with jitter — exponential, capped.
- `attempts >= max_attempts` → `state = 'dead'`. Terminal.
- `failed` is retryable; `dead` is not. Keeping them distinct is what makes the
  admin endpoint useful, and it closes the other half of ISS-016 (a `failed`
  repository currently cannot be retried through the API at all).

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

- [You don't need a job queue — Postgres already has SKIP LOCKED (Prisma)](https://www.prisma.io/blog/you-dont-need-a-job-queue-postgres-already-has-skip-locked)
- [Making Postgres queues scale (DBOS)](https://www.dbos.dev/blog/making-postgres-queues-scale)
- [Potential consequences of using Postgres as a job queue (Microsoft)](https://techcommunity.microsoft.com/blog/adforpostgresql/potential-consequences-of-using-postgres-as-a-job-queue/4514332)
- [pgmq](https://github.com/pgmq/pgmq) · [PGMQ: a self-regulating queue (Tembo)](https://legacy.tembo.io/blog/pgmq-self-regulating-queue/)
- [XAUTOCLAIM for auto-reassignment in Redis Streams](https://oneuptime.com/blog/post/2026-03-31-redis-xautoclaim-auto-reassignment/view)
- [XPENDING — Redis docs](https://redis.io/docs/latest/commands/xpending/)
- [Reliable data processing: queues and workflows (Temporal)](https://temporal.io/blog/reliable-data-processing-queues-workflows)
- [Durable execution: what Temporal and Conductor solve that queues can't](https://www.javacodegeeks.com/2026/05/durable-execution-what-temporal-and-conductor-are-solving-that-queues-cant.html)

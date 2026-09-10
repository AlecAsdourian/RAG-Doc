# v2 Substrate — Decisions D1–D4

**Status:** Settled, pending reviewer approval.
**Decided:** 2026-09-10
**Inputs:** `DESIGN.md` (the proposals), `RESEARCH.md` (R-A…R-G), plus the
measurement in §5 of this document.

> These four are the only parts of `DESIGN.md` that block Phases 21–22. They are
> written as decisions, not recommendations, because Phase 22 writes the first
> real rows and reversing any of them afterwards means re-ingesting every
> repository we have indexed.
>
> Everything else in `DESIGN.md` stays a proposal for the planner to sequence.

---

## D1 — Stable symbol identity

### Decision

**Yes. Add a `symbols` table with a deterministic id, and hang `chunks` off it.**

Identity is the triple **(repository, file path, symbol path)** — deliberately
*sub-file* granularity, and the symbol path is **normalized through aliases to
the leaf definition** before the id is computed.

```sql
CREATE TABLE symbols (
  -- uuid_v5(NS_SYMBOL, repository_id || E'\0' || file_path || E'\0' || symbol_path)
  -- Deterministic, so a re-ingest of unchanged code produces the same id
  -- without a lookup, and two workers racing the same file agree.
  id UUID PRIMARY KEY,

  organization_id UUID NOT NULL,          -- partition + RLS key (see D2)
  repository_id   UUID NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,

  file_path   TEXT NOT NULL,
  -- Normalized breadcrumb, alias-resolved. `pkg/api/handlers.RepositoriesHandler.Connect`
  symbol_path TEXT NOT NULL,
  kind        TEXT NOT NULL,              -- function|method|class|type|const|module

  start_line INTEGER NOT NULL,
  end_line   INTEGER NOT NULL,

  -- SHA-256 over the symbol's SPAN, not the file. This is the drift detector
  -- that D4's anchors compare against.
  span_digest TEXT NOT NULL,

  first_seen_commit TEXT NOT NULL,
  last_seen_commit  TEXT NOT NULL,

  UNIQUE (repository_id, file_path, symbol_path)
);
```

`chunks` gains `symbol_id UUID REFERENCES symbols(id) ON DELETE SET NULL`. It is
nullable because not every chunk belongs to a named symbol — file headers,
prose in markdown, config blobs.

### Why, and what changed the shape

The plain argument is in `DESIGN.md` § R2: `content_hash` is a dedupe key and
`breadcrumb` is a search field; neither is an identity, so nothing can be
anchored to code that does not hold still.

Two specifics come from **R-G / EA-Graph**, which measured this exact design:

1. **Sub-file granularity is not a refinement, it is the point.** EA-Graph
   measured file-level invalidation against sub-path identity and found sub-path
   avoided **~71 false alarms per 96 behaviours**. File-level anchoring produces
   so much spurious drift that the signal becomes noise, which is the failure
   mode that makes people turn staleness warnings off.

2. **Aliases must resolve to the leaf definition before identity is assigned.**
   In EA-Graph's words: *"the normalization function must follow these chains to
   the leaf definition. Otherwise, one artifact acquires multiple identities."*
   A re-export in an `index.ts` barrel file, or a dependency-injection binding,
   would otherwise create a second identity for the same code and generate
   phantom drift on both.

### Known limitation, stated rather than hidden

**A rename changes `symbol_path`, therefore changes `symbol_id`.** Name-based
identity cannot see through renames: the old symbol disappears and a new one
appears, and any memory anchored to the old one goes `unprovable` (D4).

That is the correct conservative behaviour — a renamed function may also have
changed — but it is lossy. **Deferred mitigation, not day one:** during a
re-ingest, if a symbol vanished and a new one appeared in the same file with an
identical `span_digest`, treat it as a rename and carry anchors across. Cheap,
and it can be added later without a migration because it only writes rows.

### Verification required in Phase 22

- Re-ingesting an unchanged commit produces **identical** `symbol_id` values.
- Adding an unrelated line to a file does not change the `symbol_id` of symbols
  below it (this is what `content_hash` alone got wrong).
- A re-export resolves to the same id as its leaf definition.

---

## D2 — pgvector, and partition `chunks` by organization

### Decision

**Adopt pgvector. Drop Qdrant. Partition `chunks` by `HASH (organization_id)`
with `MODULUS 64`.**

Both halves are the decision. Partitioning is not a follow-up optimisation —
see §5, where it is the difference between 80% and 100% recall in the shape we
deploy.

```sql
CREATE TABLE chunks (
  id              UUID NOT NULL DEFAULT gen_random_uuid(),
  organization_id UUID NOT NULL,
  repository_id   UUID NOT NULL,
  symbol_id       UUID REFERENCES symbols(id) ON DELETE SET NULL,
  ...
  embedding vector(1536) NOT NULL,        -- text-embedding-3-small
  PRIMARY KEY (organization_id, id)       -- partition key must be in the PK
) PARTITION BY HASH (organization_id);
-- 64 partitions created once, at migration time.

-- Local per-partition indexes, created on the parent:
CREATE INDEX ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ON chunks (organization_id);   -- pgvector's own recommendation
```

Session default: `hnsw.iterative_scan = relaxed_order`.

### Why HASH rather than LIST

LIST-per-organization would mean **DDL on every signup** and an unbounded
partition count; planning time degrades as partitions multiply. HASH with a
fixed modulus gives a bounded count, no DDL on the signup path, and still prunes
to a single partition on an equality predicate.

What we give up is `DETACH PARTITION` as a fast per-tenant delete. We delete
with `DELETE`, which is what the existing cascade already does. Acceptable.

### Why pgvector at all

Three reasons from `DESIGN.md` § R6, unchanged: one transaction instead of two
non-transactional writes about to be wired into a *retrying* queue; RLS covering
the vectors, which closes the §2.1 isolation asymmetry rather than patching it;
and one store to back up, which deletes work from Phase 24.

R-B added the scale evidence: pgvector HNSW matches or beats Qdrant at 1M
vectors on equivalent compute, and the consensus threshold for leaving Postgres
is ~10–50M. Our estimated ceiling is ~10M (100 orgs × 5 repos × 20k chunks);
near-term we are well under 1M.

**Revisit trigger:** 10M vectors, or a measured p95 we cannot meet.
`pgvectorscale` is the intermediate step before leaving Postgres.

### What this forecloses

Qdrant goes away entirely, including `qdrant_writer.py` and `vector_retriever.py`'s
Qdrant path. That is a deliberate simplification, not a deferral — keeping both
is the consistency hazard we are removing.

### Verification required in Phase 22

- **A multi-tenant recall test**, seeded with many organizations, querying as
  one, measured against an exact-search baseline. A single-tenant fixture
  **cannot** detect the failure this decision exists to prevent, because with
  one tenant every candidate passes the filter. This test is the regression
  guard; without it the decision's benefit is unverifiable.
- `EXPLAIN` on the production query shape shows `Subplans Removed`.

---

## D3 — Graph edges: emit now, resolve in two tiers

### Decision

**Build tier 1 (build-free) in Phase 22. Defer tier 2 (SCIP) behind the
sandbox. Lock the edge schema now.**

The schema is the part that cannot slip, because it is what lets tier 2 upgrade
tier 1 later without a migration.

```sql
CREATE TABLE symbol_edges (
  organization_id UUID NOT NULL,
  repository_id   UUID NOT NULL,
  from_symbol_id  UUID NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
  to_symbol_id    UUID NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,

  edge_kind TEXT NOT NULL,        -- calls|imports|references|implements|extends

  -- EA-Graph's evidence lattice, NOT a float confidence.
  -- unknown < partial < proven. Tier 1 emits 'partial'; SCIP emits 'proven';
  -- cross-boundary linkage with no supporting evidence is 'unknown'.
  evidence TEXT NOT NULL CHECK (evidence IN ('unknown','partial','proven')),

  source_tier SMALLINT NOT NULL,  -- 1 = heuristic, 2 = SCIP

  PRIMARY KEY (organization_id, from_symbol_id, to_symbol_id, edge_kind)
);
```

**Upgrade rule** — tier 2 overwrites tier 1, never the reverse:

```sql
INSERT INTO symbol_edges (...) VALUES (...)
ON CONFLICT (organization_id, from_symbol_id, to_symbol_id, edge_kind)
DO UPDATE SET evidence = EXCLUDED.evidence, source_tier = EXCLUDED.source_tier
WHERE EXCLUDED.source_tier > symbol_edges.source_tier;
```

**Phase 22's chunk-level event payload widens now** to carry call-site and
import candidates. Cheap — the parser already walks the tree — and it means the
tier-1 resolver can be built without touching the pipeline again.

### Why an evidence lattice instead of a confidence float

A float invites false precision and has no defined composition rule: what is the
confidence of a two-hop path through a 0.8 and a 0.6 edge? The lattice from
EA-Graph has an ordering, composes by taking the minimum along a path, and —
importantly — **is the same vocabulary D4 uses for memories.** One concept
covers both, so retrieval can rank code edges and memories on the same axis.

### ⚠ Correctness requirement: cycles

Call graphs are cyclic — recursive and mutually recursive functions. Postgres's
recursive executor **keeps no visited set across iterations** (R-D), so a naive
`WITH RECURSIVE` over `symbol_edges` **will not terminate.**

Every traversal must use the `CYCLE` clause (PostgreSQL 14+) or an explicit path
array with a membership check. This is a correctness rule, not a tuning note,
and it belongs in the first traversal helper rather than in a code review.

### Verification required

- A traversal over a deliberately cyclic fixture terminates.
- A tier-2 edge upgrades a tier-1 edge in place; a tier-1 re-ingest does **not**
  downgrade a tier-2 edge.

---

## D4 — The anchor and memory model

### Decision

**Design it now, build the tables later — and split confidence into two
independent axes plus a disposition, per EA-Graph.**

This is the decision that changed most against `DESIGN.md` § R7, which proposed
a single `confidence` and a lifecycle `proposed → confirmed → stale →
contradicted`. That collapses two different questions into one number.

```sql
CREATE TABLE memories (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id UUID NOT NULL,
  scope           TEXT NOT NULL CHECK (scope IN ('private','task','fleet')),
  author_agent_id UUID,
  body            TEXT NOT NULL,

  -- AXIS 1 — how well grounded is this claim?
  evidence   TEXT NOT NULL CHECK (evidence IN ('unknown','partial','proven')),

  -- AXIS 2 — is it still valid? Independent of axis 1.
  freshness  TEXT NOT NULL CHECK (freshness IN ('fresh','stale','unprovable')),

  -- Separate again: withdrawing a claim from retrieval is not deleting it.
  disposition TEXT NOT NULL DEFAULT 'retain' CHECK (disposition IN ('retain','withdrawn')),

  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE memory_anchors (
  memory_id UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  symbol_id UUID NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,

  -- The symbol's span_digest AT BINDING TIME. Drift = this no longer matches
  -- symbols.span_digest.
  span_digest_at_binding TEXT NOT NULL,
  bound_at_commit        TEXT NOT NULL,

  PRIMARY KEY (memory_id, symbol_id)
);
```

### Why two axes instead of one number

EA-Graph's reasoning, which we adopt: *"Keeping them apart is what lets the
model say the two different things an agent needs to hear."*

A **well-evidenced but stale** claim and a **fresh but weakly-evidenced** claim
are different situations calling for different agent behaviour, and averaging
them into one score destroys exactly the distinction that matters. A stale claim
is withdrawn *regardless* of how well evidenced it was.

### The three drift outcomes

On re-ingest, each anchor is rehashed and compared:

| Outcome | Condition | Effect |
|---------|-----------|--------|
| **Unaffected** | digest matches | nothing |
| **Affected** | digest differs, symbol still exists | `freshness = 'stale'`, carry the diff for review |
| **Unprovable** | symbol no longer exists | `freshness = 'unprovable'` — **terminal** |

`unprovable` is a terminal state, not a low confidence grade. EA-Graph's stance,
which we adopt: *the system refuses to return stale claims rather than qualify
them.* An agent handed a qualified-but-wrong claim about deleted code does worse
than one handed nothing.

### Disposition is separate from status

EA-Graph again: *"Loss of proof does not authorize destruction of the last
verified artifact."* Staleness sets `disposition = 'withdrawn'`, which removes a
memory from retrieval. It never deletes the row. A human or a later agent can
review, re-anchor, and restore it.

### How F9's promotion fits

Promotion on merge sets `evidence = 'proven'` and `scope = 'fleet'`. It touches
axis 1 only — a merged PR says the claim was *grounded*, not that it will stay
fresh forever. Freshness remains the anchor's business.

### What must be true now, and what can wait

**Now (blocks D1's schema):** `symbols.span_digest` must exist and be a hash
over the span, so anchors have something to compare against. That is already in
D1.

**Later:** the `memories` and `memory_anchors` tables themselves. Nothing in
Phases 21–22 writes them.

---

## 5. Measured evidence for D2

R-B flagged one thing as explicitly unverified: *"partition pruning on
`current_setting('app.current_tenant')` is runtime pruning, not plan-time … this
must be confirmed with `EXPLAIN ANALYZE` against a partitioned table under a
real tenant transaction, not assumed."*

Measured 2026-09-10 on PostgreSQL 17.11 with pgvector, in the deployment shape
this repo documents: tables owned by a `NOSUPERUSER NOBYPASSRLS` role, `FORCE
ROW LEVEL SECURITY`, tenant set with `set_config(..., true)` inside a
transaction. 40 organizations × 500 rows × `vector(128)` = 20,000 rows; one
organization is 2.5% of the table.

### Result 1 — pruning fires

```
->  Append (actual rows=500 loops=1)
      Subplans Removed: 15
      ->  Bitmap Heap Scan on chunks_part_15
            Recheck Cond: (organization_id = (current_setting('app.current_tenant', true))::uuid)
```

**15 of 16 partitions pruned at execution start**, driven purely by the RLS
policy predicate — no explicit `WHERE organization_id = …` in the query.
Confirmed as *runtime* pruning, as R-B predicted.

### Result 2 — the unpartitioned table shows the documented failure

```
->  Index Scan using chunks_flat_embedding_idx on chunks_flat
      Filter: (organization_id = (current_setting('app.current_tenant', true))::uuid)
      Rows Removed by Filter: 306
```

The post-index filter, exactly as described: the HNSW walk returns candidates,
306 of which are other tenants' rows, discarded after the fact.

### Result 3 — recall, against an exact-search baseline

| Variant | Overlap with exact top-10 |
|---------|---------------------------|
| Unpartitioned, HNSW default | **8 / 10** |
| Unpartitioned, `iterative_scan = relaxed_order` | **8 / 10** |
| **Partitioned** | **10 / 10** |

Two things worth noting, one of them a correction to R-B:

- **`iterative_scan` did not help here.** R-B presented it as the primary
  mitigation. In this configuration it changed nothing, and partitioning is what
  moved recall. Keep the setting — it is cheap and helps in other shapes — but
  it is not the fix.
- **All variants returned 10 rows.** The "fewer rows than `LIMIT`" symptom did
  *not* appear at this selectivity. The failure presented purely as **silently
  worse results**, which is the more dangerous shape: a short result set is at
  least detectable.

### ⚠ What this does NOT prove

Inspecting the partitioned plan shows the planner chose a **btree scan plus an
exact sort**, not HNSW — after pruning to ~1,250 rows, exact search is genuinely
cheaper. Forcing `enable_seqscan=off` and `enable_bitmapscan=off` did not change
that.

**So the 10/10 is partly "the partition was small enough to search exactly."**
At production per-tenant volumes the planner will use HNSW *within* a partition,
and recall will be the normal HNSW approximation over a smaller, tenant-relevant
graph — better than a filtered global graph, but not necessarily perfect.

The decision does not depend on the unmeasured part. It rests on pruning firing
(measured), a smaller per-tenant search space (structural), and partitioning
being cheap now and a rewrite later. **The recall curve at scale is a tuning
question for the Phase 22 recall test**, which is why that test is a required
deliverable of D2 rather than a nice-to-have.

Reproduction script: `scratchpad/d2_test.sql` in the authoring session; the
container was `pgvector/pgvector:pg17` and has been removed.

---

## 6. What still blocks nothing but should be tracked

Carried from `RESEARCH.md`, none of these gate Phases 21–22:

- What fraction of real repositories build in a cold clone with no credentials?
  Determines whether SCIP tier 2 is a headline feature or a bonus. Measurable.
- Does SCIP's symbol format map onto D1's `symbol_id`, or is a translation layer
  needed?
- SCIP indexers run whole-project; Phase 22's changed-files-only re-index has no
  obvious tier-2 equivalent.
- Rename detection for D1 (deferred mitigation above).
- ISS-016 — `sync_state` has no lease — must be settled before Phase 21 builds
  the queue. Unrelated to these four, still blocking.

---

*Decided by the worker session, 2026-09-10, on branch `design/v2-substrate`.*

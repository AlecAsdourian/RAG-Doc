# v2 Substrate — Decisions D1–D4

**Status:** Revised 2026-09-10 after two review rounds. **Every schema in this
document has now been executed against PostgreSQL 17** — the second review found
three of four revisions invalid, which is what that measurement is for. What was
verified is recorded under each decision. See `REWORK.md` for the decisions
taken between the first draft and this one.
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
  -- uuid_v5(NS_SYMBOL, repository_id ||E'\0'|| file_path ||E'\0'||
  --                    symbol_path ||E'\0'|| kind ||E'\0'|| ordinal)
  --
  -- Deterministic, so a re-ingest of unchanged code produces the same id
  -- without a lookup, and two workers racing the same file agree.
  --
  -- ⚠ `kind` AND `ordinal` ARE BOTH IN THE HASH INPUT, and the second review
  -- is why. An earlier revision added `kind` only to the UNIQUE constraint
  -- below and not to the id, so the primary key collided first and the UNIQUE
  -- was unreachable -- the fix was inert.
  --
  -- And `kind` alone does not disambiguate two of the three collisions this
  -- schema exists to survive: Go's two package-scope `init()` are both
  -- functions, and Python's `@property` / `@x.setter` pair are both function
  -- definitions. Same path, same name, same kind. `ordinal` -- the 0-based
  -- index of this symbol among those sharing (file_path, symbol_path, kind),
  -- in source order -- is what separates them.
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

  -- `kind` IS PART OF THE KEY, and that is not defensive padding: the
  -- triple without it collides in all four languages we parse.
  --   Go         two `init()` in one file, both at package scope
  --   Python     `@property` and `@x.setter` share a name
  --   TypeScript declaration merging (an interface and a function, same name)
  -- Found in review. Adding it later is a re-ingest.
  -- 0-based index among symbols sharing (file_path, symbol_path, kind) in
  -- this file, in source order. Almost always 0.
  --
  -- KNOWN LIMITATION, stated rather than discovered later: inserting a second
  -- `init()` ABOVE an existing one renumbers it, changing its id, which reads
  -- as a delete plus a create. That is rare, self-correcting on the next
  -- ingest, and strictly better than the collision it replaces -- but it is
  -- not free, and anchored memories on a renumbered symbol go `unprovable`.
  ordinal SMALLINT NOT NULL DEFAULT 0,

  UNIQUE (repository_id, file_path, symbol_path, kind, ordinal)
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
   measured file-level invalidation against sub-path identity and found a large
   reduction in spurious drift. File-level anchoring produces so much noise that
   the signal is lost, which is the failure mode that makes people turn
   staleness warnings off.

   *The specific "~71 false alarms per 96 behaviours" figure quoted in an
   earlier revision is withdrawn here — it is our arithmetic on their reported
   counts, from an unrefereed preprint evaluated on synthetic repositories at
   n=1 per condition. See `RESEARCH.md` § R-G. The direction of the result
   carries this decision; the magnitude should not be quoted.*

2. **Aliases must resolve to the leaf definition before identity is assigned.**
   In EA-Graph's words: *"the normalization function must follow these chains to
   the leaf definition. Otherwise, one artifact acquires multiple identities."*
   A re-export in an `index.ts` barrel file, or a dependency-injection binding,
   would otherwise create a second identity for the same code and generate
   phantom drift on both.

### ⚠ D1 depends on D3 tier 1, which the first draft missed

Alias resolution to the leaf definition **requires an import graph** — following
a re-export in a barrel file to what it re-exports means knowing what the file
imports. That is exactly what D3's tier-1 resolver builds.

So these are not independent decisions and cannot be sequenced apart: **the
tier-1 import resolution has to land before, or with, symbol identity.** Until
it does, alias-heavy code produces multiple identities for one artifact — the
precise failure EA-Graph warns about.

Phase 22 must order them accordingly.

### Symbols are archived, not deleted

**Decided after the second review.** `symbols` rows are never `DELETE`d by
ingest. A symbol that disappears from the source gets `archived_at` set:

```sql
ALTER TABLE symbols ADD COLUMN archived_at TIMESTAMPTZ;
CREATE INDEX idx_symbols_live ON symbols (repository_id, file_path)
  WHERE archived_at IS NULL;
```

Three things fall out of it, and the third is the reason:

1. **`memory_anchors.symbol_id` never dangles**, so it needs no `CASCADE` and no
   nullable FK — the pair of bugs D4 went through.
2. **`unprovable` becomes reachable**: the anchor still resolves, and the symbol
   it resolves to is archived. That is a readable state rather than a dead
   pointer.
3. **Rename detection stops being aspirational.** D1 claims below that renames
   can be picked up later by matching a vanished symbol's `span_digest` against
   a newly-appeared one. That is only true if the vanished symbol's row still
   exists to be matched. Under deletion the claim was false; under archival it
   is simply deferred work.

Cost: the table grows monotonically. `archived_at` plus the partial index above
keeps live lookups off the dead rows, and pruning archived symbols with no
anchors is a Phase 24 maintenance job, not a correctness concern.

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
with `MODULUS 64`. Add a stored `organization_id` to `chunks`, maintained by
trigger (D5).**

Three parts, and the third was missing from the first draft: **a partition key
must be a column on the table, and `chunks` does not have one today.** Tenancy
is derived through `repositories → projects → organizations`, and the RLS policy
is a two-hop `EXISTS` join. Partitioning by organization therefore requires
denormalising it onto the row — which is a decision with its own drift risk, not
a mechanical consequence. See **D5**.

**On why partitioning is kept.** The first draft justified it on recall, citing
§5. Review contested that: the planner flip to exact search reproduces *without*
partitioning, so the b-tree companion index may account for the delta. That
argument is now marked contested and is **not** load-bearing.

The argument that does stand is **index-size runway.** pgvector's "comfortable
to ~10M vectors" guidance is about holding one HNSW graph in memory. With 64
partitions each holding ~1/64 of the rows, and queries pruned to one partition,
the working set is a single partition's index — so the ceiling extends well past
10M aggregate. That is a structural property, independent of any measurement.

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

### On `MODULUS 64`, and when it stops being right

64 is chosen so that partition count stays bounded and planning time stays flat,
while each partition holds a small enough slice to keep its HNSW graph resident.

**The honest limit:** the modulus fixes a *ratio*, not a size. At a few thousand
organisations each tenant is roughly 1.3% of its own partition — a lower
selectivity than the 2.5% §5 measured, so co-tenancy within a partition grows
with customer count rather than shrinking.

**Revisit trigger:** more than ~1,000 organisations, or a measured p95 we cannot
meet. Changing the modulus rewrites every row, which is the re-ingest this
document exists to avoid — so treat it as a one-way door and re-measure before
the customer count gets there, not after.

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

**Build tier 1 (build-free) in Phase 22. Tier 2 is precise SCIP run in the
customer's CI and uploaded to us. Lock the edge schema now.**

**Revised 2026-09-10.** The first draft put tier 2 "behind the sandbox",
because R-A concluded SCIP indexers must run the repository's build and
therefore execute untrusted code. That found only the *precise* mode and
treated its requirement as SCIP's.

Precise navigation is opt-in and requires the *owner* to upload an index per
repository -- they run the indexer in their own CI. **We never execute a
customer's build**, so no sandbox is on this path at all, and Phase 24 loses
the nested-virtualization constraint that claim had imposed.

Build-free *syntactic* SCIP is deliberately NOT written in as a middle rung:
what the documentation describes is `syntax_kind` for highlighting plus a
search-based fallback, which may simply be tier 1 under another name. Evaluate
it against tier 1 before adopting; do not assume it.

The schema is the part that cannot slip, because it is what lets tier 2 upgrade
tier 1 later without a migration.

```sql
CREATE TABLE symbol_edges (
  organization_id UUID NOT NULL,
  repository_id   UUID NOT NULL,
  from_symbol_id  UUID NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
  -- NULLABLE, deliberately. An unresolved reference -- we saw a call to a
  -- name we could not bind -- is exactly what `evidence='unknown'` is for,
  -- and it is precisely what tier 2 would later upgrade. NOT NULL made that
  -- state unrepresentable and would have thrown the rows away.
  to_symbol_id    UUID REFERENCES symbols(id) ON DELETE CASCADE,
  -- The unresolved name, kept so tier 2 has something to bind later.
  to_symbol_name  TEXT,

  edge_kind TEXT NOT NULL,        -- calls|imports|references|implements|extends

  -- EA-Graph's evidence lattice, NOT a float confidence.
  -- unknown < partial < proven. Tier 1 emits 'partial'; SCIP emits 'proven';
  -- cross-boundary linkage with no supporting evidence is 'unknown'.
  evidence TEXT NOT NULL CHECK (evidence IN ('unknown','partial','proven')),

  source_tier SMALLINT NOT NULL,  -- 1 = heuristic, 2 = SCIP

  -- to_symbol_id is nullable, so it cannot carry the primary key. A surrogate
  -- key plus a partial unique index on each of the two shapes.
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  CHECK (to_symbol_id IS NOT NULL OR to_symbol_name IS NOT NULL)
);

CREATE UNIQUE INDEX idx_symbol_edges_resolved
  ON symbol_edges (organization_id, from_symbol_id, to_symbol_id, edge_kind)
  WHERE to_symbol_id IS NOT NULL;

CREATE UNIQUE INDEX idx_symbol_edges_unresolved
  ON symbol_edges (organization_id, from_symbol_id, to_symbol_name, edge_kind)
  WHERE to_symbol_id IS NULL;
```

**Upgrade rule** — tier 2 overwrites tier 1, never the reverse:

```sql
INSERT INTO symbol_edges (...) VALUES (...)
ON CONFLICT (organization_id, from_symbol_id, to_symbol_id, edge_kind)
  WHERE to_symbol_id IS NOT NULL        -- ⚠ the partial index's predicate,
                                        -- repeated. Without it Postgres raises
                                        -- "no unique or exclusion constraint
                                        -- matching the ON CONFLICT
                                        -- specification" -- measured in review.
DO UPDATE SET evidence = EXCLUDED.evidence, source_tier = EXCLUDED.source_tier
WHERE EXCLUDED.source_tier > symbol_edges.source_tier;
```

**⚠ That upsert alone does NOT retire the tier-1 guess.** An unresolved tier-1
row lives in the *other* partial index, keyed on `to_symbol_name`, so tier 2's
resolved insert never conflicts with it — it creates a second row and leaves the
`unknown` phantom in place forever. Because path evidence composes by taking the
minimum, that phantom makes answers *worse the better tier 2 gets*.

**The reconciliation contract, decided:** tier 1 records the name it could not
bind in `to_symbol_name`, and tier 2 deletes on exactly that tuple before
inserting its resolved edge, in one transaction:

```sql
DELETE FROM symbol_edges
WHERE organization_id = $1
  AND from_symbol_id  = $2
  AND edge_kind       = $3
  AND to_symbol_id IS NULL
  AND to_symbol_name  = $4;   -- the name tier 1 could not resolve
```

So `to_symbol_name` is **not** a debugging convenience — it is the join key the
two tiers meet on, and tier 1 must record it even when it resolves successfully,
or tier 2 has nothing to match.

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

  -- ⚠ TWO EARLIER REVISIONS OF THIS LINE WERE BOTH WRONG, and the second was
  -- worse than the first. `ON DELETE CASCADE` destroyed the anchor and with it
  -- `span_digest_at_binding`, so `unprovable` was unreachable. Replacing it
  -- with `ON DELETE SET NULL` was invalid: `symbol_id` sat in a composite
  -- primary key and was therefore NOT NULL, so the clause raised at runtime and
  -- REJECTED THE SYMBOL DELETE ENTIRELY -- breaking ingest's symbol reaping for
  -- any repository that had memories. Measured against PostgreSQL 17 in review.
  --
  -- The actual fix is upstream: `symbols` rows are ARCHIVED, never deleted, so
  -- there is no delete for this FK to react to.
  -- Plain FK to a row that is ARCHIVED rather than deleted (see below), so
  -- this never dangles and never needs to be nulled.
  symbol_id UUID NOT NULL REFERENCES symbols(id),

  -- Kept independently of the FK, so the anchor is still identifiable after
  -- the symbol row is gone.
  symbol_path_at_binding TEXT NOT NULL,

  -- The symbol's span_digest AT BINDING TIME. Drift = this no longer matches
  -- symbols.span_digest.
  span_digest_at_binding TEXT NOT NULL,
  bound_at_commit        TEXT NOT NULL,

  -- Surrogate key, following D3's pattern for the same reason.
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  UNIQUE (memory_id, symbol_id)
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

## D5 — Denormalised tenancy is maintained by trigger, everywhere

### Decision

**Any table that stores an `organization_id` it does not own gets a trigger on
the parent that keeps it in step. No exceptions, and no application-code
maintenance.**

### Why this is a decision and not a detail

Review surfaced this twice without either report naming it as one thing.

Neither `chunks` nor `repositories` carries `organization_id`. Tenancy is
derived: `chunks → ingestion_runs / repositories → projects → organizations`,
and the RLS policies are two-hop `EXISTS` joins.

Two things in this redesign need it **stored**, not derived:

| Table | Why it needs a stored column |
|-------|------------------------------|
| `chunks` | a partition key must be a column on the table (D2) |
| `ingestion_jobs` | a worker claims a job before it knows the tenant (Phase 21, L5) |

Both are therefore two-hop copies that can drift from the truth they mirror. A
drifted `organization_id` on `chunks` is a chunk filed under the wrong tenant —
which, once RLS reads that column instead of the join, means it is *served* to
the wrong tenant.

### We have already solved this once, and cited the wrong half of it

Migration `000012` contains both patterns, and PR #25 cited the wrong one:

- `github_webhook_deliveries` — no RLS, `organization_id` a nullable
  **annotation** never used to authorize. Fine, because nothing reads it to make
  a decision.
- `github_installation_tenants` — a **trigger-maintained mirror**
  (`sync_github_installation_tenant`), built that way precisely because drift
  was representable and the value *is* an authorization input.

Our two new cases are the second kind, not the first.

### The pattern to follow

`sync_github_installation_tenant` is the reference implementation, and its
details were each paid for by a review round:

- `SET search_path = public, pg_temp` and a schema-qualified body — an
  unqualified write is resolvable through a caller's temp schema, and that was
  measured landing a mirror write in a `TEMP` table.
- A `DELETE` branch for `TG_OP = 'DELETE'`.
- A stale-key delete before the upsert, so a re-key cannot leave the old value
  pointing at the row.

### Consequence worth having

Once `chunks.organization_id` is stored, the RLS policy becomes **scalar
equality** rather than a two-hop `EXISTS` join — simpler, faster, and (see §5)
the shape the partition-pruning measurement actually used.

### ⚠ Cross-organisation re-parenting is FORBIDDEN, not supported

The first version of this decision listed "re-parenting a repository to a
project in another organisation updates every dependent `organization_id`" as a
verification criterion. **Review ran it: it fails** —
`new row violates row-level security policy for table "chunks"`. Under
`FORCE ROW LEVEL SECURITY` the trigger runs with the session's tenant context,
and rewriting a chunk into a *different* tenant is exactly what the policy
exists to prevent.

The only ways through are `SECURITY DEFINER` — the bug migration 000012
documents at length, and which this document cites as a cautionary tale three
sections earlier — or `BYPASSRLS`. Both are worse than the problem.

**So the criterion was wrong, not the schema.** Moving a repository between
organisations is a tenant-boundary crossing, not a routine operation: nobody has
asked for it, and permitting it would mean every denormalised column needs a
privileged write path. It is refused:

```sql
-- on repositories
CREATE OR REPLACE FUNCTION reject_cross_org_reparent() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
BEGIN
  IF (SELECT organization_id FROM public.projects WHERE id = NEW.project_id)
     IS DISTINCT FROM
     (SELECT organization_id FROM public.projects WHERE id = OLD.project_id) THEN
    RAISE EXCEPTION
      'cannot move repository % across organisations; export and re-ingest instead',
      OLD.id;
  END IF;
  RETURN NEW;
END; $$;
```

Re-parenting *within* one organisation stays allowed and is a no-op for every
denormalised `organization_id`, so the trigger has nothing to do.

### Verification required in Phase 22

- **Cross-organisation re-parenting is rejected** with a clear error. Same-org
  re-parenting succeeds and leaves `organization_id` untouched.
- Inserting a job or chunk whose `organization_id` disagrees with its
  repository's is rejected at write time (a `BEFORE INSERT` trigger on the
  child, not an `AFTER` mirror on the parent — an `AFTER` trigger on
  `repositories` cannot validate a value a producer supplied on another table).
- A trigger-disabled bulk load followed by re-enabling does **not** leave drift
  — or the load path is documented as forbidden.
- A drift-detection query exists and is run in CI: any row whose stored
  `organization_id` disagrees with the join is a hard failure.

---

## 5. Measured evidence for D2

R-B flagged one thing as explicitly unverified: *"partition pruning on
`current_setting('app.current_tenant')` is runtime pruning, not plan-time … this
must be confirmed with `EXPLAIN ANALYZE` against a partitioned table under a
real tenant transaction, not assumed."*

**⚠ Read this first: the experiment used a schema we do not have today.**
Review caught it and it matters. The test table carried a **stored**
`organization_id` and an RLS policy of scalar equality. Real `chunks` has
neither — no such column, and a two-hop `EXISTS` join through `repositories`
and `projects`.

So `Subplans Removed: 15` came from a predicate the current codebase cannot
produce. **Under D2 + D5 that becomes exactly the shape we are building** — the
column is added and the policy is rewritten to scalar equality — so the
experiment describes the *target* schema rather than the current one. That is a
meaningful result and it is not the one the first draft claimed.

Nothing here has been measured against a two-hop `EXISTS` policy. If D5 were
dropped, none of §5 would apply.

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

**⚠ Contested.** An independent reviewer reproduced the planner flipping to
exact search *without* partitioning, which would mean the b-tree companion index
accounts for the delta rather than partitioning. That has not been re-measured
here.

**D2 no longer rests on this table.** The argument that carries it is
index-size runway (see D2), which is structural. Treat these numbers as
suggestive, not as the justification.

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

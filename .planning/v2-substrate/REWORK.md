# v2 Substrate — Rework Plan

**Written:** 2026-09-10, after all three PRs came back blocking.
**Purpose:** record every decision made since the reviews, and turn the review
findings into a mechanical checklist, so the rework is execution rather than
re-derivation.

> Read this before touching `DESIGN.md`, `DECISIONS.md`, `RESEARCH.md` or the
> Phase 21 documents. Where this file and those disagree, **this file is
> newer**.

---

## 1. Where the three PRs stand

| PR | Scope | Review | State |
|----|-------|--------|-------|
| **#24** | v2 substrate design, research, D1–D4 | Blocking, 10 findings | Needs rework — §5 below |
| **#25** | Phase 21 research + context, ISS-016 | Blocking, 8 findings | Needs rework — §6 below. Two decisions still open. |
| **#26** | ISS-020 semantic cache | Blocking → **addressed** | Fix + tests pushed; awaiting re-review |

---

## 2. Decisions made since the reviews

Each of these is settled. Reasoning is recorded because the reasoning is what
a future reader will want to challenge.

### K1 — pgvector, Qdrant dropped, partitioning kept

**The scale question, answered with arithmetic instead of assertion.** At
function-granularity chunking, roughly **one chunk per ~20 lines** of source
including file summaries. So:

| Codebase | Lines | Approx. chunks |
|----------|-------|----------------|
| A large service | 1M | ~50k |
| Chromium | ~35M | ~1.75M |
| Linux kernel | ~40M | ~2M |
| **10M vectors needs** | **~200M** | — |

A *single* codebase reaching 10M vectors needs about 5× the Linux kernel.
That is hyperscaler-monorepo territory and not a customer we are designing for.

**The 10M figure in `DECISIONS.md` was never per-codebase — it is aggregate
across all tenants** (100 orgs × 5 repos × 20k chunks). That is the number that
binds first, and reaching it means roughly 500 paying organizations. A good
problem, not a design constraint today.

**Partitioning extends the ceiling, and this argument is independent of the
contested recall measurement.** pgvector's "comfortable to ~10M" guidance is
about holding one HNSW index in memory. With 64 partitions each holding ~1/64
of the rows, and queries pruned to one partition, the working set is a single
partition's index. Runway extends well past 10M aggregate.

Keep this distinction sharp in the rewrite: **partitioning is justified on
index-size runway, which stands, and separately claimed a recall benefit, which
the review contested.** Do not lean on the contested half.

**Reversibility settles it.** pgvector → Qdrant later is a bulk export/import of
vectors we already store — no re-embedding, therefore no LLM cost, hours of
batch work. Running both now means carrying the dual-write consistency hazard
indefinitely. Cheap to reverse, expensive to defer.

### K2 — Delete `pkg/vectordb`, and stop calling it a D2 consequence

`pkg/vectordb` is **Go** code with zero importers that was uncompilable from
Phase 3 to Phase 19. Deleting it is correct regardless of what we decide about
Qdrant — it is dead code removal, not a consequence of K1. The live Qdrant path
is the Python one.

This also disposes of the `DeleteByChunkID` missing-tenant-predicate finding:
delete the package rather than add a predicate to something nothing calls.

### K3 — SCIP retiered; the sandbox is unwound

**What was wrong:** R-A claimed SCIP indexers require the repository's build
environment, therefore executing untrusted customer code, therefore needing a
hardened sandbox — which pulled a Firecracker dependency forward and put a
nested-virtualization constraint on Phase 24.

That found only the *precise* mode and treated its requirement as SCIP's.

**Verified 2026-09-10:** precise navigation is opt-in and *"requires you to
upload indexes for each repository"* — the customer runs the indexer in **their
own CI** and sends us the index. We never execute their build.

**Not verified, and therefore not assumed:** that build-free *syntactic* SCIP
yields useful reference edges. What the documentation describes is `syntax_kind`
for highlighting plus a search-based fallback, which may simply *be* our tier-1
heuristic under another name. Evaluate before adopting; do not write it into the
plan as a middle rung.

**Revised tiers:**

| Tier | Method | Runs where | Sandbox? |
|------|--------|-----------|----------|
| 1 | our heuristic — tree-sitter + imports + scope matching | our ingest worker | no |
| 2 | precise SCIP | **the customer's CI**, uploaded to us | no |

**Consequences:** the sandbox goes back to the fleet layer where it belongs
(isolating *agent* execution, not ingestion), and **Phase 24 loses the
nested-virtualization constraint**, which materially widens our hosting options.

### K4 — Denormalized tenancy needs one pattern, applied twice

This is a **new cross-cutting decision** the reviews surfaced twice without
naming it once.

Neither `chunks` nor `repositories` carries `organization_id` today. Tenancy is
derived: `chunks → ingestion_runs/repositories → projects → organizations`, and
the RLS policies are two-hop `EXISTS` joins.

Two things in this redesign need a **stored** organization column:

- `chunks`, because a partition key must be a column on the table (K1).
- `ingestion_jobs`, because a worker claims a job before it knows the tenant
  (Phase 21 L5).

So both are two-hop denormalizations that can drift from the truth they mirror.
**We already solved this once:** `github_installation_tenants` (migration
000012) is trigger-maintained *precisely because drift was possible*, and PR #25
cited the wrong half of that migration as its precedent.

**Decision: every denormalized `organization_id` is maintained by a trigger on
its parent, following the `sync_github_installation_tenant` pattern** —
schema-qualified body, `SET search_path = public, pg_temp`, a DELETE branch, and
a stale-key delete before the upsert.

Side benefit worth stating in the rewrite: storing the column lets the RLS
policy become scalar equality instead of a two-hop join, which is both faster
and the shape my §5 experiment actually measured.

### K5 — ISS-020 fixed, ISS-021 and ISS-022 filed

- **ISS-020** (cache key not tenant-scoped): fixed on #26. Key is
  `cache:query:{hash}:{organization_id}:{repository_id}`, all four scan patterns
  updated, `clear_cache`/`get_cache_stats` refuse a repository without an org,
  and the stored `organization_id` is re-checked on read as defence in depth.
  Verified by mutation: reverting the key format, the scan and the org check
  fails 4 of 5 tests; restoring passes 5 of 5.
- **ISS-021** (the cache has never run — constructor signature drift since Phase
  12). **Do not fix before ISS-020 ships**, because repairing it would arm the
  leak. Ordering matters more than either fix.
- **ISS-022** (no CI job runs the Python worker tests). Until this lands, the
  ISS-020 guard is documentation rather than enforcement.

---

## 3. The two decisions, now settled

**Both confirmed by the user 2026-09-10.** The recommendations below were accepted as written; #25 is unblocked.

### O1 — ISS-016 scope · CONFIRMED

**Decision: narrow it.** ISS-016 becomes the racing-relink half only,
which Phase 21 genuinely closes. The second half — *a `failed` repository cannot
be retried through the public API* — becomes its own issue, owned by whichever
phase works the API surface (22 or 23).

**Why:** an issue that half-closes never closes cleanly, and the three-file
contradiction the reviewer found came from trying to have it both ways —
`21-RESEARCH.md` claimed the retry loop closed it, `21-CONTEXT.md` put API retry
out of scope, `ISSUES.md` said it closes when Phase 21 ships.

### O2 — `failed` vs `dead` · CONFIRMED

**Decision: collapse `failed`.** On a failed attempt, set `state='queued'`
with `run_after` in the future and record `last_error`. `dead` becomes the only
failure terminal.

States: `queued | running | completed | dead | superseded`.

**Why:** it directly fixes the reviewer's blocker (a `failed` state with no edge
back to the claimable set and no place in the unique index), it removes a state
and its transitions rather than adding an edge, and no information is lost —
"this repository is currently failing" is `state='queued' AND attempts > 0`,
which the admin endpoint and the `sync_state` projection can both read. The
unique index set stays `('queued','running')` exactly as written.

---

## 4. What I got wrong, and the practice changes

Recorded because the process failures produced more findings than the design
failures did.

**I cited sources I never opened.** `21-RESEARCH.md` cites DBOS's "Making
Postgres queues scale" as evidence of a ~1,000 jobs/sec ceiling. Fetched: that
figure is a bug they *fixed*, and the article's thesis is 30k/s. Two other
quotes could not be found in any cited page. The mechanism: WebSearch returns a
synthesized summary across results, and I listed the URLs it surfaced as a
Sources section as though each backed the claim beside it.

> **Practice change: anything cited in a planning document gets fetched first,
> or is marked unverified.** A Sources list is a claim that I read them.

**I repeated another agent's findings without checking them.** Two claims in the
first ISS-020 entry came from a reviewer report and went in as fact. One was
right, one led me to cite the wrong endpoint entirely.

> **Practice change: a report from another agent is evidence, not a finding.
> Verify before it enters a durable document.**

**I wrote a reassuring caveat about code I had not read.** `DESIGN.md` §2.1 said
"callers resolve the repository under RLS first." They do not. That made a real
hole look closed.

> **Practice change: no reassuring caveat without a file:line to back it.**

**I asserted a mechanism from one search result.** The SCIP build-environment
claim was true of precise indexing and false as a general statement, and it
moved a sandbox and a hosting constraint on the roadmap.

> **Practice change: before a finding reorders the roadmap, look for the case
> that contradicts it.**

---

## 5. Rework checklist — PR #24

### Factual corrections
- [x] **§2.1** — remove "callers resolve the repository under RLS first"; it is
      false. Replace with what is actually there: `search.go`/`chat.go` take
      `repository_id` from the body with a format check only, and the real
      partial containment is the post-hoc RLS re-read in
      `query_engine.py:286-348` — partial because metadata counts at `:216-223`
      bypass it and the Qdrant leg at `:264-281` is org-unscoped.
- [x] **§2.1** — cross-reference ISS-020, ISS-021.
- [x] **§2 inventory, parser row** — `method_declaration` appears nowhere, so
      **every Go method is invisible** to the parser, including D1's own worked
      example `RepositoriesHandler.Connect`. Also note "TypeScript" is the
      JavaScript grammar.
- [ ] **§2 inventory** — spot-check the remaining rows; the parser row was not
      the only optimistic one.

### Decisions
- [x] **D1** — add `kind` to the unique key; the triple collides in all four
      languages (Go double `init()`, Python `@property`/`@x.setter`, TS
      declaration merging).
- [x] **D1** — record that alias resolution to leaf definitions **requires an
      import graph**, so D1 depends on D3 tier 1. Currently documented as
      independent.
- [x] **D2** — state the denormalization sub-decision explicitly per K4:
      `chunks` gains `organization_id`, maintained by trigger, and the RLS
      policy moves from a two-hop `EXISTS` to scalar equality.
- [x] **D2** — justify partitioning on **index-size runway** (K1), and mark the
      recall claim as contested rather than load-bearing.
- [x] **D2** — address `MODULUS 64` at scale: at a few thousand orgs each tenant
      is ~1.3% of its partition. Either justify the modulus or state the
      revisit trigger.
- [x] **D3** — make `to_symbol_id` nullable; `NOT NULL` makes
      `evidence='unknown'` unrepresentable and discards exactly what tier 2
      would later upgrade.
- [x] **D3** — retier per K3: tier 2 is customer-CI upload, not our sandbox.
- [x] **D4** — change `ON DELETE CASCADE` on `memory_anchors`; it destroys
      `span_digest_at_binding`, making `unprovable` unreachable and D1's
      deferred rename detection impossible.
- [ ] **`chunks.symbol_id`** — one nullable FK does not fit chunking that is not
      symbol-aligned. Decide: a join table, or state the limitation.

### The measurement
- [x] **§5** — reframe honestly. It measured scalar-equality RLS on a table with
      a stored `organization_id`; `chunks` has neither today. Under K4 that
      becomes the shape we are building — so the experiment describes the
      *target* schema, not the current one. Say that plainly.
- [x] **§5** — note the reviewer reproduced the planner flip to exact search
      *without* partitioning, so the b-tree companion index may account for the
      recall delta.

### Research
- [ ] **R-A** — rewrite per K3.
- [ ] **R-B** — drop the "RLS is specially bad" framing; RLS and a plain `WHERE`
      measured byte-identical.
- [ ] **R-C** — re-check against the current MCP revision; it was written
      against one since superseded.
- [ ] **R-G** — qualify the "71 false alarms" figure: it is our arithmetic on
      "88 of 96 vs 17", from an unrefereed preprint using synthetic repositories
      with n=1 per condition.
- [ ] **All sections** — rebuild every Sources list from pages actually fetched.
      Remove anything that cannot be found in a cited page.

### Sequencing
- [x] Unwind the sandbox from the ingestion path; return it to the fleet layer.
- [x] Remove the nested-virtualization constraint on Phase 24.
- [ ] Re-cost R3 and the affected sequence steps.

---

## 6. Rework checklist — PR #25

Blocked on **O1** and **O2**.

### Correctness bugs in the schema
- [ ] **L4 ordering** — supersede **before** enqueue. As written (enqueue, then
      supersede) it raises 23505 against the non-deferrable partial unique index
      in exactly the ISS-016 case it exists to fix, and fails wholesale on a
      bulk `installation_repositories.added` re-queue.
- [ ] **Poison jobs** — add `attempts < max_attempts` to the reclaim branch, and
      a sweeper that moves over-limit rows to `dead`. Dead-lettering as designed
      requires the worker to survive, which a poison job prevents. This is
      pitfall #3 in the same document.
- [ ] **`lease_expires_at IS NULL`** — `NULL < NOW()` is NULL, so such a row is
      invisible to both claim branches while occupying the unique index and
      blocking that repository forever. Use
      `(lease_expires_at IS NULL OR lease_expires_at < NOW())`.
- [ ] **State machine** — apply O2.
- [ ] **Tenancy** — apply K4: `ingestion_jobs.organization_id` is a two-hop
      denormalization and needs the trigger pattern. Cite the correct half of
      migration 000012 (`github_installation_tenants`, not
      `github_webhook_deliveries`).

### Consistency and framing
- [ ] **ISS-016** — apply O1; make all three files agree.
- [ ] **pgmq rejection** — both stated reasons are defective. It has a documented
      **pure-SQL install path**, so "a third deploy-target constraint" is false;
      and calling two tables in one Postgres transaction a "dual write" drains
      the term the document runs on. Re-justify on L2's data-model argument
      alone — 21-03 and 22-04 need a first-class queryable job row, not an
      opaque JSONB message — or reverse the decision.
- [ ] **Throughput** — the ~0.1 jobs/sec arithmetic is right but answers the
      wrong question. L6 puts a 10-minute job behind one queue entry, so
      **concurrency** (~69 workers) is the constraint, not enqueue rate.
- [ ] **Stacking rationale** — overstated. Chunks have been in Postgres since
      migration 000003; D2 moves only the *vectors*. L1 survives intact even if
      K1 were reversed. Retarget #25 to `main` if #24 is going to take longer.
- [ ] **Sources** — same rebuild as #24. The DBOS citation is inverted.

### STATE.md
- [ ] Remove the SCIP claim (wrong, K3) and the partition-pruning claim (measured
      a schema we do not have) as settled fact.
- [ ] Correct "PR #24, awaiting review" — it came back blocking.

---

## 7. Order of work

1. **#26 re-review and merge.** Independent of everything else, and it is the
   only thread with a live bug at the end of it.
2. **#24 rework** — §5. Largest, and #25 partly depends on its outcome.
3. **Confirm O1 and O2**, then **#25 rework** — §6.
4. **ISS-022** (Python CI) — until it lands, the ISS-020 guard does not gate.
5. **ISS-021** (repair the cache) — only after ISS-020 has shipped.

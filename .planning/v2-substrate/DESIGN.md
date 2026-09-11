# v2 Substrate — Design

**Status:** Draft, under active research. Not yet a milestone.
**Started:** 2026-09-10, after Phase 20 closed at `0c28704`
**Owner:** worker session, at the user's direction
**Companions in this directory:**
`RESEARCH.md` — agenda and findings (R-A…R-G, all complete).
`DECISIONS.md` — **D1–D4 settled, with schemas. Authoritative over §8 below.**

> This document is the raw material for a v2 milestone. It is **not** a roadmap
> and does not supersede `.planning/ROADMAP.md`. The planner session owns
> folding it in. Everything here that affects Phases 21–22 is isolated in
> **Decisions D1–D4**, which are the only parts that need settling before
> execution resumes.

---

## Why this exists

Phase 20 closed the GitHub integration. Phases 21 and 22 build the job queue
and the ingest pipeline — and **whatever ingest writes is what retrieval is
stuck with for the next year.** Changing the shape of a stored chunk after
customers have repositories indexed means re-ingesting all of them.

So this is the last cheap moment to decide what we persist.

---

## 1. The argument

Everything built so far answers one question shape: *a human types a sentence,
gets five chunks back, and reads a generated paragraph.* That is a search
engine, and a good one — hybrid FTS + vector, RRF fusion, metadata boosting,
a semantic cache.

An agent wants something different in five specific ways.

| Need | Why the current pipeline doesn't serve it |
|------|-------------------------------------------|
| **Evidence, not prose** | An agent can't act on a paragraph. It wants file paths and line spans it can open and verify. |
| **Structure, not similarity** | "Who calls this?" and "what breaks if I change this signature?" are what agents actually ask. Embeddings answer neither. |
| **Freshness guarantees** | A human notices when a snippet looks outdated. An agent edits confidently against stale context and produces a plausible, wrong diff. |
| **A token budget, not a top-k** | "The best 4,000 tokens you have" is the real request. Five chunks from one file is a wasted budget. |
| **A write path** | The biggest gap. Today the pipeline is read-only from an agent's side. A store agents can't write to is a library, not a memory. |

**The redesign in one line:** retrieval returns *evidence with provenance and
freshness*, answer generation becomes one consumer of it rather than the point
of it, and the store becomes *bi-directional*.

None of this throws anything away. The retrieval components are sound and the
seams are mostly in the right places. What changes is the **shape of what we
persist** — which is exactly what Phase 22 is about to lock in.

---

## 2. What is actually built

Read from the code at `0c28704`, not from the roadmap. The honest headline:
**the retrieval machinery works standalone and nothing has ingested a real
repository end-to-end under the multi-tenant model yet.**

| Component | State | Notes |
|-----------|-------|-------|
| Tree-sitter parsing | **Partial** | Extracts *definitions* — functions, classes, docstrings, ancestor chain, `breadcrumb`. **No references, calls, or imports.** ⚠ Two overstatements corrected after review: `method_declaration` appears nowhere in the parser, so **every Go method is invisible** — including D1's own worked example `RepositoriesHandler.Connect`; and "TypeScript" is the JavaScript grammar, not a TypeScript one. Fixing both is a prerequisite for D1, not a follow-up. |
| Chunking | Built | Semantic and fixed-size, plus a summary generator. |
| Embeddings | Built | OpenAI, batched. |
| Full-text search | Built | Postgres GIN on `content` and `breadcrumb`. |
| Vector search | Caveat | Qdrant, one shared collection `code_embeddings`, filtered by `repository_id` only. See §2.1. |
| RRF fusion + boosting | Built | Reciprocal rank fusion, then chunk-type / path / exact-match boosts, env-tunable. |
| Answer generation | Built | With a semantic cache in front, for the cost constraint. |
| Tenant isolation (Postgres) | Built | Phases 17/19/20. `FORCE ROW LEVEL SECURITY`, request-scoped tenant transactions, a CI gate that fails PRs adding unisolated mutation endpoints. |
| GitHub App + webhooks | Built | Phase 20. Install flow with a user-authorization leg, signature-verified receiver, idempotent on `X-GitHub-Delivery`. |
| Job queue | Not started | Phase 21. Redis Streams tentative, not decided. |
| Clone → ingest orchestration | Not started | Phase 22. **This is the phase that locks the schema in.** |
| Incremental re-index on push | Not started | Phase 22. Webhook already delivers the event; nothing consumes it. |
| Frontend on real data | Not started | Phase 23. Every surface is currently mocked. |
| Code graph | Not started | No edges of any kind exist. |
| Agent memory | Not started | No write path, no memory objects, no MCP surface. |

### 2.1 Vector isolation is weaker than Postgres isolation, asymmetrically

Found while grounding this document, 2026-09-10.

Every tenant's embeddings live in one Qdrant collection (`code_embeddings`,
`services/workers/workers/storage/qdrant_writer.py`). The only guard is an
application-supplied `repository_id` filter, applied in
`VectorRetriever.search`. **There is no `organization_id` in the Qdrant payload
at all** — so a defence-in-depth org filter cannot be added without re-embedding
everything.

**⚠ The first draft said "callers resolve the repository under RLS first."
That is false, and it made a real hole look closed.** Corrected 2026-09-10
after review.

What is actually there:

- `pkg/api/handlers/search.go:58-95` and `chat.go:32` take `repository_id` from
  the **request body** with `validate:"required,uuid"` — a *format* check.
  **No query anywhere asks whether that repository belongs to the caller's
  organization.** The handler does forward the caller's `organization_id` from
  the tenant context, which is what the Python side then uses.
- The real partial containment is a **post-hoc RLS re-read**:
  `_enrich_results_with_metadata` (`query_engine.py:286-348`, called
  unconditionally at `:202`) re-reads the returned chunk ids under
  `require_tenant` and drops rows RLS withholds.
- It is *partial*: the metadata counts at `:216-223` bypass that filter, and the
  Qdrant leg at `:264-281` is org-unscoped.

So the failure shape stands and is worse than described — Postgres fails safe,
Qdrant fails open, and the backstop is a re-read rather than the store itself.

**And a path that skips even that:** the semantic cache is consulted before
retrieval and returns without touching Postgres. See **ISS-020** (fix open in
PR #26, not yet merged) and
**ISS-021** (why it was not exploitable — the cache has never run).

Feeds **R6**, **D2** and **D5** directly.

---

## 3. Part A — the feature list

Three audiences share one substrate. The framing that keeps this coherent:
**documentation, search, and agent memory are three renderings of the same
store**, not three products. Deciding that now is what stops the roadmap
fragmenting later.

Legend: `[built]` `[partial]` `[planned]` `[new]`

### 3.1 Ingest
- `[built]` Connect a GitHub organization, pick repositories
- `[partial]` Clone, parse, chunk, embed, store
- `[planned]` Incremental re-index on push, by changed file
- `[planned]` Live progress while a repo indexes
- `[new]` Extract the code graph — symbols, calls, imports
- `[new]` Ingest more than code: markdown, ADRs, PR discussions, issue threads

### 3.2 Retrieve — the core
- `[built]` Hybrid semantic + keyword search with RRF fusion
- `[built]` Generated answers with citations back to file and line
- `[built]` Semantic cache to hold LLM cost down
- `[new]` Structural queries — *who calls this*, *what does this depend on*
- `[new]` Budgeted retrieval — best N tokens, diversified, spans merged
- `[new]` Freshness on every result — which commit, and has HEAD moved
- `[partial]` Answer quality feedback (schema exists, UI not wired)

### 3.3 Document — the part we keep
- `[planned]` Auto-generated docs per module, service and entry point
- `[planned]` Human-authored tribal knowledge, first-class alongside generated
- `[new]` **Docs anchored to code, flagged stale when the code moves**
- `[new]` Agents as doc authors — write down what you just worked out
- `[planned]` Export as markdown / JSON / natural-language context packs

### 3.4 Fleet — the new layer
- `[new]` MCP server: the one endpoint every agent runtime plugs into
- `[new]` Per-agent private memory, promoted to fleet memory on merge
- `[new]` Fleet registry: who is alive, on what, in which worktree
- `[new]` Advisory claims over symbols, so two agents don't collide
- `[new]` Semantic conflict warnings, from the graph
- `[new]` Durable task objects with owners and state
- `[new]` Blackboard event log — agents talk through the substrate
- `[new]` Agent types as portable capability envelopes
- `[new]` A registry of agent types carrying real outcome data
- `[new]` Topology as a versioned file, edited through a canvas
- `[new]` Fleet dashboard — lanes, claims, blocks, live conflict warnings

### 3.5 Platform
- `[built]` Auth, orgs, RLS tenant isolation
- `[partial]` Web UI for search, browse, repo settings (mocked)
- `[planned]` Per-org rate limits and hard cost caps
- `[planned]` Deployment, backups, incident runbook

---

## 4. Part A continued — seven changes to the RAG services

Ordered by leverage, not effort. **R2 matters most** — it is a prerequisite for
the graph, for memory anchoring, and for staleness detection, and it costs
almost nothing if done before Phase 22 writes the first row.

### R1 — Split retrieval from answer generation · 6–10h

Today `QueryEngine` flows into `AnswerGenerator` as one path. Make retrieval a
first-class API returning ranked spans with provenance; answer generation
becomes one consumer, for the web UI. Agents skip it entirely — and skip its
cost.

The seam already exists. Mostly an interface decision, and it is what makes
every other item here addressable.

### R2 — Stable symbol identity that survives edits · 10–16h · **highest leverage**

A chunk has `content_hash` for deduplication and `breadcrumb` for search.
Neither is an *identity*. Re-ingest after someone adds a line and every chunk id
changes.

Add a `symbol_id` — derived from repository plus normalized breadcrumb — stable
across re-ingests. That single column is what makes the rest possible:

- Graph edges have something durable to point at.
- A memory or doc can be *anchored* to a piece of code.
- Comparing content hashes across runs at the same symbol tells you the code
  changed — which is how anchored knowledge gets flagged stale.

Retrofitting after Phase 22 means re-ingesting every customer repository. Doing
it before costs a migration and a parser field.

### R3 — Reference edges, two tiers · 20–30h now + a later phase

**Revised 2026-09-10 after research R-A. The original plan was wrong.**

The parser finds definitions. The graph needs *references* — call edges, import
edges, type edges. Originally costed at 30–50h of hand-written per-language
resolvers. **Don't write them.**

SCIP — the code-intelligence format Sourcegraph built, moved to independent
governance in 2026 — has mature indexers covering exactly our four languages:
`scip-go`, `scip-python`, `scip-typescript` (TS and JS together). They emit
precise definitions and references. That is the whole graph, already solved.

**The catch is operational, and it is real.** Every SCIP indexer needs the
repository's build environment: `scip-typescript` wants `npm install` and a
`tsconfig.json`; `scip-python` wants an activated virtualenv; `scip-go` wants a
Go toolchain that can build the app. Many customer repos will not build for us
— private dependencies, pinned versions, required secrets — and the indexers
OOM on large trees. Worse, running `npm install` or a `setup.py` on arbitrary
customer code **executes arbitrary code from a stranger**, which needs a
hardened sandbox we had not scheduled.

So: **two tiers over one graph.**

| Tier | Method | Coverage | Accuracy | Needs |
|------|--------|----------|----------|-------|
| 1 | tree-sitter + import resolution + scope matching | 100% of repos | ~80%, confidence-scored | nothing new |
| 2 | precise SCIP, run in the **customer's CI** and uploaded | repos whose owners opt in | precise | none |

Tier 2 edges **upgrade** tier-1 edges in place rather than replacing the graph.
The per-edge confidence score — originally proposed so retrieval could rank on
edge quality — turns out to be the mechanism that lets both tiers coexist.

**⚠ Corrected 2026-09-10.** The claim above -- that SCIP requires the
repository's build environment, therefore executes untrusted code, therefore
needs a sandbox -- found only the *precise* mode and treated its requirement as
SCIP's. Precise navigation is opt-in and requires the repository owner to
upload an index they generated **in their own CI**. We never execute a
customer's build, so **no sandbox is on this path**, and Phase 24 loses the
nested-virtualization constraint this claim had imposed.

Build-free *syntactic* SCIP is deliberately not written in as a middle rung --
it may simply be tier 1 under another name. Evaluate before adopting.

Tier 1 belongs in the pipeline now. Tier 2 is gated on customer opt-in, not on
infrastructure we have to build.

See `RESEARCH.md` § R-A for sources.

### R4 — Budgeted, diversified retrieval · 12–20h

Replace `top_k=5` with a token budget. Three things fall out: diversity (don't
spend the whole budget on one file), span merging (three adjacent chunks become
one contiguous span with real line numbers), and graph expansion (having found
the function, spend remaining budget on its callers).

This is where the graph pays for itself at query time, not only at
"who calls this" time.

### R5 — Freshness as a returned field · 6–10h

Every span comes back with the commit it was ingested from and whether HEAD has
moved past it. Agents decide: re-read the file, or trust the index. Humans get
a quiet "indexed 3 commits ago" instead of silently wrong context.

Cheap, and it converts our worst failure mode from invisible to visible.

### R6 — Collapse Qdrant into Postgres with pgvector · 26–38h · **decide before 22**

We write chunks to Postgres and vectors to Qdrant in two separate,
non-transactional writes. That is a permanent consistency hazard — a chunk in
one store and not the other — and it is about to be wired into a *retrying* job
queue, which makes partial writes routine rather than rare.

Folding vectors into Postgres via pgvector buys three things at once:

1. **One transaction.** Chunk and vector land together or not at all.
2. **RLS covers the vectors too.** The §2.1 asymmetry disappears rather than
   being patched.
3. **One store to back up and restore** — which deletes work from Phase 24.

The honest counter-argument is scale: Qdrant beats pgvector on very large
collections. We are nowhere near that, and we can move back if we ever are.
Carrying a distributed-consistency problem now, to avoid a migration we may
never need, is the wrong trade.

**Research R-B confirmed this on scale grounds too** — pgvector HNSW matches or
beats Qdrant at 1M vectors on equivalent compute, and the consensus threshold
for leaving Postgres is roughly 10–50M. Our estimated ceiling is ~10M
(100 orgs × 5 repos × 20k chunks); near-term we are well under 1M.

**But R-B also found the thing that would have made a naive adoption fail
silently.** The HNSW index is not security-aware: Postgres applies RLS policies
as security quals *after* the index scan returns its candidates. With a
selective tenant filter — which our RLS policy always is — the candidate set
may contain too few passing rows, and the query returns fewer results than
asked for, or quietly worse ones, **with no error**.

A single-tenant test database cannot detect this: with one tenant, every
candidate passes. Same failure class as 000012's `SECURITY DEFINER` lookup and
20-05's re-claim rule — correct in the shape it was tested, wrong in the shape
it deploys.

So R6 carries four implementation requirements, not one:

1. **Partition `chunks` by organization.** Turns the tenant filter into
   partition pruning, with a smaller HNSW index per partition. Structurally
   removes the problem rather than tuning around it — and, like R2, is cheap now
   and expensive later. *This belongs in D2's answer.*
2. **`hnsw.iterative_scan`** (pgvector 0.8+), `strict_order` or `relaxed_order`,
   tuned with `max_scan_tuples` and `hnsw.scan_mem_multiplier`.
3. **A btree index on the filter column** beside the HNSW index — pgvector's own
   recommendation.
4. **A recall test with many tenants**, measured against an exact-search
   baseline. Nothing else can catch a regression here.

See `RESEARCH.md` § R-B.

### R7 — Memories as first-class objects · 16–24h

The write path. A **memory** is a note, decision, gotcha or convention that:

- anchors to zero or more symbols or files
- carries provenance — which agent, which session, which commit, which PR
- carries a confidence
- has a lifecycle — `proposed → confirmed → stale → contradicted`

**⚠ Superseded by D4.** Research R-G (EA-Graph) showed the single-confidence
model collapses two different questions into one number. The settled model uses
**two independent axes** — `evidence` (unknown < partial < proven) and
`freshness` (fresh | stale | unprovable) — plus a separate `disposition`
(retain | withdrawn), because a well-evidenced-but-stale claim and a
fresh-but-weakly-evidenced claim call for different agent behaviour. See
`DECISIONS.md` § D4.

Retrieval then searches code and memories together, ranking memories on
provenance and corroboration rather than embedding distance alone. This one
object is the difference between a search product and a substrate.

---

## 5. Part B — fleet infrastructure

Two of these are genuinely differentiated — possible only because we hold both
the code graph and the fleet registry, which nobody else does. Marked ★.

### Parallel safety

**F1 — Worktree per agent, microVM per run · 20–35h · table stakes**

Two things people conflate. *Isolation of edits* is a git worktree — cheap,
shares the object store, native, each agent gets its own branch and working
directory free. *Isolation of execution* — running tests, running the agent's
code, and (per R-A) running SCIP's build commands on untrusted repos — is a
container, a different problem with different failure modes.

Worktrees first. They solve the day-one problem: two agents stepping on each
other's files.

**Research R-E confirmed the split and demoted the item.** Worktree-per-agent is
*the established default* in 2026, not a novel idea: JetBrains shipped
first-class support in 2026.1, VS Code in July 2025, Cursor in 2026.1; Claude
Code has `--worktree`; and Intent, AQ, Atlas, Nimbalyst and Warp already
automate worktree creation, assignment, review and cleanup.

**So F1 is table stakes.** Adopt the standard pattern as cheaply as possible and
spend the differentiation budget on F2, F3 and F9, which none of those tools
has. The literature states the gap almost exactly: *"a good multi-agent
orchestration tool would combine the speed of local worktrees with the isolation
of cloud environments, plus coordination features that neither has"* — which is
F1 + F2 + F3 + F5.

It also independently confirms the edit/runtime split: *"git worktrees alone are
not enough to stop one task's runtime from trampling another task's ports,
databases, caches, secrets, or test state."*

**⚠ The execution half needs a microVM, not a container.** Containers share the
host kernel across ~350 syscalls; one bug is an escape. For agent-authored code
and for R3 tier 2's `npm install` on customer repositories, that is not
adequate. **Firecracker** (own guest kernel on KVM, ~125ms boot, <5 MiB per VM,
powers Lambda and Fargate) is the recommendation, with **gVisor** (~50ms,
userspace syscall interception) as the fallback where KVM is unavailable.

*Phase 24, softened:* prefer a deploy target offering nested virtualization,
falling back to gVisor where unavailable. **No longer a hard constraint** — the
claim that made it one (SCIP needing to run customer builds) was withdrawn, and
F1's microVM is justified by agent execution alone, which lands in step 6 rather
than gating the deploy decision.

**F2 — Advisory claims over symbols · 16–24h**

Before editing, an agent declares intent over a set of symbols. The substrate
answers: *someone else holds a claim on this, here's who and what they're
doing.*

Deliberately advisory, not a lock. Hard locks deadlock a fleet whose members
crash and lose their leases. Claims expire on a heartbeat; an agent can proceed
anyway — it just does so knowingly.

**★ F3 — Semantic conflict prediction · 20–30h**

The conflicts that hurt a parallel fleet are the ones **git cannot see**. Agent
A changes a function's signature. Agent B, in a different file, writes a new
call to it. Both branches merge cleanly. The build breaks and nobody knows
whose fault it was.

With the graph plus the claims registry, that is predictable *while both agents
are still working*. A holds a claim on a symbol; B's claim touches something one
hop away in the call graph; warn both, now, before either finishes.

Only possible because we hold code structure and fleet state in the same place.
Falls out of R2 + R3 + F2 almost free.

### Inter-agent communication

**Recommendation against: do not build an agent chat bus.**

Agent-to-agent conversation is seductive and mostly produces token-burning games
of telephone: two agents negotiating in prose, each paying for the other's
context, converging on nothing.

Our own fleet is the evidence. Planner, worker and reviewer coordinate entirely
through GitHub — PRs, review comments, merge state — and never session to
session. That works because the artifacts are durable, auditable, and don't
require both parties awake.

**F4 — A blackboard, not a mesh · 20–30h**

Agents communicate *through the substrate*. A writes a finding; B queries later
and finds it. Asynchronous, durable, no liveness requirement, no protocol to
negotiate — and every message is also a memory, so the audit trail is free.

Then a thin direct layer only where the blackboard is genuinely wrong:

- **Requests that need a reply** (planner → worker: do this). A work queue, not
  a conversation.
- **Notifications** (something you claimed just changed). Pub/sub on substrate
  events.

One durable event log with subscriptions. Not *n*×*n* channels.

### Fleet organization

**F5 — Registry, heartbeats, reaping · 16–24h**

Who is alive, what they're working on, which worktree, which claims, last
heartbeat. Unglamorous and load-bearing — claims need it, F3 needs it, the
dashboard needs it.

The part that's easy to forget: **reaping**. An agent that stops heartbeating
releases its claims. Without that the claims table becomes a graveyard within a
week and everyone learns to ignore it.

**F6 — Roles as enforced permissions · 20–30h**

A reviewer role that *literally cannot push to main* — enforced by the
credentials the substrate hands out, not by an instruction in a prompt.

Prompt-enforced discipline is fine for a fleet of three where a human reads
every PR. It does not survive thirty agents, and it never survives one agent
that misreads its instructions. (Superseded in framing by **F18** — this is the
enforcement half of an agent type.)

**F7 — Tasks as durable objects · 16–24h**

A task has an id, an owner, a state, a parent, and an artifact. Not a message in
a conversation. Agents crash and sessions run out of context; the work item has
to outlive both, and be reassignable to a fresh agent that *reads* its history
rather than being told it.

### Memory

**F8 — Three scopes, one store · included in R7**

*Private* (this agent's scratch), *task* (shared by everyone on this work item),
*fleet* (promoted, durable, everyone sees it). Same object, one field.

The interesting mechanic isn't the scopes — it's *promotion*.

**★ F9 — Promotion on merge, invalidation on change · 24–40h**

**Promotion:** a memory anchored to code that landed in a merged PR gets
promoted automatically. That ties knowledge validity to something objective — a
human approved this and it shipped — rather than to an agent's self-assessment.
We can do this because we already own the GitHub integration.

**Invalidation:** when an anchored symbol's content hash changes, the memory is
flagged *stale* and carries the diff. Not deleted — it may still be right, and
the human or the next agent can judge.

This is the mechanism that stops the substrate rotting, and rot is precisely why
every "AI memory" product dies around month four: it accumulates confident
claims about code that no longer exists and gets slowly more dangerous than
having no memory at all. Invalidation is not a nice-to-have; it is what makes
the product viable at month twelve.

**Research R-G turned that from a hunch into the best-supported claim in this
document.** "Context rot" — divergence between agent-facing docs and the code
they describe — was measured in **23.0% of 356 repositories**. The mechanism is
exactly the one stated above:

> *"While missing elements announce themselves through errors, stale elements do
> not."*

> *"RAG has no model of time — when a function is renamed, RAG retrieves both
> the stale and current value with near-identical embedding similarity."*

That last line is why R5 and F9 are not polish: embedding similarity cannot
distinguish the old truth from the new one, so no amount of better retrieval
fixes it.

There is prior art to build on rather than guess at — **EA-Graph**
(artifact-anchored verification memory under upstream drift) is directly this
design, and should be read **before D4's anchor schema is locked**. And **STALE**
is a benchmark for "can an agent tell when its memories went stale", which gives
F9 a measurable success criterion instead of an assertion. See `RESEARCH.md`
§ R-G for all four papers.

**F10 — Contradiction detection · 12–20h**

Two memories on the same anchor that disagree. Surface the conflict rather than
silently returning both — an agent handed contradictory context performs *worse*
than one handed nothing, because it picks one at random and proceeds with full
confidence.

**Research R-G showed this was under-specified.** The literature separates two
cases, and the above describes only the easy one:

- **Explicit conflict** — two memories on the same anchor that disagree.
  Detectable, and what F10 solves.
- **Implicit conflict** — *a later observation invalidates an earlier memory
  without explicit negation*. Named as **the critical failure mode**, and it
  needs contextual inference; there is a benchmark of 400 expert-validated
  scenarios.

Our anchoring model gives a partial answer the general case lacks: when the
anchor's content hash changes we know *something* invalidated the memory, even
if we cannot infer *what*. **F10 should claim the explicit case and treat
anchor-hash change as a partial signal for the implicit one** — stronger than
nothing, weaker than semantic conflict detection, and honest about which.

### The hive-mind endpoint

The instinct — one endpoint, ask for what you need, typed like gRPC — is right
about the interface. Two adjustments.

**Agree on the interface.** One entry point, typed schema, structured question
in and structured answer out. It should be **MCP** as the outer protocol,
because that is what agent runtimes actually speak. A bespoke protocol means
every client needs an adapter; MCP means Claude Code, Cursor and anything else
plug in on day one. Use gRPC internally between our own services if we want the
typing — but the agent-facing surface has to be what agents already speak.

**Question the separate graph DB.** A repository's graph is small — tens of
thousands to low millions of nodes. Postgres with recursive CTEs handles bounded
traversals (one to three hops, which is all retrieval needs) comfortably.

Adding FalkorDB or Neo4j means a *second* store, a second consistency problem —
exactly the one R6 is deleting — and a second isolation model with no RLS. That
is the §2.1 situation again with a new logo.

**Build the graph as tables in Postgres, keep the query interface abstract**,
and move to a real graph engine when we hit a query we cannot express or a
latency we cannot meet. The interface is the valuable part; the storage engine
is swappable behind it.

**Research R-D confirmed this, and named the boundary.** Our query shape —
bounded fan-out from a node to fill a context budget — is the one case where
Postgres is reported to *win* rather than merely suffice: a properly indexed
edge table handles tens of millions of edges with sub-second responses at
typical depths, and it is one less system.

Postgres loses at deep path enumeration (measured p50 334ms / p95 1.8s against
Neo4j's 28ms), at dense relationships, and at genuine graph *algorithms* —
PageRank, community detection, weighted shortest path. None of those is on the
roadmap; several are plausible v3 features (automatic architecture summaries,
suggested module boundaries), so this is a revisit-later, not a never.

**⚠ One correctness item, not a tuning one.** Postgres's recursive executor
keeps no visited set across iterations, and call graphs are cyclic — recursive
and mutually recursive functions. A naive `WITH RECURSIVE` over call edges
**will not terminate**. Use the `CYCLE` clause (PostgreSQL 14+) or an explicit
path array with a membership check. See `RESEARCH.md` § R-D.

**One failure mode to design around:** "one endpoint" can collapse into a single
`query()` that does everything — and then the agent is playing a phrasing
guessing game against a natural-language interface. Agents are *much* better at
picking from a typed menu than at wording one universal question.

**F11 — A small set of sharply-typed MCP tools · 32–52h**

One fuzzy tool, the rest exact:

| Tool | Returns |
|------|---------|
| `search(query, budget)` | the fuzzy one — hybrid retrieval |
| `find_symbol(name)` | exact: definition and span |
| `who_calls(symbol)` | graph, one hop |
| `impact_of_change(symbol)` | graph, bounded expansion |
| `recall(anchor \| topic)` | memories, ranked by provenance |
| `remember(note, anchors, confidence)` | the write path |
| `claim(symbols, intent)` | advisory, heartbeat-scoped |
| `whos_working_on(area)` | fleet registry |
| `docs(topic \| symbol)` | documentation, anchored |

Nine tools an agent can hold in its head, each with an obvious answer shape.
That is the hive-mind endpoint — it just isn't a single function.

**⚠ R-C is LOW confidence and this paragraph rests on it.** It was written
against MCP revision **2025-11-25**, superseded by **2026-07-28**, which makes
authorization align with OAuth/OIDC rather than mandating it universally and
also makes the protocol core stateless — which undercuts the session-oriented
assumption below. **Fetch the current spec before planning F11.**

As written against the superseded revision: the MCP specification required
**OAuth 2.1 with PKCE** for any internet-reachable server and **explicitly
prohibited token passthrough** — we
may not accept an agent's token and forward it to GitHub or Supabase.

That prohibition is a gift. The token our MCP server mints is exactly where
F18's capability envelope lives: an agent authenticates and receives a token
scoped to its agent type's tools, memory scopes, repositories and paths. The
spec forbids the shortcut that would have let us defer designing the envelope,
so **F11 and F18 should be planned together rather than in sequence.**

**⚠ And one hardening requirement.** Of 30+ MCP CVEs filed in early 2026,
**43% were command injection** — and our tool list is almost entirely
string-taking tools. Every parameter is an injection surface. This repo already
has the lesson from another angle: `uuid.Parse` is a parser, not a validator,
and the fix was to validate the whole *class* of input rather than the one
instance a reviewer found. Same discipline here, designed in rather than added
after a finding.

(Context worth knowing: 25% of public MCP servers have no authentication at all
and 53% rely on static API keys. Implementing the spec properly is a
differentiator in this market, not table stakes.)

---

## 6. Part B continued — agent types

Added 2026-09-10 at the user's suggestion. Three parts with very different risk
profiles, worth separating before they get built as one thing.

**★ F18 — An agent type is a capability envelope, not a prompt · 24–36h**

A markdown file with instructions is a prompt; a gist hosts those for free. What
makes an agent type *ours* is that it also carries an enforced envelope:

- instructions — the part everyone already has
- which tools it may call
- **which credential scope it receives** — the reviewer that cannot push to main
- **which memory scopes it may read and write** — can it promote to fleet
  memory, or only propose?
- which repositories and paths it may touch

That is F6 arriving with a home.

**Research R-F settled the format, and found a distinction that matters.** There
are two formats in this space and conflating them would be a design error:

| Format | What it is | Shape |
|--------|-----------|-------|
| **AGENTS.md** | Cross-tool standard at repo root; how to build, test and change *this project* | Plain markdown, **no frontmatter** |
| **`.claude/agents/*.md`** | Claude Code subagent — a *role* | Markdown + **YAML frontmatter** |

**F18 imports the second.** AGENTS.md is project context, a different feature.

The good news is that Claude Code's frontmatter (`name`, `description`, `tools`,
`model`, permissions) is a **strict subset of our envelope** — we add credential
scope, memory scopes, and repository/path restrictions. So an existing Claude
Code subagent **imports unchanged**, and the fields it does not express get the
narrowest default — which is exactly the security rule below already requires
for third-party definitions. The format decision and the security decision turn
out to be the same decision.

Keep field names identical where they overlap, so import stays lossless.

**★ F19 — A registry where definitions carry evidence · 30–45h**

Every prompt-sharing site has become a junk drawer for one reason: no quality
signal. A thousand definitions, no way to tell which work, so the good ones are
unfindable and nobody comes back.

We can do what nobody else can: **we sit on the merge data.** F16 already tracks
which context fed tasks that succeeded; the GitHub App already sees what got
approved, what needed three review rounds, what got reverted. Attach that to
each definition and the registry stops being a list of prompts and becomes a
ranked list with evidence behind it.

Same principle as the retrieval redesign — *evidence over prose* — applied to
the marketplace itself. It is also why the ranking must exist in v1: ship an
unranked list and it fills with junk *before* the signal arrives, and then the
signal has nothing good left to surface.

**Schema constraint:** outcome stats aggregate across tenants, so they must be
counts only, behind a minimum-sample threshold, with nothing that could describe
a tenant's code.

**Cold start, answered by R-F:** the registry does not launch empty. Every
Claude Code subagent definition people have already written is a valid import,
so the seed corpus already exists in public repositories and gists.

**F20 — Topology is a file; the canvas is a view of it · 30–45h**

The motivating example — *planner, then decide which worker, then a specific
reviewer* — has a **decision in the middle of it**. That is not a static chain
of agent-to-agent edges, and modelling it as one will hurt within a month.

Model it as **routing rules over durable task objects** (F7). A task moves
through states; each state names a required capability; a rule picks which agent
type handles it based on the *task's own attributes*:

- touches `migrations/**` → the database reviewer
- adds an endpoint or touches auth → the security reviewer
- everything else → the general reviewer

This is what "the kind of review needed varies" was reaching for. Attribute
routing gives it, and keeps giving it at thirty agents, where hand-wired pairs
would need rewiring every time someone joins.

**And the topology must live in a versioned file in the repo, not in a database
behind a canvas.** A fleet configuration that exists only as UI state cannot be
code-reviewed, diffed, rolled back when it misbehaves, or run in CI. As a file
it gets exactly the treatment the code gets — the discipline this project
already runs on everywhere else.

Build the drag-and-drop canvas. Build it as a renderer and editor *of that
file*. Never the other way round — that is the difference between a tool and a
demo.

**F21 — The fleet dashboard, and why it is possible at all · 24–36h**

The ask was an easy, intuitive, observable way to run agents in parallel on one
goal. The thing worth noticing: **the no-chat-bus decision is what makes that
possible.**

If agents coordinate by talking to each other, the coordination lives in two
context windows and nowhere else — there is nothing to render, and the only way
to find out what happened is to read two transcripts. Because everything goes
through the blackboard instead, *every coordination act is already a durable
row.*

So the dashboard is not extra work layered on top. It is a rendering of state
the system needs anyway: a lane per agent, what each holds a claim on, task
states and what blocks them, and F3's conflict warnings drawn live between
lanes. The constraint bought the feature.

### Security consideration this whole section introduces

Once definitions are shareable, **an agent type is executable configuration
arriving from a stranger.** A downloaded definition can carry prompt-injection
payloads in its instructions and can request a broad capability envelope — and
the envelope is the dangerous half, because instructions only matter if the
tools are there to act on them.

Two rules to design in from the start, not retrofit:

1. An imported definition's requested envelope is **shown prominently at
   import** — "this agent asks for: write to main, read all memory scopes".
2. It is **granted the narrowest envelope by default** regardless of what it
   asked for, with each capability widened deliberately.

Treat a shared agent type the way a careful team treats a new dependency.

---

## 7. Part B continued — four more

**★ F14 — Trajectory memory · 20–30h**

Store the *sequence of actions* that resolved a task, not just the conclusion.
When an agent is about to do something similar, retrieval returns "here is how
this went last time — including the two approaches that didn't work and why."

Almost nobody does this well, and it is the highest-value thing a fleet
accumulates that a codebase doesn't already contain. The conclusion is often
re-derivable from the code. The path — and the dead ends — never is.

**F15 — Negative knowledge · 8–12h**

Record what *didn't* work. Agents repeat failed approaches constantly, and a
"we tried this, it failed because Y" memory is frequently worth more than a
positive one — it prunes a branch instead of suggesting one.

Cheap: a flag on a memory plus a ranking rule. Nearly free, and nobody captures
it.

**F16 — Attention accounting · 12–20h**

Track which memories were actually retrieved into a task that then succeeded.
Feeds ranking with real signal instead of embedding distance, and gives a
defensible answer to "is this working?" beyond thumbs up and down. Also feeds
the cost constraint: a memory nobody's retrieval ever uses is one we can stop
embedding. Feeds **F19**.

**F17 — Replay and audit · 12–20h**

Reconstruct exactly what context an agent held when it made a given decision.
Needed to debug the fleet, and needed before any team will let a fleet touch a
repository they care about.

---

## 8. Decisions blocking Phase 22

> **These are now settled in `DECISIONS.md`, with concrete schemas and a
> measurement.** What follows is the reasoning that led there, kept for
> context. Where the two differ, `DECISIONS.md` wins — most notably D2, which
> gained a partitioning requirement, and D4, which split confidence into two
> independent axes.

Everything else here can be decided later without cost. These four cannot,
because Phase 22 writes the first real rows and changing them afterwards means
re-ingesting every repository we've indexed.

### D1 — Do we add stable symbol identity now?
**Recommendation: yes.** (R2, 10–16h) Cheapest item with the largest downstream
unlock, and the one thing here genuinely expensive to retrofit.

### D2 — pgvector, or stay on Qdrant?
**Recommendation: pgvector, and partition `chunks` by organization.** (R6,
26–38h) One transaction, RLS over the vectors, one thing to back up — and it
closes the §2.1 asymmetry rather than papering over it. Confirmed on scale
grounds by R-B.

**The partitioning half is not optional and not a follow-up.** RLS makes every
vector query a filtered query, which is pgvector's worst case; partitioning
converts the tenant filter into partition pruning and removes the failure mode
structurally. Adding partitions after rows exist is a rewrite. See R6.

### D3 — Does ingest emit graph edges from day one?
**Recommendation: emit the events and build tier 1; defer SCIP.** (R3, 20–30h)
Phase 22 already carries a breadcrumb to define chunk-level event payloads so a
future graph worker can subscribe without pipeline changes — widen that payload
now to carry call-site and import candidates, cheap since the parser is already
walking the tree.

What must be decided now is only the **edge schema**: that it carries a
confidence score and a source tier, so tier-2 edges can upgrade tier-1 edges
later without a migration.

### D4 — Does the schema reserve room for memories?
**Recommendation: design the anchor model now, build the objects later.** (R7)
We only need `symbol_id` shaped so a memory can point at it. The memory tables
themselves can wait.

---

## 9. Recommended sequence

> **The risk in this whole document:** nothing has ingested a real repository
> end-to-end yet. Three of nine phases are done, the core experience is
> unproven, and everything above is design on top of an assumption. Designing a
> whole substrate before proving retrieval works on one real codebase is how a
> project acquires a beautiful architecture and no users.
>
> So: make the schema decisions now, because they're cheap now and expensive
> later — then **prove the core before building the fleet layers.**

| # | Step | Covers | Est. |
|---|------|--------|------|
| 1 | ~~Research~~ ✅ done; settle D1–D4 and ISS-016 | read EA-Graph first | 8–12h |
| 2 | Phases 21 and 22 with the decisions folded in | roadmap scope + R2, R5, R6, R3 tier 1 | roadmap |
| 3 | **Prove retrieval quality on one real repository** | index RAG-Doc itself; 30 questions with known answers; measure | 16–24h |
| 4 | Agent-facing retrieval surface | R1, R4, R7 | 34–54h |
| 5 | MCP server **with F18's envelope** | F11 + F18 | 56–88h |
| 6 | Fleet layer | F5, F2, F7, F4 | 72–108h |
| 7 | Registry, topology, dashboard | F19–F21 | 84–126h |
| 8 | Precise graph and conflict prediction | R3 tier 2, F3 | 40–70h |
| 9 | Docs as a rendering | F12, F13 | 30–50h |

Step 3 is the real gate. It is the step that tells us whether months of
retrieval work actually produce good answers, and it is the easiest to skip and
most expensive to have skipped.

Step 5 now carries F18, because R-C found that MCP forbids token passthrough —
so the envelope *is* the token the server mints, and the two cannot be
sequenced apart.

Step 8 no longer carries a sandbox. That was an artefact of the SCIP error
corrected in R3: tier 2 runs in the customer's CI, so nothing on the ingestion
path executes untrusted code. **F1's sandbox moves back to the fleet layer**
(step 6), where it isolates *agent* execution -- its original and only real
justification -- and **Phase 24 loses the nested-virtualization constraint**,
which materially widens the hosting options.

**A structural note on this ordering.** It deliberately puts the three most
defensible features — F3, F9, F19 — after the unglamorous work they depend on.
All three look like the headline features. None is buildable without symbol
identity, the graph, the registry and the merge data underneath. Building them
early would mean building demos of them.

The consolation is that all three become *cheap* once the substrate exists, and
none is available to anyone who skipped it. That is the moat: not the features,
the order.

---

## 10. Open questions

- **Documentation surfaces (F12, F13)** are described in §3.3 and step 9 but not
  yet costed as their own items beyond the 30–50h estimate. Needs the same
  treatment R1–R7 got.
- **Where does the human web UI fit** once retrieval is agent-shaped? Phase 23
  assumes the current search-and-chat model. Probably unchanged, but unverified.
- **Multi-repo graphs.** Everything here assumes one repository's graph.
  Cross-repo symbol resolution (a monorepo split across repos, or a shared
  internal library) is undesigned.
- **Cost model for the substrate.** PROJECT.md's constraint is LLM cost at
  scale. Memory writes and graph construction add embedding and storage cost per
  tenant that nothing currently models.
- **What fraction of real repositories build in a cold clone?** (R-A) Determines
  whether SCIP tier 2 is a headline feature or a bonus. Measurable: clone the top
  N public repos per language and count.
- **Does SCIP's symbol format map onto R2's `symbol_id`?** (R-A) If it does,
  tier 2 gets much cheaper.
- **Incremental indexing has no tier-2 equivalent.** (R-A) SCIP indexers run
  whole-project; Phase 22's changed-files-only re-index does not obviously
  compose with that.
- **Does partition pruning fire on `current_setting('app.current_tenant')`?**
  (R-B) It is runtime rather than plan-time pruning. Must be confirmed with
  `EXPLAIN ANALYZE` against a partitioned table inside a real tenant
  transaction — not assumed.

---

*Revision history*
- 2026-09-10 — initial draft, worker session
- 2026-09-10 — rev 2: added F18–F21 (agent types, registry, topology,
  dashboard); folded in research R-A, which revised R3 and D3 downward and
  pulled the sandbox forward into step 8
- 2026-09-10 — rev 4: D1–D4 settled in `DECISIONS.md`, including a measured
  answer to R-B's open pruning question. §8 is now context; `DECISIONS.md` is
  authoritative. R7's confidence model superseded by D4's two-axis model.
- 2026-09-10 — rev 3: folded in research R-B through R-G. Changed: D2 gained a
  partitioning requirement; R6 gained four implementation requirements; F1
  demoted to table stakes and given a microVM dependency; F10 split into
  explicit and implicit conflict; F11 gained an OAuth design and merged into a
  step with F18; F18's format decided; F19's cold start answered; the graph
  store confirmed with cycle handling flagged. See `RESEARCH.md` § *What the
  research changed*.

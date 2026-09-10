# v2 Substrate — Design

**Status:** Draft, under active research. Not yet a milestone.
**Started:** 2026-09-10, after Phase 20 closed at `0c28704`
**Owner:** worker session, at the user's direction
**Companion:** `RESEARCH.md` in this directory — agenda and findings.

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
| Tree-sitter parsing | Built | Python, Go, TypeScript, JavaScript. Extracts *definitions* — functions, classes, docstrings, ancestor chain, `breadcrumb`. **No references, calls, or imports.** |
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

**This is not a live exploit.** `repository_id` is a required argument and
callers resolve the repository under RLS first. The problem is the failure
shape: if that check were ever missed, Postgres returns zero rows and Qdrant
returns another tenant's code. One store fails safe, the other fails open —
and the one that fails open has no schema hook to fix later.

Feeds **R6** and **D2** directly.

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
| 2 | SCIP indexers | repos that build | precise | a hardened sandbox |

Tier 2 edges **upgrade** tier-1 edges in place rather than replacing the graph.
The per-edge confidence score — originally proposed so retrieval could rank on
edge quality — turns out to be the mechanism that lets both tiers coexist.

Tier 1 belongs in the pipeline now. Tier 2 slips to its own phase and gets its
sandbox free, because **F1 builds the same sandbox for agent execution**.

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

### R6 — Collapse Qdrant into Postgres with pgvector · 20–30h · **decide before 22**

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

*Pending research R-B before this is final.*

### R7 — Memories as first-class objects · 16–24h

The write path. A **memory** is a note, decision, gotcha or convention that:

- anchors to zero or more symbols or files
- carries provenance — which agent, which session, which commit, which PR
- carries a confidence
- has a lifecycle — `proposed → confirmed → stale → contradicted`

Retrieval then searches code and memories together, ranking memories on
provenance and corroboration rather than embedding distance alone. This one
object is the difference between a search product and a substrate.

---

## 5. Part B — fleet infrastructure

Two of these are genuinely differentiated — possible only because we hold both
the code graph and the fleet registry, which nobody else does. Marked ★.

### Parallel safety

**F1 — Worktree per agent, container per run · 30–50h**

Two things people conflate. *Isolation of edits* is a git worktree — cheap,
shares the object store, native, each agent gets its own branch and working
directory free. *Isolation of execution* — running tests, running the agent's
code, and (per R-A) running SCIP's build commands on untrusted repos — is a
container, a different problem with different failure modes.

Worktrees first. They solve the day-one problem: two agents stepping on each
other's files.

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

**F10 — Contradiction detection · 12–20h**

Two memories on the same anchor that disagree. Surface the conflict rather than
silently returning both — an agent handed contradictory context performs *worse*
than one handed nothing, because it picks one at random and proceeds with full
confidence.

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
is swappable behind it. *Pending research R-D.*

**One failure mode to design around:** "one endpoint" can collapse into a single
`query()` that does everything — and then the agent is playing a phrasing
guessing game against a natural-language interface. Agents are *much* better at
picking from a typed menu than at wording one universal question.

**F11 — A small set of sharply-typed MCP tools · 24–40h**

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

That is F6 arriving with a home. Markdown-with-frontmatter is the right format —
it is what Claude Code's own `.claude/agents/*.md` already uses, so importing
one is a file upload and nothing else, and definitions people have already
written come across unchanged.

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

Everything else here can be decided later without cost. These four cannot,
because Phase 22 writes the first real rows and changing them afterwards means
re-ingesting every repository we've indexed.

### D1 — Do we add stable symbol identity now?
**Recommendation: yes.** (R2, 10–16h) Cheapest item with the largest downstream
unlock, and the one thing here genuinely expensive to retrofit.

### D2 — pgvector, or stay on Qdrant?
**Recommendation: pgvector.** (R6, 20–30h) One transaction, RLS over the
vectors, one thing to back up — and it closes the §2.1 asymmetry rather than
papering over it. *Confirm against research R-B before locking.*

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
| 1 | Finish research, settle D1–D4 and ISS-016 | R-B…R-G | 32–46h + 8–12h |
| 2 | Phases 21 and 22 with the decisions folded in | roadmap scope + R2, R5, R6, R3 tier 1 | roadmap |
| 3 | **Prove retrieval quality on one real repository** | index RAG-Doc itself; 30 questions with known answers; measure | 16–24h |
| 4 | Agent-facing retrieval surface | R1, R4, R7 | 34–54h |
| 5 | MCP server | F11 | 24–40h |
| 6 | Fleet layer | F5, F2, F7, F4 | 72–108h |
| 7 | Agent types, topology, dashboard | F18–F21 | 108–162h |
| 8 | Sandbox, precise graph, conflict prediction | F1, R3 tier 2, F3 | 70–110h |
| 9 | Docs as a rendering | F12, F13 | 30–50h |

Step 3 is the real gate. It is the step that tells us whether months of
retrieval work actually produce good answers, and it is the easiest to skip and
most expensive to have skipped.

Step 8 is one sandbox with two payoffs: it isolates agent execution *and* it is
what lets us run SCIP's build-dependent indexers on untrusted customer code.

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

---

*Revision history*
- 2026-09-10 — initial draft, worker session
- 2026-09-10 — rev 2: added F18–F21 (agent types, registry, topology,
  dashboard); folded in research R-A, which revised R3 and D3 downward and
  pulled the sandbox forward into step 8

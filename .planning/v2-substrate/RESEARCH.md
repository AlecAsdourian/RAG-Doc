# v2 Substrate — Research

**Companion to** `DESIGN.md` in this directory.
**Started:** 2026-09-10

Findings are appended as they land. Each topic states **what it settles** up
front, so a reader can tell whether it still matters before reading it.

---

## Agenda

| Topic | Settles | State | Outcome |
|-------|---------|-------|---------|
| **R-A** Code graph construction | D3, and the cost of R3 | ✅ Done | Revised R3 down; pulled the sandbox forward |
| **R-B** pgvector at our scale | D2 | ✅ Done | Confirmed pgvector; **added partitioning to D2**. One claim later corrected by measurement — see `DECISIONS.md` § 5 |
| **R-C** Multi-tenant MCP | F11 | ✅ Done | OAuth 2.1 + PKCE mandatory; F11 and F18 coupled |
| **R-D** Graph queries in Postgres | The no-separate-graph-DB call | ✅ Done | Confirmed; cycle handling is a correctness item |
| **R-E** Sandboxing and orchestration | F1, and R3 tier 2 | ✅ Done | Worktrees are table stakes; Firecracker named |
| **R-F** Agent definition formats | F18, F19, F20 | ✅ Done | Claude Code subagent schema; AGENTS.md is separate |
| **R-G** Memory invalidation prior art | F9 | ✅ Done | Externally validated; F10 sharpened |

All seven completed 2026-09-10. See **What the research changed** at the end of
this document for the consolidated diff against `DESIGN.md`.

---

## R-A — Code graph construction

**Researched:** 2026-09-10
**Settles:** D3, and the cost of R3
**Confidence:** HIGH on what exists; MEDIUM on the operational cost of running
it against arbitrary customer repositories, which we have not measured.

### Summary

The plan going in was to hand-write per-language reference resolvers — imports
plus scope matching — costed at 30–50h for our four languages. **That was the
wrong plan.** A standard format with mature indexers already covers exactly our
language set.

The finding has two halves, and the second half is the one that changed the
roadmap.

### What exists

**SCIP** (pronounced "skip") is a language-agnostic source indexing protocol
introduced by Sourcegraph in June 2022. As of 2026 it has moved from a
Sourcegraph-owned project to independent governance with a Core Steering
Committee including engineers from Uber and Meta — so it is not a
single-vendor format we would be betting on.

Indexers relevant to us:

| Language | Indexer | Our use |
|----------|---------|---------|
| Go | `scip-go` | backend |
| Python | `scip-python` | workers |
| TypeScript + JavaScript | `scip-typescript` | frontend — one tool, both languages |

Others exist (`scip-java`, `scip-clang`, `scip-ruby`, `scip-dotnet`,
`scip-dart`, `scip-php`, and `rust-analyzer` emits SCIP natively), which matters
for the SaaS story: customer repos are not limited to our four languages, and
the marginal cost of a new language becomes "wire up an existing indexer"
rather than "write a resolver."

SCIP emits precise **definitions and references** — which is the whole graph.

### The catch

**Every SCIP indexer requires the repository's build environment.**

| Indexer | Requires |
|---------|----------|
| `scip-typescript` | `npm install` first; a `package.json` or `tsconfig.json`; Node 18 or 20 |
| `scip-python` | an activated virtualenv |
| `scip-go` | a Go toolchain that can build the app (Bazel/Buck via the Go Packages Driver Protocol) |

Three consequences for a SaaS that clones arbitrary customer repositories:

1. **Many repos will not build for us.** Private dependencies, pinned versions,
   required secrets, unusual build systems. This is not an edge case — it is
   probably the majority for a cold clone with no credentials.
2. **Memory.** Both `scip-typescript` and `scip-python` OOM on large trees;
   `scip-typescript`'s cross-project symbol cache is itself the memory cost, and
   the documented workaround is `NODE_OPTIONS="--max-old-space-size=8192"` plus
   sharding by project file. Per-repo resource limits are not optional.
3. **⚠ Running an indexer executes untrusted code.** `npm install` runs
   `postinstall` scripts; Python packaging runs `setup.py`. On arbitrary
   customer code, in a multi-tenant service, that is arbitrary code execution by
   design. **This needs a hardened sandbox, which was not on the roadmap.**

Point 3 is the finding that moved the plan. It was not guessable from the
outside, and it converts "run SCIP in the ingest worker" from a small task into
one that depends on infrastructure we had scheduled much later.

### The alternative that has the opposite trade

**stack-graphs** (GitHub, built on tree-sitter) resolves names *without* the
build environment — that is explicitly its design goal: name-binding rules
declared in a DSL, "efficient, incremental, and does not need to tap into
existing build or program analysis tools." It powers GitHub's own code
navigation at sub-100ms for named symbols.

The trade is coverage and precision: language definitions exist for Python,
JavaScript and TypeScript — **notably not Go**, which is our backend — and
resolution is approximate where SCIP is precise.

### Prior art worth reading before building

`stakwork/stakgraph` — "a source code parser using treesitter, LSP, and neo4j,
powering software knowledge graphs for AI agents." Directly adjacent to what
§5/F3 describes. Not evaluated yet; worth a read before we design the edge
schema, specifically for how they model confidence and handle partial
resolution.

Also noted: OpenHands has an open issue exploring stack graphs for repo mapping
and agent context — the same problem from the agent side.

### Recommendation

**Two tiers over one graph.**

| Tier | Method | Coverage | Accuracy | Needs |
|------|--------|----------|----------|-------|
| 1 | tree-sitter + import resolution + scope matching | 100% of repos | ~80%, confidence-scored | nothing new |
| 2 | SCIP indexers | repos that build | precise | a hardened sandbox |

Tier 2 **upgrades** tier-1 edges in place rather than replacing the graph. The
per-edge confidence score — originally proposed so retrieval could rank on edge
quality — turns out to be the mechanism that lets both tiers coexist in one
table.

Do not adopt stack-graphs as tier 1 despite the appealing build-free property:
it does not cover Go, so we would still hand-write the Go path, and we would
carry a Rust dependency and a DSL to learn for the languages it does cover.
Tier 1 as described is simpler and we control its confidence model. Revisit if
tier 1's accuracy measures worse than expected.

### Effect on the plan

- **R3 drops from 30–50h to 20–30h** for tier 1, plus a later phase for tier 2.
- **D3 changes** from "defer the resolver" to "build tier 1 now, defer SCIP" —
  and the thing that must be decided now narrows to the *edge schema* carrying a
  confidence score and a source tier.
- **F1 (sandboxing) moves earlier and gets more valuable**: one sandbox
  isolates agent execution *and* runs SCIP's build commands on untrusted code.
  Two payoffs, one build.

### Open questions this leaves

- What fraction of real repositories actually build in a cold clone with no
  credentials? Determines whether tier 2 is a headline feature or a bonus for
  well-configured repos. **Measurable** — clone the top N public repos in each
  language and count. Worth doing before tier 2 is scheduled.
- Does SCIP's symbol format map cleanly onto R2's `symbol_id`, or do we need a
  translation layer? If it maps, tier 2 gets much cheaper.
- Incremental indexing: SCIP indexers run whole-project. Phase 22's incremental
  re-index (changed files only) has no obvious tier-2 equivalent.

### Sources

- [The future of SCIP | Sourcegraph](https://sourcegraph.com/blog/the-future-of-scip)
- [SCIP Code Intelligence Protocol](https://scip-code.org/)
- [sourcegraph/scip](https://github.com/sourcegraph/scip)
- [scip-go](https://github.com/sourcegraph/scip-go)
- [scip-python](https://github.com/sourcegraph/scip-python)
- [scip-typescript](https://github.com/sourcegraph/scip-typescript)
- [Index a Go repository — Sourcegraph docs](https://sourcegraph.com/docs/code-navigation/how-to/index-a-go-repository)
- [Introducing stack graphs — The GitHub Blog](https://github.blog/open-source/introducing-stack-graphs/)
- [github/stack-graphs](https://github.com/github/stack-graphs)
- [stakwork/stakgraph](https://github.com/stakwork/stakgraph)
- [OpenHands issue #742 — stack graphs for code search / repo map](https://github.com/OpenHands/OpenHands/issues/742)

---

## R-B — pgvector at our scale

**Researched:** 2026-09-10
**Settles:** D2
**Confidence:** HIGH on the mechanism and the mitigations; MEDIUM on our own
numbers, which are estimates and should be measured.

### Summary

**D2's recommendation stands — pgvector — but naively adopting it would have
shipped a silent recall bug**, and that bug is the same failure class this
project has already been bitten by twice.

### Scale: pgvector is comfortably the right choice

| Source | Finding |
|--------|---------|
| Supabase benchmarks | pgvector HNSW **matches or beats Qdrant** on equivalent compute at 1M vectors, at 99% accuracy |
| Qdrant published | ~850 QPS p95, 8ms latency at 1M vectors; ~12ms p99 at 10M |
| Consensus guidance | Default to pgvector if already on Postgres and under **~10–50M vectors**; Qdrant when filtering/latency is critical or scale is beyond that |

Our estimate: a repository produces order 5k–50k chunks. 100 organizations × 5
repositories × 20k chunks ≈ **10M vectors** — the top of pgvector's comfortable
range, and far beyond anything v1 or v2 will see. Near-term we are well under
1M.

**Threshold to watch: ~10M vectors.** Above that, revisit. `pgvectorscale` is
the intermediate step before leaving Postgres.

### ⚠ The finding that matters: RLS makes every query a filtered query

**The HNSW index is not security-aware.** Postgres injects RLS policies into the
plan as *security quals*, evaluated on candidate rows the index already
returned. pgvector walks the HNSW graph first, produces the top `ef_search`
approximate candidates, and applies the filter afterwards.

When the filter is selective — one organization out of many, which is *exactly*
our RLS policy — the candidate set the index handed back may contain very few
rows that pass. The result is **fewer rows than the requested LIMIT, or silently
degraded recall, with no error.**

This is the same shape as two bugs this project has already shipped and caught:

- migration 000012's `SECURITY DEFINER` lookup, which worked in the harness
  (superuser-run migrations) and returned zero rows in the deployment shape;
- the 20-05 re-claim rule, which passed locally three times and broke under
  twelve concurrent redeliveries in CI.

In all three cases the code is correct in the shape it was tested and wrong in
the shape it deploys. Here the trap is sharper still: **a single-tenant test
database has no selective filter**, so every test passes at full recall and
production quietly returns worse results forever.

### Three mitigations, and they compose

**1. Partition `chunks` by organization.** The planner prunes to the tenant's
partition, and each partition carries its own smaller HNSW index — so the tenant
filter becomes *partition pruning* instead of a post-index filter, and the
problem is structurally gone rather than tuned around. Documented as the pattern
for "1000s of tenants"; the single-table + composite-index pattern is described
as good to roughly 100 tenants.

This is cheap now and expensive later, exactly like R2. **It should be part of
D2's answer, not a follow-up.**

*To verify:* partition pruning on `current_setting('app.current_tenant')` is
runtime pruning, not plan-time, because the value is not a constant at planning.
`current_setting` is STABLE so initial pruning at execution start should apply —
but this must be confirmed with `EXPLAIN ANALYZE` against a partitioned table
under a real tenant transaction, not assumed.

**2. `hnsw.iterative_scan`** (pgvector 0.8+). Keeps pulling candidates from the
index until enough rows pass the filter.

> **⚠ Corrected by measurement, 2026-09-10.** This section presented iterative
> scan as the primary mitigation. It was measured to change **nothing** —
> 8/10 recall with and without it — while partitioning moved recall to 10/10.
> Keep the setting; it is cheap and helps in other shapes. It is not the fix.
> See `DECISIONS.md` § 5.
 `strict_order` when exact distance
ordering matters, `relaxed_order` for speed. Costs CPU, memory and tail latency.
Tune with `max_scan_tuples` and `hnsw.scan_mem_multiplier` (a multiple of
`work_mem`).

**3. A btree index on the filter column** alongside the HNSW index — pgvector's
own recommendation.

**4. And the one that actually catches regressions: a recall test in the shape
we deploy.** Seed many tenants, query as one, and measure recall against an
exact-search baseline. A single-tenant fixture cannot detect this failure by
construction.

### Effect on the plan

- **D2 confirmed: pgvector** — for the reasons already stated (one transaction,
  RLS over vectors, one store to back up), now with scale evidence behind it.
- **D2 grows a requirement:** partition `chunks` by organization from day one.
- **R6 grows three implementation notes** (iterative scan, btree companion
  index, multi-tenant recall test) and roughly 4–8h.

### Sources

- [Scaling vector search in Postgres — ClickHouse](https://clickhouse.com/resources/engineering/scale-vector-search-postgres)
- [Tuning pgvector Performance — ParadeDB](https://www.paradedb.com/learn/postgresql/tuning-pgvector)
- [pgvector Limitations — ParadeDB](https://www.paradedb.com/learn/postgresql/pgvector-limitations)
- [pgvector issue #721 — HNSW bypassed when filter selectivity exceeds threshold](https://github.com/pgvector/pgvector/issues/721)
- [pgvector issue #980 — filtered HNSW / segment-level indexes](https://github.com/pgvector/pgvector/issues/980)
- [Building successful multi-tenant RAG applications — Nile](https://www.thenile.dev/blog/multi-tenant-rag)
- [pgvector vs Qdrant in 2026 — Encore](https://encore.dev/articles/pgvector-vs-qdrant)
- [Postgres RLS footguns — Bytebase](https://www.bytebase.com/blog/postgres-row-level-security-footguns/)
- [Hard multi-tenancy for pgvector with RLS](https://pradeepbhandari.com/blog/pgvector-multi-tenancy-postgresql-rls-guide)

---

## R-C — Multi-tenant MCP

**Researched:** 2026-09-10
**Settles:** F11
**Confidence:** HIGH on the spec requirements; MEDIUM on ecosystem support,
which is moving.

### Summary

The MCP specification (November 2025 revision) **requires OAuth 2.1 with PKCE**
(S256) for any MCP server reachable over the internet, and **explicitly
prohibits token passthrough**. Both requirements happen to push us toward the
architecture F18 already wanted.

### What the spec requires

- OAuth 2.1 + PKCE, no exceptions for internet-reachable servers.
- **No token passthrough.** We may not accept an agent's token and forward it
  to GitHub or Supabase. The MCP server must mint its own credentials.

That second rule is a gift rather than a constraint: *the token the MCP server
issues is exactly where F18's capability envelope belongs.* An agent
authenticates, and what comes back is a token scoped to its agent type's
envelope — tools, memory scopes, repositories, paths. The spec forbids the
shortcut that would have let us skip designing the envelope.

### The ecosystem is in bad shape, which is worth knowing

From 2026 security surveys:

- 30+ MCP-related CVEs filed in early 2026; **43% were command injection**.
- **53%** of open-source MCP servers rely on static API keys or PATs rather than
  OAuth.
- **25%** of public MCP servers have **no authentication at all**.

Two implications for us. First, "we implement the spec properly" is a genuine
differentiator in this market, not table stakes. Second, **command injection is
the dominant CVE class**, and our tool list is full of string-taking tools —
`find_symbol(name)`, `who_calls(symbol)`, `docs(topic)`. Every one of them is an
injection surface. This repo already has the lesson written down from a
different angle: `uuid.Parse` is a parser, not a validator, and the fix was to
validate the *class* of input rather than the one instance found. Same discipline
applies to every MCP tool parameter.

### Architecture note

The **gateway pattern** is the dominant enterprise deployment shape in 2026: one
centralized proxy handles token validation, scope-based routing, and a unified
audit log, rather than each MCP server implementing OAuth itself. Worth adopting
if we ever run more than one MCP server; premature at one.

### Effect on the plan

- **F11 grows an auth design** — OAuth 2.1 + PKCE, own-credential minting. Add
  roughly 8–12h; F11 becomes ~32–52h.
- **F11 and F18 are more coupled than the design assumed.** The envelope is not
  a nice-to-have layered on later; it is the content of the token the MCP server
  issues. They should be planned together.
- **Every MCP tool parameter needs input validation designed in**, not added
  after a finding.

### Sources

- [Diving into the MCP authorization specification — Descope](https://www.descope.com/blog/post/mcp-auth-spec)
- [MCP OAuth 2.1 authentication guide 2026](https://baeseokjae.github.io/posts/mcp-oauth-authentication-guide-2026/)
- [Multi-tenant MCP servers: auth, tenancy, rate limiting — PADISO](https://www.padiso.co/blog/multi-tenant-mcp-servers-auth-tenancy-rate-limiting/)
- [Multi-tenant MCP for SaaS — Albato](https://albato.com/blog/publications/embedded-multi-tenant-mcp-saas)
- [MCP authentication explained — Maxim](https://www.getmaxim.ai/articles/mcp-authentication-explained-oauth-api-keys-and-token-management/)

---

## R-D — Graph queries in Postgres

**Researched:** 2026-09-10
**Settles:** the no-separate-graph-DB call
**Confidence:** HIGH

### Summary

**Confirmed: build the graph as Postgres tables.** Our query shape is the one
case where Postgres is reported to *win*, not merely suffice.

The decisive line from the literature: *"Fan-out from a node to assemble context
for RAG? Postgres recursive CTEs are faster and one less system."* That is
precisely `who_calls` and `impact_of_change` — bounded expansion, one to three
hops, to fill a token budget.

### Where Postgres holds

- A properly indexed edge table scales to **tens of millions of edges** with
  sub-second responses at typical depths.
- One store, one backup, one isolation model. Under RLS, which a graph engine
  would not give us.

### Where it breaks, precisely

- **Deep traversal / path enumeration.** Measured comparison: Postgres recursive
  CTE dragging a path array hit **p50 334ms, p95 1.8s**, against Neo4j's 28ms.
  Neo4j's index-free adjacency makes a hop a pointer dereference; in Postgres it
  is a join.
- **Dense relationships** degrade it further.
- **Genuine graph algorithms** — PageRank, community detection, weighted
  shortest path — are not a contest. That is what a graph engine exists for.
- **The recursive executor cannot maintain visited state across iterations**, so
  it cannot skip already-explored nodes and collapses duplicates only at the end.

### ⚠ Implementation note: call graphs have cycles

Recursive functions and mutual recursion make the call graph cyclic. Because the
recursive executor keeps no visited set, a naive `WITH RECURSIVE` over call
edges **will not terminate** on a cycle. Use the `CYCLE` clause (PostgreSQL 14+)
or carry an explicit path array with a membership check. This is a correctness
requirement, not a tuning one.

### The trigger condition for revisiting

Adopt a graph engine when we want a query that is an *algorithm* rather than a
*traversal*:

- "what are the most central symbols in this repository" (PageRank)
- "what are the natural module boundaries" (community detection)
- unbounded path enumeration

None of those is on the roadmap. Several are plausible v3 features — automatic
architecture summaries, suggested module boundaries — so this is a
revisit-later, not a never.

### Effect on the plan

- **Recommendation confirmed unchanged**, now with the boundary named.
- **Add cycle handling to the edge-traversal design** — a correctness item.
- Keep the graph query interface abstract so the engine stays swappable, as
  already planned.

### Sources

- [Graph queries with recursive CTEs — you don't need Neo4j](https://medium.com/codex/graph-queries-with-recursive-ctes-you-dont-need-neo4j-3aade6fb7f85)
- [Postgres vs Neo4j — PuppyGraph](https://www.puppygraph.com/learn/postgres-vs-neo4j)
- [When does a knowledge graph beat vector search — Pedro Alonso](https://www.pedroalonso.net/blog/graphrag-vs-vector-postgres/)
- [Your PostgreSQL already has a graph engine](https://dev.to/ineron/your-postgresql-already-has-a-graph-engine-you-just-have-to-build-it-2ng7)

---

## R-E — Sandboxing and orchestration

**Researched:** 2026-09-10
**Settles:** F1, and R3 tier 2
**Confidence:** HIGH

### Summary

Two findings, and the second one should change how we spend effort.

### Finding 1: a container is not a sandbox for untrusted code

Containers share the host kernel. The Linux kernel exposes roughly 350
syscalls, and one exploitable bug in any of them is a container escape. For
running `npm install` on arbitrary customer repositories (R-A tier 2) or
executing agent-authored code (F1), that is not adequate isolation.

| Option | Isolation | Startup | Overhead | Use when |
|--------|-----------|---------|----------|----------|
| **Firecracker** microVM | Own guest kernel on KVM — attacker must escape guest kernel *and* break out of the VM | ~125ms | <5 MiB/VM, up to 150 VMs/sec/host | untrusted, syscall-heavy code — **our case** |
| **gVisor** | Userspace syscall interception (Sentry); only a vetted subset reaches the host | ~50ms | more memory-efficient | KVM unavailable |
| Plain container | Shared kernel | fastest | lowest | trusted code only |

Firecracker powers AWS Lambda and Fargate. **Recommendation: Firecracker for
both agent execution and SCIP indexing**, gVisor as the fallback where KVM is
not available (which may constrain the Phase 24 deploy-target decision — worth
flagging to that phase).

### Finding 2 — ⚠ worktree orchestration is table stakes, not differentiation

Git worktrees for parallel agents are **the established default in 2026**, not a
novel idea:

- JetBrains shipped first-class worktree support in 2026.1 (March 2026); VS Code
  in July 2025; Cursor in 2026.1.
- Claude Code has `--worktree` / `-w` for isolated sessions plus subagent
  isolation in separate worktrees.
- **Intent, AQ, Atlas, Nimbalyst and Warp's cloud orchestration already automate
  worktree creation, assignment, review and cleanup**, treating the worktree as
  the unit of isolation and building scheduling, diff review and merge gating on
  top.

Practitioners report 4–5 agents routinely, and 12 parallel sessions on one
48 GB machine.

**This confirms F1's choice and removes it as a differentiator.** We should
adopt the standard pattern as cheaply as possible and spend the differentiation
budget on F2, F3 and F9, which nobody in that list has.

The literature also independently confirms the F1 split between edit isolation
and runtime isolation: *"Git worktrees alone are not enough to stop one task's
runtime from trampling another task's ports, databases, caches, secrets, or test
state."* And it names the gap we would be filling: *"a good multi-agent
orchestration tool would combine the speed of local worktrees with the isolation
of cloud environments, plus coordination features that neither has."*

That sentence is close to a description of F1 + F2 + F3 + F5.

### Effect on the plan

- **F1 scope narrows and its ambition drops.** Adopt the standard worktree
  pattern; do not invent one. Possibly 20–35h rather than 30–50h.
- **Firecracker (or gVisor) is a named dependency** for F1's execution half and
  for R3 tier 2 — one build, two payoffs, as already sequenced in step 8.
- **Phase 24's deploy-target decision gains a constraint:** the target must
  offer nested virtualization / KVM, or we fall back to gVisor. Several managed
  platforms do not.
- **Strategic:** worktree plumbing is not where this product wins. F3 and F9
  are.

### Sources

- [How to sandbox AI agents in 2026 — Northflank](https://northflank.com/blog/how-to-sandbox-ai-agents)
- [Your container is not a sandbox: microVM isolation in 2026](https://emirb.github.io/blog/microvm-2026/)
- [Firecracker vs gVisor: which sandbox in 2026?](https://www.alekseialeinikov.com/en/blog/topics/devops/microvms-firecracker-vs-gvisor-secure-workloads-2026)
- [AI agent sandboxing compared — amux](https://amux.io/guides/ai-agent-sandboxing/)
- [Git worktrees for parallel AI agent execution — Augment Code](https://www.augmentcode.com/guides/git-worktrees-parallel-ai-agent-execution)
- [Git worktrees need runtime isolation for parallel AI agent development](https://www.penligent.ai/hackinglabs/git-worktrees-need-runtime-isolation-for-parallel-ai-agent-development/)

---

## R-F — Agent definition formats

**Researched:** 2026-09-10
**Settles:** F18, F19, F20
**Confidence:** HIGH

### Summary

There are **two different formats** in this space and conflating them would be a
design error. F18's import path should target the second.

| Format | What it is | Shape | Our use |
|--------|-----------|-------|---------|
| **AGENTS.md** | Cross-tool standard at repo root; tells any agent how to build, test and change *this project* | Plain markdown, **no frontmatter**, no required fields | Project context — orthogonal to agent types |
| **`.claude/agents/*.md`** | Claude Code subagent definition — a *role* | Markdown + **YAML frontmatter** | **This is what F18 imports** |

Claude Code subagents live in `.claude/agents/` (project) or `~/.claude/agents/`
(machine). Frontmatter declares `name`, `description`, `tools`, `model`, and
permissions; the body becomes the system prompt.

Note: Claude Code still loads `CLAUDE.md` rather than `AGENTS.md` as of August
2026, with the convention being a first-line `@AGENTS.md` import to bridge them.

### Why this is good news for F18

Claude Code's frontmatter is **a subset of our capability envelope**:

| Claude Code frontmatter | Our envelope |
|---|---|
| `name`, `description` | same |
| `tools` | same |
| `model` | same |
| permissions | same |
| — | **credential scope** (the reviewer that cannot push) |
| — | **memory scopes** — read/write, and may-it-promote |
| — | **repository and path restrictions** |

So the import story is clean and honest: **an existing Claude Code subagent
imports unchanged**, and the fields it does not express are granted the
narrowest default — which is exactly the security rule §6 already requires for
third-party definitions. The format decision and the security decision turn out
to be the same decision.

It also means F19's registry starts non-empty: every subagent definition people
have already written for Claude Code is a valid import.

### Effect on the plan

- **F18 format decided:** markdown + YAML frontmatter, superset of Claude Code's
  subagent schema. Keep field names identical where they overlap so import is
  lossless.
- **Do not use AGENTS.md for agent types.** If we support it at all, it maps to
  *project* context, which is a different feature.
- **F19 seeding:** the existing corpus of Claude Code subagent definitions is
  the registry's cold-start answer.

### Sources

- [Claude Code subagents: complete 2026 reference — The Prompt Shelf](https://thepromptshelf.dev/blog/claude-code-subagents-complete-reference-2026/)
- [AGENTS.md spec (2026): AGENTS.md vs CLAUDE.md vs .cursorrules — Morph](https://www.morphllm.com/agents-md-guide)
- [Build custom sub-agents in Claude Code: YAML, tools, triggers — MindStudio](https://www.mindstudio.ai/blog/build-custom-sub-agents-claude-code-yaml)
- [Claude Code subagents: a 2026 practical guide — Tembo](https://www.tembo.io/blog/claude-code-subagents)

---

## R-G — Memory invalidation prior art

**Researched:** 2026-09-10
**Settles:** F9, and sharpens F10
**Confidence:** HIGH — this is the best-supported item in the document.

### Summary

F9's core claim — *invalidation is what makes an agent memory viable, and
staleness is more dangerous than absence* — is not a hunch. It is an actively
researched problem in 2026 with named failure modes, measured prevalence, a
benchmark, and at least one directly competing design.

### The problem is measured

**"Context rot"** — the divergence between AI configuration files and the
codebases they describe — was found in **23.0% of 356 repositories analyzed**
(95% confidence, 5% margin). Nearly a quarter of repositories carry stale code
references in their agent-facing documentation.

The mechanism is exactly the one F9 and R5 address:

> *"While missing elements announce themselves through errors, stale elements do
> not — a changed value can produce a silent error in any behavior that reads
> it."*

And on why retrieval alone cannot fix it:

> *"RAG has no model of time, and when a fact changes — such as when a function
> is renamed or a configuration value is bumped — RAG retrieves both the stale
> and current value with near-identical embedding similarity."*

That is a precise statement of why R5 (freshness as a returned field) and F9
(anchored invalidation) are not optional polish. Embedding similarity cannot
distinguish the old truth from the new one.

### ⚠ F10 was under-specified

The literature separates two kinds of conflict, and the design only described
the easy one:

- **Explicit conflict** — two memories on the same anchor that disagree. This is
  what F10 currently describes, and it is the detectable case.
- **Implicit conflict** — *"a later observation invalidates an earlier memory
  without explicit negation, requiring contextual inference to detect."* This is
  named as **the critical failure mode**, and there is a benchmark of 400
  expert-validated conflict scenarios.

Our anchoring model gives us a partial answer the general case lacks: when the
anchor's content hash changes, we know *something* invalidated the memory even
if we cannot infer *what*. That is weaker than semantic conflict detection and
stronger than nothing, and F10 should be honest about the distinction rather
than claiming to solve implicit conflict.

### Prior art to read before designing F9

- **EA-Graph: Artifact-Anchored Verification Memory for Coding Agents under
  Upstream Drift** (arXiv 2608.04278) — directly our F9. Read before finalizing
  the anchor schema.
- **Temporal Validity in Retrieval Memory** (arXiv 2606.26511) — a
  "deterministic supersession layer that RAG cannot match by construction."
  Relevant to the memory lifecycle state machine.
- **STALE: Can LLM Agents Know When Their Memories Are No Longer Valid?**
  (arXiv 2605.06527) — **a benchmark we can evaluate against**, which turns F9
  from an assertion into something measurable.
- **Context Rot in AI-Assisted Software Development** (arXiv 2606.09090) — the
  23% figure and the methodology behind it.

### Effect on the plan

- **F9 validated**, with external evidence to cite and prior art to build on
  rather than guess at.
- **F10 sharpened:** distinguish explicit from implicit conflict; claim only the
  former, and use anchor-hash change as a partial signal for the latter.
- **A measurable success criterion exists.** The STALE benchmark gives step 3 of
  the sequence (prove the core) a second axis beyond retrieval quality: can the
  substrate tell when its own memories went stale?
- **Read EA-Graph before the anchor schema is locked** — this is a research task
  to slot before D4 is final.

### Sources

- [Context Rot in AI-Assisted Software Development (arXiv 2606.09090)](https://arxiv.org/html/2606.09090)
- [EA-Graph: Artifact-Anchored Verification Memory (arXiv 2608.04278)](https://arxiv.org/html/2608.04278v1)
- [Temporal Validity in Retrieval Memory (arXiv 2606.26511)](https://arxiv.org/html/2606.26511v1)
- [STALE: Can LLM Agents Know When Their Memories Are No Longer Valid? (arXiv 2605.06527)](https://arxiv.org/abs/2605.06527)
- [awesome-harness-engineering](https://github.com/ai-boost/awesome-harness-engineering)

---

## What the research changed

A summary for anyone reading `DESIGN.md` who wants to know which parts moved.

| Item | Before research | After |
|------|-----------------|-------|
| **R3** | 30–50h of hand-written per-language resolvers | Two tiers; 20–30h now, SCIP deferred behind a sandbox |
| **D3** | "defer the resolver" | "build tier 1 now, defer SCIP"; decide only the edge schema |
| **R6 / D2** | pgvector, on consistency grounds | pgvector confirmed on scale evidence too — **plus partition by organization from day one**, iterative scan, and a multi-tenant recall test |
| **F1** | 30–50h, implicitly novel | 20–35h, **table stakes**; adopt the standard pattern, spend differentiation elsewhere. Firecracker named. |
| **F10** | "two memories that disagree" | Explicit vs implicit conflict; claim only the former honestly |
| **F11** | 24–40h | 32–52h; OAuth 2.1 + PKCE mandatory, no token passthrough, injection-hardened parameters |
| **F18** | "markdown with frontmatter" | Specifically Claude Code's subagent schema as a subset; AGENTS.md is a different thing |
| **F19** | cold-start problem unaddressed | Existing Claude Code subagent corpus is the seed |
| **Graph store** | "probably Postgres" | Confirmed, with the boundary named and cycle handling flagged as correctness |
| **F9** | a strong hunch | Externally validated, with prior art and a benchmark |

**Two findings were not guessable and changed the sequence:** that SCIP indexers
need to execute untrusted build commands (pulling the sandbox forward), and that
RLS turns every vector query into the filtered-search case pgvector is worst at
(adding partitioning to D2).

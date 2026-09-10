# v2 Substrate — Research

**Companion to** `DESIGN.md` in this directory.
**Started:** 2026-09-10

Findings are appended as they land. Each topic states **what it settles** up
front, so a reader can tell whether it still matters before reading it.

---

## Agenda

| Topic | Settles | State | Est. |
|-------|---------|-------|------|
| **R-A** Code graph construction | D3, and the cost of R3 | ✅ Done 2026-09-10 | — |
| **R-B** pgvector at our scale | D2 | ⏳ Next | 4–6h |
| **R-C** Multi-tenant MCP | F11 | Queued | 6–8h |
| **R-D** Graph queries in Postgres | The no-separate-graph-DB call | Queued | 4–6h |
| **R-E** Sandboxing and orchestration | F1, and R3 tier 2 | Queued | 8–12h |
| **R-F** Agent definition formats | F18, F19, F20 | Queued | 6–8h |
| **R-G** Memory invalidation prior art | F9 | Queued | 4–6h |

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

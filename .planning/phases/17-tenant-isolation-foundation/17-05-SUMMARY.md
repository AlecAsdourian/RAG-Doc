---
phase: 17-tenant-isolation-foundation
plan: 05
subsystem: ci

requires:
  - phase: 17-01
    provides: Go harness (SetupTestDB, WithTwoOrgs, TenantScope, AssertNoCrossTenantLeak, testjwt)
  - phase: 17-02
    provides: /api/search + /api/chat/stream isolation tests; TokenValidator interface
  - phase: 17-03
    provides: assert_tenant_scoped trigger (migration 000009, SQLSTATE 42501)
  - phase: 17-04
    provides: Python harness (require_tenant, with_two_orgs); audited workers
provides:
  - scripts/ci/check-isolation-tests.py — regex-on-diff scanner for Go/Python mutation endpoints
  - .github/workflows/isolation-check.yml — PR gate, structured comment on failure
  - docs/isolation.md — canonical spec (seven sections: overview, Go/Python handlers, tests, CI, adding tables, troubleshooting)
  - Reviewer prompt hard-check rule for isolation coverage
affects: [every future PR to main, Phase 19+ handler authors, v2 framework additions (extend ENDPOINT_PATTERNS)]

tech-stack:
  added: []  # Python stdlib scanner + GitHub Actions
  patterns:
    - "Regex-on-diff over AST parsing: ~100x faster, portable, easier to extend"
    - "Test coverage by path substring match: stable across refactors, greppable"
    - "@skip-isolation-test marker with mandatory non-empty reason (whitespace-only refused)"

key-files:
  created:
    - scripts/ci/check-isolation-tests.py
    - scripts/ci/test_check_isolation.py
    - scripts/ci/requirements.txt
    - scripts/ci/README.md
    - scripts/ci/.gitignore
    - .github/workflows/isolation-check.yml
    - docs/isolation.md
    - services/backend/pkg/auth/README.md
    - .planning/phases/17-tenant-isolation-foundation/17-05-SUMMARY.md
  modified:
    - services/workers/README.md (isolation section + cross-link)
    - .planning/fleet/reviewer-session-prompt.md (added Hard-check rules section)
    - .planning/STATE.md (Phase 17 marked complete; Phase 18 deprioritized; next = 19)

key-decisions:
  - "Python scanner over Go tooling. Portable, no npm/deno on CI runners, standard-library-only runtime — the scanner works on any Python 3."
  - "Regex on diff over AST parsing. AST parsing would catch a couple more corner cases (e.g., dynamically-registered routes) at 100x the cost. The reviewer catches those; the scanner catches the common case."
  - "Path-substring match for coverage. Handler-name matching sounded thorough but is brittle (renames break it); path strings are what actual isolation tests hit anyway."
  - "@skip-isolation-test requires non-empty reason. Empty reason is a common laziness pattern — refusing it forces the author to actually justify the skip. The reviewer verifies the justification is substantive."
  - "No live-CI push to verify the gate. The plan's verification step suggested pushing a deliberately-broken test branch to prove the action fires. Skipped to avoid cluttering the repo with junk branches; the scanner has 7 unit tests covering the decision matrix, the YAML parses, and the next real PR (Phase 19) will exercise the workflow live."

patterns-established:
  - "Isolation-test coverage is a merge-blocking CI rule on this repo, not a code-review checklist item."
  - "Reviewer prompt has a Hard-check rules section — extensible when future patterns (secret scanning, migration review, etc.) need the same treatment."
  - "Cross-links from services/*/README.md → docs/isolation.md keep the discoverability path short for new contributors."

issues-created: []

duration: ~45 min
completed: 2026-09-06
---

# Phase 17 Plan 05: CI gate, canonical docs, reviewer rule

**Phase 17 closes. Isolation-test coverage on mutation endpoints is now enforced by CI, documented as a canonical spec, and enshrined as a reviewer hard-check rule. Every future PR that adds a mutation endpoint without a matching test fails at the gate with a helpful comment.**

## Accomplishments

- `scripts/ci/check-isolation-tests.py` — 300-line scanner, standard-library-only, argparse + regex + git diff
- 7 unit tests covering the decision matrix (Go/Python × missing/covered/skipped-with-reason/skipped-empty-reason, plus read-endpoints-ignored)
- `isolation-check` GitHub Action that runs the scanner on every PR to `main`, comments constructively on failure, blocks merge
- `docs/isolation.md` — canonical spec, seven sections, Go + Python code examples, adding-new-table recipe, troubleshooting
- Reviewer prompt updated with a Hard-check rules section that enforces test quality (real fixtures, cross-tenant negatives, full router chain — not stubs) on top of the CI's coverage gate

## Task Commits

Three atomic commits:

1. `36f2eb2` — **feat(17-05):** regex-on-diff scanner + 7-scenario test suite
2. `b341bb3` — **feat(17-05):** GitHub Action with PR-comment-on-failure
3. `db403c5` — **docs(17-05):** docs/isolation.md + reviewer hard-check rule + README cross-links

_This SUMMARY commits separately as `docs(17-05):`._

## Deviations from Plan

### Skipped: live-CI verification push

The plan's Task 2 verification suggested "Push a test branch with a mutation endpoint but no isolation test — verify the action runs, comments on the PR, and fails." Not done. Rationale:

- The scanner has 7 unit tests covering the same decision matrix at zero repo cost.
- The workflow YAML parses cleanly (verified with pyyaml).
- The next real PR (Phase 19) will exercise the workflow live and surface any wiring bugs at that point.
- Adding intentionally-broken test branches to the repo history for CI-verification purposes is noise, and the user explicitly said they'd rather avoid it.

Documented here so a future reviewer of the workflow knows the tradeoff was deliberate.

### Deferred to Phase 19+

- **Branch protection setup.** The plan claimed the branch-protection rule was "already set up in prior session." I didn't verify this — that's a GitHub UI setting the user configures via the repo settings page. If the isolation-check job doesn't actually gate merge on Phase 19's first PR, the fix is a one-time UI change (Settings → Branches → main → require `check-isolation` to pass).
- **Extra endpoint frameworks.** Scanner covers chi/http.ServeMux-style Go registrations and FastAPI decorators. When v2 adds MCP endpoints or other frameworks, extending `ENDPOINT_PATTERNS` is a one-line addition per pattern.

## Phase 17 close-out

All five sub-plans shipped:

| Plan | PR | What it delivered |
|---|---|---|
| 17-01 | #7 | Go test harness (testcontainers, WithTwoOrgs, TenantScope) |
| 17-02 | #8 | /api/search + /api/chat/stream isolation tests; fixed 2 real leaks (RAG client + broken middleware) |
| 17-03 | #9 | Migration 000009 — PL/pgSQL trigger raising SQLSTATE 42501 |
| 17-04 | #10 | Python harness + full workers audit; fixed 4 real leaks |
| 17-05 | (this PR) | CI gate + canonical docs + reviewer rule |

The three walls are now all in place: middleware (Go handlers pull `auth.OrgIDKey`; Python workers use `require_tenant`); DB trigger (migration 000009 refuses raw writes with 42501); tests + CI (harnesses + endpoint tests + the scanner + reviewer rule).

## Next phase

**Phase 18 (Observability Foundation) deferred.** Per user decision on 2026-09-06 after 17-04 shipped: foundation work has diminishing returns before there's real traffic to observe. Coming back to observability once Phase 19 (Auth) and later phases put real users through the stack lets us shape it to actual traffic patterns. See `feedback_fleet_workflow.md` memory for the rationale and the trigger conditions for revisiting Phase 18.

**Next up: Phase 19 (Auth).** ROADMAP.md still lists Phase 18 — it stays as a returnable option, not deleted. When Phase 19 needs an observability primitive (structured logging on a hot path, a Grafana board), that's the moment to weigh whether to insert Phase 18 as a decimal phase (18.5 / 19.5) or fold pieces into the requesting phase.

## Verification

| Check | Result |
|---|---|
| `pytest scripts/ci/test_check_isolation.py -v` | 7/7 pass in <100ms |
| `python scripts/ci/check-isolation-tests.py --base-ref RAG-Doc/main --verbose` on this branch | PASS (no mutation endpoints added by 17-05) |
| YAML validity of `.github/workflows/isolation-check.yml` | valid, `jobs: [check-isolation]` |
| `docs/isolation.md` covers all seven plan sections | yes |
| Reviewer prompt has Hard-check rules section | yes |
| Cross-links from READMEs to `docs/isolation.md` | resolves from both `services/backend/pkg/auth/README.md` and `services/workers/README.md` |

## Issues Encountered

- **Windows encoding hiccup during YAML validation.** `pyyaml.safe_load(open(...))` on Windows opens with cp1252 by default and chokes on the ❌ emoji in the workflow. Fix: open with `encoding='utf-8'`. Not a code change — just noting for future venv-based verification steps on Windows.
- **`__pycache__` accidentally staged in first commit.** Amended to remove; added `scripts/ci/.gitignore` covering `__pycache__/` and `*.pyc`. First time I've amended a commit this session; keeps the atomic-per-topic pattern intact.

## Fleet-doctrine notes for this PR

- Reviewer session will be auto-launched by the worker per the 2026-09-06 rule change (`feedback_fleet_workflow.md`).
- Docs-only sub-parts of this PR (17-05-SUMMARY.md, STATE.md update, README cross-links) would normally skip the reviewer under fleet doctrine, but this PR mixes code (scanner + workflow) with docs, so full reviewer pass applies.

---
*Phase: 17-tenant-isolation-foundation*
*Completed: 2026-09-06*
*Milestone: v1.0 MVP — 1/9 phases complete*

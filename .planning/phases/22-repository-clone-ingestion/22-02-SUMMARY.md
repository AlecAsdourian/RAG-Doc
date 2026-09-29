---
phase: 22-repository-clone-ingestion
plan: 02
subsystem: backend, workers
tags: [migrations, partitioning, pgvector, rls, tenancy, drift, mutation-testing, ingestion]

requires:
  - phase: 22-01
    provides: "pgvector on every Postgres, the seeded-migration gate with its tenant audit (which 000017 had to pass), ScratchDatabase, appRoleGrants, and ISS-031's rule"
  - phase: 21-01
    provides: "repositories.organization_id and repositories_id_org_key, which both new composite keys reference; the drift-check pattern"
  - phase: 17-01
    provides: "the testcontainers harness, the app role, WithTwoOrgs and TenantScope"
provides:
  - "migration 000017_partitioned_chunks: chunks partitioned by HASH (organization_id) MODULUS 64 with RLS, FORCE and the scalar tenant policy on every partition; embedding vector(1536) NOT NULL; embedding_model; symbol_id; PRIMARY KEY (organization_id, id); chunks_repo_tenant_fk inside CREATE TABLE"
  - "symbols (D1), unpartitioned (P7), archived not deleted, with symbols_repo_tenant_fk inside CREATE TABLE"
  - "retrievals_chunk_id_fkey dropped (P17); the down restores it NOT VALID"
  - "isolation.CheckChunkTenantDrift / AssertNoChunkTenantDrift, run at the end of every test that writes chunks or symbols"
  - "isolation.TestChunkInsertSQL, TestEmbeddingSQL, TestEmbeddingModel (Go) and tests.isolation.fixtures.TEST_CHUNK_INSERT_SQL (Python): the row every test writer writes"
  - "PostgresWriter.insert_chunks(embeddings=, embedding_model=): the tenant, the vector and the model on every row, duplicates included"
  - "EmbeddingGenerator.model, public"
  - "DELETE /api/repositories/{id} deleting retrievals explicitly, with its limit written down"
affects: [22-03 (the vector leg reads chunks.embedding; the Qdrant upsert is retired only after the equivalence gate), 22-05 (write_results replaces a repository's chunks; every writer supplies organization_id), 22.1-01 (symbols' shape and the resurrection upsert are ready), 24 (grants must never include TRUNCATE on chunks, its partitions or symbols)]

tech-stack:
  added: []
  removed: []
  patterns:
    - "Row-level security on a partitioned table goes on EVERY partition, with the policy; the parent's does not reach a partition addressed by name"
    - "The tenant policy on a partitioned table is scalar equality on the partition key, so the planner prunes from the policy alone"
    - "Every writer of a partitioned table supplies the partition key; a BEFORE trigger cannot route a row to another partition"
    - "A composite tenant key holds only while foreign-key triggers are enabled: loading under replica mode or DISABLE TRIGGER is forbidden by comment, pinned by test, and guarded by a drift query in CI"
    - "A cross-partition test asserts its premise (different partitions, through the hash and tableoid), because two tenants share a partition 1 time in 64"
    - "pgvector prints a stored float4 as its shortest float32 decimal; read its text output back as float32, never as a double"
    - "Prove a migration mutation against the gate from a scratch copy, and against the harness tests on a fresh container, since golang-migrate never re-applies a recorded version"

key-files:
  created:
    - services/backend/migrations/000017_partitioned_chunks.up.sql
    - services/backend/migrations/000017_partitioned_chunks.down.sql
    - services/backend/pkg/testing/isolation/chunks_partition_test.go
  modified:
    - services/backend/pkg/testing/isolation/drift.go
    - services/backend/pkg/testing/isolation/fixtures.go
    - services/backend/pkg/testing/isolation/fixtures_test.go
    - services/backend/pkg/testing/isolation/db_assertion_test.go
    - services/backend/pkg/testing/isolation/migration_seeded_test.go
    - services/backend/pkg/api/handlers/repositories.go
    - services/backend/pkg/api/handlers/repositories_connect_test.go
    - services/backend/pkg/api/handlers/search_isolation_test.go
    - services/backend/pkg/auth/provisioning_isolation_test.go
    - services/backend/pkg/auth/isolation_test.go
    - services/workers/workers/storage/postgres_writer.py
    - services/workers/workers/pipeline/ingestion_pipeline.py
    - services/workers/workers/embeddings/embedding_generator.py
    - services/workers/workers/db/tenant.py
    - services/workers/tests/isolation/fixtures.py
    - services/workers/tests/isolation/test_harness.py
    - services/workers/tests/isolation/test_postgres_writer_isolation.py
    - services/workers/tests/isolation/test_query_engine_isolation.py
    - services/workers/workers/pipeline/test_pipeline.py
    - docs/api-repositories.md
    - docs/isolation.md
    - .planning/ISSUES.md
    - .planning/ROADMAP.md
    - .planning/STATE.md

key-decisions:
  - "Both single-column keys (repository_id, ingestion_run_id) are kept beside the composite tenant key, as 000014 did: the composite is the guarantee, the single-column ones the backstop, and a misfiled row is reported on the composite either way"
  - "The drift query treats an orphan (a chunk or symbol whose repository is gone) as drift too: a row with no repository has no truth to agree with, and only a disabled key can produce one"
  - "No pgvector client package: the text form round-trips exactly up to the float4 storage, measured, once the OUTPUT is read back as float32"
  - "The writer's return value keeps one chunk id per content hash (the Qdrant upsert still consumes it until 22-03); every duplicate row is written"
  - "The pre-existing probe test's migrations moved from 000017/000018 to 000018/000019, numbered from partitionedChunksVersion so the next migration does not collide again"

issues-closed: [ISS-031]
issues-updated: [ISS-035]
review: "PR #49, open."
duration: ~9h
completed: 2026-09-29
---

# Phase 22 Plan 02: `chunks` partitioned by organization, with row-level security on every partition, and every writer moved

**`chunks` is now D2's table as corrected:** partitioned by `HASH
(organization_id)` into 64 partitions, each of which enforces the tenant
policy by itself; tied to its tenant by `chunks_repo_tenant_fk`; carrying
`embedding vector(1536) NOT NULL`, the model that produced it, and one
nullable `symbol_id`. **`symbols` exists** (D1, unpartitioned, P7).
**`retrievals` no longer references `chunks`** (P17), and the repository
delete stays truthful about it. **Every writer of `chunks`, Go and Python,
moved in the same PR**, and `PostgresWriter` stores each chunk with its
tenant, its vector and the generator's model.

**000017 is the first migration written under ISS-031's rule, and 22-01's
gate proved it in the deployment shape:** every foreign key inside its
`CREATE TABLE`, the 64 partitions inheriting them, the one-session `up`
from 12 to 17 as `rag_doc_owner` clean with the tenant audit empty. The
mutation that moves the tenant key into an `ALTER TABLE` fails the gate at
17, dirty, with `22P02`. **ISS-031 is closed.**

## The migration

`000017_partitioned_chunks.up.sql`, in order, with no DML and no GRANTs:

1. `ALTER TABLE retrievals DROP CONSTRAINT retrievals_chunk_id_fkey` (P17).
   The column stays. It has to go before step 3, or `DROP TABLE` names it
   (mutation M5).
2. `CREATE TABLE symbols`: D1's DDL with `archived_at`, `idx_symbols_live`,
   `symbols_repo_tenant_fk` inside the statement (P3, ISS-031), `ENABLE` and
   `FORCE ROW LEVEL SECURITY`, the scalar policy, `trg_assert_tenant`.
3. `DROP TABLE chunks`, plain, so a dependency would fail loudly.
4. `CREATE TABLE chunks (...) PARTITION BY HASH (organization_id)`: 000003's
   columns and CHECKs, 000006's `breadcrumb`, `organization_id UUID NOT
   NULL`, `symbol_id UUID REFERENCES symbols(id) ON DELETE SET NULL`,
   `embedding vector(1536) NOT NULL`, `embedding_model TEXT NOT NULL`,
   `PRIMARY KEY (organization_id, id)`, the `ingestion_run_id` and
   `repository_id` keys, and `chunks_repo_tenant_fk`, all inside the
   statement.
5. A `DO` loop creates `chunks_p0` … `chunks_p63` (`MODULUS 64`) and, on
   each, `ENABLE ROW LEVEL SECURITY`, `FORCE ROW LEVEL SECURITY` and
   `CREATE POLICY tenant_isolation ... USING (organization_id =
   current_setting('app.current_tenant', true)::uuid)`. Then the same on
   the parent.
6. `CREATE TRIGGER trg_assert_tenant ... ON chunks`, cloned to every
   partition (measured: 64 of 64, `tgisinternal = false`, one `tgparentid`).
7. Indexes on the parent, so every partition carries them: HNSW
   `(embedding vector_cosine_ops)`, `(organization_id)`, `(repository_id,
   file_path)`, `(ingestion_run_id)`, `(content_hash)`, `(symbol_id) WHERE
   symbol_id IS NOT NULL`, GIN on `to_tsvector('english', content)` and
   GIN on `to_tsvector('english', COALESCE(breadcrumb, ''))`, the
   expression `fts_retriever.py` queries (22-03 owns the proof that it
   matches). Nine indexes per partition, 576 in all, plus `symbols`'.
8. `COMMENT ON TABLE` for both, and on `chunks.organization_id`,
   `chunks.embedding_model`, `chunks.symbol_id` and `retrievals.chunk_id`:
   the fixed modulus and its revisit trigger, per-partition RLS and the
   measured leak, the scalar policy, vectors here, P4, no DML, the
   replica-mode limit and the ban on trigger-disabled loads, and the ban on
   granting `TRUNCATE`.

**The header carries the rule for the next author:** why every key is
inside its `CREATE TABLE`, with the `''` mechanism and the gate that
enforces it; why a trigger cannot fill `organization_id` (`0A000`,
measured); and P15's transition.

**The resulting catalog**, read on a fresh `pgvector/pgvector:pg16` after
`migrate up`: 64 partitions; `relrowsecurity` and `relforcerowsecurity` on
all 64, the parent and `symbols` (66 relations); 65 `tenant_isolation`
policies on `chunks%` with **one distinct `qual`**,
`(organization_id = (current_setting('app.current_tenant'::text, true))::uuid)`;
64 cloned `trg_assert_tenant`; every key `convalidated`; no foreign key on
`retrievals` but `retrievals_query_id_fkey`; the comments 1,482 and 764
characters long.

**Both harness paths apply it.** golang-migrate (the Go harness, the gate,
the CLI) and psycopg2's one-`execute`-per-file path (the Python conftest,
291 tests green on it).

### The down

Drops `chunks` (partitions with it) and `symbols`, recreates `chunks`
exactly as 000003 + 000006 + 000008 + 000009 left it (columns in 000003's
order with `breadcrumb` last, the five 000003 indexes and the two GIN
ones, `ENABLE`/`FORCE` and the two-hop `EXISTS` policy, the trigger), and
restores `retrievals_chunk_id_fkey` **`NOT VALID`**.

**Why `NOT VALID`, measured here with a dangling row present.** After the
up, every retrieval that existed before it points at a chunk no chunk has.
A plain restore validates those rows and fails with `23503`; the only way
to make it pass is to delete them and the feedback hanging off them, which
is user-authored. `NOT VALID` enforces the key for every new row and
checks none of the old ones. On the scratch database with one dangling
retrieval and its feedback in place: `down 1` succeeds, `schema_migrations`
reads 16 clean, the key exists with `convalidated = f`, the retrieval and
the feedback survive, and a new retrieval pointing at a non-existent
chunk is refused with `23503`. Whoever wants the key validated repoints or
deletes the dangling rows first and runs `VALIDATE CONSTRAINT`; the down's
comment says so.

## P3's limit, pinned, and the drift check

**The composite key holds only while foreign-key triggers are enabled.**
`TestChunksPartition_TheKeyDoesNotHoldUnderReplicaMode`, as the superuser
in a transaction that is never committed: under `SET LOCAL
session_replication_role = replica`, with no tenant set at all, a chunk for
organization A naming organization B's repository is **accepted**, and so
is a symbol; `CheckChunkTenantDrift` reports exactly
`[chunks:<id> symbols:<id>]` before the rollback. That is the fact-check's
finding, written down so the claim that the key "holds without triggers"
cannot be re-derived.

**`CheckChunkTenantDrift`** (`drift.go`) joins `chunks` (without `ONLY`, so
every partition) and `symbols` to `repositories` and returns every row
whose `organization_id` is not its repository's, or whose repository is
gone, as `chunks:<id>` / `symbols:<id>`. It refuses to run under a role
that row-level security applies to (`requireRLSBypass`, now shared with
`CheckRepositoryTenantDrift`). `AssertNoChunkTenantDrift` runs at the end
of every test in `chunks_partition_test.go` that writes the tables.

**Proven to fail in a 22-01 scratch database**
(`TestChunksPartition_DriftCheckDetectsDrift`, `ScratchDatabase(t, pool,
SuperuserRole)`, migrated in full): both keys dropped, a misfiled chunk and
symbol inserted, both reported; then the single-column keys dropped and the
repository deleted out from under them, both still reported as orphans;
and under `SET ROLE rag_doc_app` the check refuses with "bypasses
row-level security". Never on the shared container (ISS-032).

## The partition tests and their premises

`pkg/testing/isolation/chunks_partition_test.go`, package `isolation`, as
the app role unless stated:

| Test | What it pins | Premise it asserts |
|---|---|---|
| `EveryPartitionEnforcesRowLevelSecurity` | exactly 64 partitions `chunks_p0..63`; each with `relrowsecurity`, `relforcerowsecurity`, a `tenant_isolation` policy whose `pg_policies.qual` **equals the parent's**, and `trg_assert_tenant`; the parent partitioned and scalar; `symbols` likewise | the parent's `qual` is the scalar expression, checked before the partitions are compared to it |
| `SchemaShape` | the four constraints by `pg_get_constraintdef`, validated; `retrievals_chunk_id_fkey` gone and the column kept; `embedding vector(1536)`, `embedding_model`, `organization_id` NOT NULL; `symbol_id` nullable; the partial `symbol_id` index | — |
| `TenantACannotReachTenantBsPartition` | as A against B's partition **by name**: `SELECT` 0, whole partition 0, `UPDATE` 0, `DELETE` 0, `INSERT` of B's row refused `42501` "violates row-level security policy for table "chunks_pNN""; B's row unchanged read as the superuser; a fresh unscoped connection sees nothing through the partition or the parent, and after a committed `SET LOCAL` the same reads raise `22P02` (ISS-013, both halves) | the two tenants hash to different partitions (`satisfies_hash_partition`; a third organization is made when they collide, 1 in 64), **and** each row's `tableoid` is the predicted partition; B reads its own row through its own partition |
| `ThePolicyAlonePrunesToOnePartition` | `EXPLAIN (COSTS OFF)` on the vector leg (bound query vector, cosine, repository filter, LIMIT) and the keyword leg, with no `organization_id` in the SQL: `Subplans Removed: 63` and exactly one `chunks_p` scan | — |
| `AMisfiledRowIsRefusedByTheKey` | a chunk and a symbol for A naming B's repository: `23503` on `chunks_repo_tenant_fk` / `symbols_repo_tenant_fk` as the app role, and again as the superuser with RLS bypassed (so it is the key, not the policy); a row **claiming** B is refused by A's policy first, `42501` | — |
| `EveryWriterMustSupplyTheNewColumns` | without `embedding` or `embedding_model`: `23502` naming the column; without `organization_id`: `42501` from the policy as the app role, `23502` on `organization_id` as the superuser (a NULL key is hashed and routed like any value) | — |
| `TheKeyDoesNotHoldUnderReplicaMode` | above | — |
| `DeletingARepositoryRemovesItsChunksAndSymbols` | the cascade through both keys, checked without RLS so gone is not invisible; the other tenant's chunk survives | — |
| `DeletingAnOrganizationCompletes` | D5 V6 for the new tables: `DELETE FROM organizations` as the app role under the tenant completes through projects, repositories, chunks and symbols | — |
| `DeletingASymbolNullsTheChunksThatPointAtIt` | P7's `ON DELETE SET NULL`; the chunk survives | — |
| `SymbolResurrectionUpsert` | a plain re-insert of an archived symbol's id collides (`23505 symbols_pkey`); D1's `ON CONFLICT (id) DO UPDATE` resurrects it: `archived_at` NULL, `span_digest`, span and `last_seen_commit` updated, `first_seen_commit` kept | — |
| `DriftCheckDetectsDrift` | above | the check is clean before the keys are dropped |

**The evidence, re-run by hand as `rag_doc_app` on the scratch database**
(A hashed to `chunks_p31`, B to `chunks_p38`, read through `tableoid`):

```
b_partition_rows_visible_to_a | 0
UPDATE 0
DELETE 0
own_rows | 1
Limit -> Sort -> Append
   Subplans Removed: 63
   -> Index Scan using chunks_p31_repository_id_file_path_idx on chunks_p31 chunks_1
        Index Cond: (repository_id = 'a2000000-…'::uuid)
        Filter: (organization_id = (current_setting('app.current_tenant'::text, true))::uuid)
ERROR:  new row violates row-level security policy for table "chunks_p38"
```

The keyword leg prunes the same way. Before P2, the research measured the
same `SELECT` returning B's row and the `UPDATE` reporting `UPDATE 1`.

**`db_assertion_test.go`:** `symbols` joins the ratchet; the `chunks`
insert without a tenant must be refused by the cloned trigger on the
partition, and the message is pinned to name `chunks_pNN` (the parent's
policy would also say `42501`, worded differently).

**The gate** (`migration_seeded_test.go`) gained 000017's assertions in
the deployment shape: `chunks` partitioned and **empty** (P1); 64
partitions each with RLS, FORCE, the policy, the trigger and owned by
`rag_doc_owner`; one policy expression across parent and partitions, and
it is scalar; `symbols` with RLS and FORCE, both composite keys present and
validated; `retrievals_chunk_id_fkey` gone; the seeded retrieval
`80000000-…0a01` surviving, still naming chunk `60000000-…a401`, which no
longer exists; its feedback surviving. The one-session `up` from 12 to 17
takes 437–801 ms across runs; the whole test 0.9–1.3 s.

**`TestForeignKeyValidationReadsTheReferencedTable`** (22-01) numbered its
probe migrations 000017 and 000018, which now collide with the real
000017 ("duplicate migration file"). They are `partitionedChunksVersion +
1` and `+ 2`, so the next migration cannot collide either.

## Every writer of `chunks`

`grep -rn "INSERT INTO chunks"` before starting matched the plan's
inventory exactly. Every one now supplies `organization_id`, `embedding`
and `embedding_model`:

| Where | How |
|---|---|
| `pkg/testing/isolation/fixtures.go` | **new** `TestChunkInsertSQL` (the tenant as `$1`, `RETURNING id`), `TestEmbeddingSQL` = `array_fill(0.01::real, ARRAY[1536])::vector` (non-zero: the cosine distance of a zero vector is undefined) and `TestEmbeddingModel` = `test-fixed` (not a real model, so a retriever mixing it with a real query vector would be refused); `cleanupOrg` deletes `symbols` too |
| `pkg/testing/isolation/db_assertion_test.go:72,221,350` | `TestChunkInsertSQL`; `protectedTables` gains `symbols`; the "allows mutations when tenant set" test writes a symbol too |
| `pkg/testing/isolation/fixtures_test.go:178,226` | `TestChunkInsertSQL`; `insertOneChunk` takes the organization id |
| `pkg/auth/provisioning_isolation_test.go:63` | `TestChunkInsertSQL` |
| `pkg/api/handlers/repositories_connect_test.go:1272,1331` | `TestChunkInsertSQL` |
| `pkg/api/handlers/search_isolation_test.go:260` | `TestChunkInsertSQL` |
| `pkg/auth/isolation_test.go:241,247` | the same column list inline with a pointer to `TestChunkInsertSQL` (package `auth` cannot import the harness); still skipped |
| `workers/storage/postgres_writer.py:134` | production, below |
| `tests/isolation/test_harness.py:85,129` | **new** `TEST_CHUNK_INSERT_SQL` in `tests/isolation/fixtures.py`; `cleanup_org` deletes `symbols` too |
| `tests/isolation/test_postgres_writer_isolation.py`, `test_query_engine_isolation.py` | through the writer, passing an embeddings map and a model |
| `docs/isolation.md:196`, `workers/db/tenant.py:47` | the examples carry the new columns and say why |

Every test kept its assertion and changed only its insert. The seed scripts
stay as ISS-035 records them; `testdata/seed_at_000010.sql` inserts chunks
at version 10, before 000017 drops them, and is unchanged.

### `PostgresWriter.insert_chunks(…, embeddings, embedding_model)`

- `embeddings` is `content_hash -> vector`, the shape
  `EmbeddingGenerator.generate_embeddings_for_chunks` returns;
  `embedding_model` is the generator's `.model`, now public. Both are
  required parameters: a caller that forgets fails at the call.
- **Every chunk gets its hash's vector**, duplicates included. Before,
  Qdrant held one point per unique hash, so every duplicate-content chunk
  after the first had no vector (12 in miniflux, 80 in mealie). The
  returned map still holds one chunk id per hash, for the Qdrant upsert
  that stays until 22-03; every row was written.
- **A chunk with no embedding raises `ValueError` before anything is
  written**, naming the file and line range
  (`missing.py:17-23`), so a batch is never half-inserted. An empty model
  raises too.
- **The vector goes as text, `%s::vector`.** No pgvector client package:
  `test_writer_stores_the_vector_it_was_given` writes 1,536 float32-exact
  values and 1,536 realistic ones (seeded), and reads both back. **What the
  first run found:** the exact vector came back "wrong" at index 1,
  `-0.9995117` for `-0.99951171875`. pgvector prints a stored float4 as
  the shortest decimal that identifies it **as a float32**, and read as a
  double that is a different number; read as a float32 it is exactly the
  stored value. The test now decodes the text through `np.float32`, the
  way it was encoded; the exact values then round-trip exactly and the
  realistic ones equal `float32(sent)` element by element. 22-03's reader
  must do the same.
- `IngestionPipeline` passes the map and `self.embedding_gen.model`, and
  **keeps the Qdrant upsert with a comment saying until when** (P15).

### The tests, as the app role

`_app_role_writer` is the one place the writer's connection does `SET ROLE
rag_doc_app`, right after connecting; every test in
`test_postgres_writer_isolation.py` goes through it, and the module
docstring says why. New: the stored model equals the generator's, with the
generator constructed as `text-embedding-3-small` so a writer or pipeline
that restated the default is caught (never asked to embed: no OpenAI
call); org B sees neither A's chunk nor its vector nor its model; the
duplicate-content pair, both rows with the vector; the missing-embedding
refusal with nothing written; the empty model; the round trip; and a chunk
for B's repository written under A's scope refused with `23503` on
`chunks_repo_tenant_fk`. `test_pipeline.py` asserts the **arguments**
passed to `insert_chunks`, positional and keyword, with the mock
generator's model `mock-embedding-model-7`, and that the Qdrant upsert
still happens.

## The delete (P17)

`DELETE /api/repositories/{id}`, inside the tenant transaction and before
`DELETE FROM repositories`:

```sql
DELETE FROM retrievals
WHERE chunk_id IN (SELECT id FROM chunks WHERE repository_id = $1)
```

the same predicate as the `feedback_deleted` count, so the two agree by
construction; `feedback` cascades from `retrievals` (000005's key is
unchanged). The response contract is unchanged. **The limit, written in
the handler's comment and in `docs/api-repositories.md`:** only retrievals
whose chunk **still exists** are found; a retrieval whose chunk a
re-index already replaced survives with its feedback, and from 22-05 on
every full ingest replaces a repository's chunks. The link's shape is U9's
deferred decision.

**Mutation M8**, the DELETE neutered to match nothing: the existing
`ingestedCounts(orgA) == {1,1,1}` assertion fails, as the fact-check
predicted, because the survivor's feedback is still counted under the
tenant through `queries` and `projects`.

## Mutations

Committed code was never edited to run a mutation. Migration mutations ran
from scratch copies of the migrations directory: through the gate with
`RAG_DOC_SEEDED_GATE_MIGRATIONS`, and through the partition tests on a
**fresh harness container** each time (the reuse container removed
before, the working-tree file mutated, the tests run, the file restored
with `git checkout --`, the container removed again). Code mutations were
made in the working tree after everything was committed and restored the
same way. Every mutation was proven to have landed by a tool that prints
the original text's count (0) and the mutated text's count (1) after
writing, and `diff -rq` named exactly the one file that differed from the
committed set. Every restore left the tree clean.

| # | Mutation | Expected | Result |
|---|---|---|---|
| M1 | `FOR i IN 0..62`: REMAINDER 63 skipped | the guard fails | **killed** twice: the gate, `should have 64 item(s), but has 63`; the partition tests, `EveryPartitionEnforcesRowLevelSecurity` and, unasked, `ThePolicyAlonePrunesToOnePartition` (`Subplans Removed: 62`) |
| M2 | every partition's policy neutered to `USING (organization_id IS NOT NULL)` | the guard and the leak test fail | **killed** twice: the gate, `one policy expression across the parent and its partitions: expected 1, actual 2`; the partition tests, the guard **and `TenantACannotReachTenantBsPartition`** |
| M3 | the per-partition `FORCE` dropped | the guard fails | **killed** twice: the gate by **two** subtests, 22-01's `every table with row-level security still forces it` (all 64 partitions listed) and 000017's own; the partition tests, the guard (`chunks_p0: row-level security must be FORCED on the partition itself`) |
| M4 | `chunks_repo_tenant_fk` moved out of `CREATE TABLE` into an `ALTER TABLE` after the partitions | the gate fails with `22P02`, 17 dirty | **killed**, exactly so: `left schema_migrations at 17 (dirty=true): SQLSTATE 22P02: invalid input syntax for type uuid: ""`, with the ISS-031 hint. This is the rule proving its own reach |
| M5 | step 1 skipped (the retrievals key not dropped) | the migration fails on `DROP TABLE` | **killed**: 17 dirty, `2BP01 cannot drop table chunks because other objects depend on it, constraint retrievals_chunk_id_fkey on table retrievals depends on table chunks` |
| M6 | `chunks_repo_tenant_fk` removed altogether | a misfiled chunk is accepted | **killed** twice: the gate, `chunks.chunks_repo_tenant_fk must exist`; the partition tests, `SchemaShape`, `AMisfiledRowIsRefusedByTheKey` and `DriftCheckDetectsDrift` (the check's premise, "clean to begin with", no longer fails to drop a key that is not there) |
| M7 | the parent's policy in 000008's `EXISTS` form | pruning stops | **killed** twice: the gate, `one policy expression: expected 1, actual 2`; the partition tests, `ThePolicyAlonePrunesToOnePartition` (no `Subplans Removed`), the guard, and two tests whose refusal path changed (`AMisfiledRowIsRefusedByTheKey`, `EveryWriterMustSupplyTheNewColumns`: with the join policy a row naming B's repository is refused by RLS before the key) |
| M8 | the handler's `DELETE FROM retrievals` predicate neutered with `AND false` (the statement keeps its arity) | the `{1,1,1}` assertion for orgA fails | **killed**, exactly there: `repositories_connect_test.go:1240: expected: []int64{1, 1, 1}, actual: []int64{1, 1, 2}`, "deleting one repository must not take orgA's other one with it". The response's counts still passed, which is why the plan said a count-only test would not do |
| M9a | the pipeline passes `"text-embedding-ada-002"` instead of the generator's model | the pipeline test fails | **killed**: `test_process_files_success` (`kwargs["embedding_model"] == "mock-embedding-model-7"`) |
| M9b | the writer stores `"text-embedding-ada-002"` instead of the model it was given | the model test fails | **killed** by three tests: the model test, the tenant test and the duplicate test, each of which reads `embedding_model` back |
| M10 | the writer skips a chunk whose hash it has already seen | the duplicate test fails | **killed** by exactly `test_every_duplicate_content_chunk_gets_its_hashs_vector` (1 row, 2 expected); the other ten pass |
| M11 | both halves of the drift query `WHERE false AND (...)` | the replica-mode pin and the drift self-test fail | **killed**: both, and nothing else |
| M0 | the committed migrations, unmutated | pass | the gate passes; the partition tests pass on a fresh container |

**Recorded limit, not a survivor:** a mutation neutering **one** partition's
policy would be killed by the guard on every run, but by the leak test only
when tenant B happens to hash to that partition. The leak test proves the
partition it addresses; the guard is what covers all 64, which is why both
exist.

## Verification

Run on the final code with `DATABASE_TEST_URL` on a scratch
`pgvector/pgvector:pg16` container on port 55482 (echoed before every Go
run) and `REDIS_URL` on a scratch Redis on 63792; port 5434, compose's
Postgres and Qdrant, and the compose volume were never touched. No OpenAI
call was made.

| Check | Result |
|---|---|
| `go build ./...`, `go vet ./...`, gofmt on every changed Go file | clean |
| `go mod tidy -diff` | no change to `go.mod` or `go.sum` (nothing was added or removed; `git diff` on both is empty). Locally the command prints every `go.sum` line removed and re-added, which is the CRLF checkout differing from tidy's LF output by `\r` alone, the same artefact 22-01 recorded for function bodies; CI's checkout is LF |
| `go test ./... -count=1 -p 1` (CI's whole-module step) | every package `ok` except `pkg/api/handlers`, whose only failure is `TestSignatureComparisonIsConstantTime`, the known CRLF artefact. **576 tests and subtests passed, 1 failed (that one), 4 skipped** (three pre-existing `pkg/auth` supersessions, the schema tool with no baseline). The gate: `5 migrations from 12 in one session in 711ms` |
| CI's package-parallelism step (default parallelism) | same |
| `-race` in `golang:1.25` (go1.25.14 linux/amd64) with the Docker socket, the module cache mounted and `TESTCONTAINERS_HOST_OVERRIDE=host.docker.internal`, CI's package list `./pkg/api/... ./pkg/db/... ./pkg/jobs/... ./pkg/testing/...` | `pkg/db`, `pkg/jobs`, `pkg/testing/isolation` and `testjwt` `ok`; `pkg/api/handlers` fails only on the CRLF test (the mounted checkout is CRLF); **0 data races** |
| `python scripts/ci/check-isolation-tests.py --base-ref RAG-Doc/main --head-ref HEAD` | `PASS`, nothing missing (no route was added) |
| `pytest tests/ workers/ -q` from `services/workers`, fresh venv from `requirements.txt` (Python 3.13.7), `REDIS_URL` on db 15, `OPENAI_API_KEY=sk-test-dummy`, no `DATABASE_URL`, no reachable `.env` | **291 passed** (284 on `main` + 7 new) |
| up, down 1, up, down -all, up with the `migrate` CLI on the fresh container | up to 17 in 2.7 s; down to 16 in 150 ms with 000003's `chunks` back (14 columns, 8 indexes, the `EXISTS` policy, the key `NOT VALID`); up to 17 in 1.9 s; `down -all` leaves `schema_migrations` alone, no functions, no extension; up again to 17 in 2.4 s, 66 relations with RLS and FORCE, no key `NOT VALID` |
| down with a dangling retrieval and its feedback present | succeeds; both rows survive; the key is `NOT VALID`; a new dangling retrieval is refused with `23503`; up again reaches 17 with the retrieval still there |
| the gate, the extension test and the partition tests | pass; no scratch database left behind |

## Deviations from the plan

1. **The two single-column keys stay** (`repository_id`, `ingestion_run_id`),
   beside the composite one, as 000014 keeps its `repository_id` key. The
   plan lists "000003's columns", which carry them. A misfiled row is
   reported on `chunks_repo_tenant_fk` either way, because the
   single-column check passes on an existing repository.
2. **Without `organization_id` the refusal is the policy's, not NOT NULL's,
   as the app role.** The plan said "NOT NULL violations on `embedding` and
   `embedding_model` are rejected", which holds; for the tenant column the
   test pins both paths as measured (`42501` as the app role, `23502` as the
   superuser).
3. **The drift query also reports orphans**, not only mismatches. Only a
   disabled key can produce one, and it is a row with no truth to agree
   with.
4. **`TestForeignKeyValidationReadsTheReferencedTable` had to move its
   probe numbers** (000017 and 000018 collided with the real 000017). The
   plan did not list that file; it was a duplicate-migration failure, not a
   test failure.
5. **The pgvector text-output finding** (float32-shortest digits) was not
   anticipated; the round-trip check the plan asked for is what found it,
   and it decides how 22-03 reads vectors back.
6. **Extra tests beyond the plan's list:** `SchemaShape`, the
   "row claiming tenant B" subtest, the superuser half of the key test, the
   empty-model refusal, and the `23503` writer test in Python.
7. **Extra mutations:** M5 (step 1 skipped) and M6/M7 also run through the
   gate, which catches all seven migration mutations on its own; M9b and
   M11 beyond the plan's list.
8. **ISS-031 is closed** and its entry moved to the closed section whole,
   with the rule text intact; ISS-035's line about 000017 is brought
   current. Both at the direction that came with this plan.

## What 22-03 inherits

- **`chunks.embedding` is there for every row, with `embedding_model`.**
  The vector leg reads `ORDER BY embedding <=> $1::vector` under
  `require_tenant`, with the query vector as a bound parameter and
  `hnsw.iterative_scan` set (P5); it must refuse a query whose model is not
  the rows' (P4).
- **Read vectors back as float32.** pgvector's text output is
  float32-shortest; parsed as doubles the values look changed.
- **The keyword leg's breadcrumb expression has its index now,
  `COALESCE(breadcrumb, '')`.** 22-03 owns proving the plan uses it.
- **The Qdrant upsert is still in `IngestionPipeline`**, commented as
  P15's transition, for the equivalence gate; retire it after.
- **Pruning is from the policy alone**: `Subplans Removed: 63` on both
  legs with no `organization_id` in the SQL. Keep the SQL that way.
- **Every writer supplies `organization_id`**; 22-05's `write_results`
  inside `complete()`'s transaction must too, and `AssertNoChunkTenantDrift`
  belongs at the end of its tests.

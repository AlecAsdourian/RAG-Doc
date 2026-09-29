package isolation

// Migration 000017 (22-02): `chunks` partitioned by HASH (organization_id),
// with row-level security on EVERY partition, tied to its tenant by the
// composite key chunks_repo_tenant_fk, carrying a vector and the model that
// produced it; `symbols` (D1) beside it.
//
// Every write below runs as the app role inside a tenant transaction, as
// production does, unless a test says otherwise. The superuser is used for
// three things only: to read a row's partition (tableoid) without RLS in
// the way, to insert rows the key must refuse with RLS out of the way, and
// to run the drift check, which refuses to run under RLS.
//
// THE PREMISE EACH CROSS-PARTITION TEST ASSERTS: two random organizations
// land in the same partition 1 time in 64, and a test that addressed "B's
// partition" while A's rows were in it would prove nothing about the
// partition's own policy. So the two tenants' partitions are computed from
// the hash first (satisfies_hash_partition), a third organization is made
// if they collide, and the placement is then read back through tableoid.
//
// `session_replication_role = replica` appears in exactly one test here,
// inside a transaction that is never committed, to PIN the fact that the
// key does not hold under it (22-CONTEXT P3's correction). Nothing else in
// this file may use it: several tests exist to prove a foreign key.

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"strings"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/stretchr/testify/require"
)

const (
	chunksPartitions   = 64
	chunksTenantFK     = "chunks_repo_tenant_fk"
	symbolsTenantFK    = "symbols_repo_tenant_fk"
	tenantPolicyName   = "tenant_isolation"
	tenantAssertName   = "trg_assert_tenant"
	scalarTenantPolicy = "(organization_id = (current_setting('app.current_tenant'::text, true))::uuid)"
)

// =====================================================================
// 1. The guard: every partition enforces the tenant policy by itself
// =====================================================================

// TestChunksPartition_EveryPartitionEnforcesRowLevelSecurity is the guard
// 22-CONTEXT P2 asks for. Row-level security on the parent does not reach a
// partition (measured: with only the parent secured, all 64 showed
// relrowsecurity = false and the app role read and overwrote another
// tenant's row through one). So every partition must carry, on its own:
// RLS enabled, RLS forced, the same scalar policy as the parent, and the
// tenant-assertion trigger.
func TestChunksPartition_EveryPartitionEnforcesRowLevelSecurity(t *testing.T) {
	pool := SetupTestDB(t)
	ctx := context.Background()

	// The parent is partitioned, secured, and its policy is the scalar one:
	// equality on the partition key, which is what lets the planner prune.
	// 000008's two-hop EXISTS form would not.
	var kind string
	var rls, force bool
	require.NoError(t, pool.QueryRow(ctx,
		`SELECT relkind::text, relrowsecurity, relforcerowsecurity FROM pg_class
		 WHERE oid = 'public.chunks'::regclass`).Scan(&kind, &rls, &force))
	require.Equal(t, "p", kind, "chunks must be a partitioned table")
	require.True(t, rls && force, "chunks must have row-level security enabled and forced")
	require.Equal(t, scalarTenantPolicy, policyQual(t, pool, "chunks"),
		"the parent's policy must be scalar equality on organization_id")

	partitions := chunkPartitions(t, pool)
	require.Len(t, partitions, chunksPartitions, "exactly 64 partitions")
	for i := 0; i < chunksPartitions; i++ {
		require.Contains(t, partitions, fmt.Sprintf("chunks_p%d", i))
	}

	for _, p := range partitions {
		t.Run(p, func(t *testing.T) {
			var rls, force bool
			require.NoError(t, pool.QueryRow(ctx,
				`SELECT relrowsecurity, relforcerowsecurity FROM pg_class
				 WHERE oid = ('public.' || $1)::regclass`, p).Scan(&rls, &force))
			require.True(t, rls, "%s: row-level security must be ENABLED on the partition itself", p)
			require.True(t, force, "%s: row-level security must be FORCED on the partition itself", p)
			require.Equal(t, scalarTenantPolicy, policyQual(t, pool, p),
				"%s: the partition's policy must equal the parent's", p)
			require.True(t, hasTrigger(t, pool, p, tenantAssertName),
				"%s: trg_assert_tenant must be cloned onto the partition", p)
		})
	}

	// symbols: unpartitioned (P7), secured the same way.
	require.NoError(t, pool.QueryRow(ctx,
		`SELECT relkind::text, relrowsecurity, relforcerowsecurity FROM pg_class
		 WHERE oid = 'public.symbols'::regclass`).Scan(&kind, &rls, &force))
	require.Equal(t, "r", kind, "symbols is unpartitioned")
	require.True(t, rls && force, "symbols must have row-level security enabled and forced")
	require.Equal(t, scalarTenantPolicy, policyQual(t, pool, "symbols"))
	require.True(t, hasTrigger(t, pool, "symbols", tenantAssertName))
}

// TestChunksPartition_SchemaShape pins the constraints and columns the rest
// of this file relies on, by definition rather than by name alone.
func TestChunksPartition_SchemaShape(t *testing.T) {
	pool := SetupTestDB(t)
	ctx := context.Background()

	constraints := []struct{ table, name, def string }{
		{"chunks", "chunks_pkey", "PRIMARY KEY (organization_id, id)"},
		{"chunks", chunksTenantFK,
			"FOREIGN KEY (repository_id, organization_id) REFERENCES repositories(id, organization_id) ON DELETE CASCADE"},
		{"chunks", "chunks_symbol_id_fkey", "FOREIGN KEY (symbol_id) REFERENCES symbols(id) ON DELETE SET NULL"},
		{"symbols", symbolsTenantFK,
			"FOREIGN KEY (repository_id, organization_id) REFERENCES repositories(id, organization_id) ON DELETE CASCADE"},
	}
	for _, c := range constraints {
		var def string
		var validated bool
		require.NoError(t, pool.QueryRow(ctx,
			`SELECT pg_get_constraintdef(oid), convalidated FROM pg_constraint
			 WHERE conrelid = $1::regclass AND conname = $2`, c.table, c.name,
		).Scan(&def, &validated), "constraint %s on %s must exist", c.name, c.table)
		require.Equal(t, c.def, def, "constraint %s", c.name)
		require.True(t, validated, "constraint %s must be validated", c.name)
	}

	// P17: the key from retrievals into chunks is gone; the column stays.
	var n int
	require.NoError(t, pool.QueryRow(ctx,
		`SELECT count(*) FROM pg_constraint WHERE conrelid = 'retrievals'::regclass AND conname = 'retrievals_chunk_id_fkey'`,
	).Scan(&n))
	require.Zero(t, n, "retrievals_chunk_id_fkey cannot exist against a partitioned chunks (P17)")
	require.NoError(t, pool.QueryRow(ctx,
		`SELECT count(*) FROM pg_attribute WHERE attrelid = 'retrievals'::regclass AND attname = 'chunk_id' AND NOT attisdropped`,
	).Scan(&n))
	require.Equal(t, 1, n, "retrievals.chunk_id stays")

	columns := []struct{ column, typ string }{
		{"organization_id", "uuid"},
		{"embedding", "vector(1536)"},
		{"embedding_model", "text"},
	}
	for _, c := range columns {
		var typ string
		var notNull bool
		require.NoError(t, pool.QueryRow(ctx,
			`SELECT format_type(atttypid, atttypmod), attnotnull FROM pg_attribute
			 WHERE attrelid = 'chunks'::regclass AND attname = $1 AND NOT attisdropped`, c.column,
		).Scan(&typ, &notNull), "chunks.%s must exist", c.column)
		require.Equal(t, c.typ, typ, "chunks.%s type", c.column)
		require.True(t, notNull, "chunks.%s must be NOT NULL", c.column)
	}
	var symbolNullable bool
	require.NoError(t, pool.QueryRow(ctx,
		`SELECT NOT attnotnull FROM pg_attribute
		 WHERE attrelid = 'chunks'::regclass AND attname = 'symbol_id' AND NOT attisdropped`,
	).Scan(&symbolNullable))
	require.True(t, symbolNullable, "chunks.symbol_id is nullable (P7)")

	// The partial index that keeps ON DELETE SET NULL off 64 full scans.
	var indexDef string
	require.NoError(t, pool.QueryRow(ctx,
		`SELECT indexdef FROM pg_indexes WHERE tablename = 'chunks' AND indexname = 'idx_chunks_symbol_id'`,
	).Scan(&indexDef))
	require.Contains(t, indexDef, "WHERE (symbol_id IS NOT NULL)")
}

// =====================================================================
// 2. The measured leak, written as a test
// =====================================================================

// TestChunksPartition_TenantACannotReachTenantBsPartition is the leak
// 22-RESEARCH.md measured with only the parent secured, run against the
// committed schema. As tenant A, addressing tenant B's partition by name:
// SELECT returns nothing, UPDATE matches nothing, DELETE matches nothing,
// and an INSERT of B's row is refused by the partition's own policy. Then
// what an UNSCOPED reader gets, pinned both ways (ISS-013).
func TestChunksPartition_TenantACannotReachTenantBsPartition(t *testing.T) {
	pool := SetupTestDB(t)
	ctx := context.Background()

	withTwoOrgsInDistinctPartitions(t, pool, func(orgA, orgB *TestOrg) {
		chunkA := commitChunk(t, pool, orgA, "a-marker")
		chunkB := commitChunk(t, pool, orgB, "b-marker")

		// The premise, read back through tableoid as the superuser: the two
		// rows really are in different partitions, and in the ones the hash
		// predicted.
		var partA, partB string
		WithSuperuserConn(t, pool, func(conn *pgx.Conn) {
			partA = partitionHolding(t, conn, chunkA)
			partB = partitionHolding(t, conn, chunkB)
		})
		require.NotEqual(t, partA, partB, "premise: the two tenants' rows are in different partitions")
		require.Equal(t, partitionFor(t, pool, orgA.ID), partA)
		require.Equal(t, partitionFor(t, pool, orgB.ID), partB)
		t.Logf("orgA in %s, orgB in %s", partA, partB)

		partBIdent := pgx.Identifier{partB}.Sanitize()

		// Positive control: the partition IS readable, by its own tenant.
		txB, err := TenantScope(ctx, pool, orgB.ID)
		require.NoError(t, err)
		require.Equal(t, 1, countRows(t, txB,
			`SELECT count(*) FROM `+partBIdent+` WHERE id = $1`, chunkB),
			"orgB must see its own row through its own partition")
		require.NoError(t, txB.Rollback(ctx))

		// As A, directly against B's partition.
		txA, err := TenantScope(ctx, pool, orgA.ID)
		require.NoError(t, err)
		defer func() { _ = txA.Rollback(ctx) }()

		require.Zero(t, countRows(t, txA,
			`SELECT count(*) FROM `+partBIdent+` WHERE id = $1`, chunkB),
			"SELECT on B's partition as A must return nothing")
		require.Zero(t, countRows(t, txA, `SELECT count(*) FROM `+partBIdent),
			"the whole of B's partition must be empty to A")

		tag, err := txA.Exec(ctx,
			`UPDATE `+partBIdent+` SET content = 'overwritten by A' WHERE id = $1`, chunkB)
		require.NoError(t, err)
		require.Zero(t, tag.RowsAffected(), "UPDATE on B's partition as A must match nothing (it matched 1 before P2)")

		tag, err = txA.Exec(ctx, `DELETE FROM `+partBIdent+` WHERE id = $1`, chunkB)
		require.NoError(t, err)
		require.Zero(t, tag.RowsAffected(), "DELETE on B's partition as A must match nothing")

		// INSERT of a row that belongs to B, into B's partition, as A: the
		// partition's own policy refuses it (FOR ALL applies USING to new
		// rows too). A 23514 here would mean the row was refused by the
		// partition constraint instead, which says nothing about RLS.
		_, err = txA.Exec(ctx, `SAVEPOINT direct_insert`)
		require.NoError(t, err)
		var runB string
		WithSuperuserConn(t, pool, func(conn *pgx.Conn) {
			require.NoError(t, conn.QueryRow(ctx,
				`SELECT ingestion_run_id::text FROM chunks WHERE id = $1`, chunkB).Scan(&runB))
		})
		_, err = txA.Exec(ctx, `INSERT INTO `+partBIdent+`
			   (organization_id, ingestion_run_id, repository_id, file_path,
			    start_line, end_line, content, content_hash, embedding, embedding_model)
			 VALUES ($1, $2, $3, 'smuggled.go', 1, 1, 'smuggled', 'h-smuggled', `+TestEmbeddingSQL+`, $4)`,
			orgB.ID, runB, orgB.RepoID, TestEmbeddingModel)
		pgErr := requirePgErr(t, err)
		require.Equal(t, "42501", pgErr.Code, "message: %s", pgErr.Message)
		require.Contains(t, pgErr.Message, "violates row-level security policy for table "+pgx.Identifier{partB}.Sanitize())
		_, err = txA.Exec(ctx, `ROLLBACK TO SAVEPOINT direct_insert`)
		require.NoError(t, err)
		require.NoError(t, txA.Rollback(ctx))

		// B's row is untouched, read without RLS in the way.
		WithSuperuserConn(t, pool, func(conn *pgx.Conn) {
			var content string
			require.NoError(t, conn.QueryRow(ctx,
				`SELECT content FROM chunks WHERE id = $1`, chunkB).Scan(&content))
			require.Equal(t, "b-marker", content, "B's row must be exactly as B wrote it")
		})

		// UNSCOPED, pinned both ways (ISS-013). A fresh connection has
		// app.current_tenant unset: the policy yields NULL, the read returns
		// nothing and raises nothing. Once that connection has COMMITTED a
		// SET LOCAL the setting reads '' for the rest of its life, and
		// ''::uuid raises 22P02. Both are "nothing leaks"; neither is the
		// other, and a pooled connection can be in either state.
		fresh := connectAsAppRole(t, pool)
		require.Zero(t, countRows(t, fresh, `SELECT count(*) FROM `+partBIdent+` WHERE id = $1`, chunkB),
			"a fresh unscoped connection must see nothing through the partition")
		require.Zero(t, countRows(t, fresh, `SELECT count(*) FROM chunks WHERE id = $1`, chunkB),
			"a fresh unscoped connection must see nothing through the parent")

		poison, err := fresh.Begin(ctx)
		require.NoError(t, err)
		_, err = poison.Exec(ctx, fmt.Sprintf("SET LOCAL app.current_tenant = '%s'", orgA.ID))
		require.NoError(t, err)
		require.NoError(t, poison.Commit(ctx))

		var n int
		err = fresh.QueryRow(ctx, `SELECT count(*) FROM `+partBIdent+` WHERE id = $1`, chunkB).Scan(&n)
		pgErr = requirePgErr(t, err)
		require.Equal(t, "22P02", pgErr.Code, "after a committed SET LOCAL the partition's policy casts ''::uuid")
		require.Contains(t, pgErr.Message, `invalid input syntax for type uuid: ""`)
		err = fresh.QueryRow(ctx, `SELECT count(*) FROM chunks WHERE id = $1`, chunkB).Scan(&n)
		pgErr = requirePgErr(t, err)
		require.Equal(t, "22P02", pgErr.Code, "and so does the parent's")

		AssertNoChunkTenantDrift(t, pool)
	})
}

// =====================================================================
// 3. Pruning: the policy alone narrows a query to one partition
// =====================================================================

// TestChunksPartition_ThePolicyAlonePrunesToOnePartition is D2's
// verification: EXPLAIN on the production query shapes, with NO
// organization_id anywhere in the SQL, shows `Subplans Removed: 63`. The
// scalar policy is what makes that true; 000008's EXISTS form would scan
// all 64 (the mutation in 22-02-SUMMARY.md).
func TestChunksPartition_ThePolicyAlonePrunesToOnePartition(t *testing.T) {
	pool := SetupTestDB(t)
	ctx := context.Background()

	WithTwoOrgs(t, pool, func(orgA, _ *TestOrg) {
		commitChunk(t, pool, orgA, "prune-marker")

		queryVector := "[" + strings.TrimSuffix(strings.Repeat("0.01,", 1536), ",") + "]"
		shapes := []struct {
			name, sql string
			args      []any
		}{
			// The vector leg 22-03 writes: the query vector as a bound
			// parameter, cosine distance, a repository filter, a LIMIT.
			{"vector leg", `EXPLAIN (COSTS OFF)
				SELECT id FROM chunks WHERE repository_id = $1
				ORDER BY embedding <=> $2::vector LIMIT 10`, []any{orgA.RepoID, queryVector}},
			// The keyword leg, as fts_retriever.py writes it.
			{"keyword leg", `EXPLAIN (COSTS OFF)
				SELECT id FROM chunks WHERE repository_id = $1
				  AND (to_tsvector('english', content) @@ plainto_tsquery('english', $2)
				       OR to_tsvector('english', COALESCE(breadcrumb, '')) @@ plainto_tsquery('english', $2))`,
				[]any{orgA.RepoID, "marker"}},
		}
		for _, s := range shapes {
			t.Run(s.name, func(t *testing.T) {
				tx, err := TenantScope(ctx, pool, orgA.ID)
				require.NoError(t, err)
				defer func() { _ = tx.Rollback(ctx) }()

				rows, err := tx.Query(ctx, s.sql, s.args...)
				require.NoError(t, err)
				plan, err := pgx.CollectRows(rows, pgx.RowTo[string])
				require.NoError(t, err)
				text := strings.Join(plan, "\n")

				require.Contains(t, text, fmt.Sprintf("Subplans Removed: %d", chunksPartitions-1),
					"the policy must prune to one partition; plan:\n%s", text)
				scanned := 0
				for _, line := range plan {
					if strings.Contains(line, " on chunks_p") {
						scanned++
					}
				}
				require.Equal(t, 1, scanned, "exactly one partition may remain in the plan:\n%s", text)
			})
		}
	})
}

// =====================================================================
// 4. Tenancy by key (P3), and the columns every writer must supply
// =====================================================================

// TestChunksPartition_AMisfiledRowIsRefusedByTheKey: a chunk or symbol whose
// organization_id is not its repository's gets 23503 on the named key. As
// the app role first, which is the production path, then as the superuser
// with row-level security out of the way, which proves it is the key and
// not the policy doing the refusing.
func TestChunksPartition_AMisfiledRowIsRefusedByTheKey(t *testing.T) {
	pool := SetupTestDB(t)
	ctx := context.Background()

	WithTwoOrgs(t, pool, func(orgA, orgB *TestOrg) {
		runA := commitRun(t, pool, orgA, "misfiled")

		// As A: A's own organization_id, B's repository. The row passes A's
		// policy (it claims A) and the key refuses the pair.
		t.Run("chunk, as the app role", func(t *testing.T) {
			tx, err := TenantScope(ctx, pool, orgA.ID)
			require.NoError(t, err)
			defer func() { _ = tx.Rollback(ctx) }()
			_, err = tx.Exec(ctx, TestChunkInsertSQL,
				orgA.ID, runA, orgB.RepoID, "misfiled.go", 1, 1, "misfiled", "h-misfiled")
			pgErr := requirePgErr(t, err)
			require.Equal(t, "23503", pgErr.Code, "message: %s", pgErr.Message)
			require.Equal(t, chunksTenantFK, pgErr.ConstraintName)
		})
		t.Run("symbol, as the app role", func(t *testing.T) {
			tx, err := TenantScope(ctx, pool, orgA.ID)
			require.NoError(t, err)
			defer func() { _ = tx.Rollback(ctx) }()
			_, err = tx.Exec(ctx, symbolInsertSQL, uuid.NewString(), orgA.ID, orgB.RepoID, "misfiled.go", "misfiled", "sha-1")
			pgErr := requirePgErr(t, err)
			require.Equal(t, "23503", pgErr.Code, "message: %s", pgErr.Message)
			require.Equal(t, symbolsTenantFK, pgErr.ConstraintName)
		})

		// The other direction as the app role: a row CLAIMING another tenant
		// is refused by A's policy before any key runs. Pinned so nobody
		// reads the key as the only guard on this path.
		t.Run("a row claiming tenant B is refused by A's policy first", func(t *testing.T) {
			tx, err := TenantScope(ctx, pool, orgA.ID)
			require.NoError(t, err)
			defer func() { _ = tx.Rollback(ctx) }()
			_, err = tx.Exec(ctx, TestChunkInsertSQL,
				orgB.ID, runA, orgA.RepoID, "claims-b.go", 1, 1, "claims b", "h-claims-b")
			pgErr := requirePgErr(t, err)
			require.Equal(t, "42501", pgErr.Code, "message: %s", pgErr.Message)
			require.Contains(t, pgErr.Message, "violates row-level security policy")
		})

		// As the superuser, which bypasses row-level security: only the key
		// stands, and it refuses the same pairs. trg_assert_tenant still
		// applies, hence the tenant.
		t.Run("chunk and symbol, as the superuser with RLS bypassed", func(t *testing.T) {
			WithSuperuserConn(t, pool, func(conn *pgx.Conn) {
				tx, err := conn.Begin(ctx)
				require.NoError(t, err)
				defer func() { _ = tx.Rollback(ctx) }()
				_, err = tx.Exec(ctx, fmt.Sprintf("SET LOCAL app.current_tenant = '%s'", orgA.ID))
				require.NoError(t, err)

				_, err = tx.Exec(ctx, "SAVEPOINT misfiled_chunk")
				require.NoError(t, err)
				_, err = tx.Exec(ctx, TestChunkInsertSQL,
					orgA.ID, runA, orgB.RepoID, "misfiled.go", 1, 1, "misfiled", "h-misfiled")
				pgErr := requirePgErr(t, err)
				require.Equal(t, "23503", pgErr.Code, "message: %s", pgErr.Message)
				require.Equal(t, chunksTenantFK, pgErr.ConstraintName)
				_, err = tx.Exec(ctx, "ROLLBACK TO SAVEPOINT misfiled_chunk")
				require.NoError(t, err)

				_, err = tx.Exec(ctx, symbolInsertSQL, uuid.NewString(), orgA.ID, orgB.RepoID, "misfiled.go", "misfiled", "sha-1")
				pgErr = requirePgErr(t, err)
				require.Equal(t, "23503", pgErr.Code, "message: %s", pgErr.Message)
				require.Equal(t, symbolsTenantFK, pgErr.ConstraintName)
			})
		})

		AssertNoChunkTenantDrift(t, pool)
	})
}

// TestChunksPartition_EveryWriterMustSupplyTheNewColumns: a chunk without an
// embedding, or without the model that produced it, is refused (23502). A
// chunk without organization_id is refused too, and by NOT NULL rather than
// filled in: nothing can fill it, because a BEFORE trigger cannot move a row
// to another partition (0A000, measured).
func TestChunksPartition_EveryWriterMustSupplyTheNewColumns(t *testing.T) {
	pool := SetupTestDB(t)
	ctx := context.Background()

	WithTwoOrgs(t, pool, func(orgA, _ *TestOrg) {
		runA := commitRun(t, pool, orgA, "columns")

		cases := []struct{ column, sql string }{
			{"embedding", `INSERT INTO chunks
			   (organization_id, ingestion_run_id, repository_id, file_path, start_line, end_line,
			    content, content_hash, embedding_model)
			 VALUES ($1, $2, $3, 'x.go', 1, 1, 'x', 'h-x', 'test-fixed')`},
			{"embedding_model", `INSERT INTO chunks
			   (organization_id, ingestion_run_id, repository_id, file_path, start_line, end_line,
			    content, content_hash, embedding)
			 VALUES ($1, $2, $3, 'x.go', 1, 1, 'x', 'h-x', ` + TestEmbeddingSQL + `)`},
		}
		for _, c := range cases {
			t.Run("without "+c.column, func(t *testing.T) {
				tx, err := TenantScope(ctx, pool, orgA.ID)
				require.NoError(t, err)
				defer func() { _ = tx.Rollback(ctx) }()
				_, err = tx.Exec(ctx, c.sql, orgA.ID, runA, orgA.RepoID)
				pgErr := requirePgErr(t, err)
				require.Equal(t, "23502", pgErr.Code, "message: %s", pgErr.Message)
				require.Equal(t, c.column, pgErr.ColumnName)
			})
		}

		// Without organization_id, measured on both paths. As the app role
		// the POLICY refuses the row first: `NULL = tenant` is not true, so
		// the parent's WITH CHECK fails with 42501 before NOT NULL is looked
		// at. As the superuser (RLS bypassed) a NULL key is hashed and routed
		// like any value, and the partition's NOT NULL refuses it with 23502.
		// Either way nothing fills it in.
		const withoutOrg = `INSERT INTO chunks
			   (ingestion_run_id, repository_id, file_path, start_line, end_line,
			    content, content_hash, embedding, embedding_model)
			 VALUES ($1, $2, 'x.go', 1, 1, 'x', 'h-x', ` + TestEmbeddingSQL + `, 'test-fixed')`
		t.Run("without organization_id, as the app role", func(t *testing.T) {
			tx, err := TenantScope(ctx, pool, orgA.ID)
			require.NoError(t, err)
			defer func() { _ = tx.Rollback(ctx) }()
			_, err = tx.Exec(ctx, withoutOrg, runA, orgA.RepoID)
			pgErr := requirePgErr(t, err)
			require.Equal(t, "42501", pgErr.Code, "message: %s", pgErr.Message)
			require.Contains(t, pgErr.Message, `violates row-level security policy for table "chunks"`)
		})
		t.Run("without organization_id, as the superuser", func(t *testing.T) {
			WithSuperuserConn(t, pool, func(conn *pgx.Conn) {
				tx, err := conn.Begin(ctx)
				require.NoError(t, err)
				defer func() { _ = tx.Rollback(ctx) }()
				_, err = tx.Exec(ctx, fmt.Sprintf("SET LOCAL app.current_tenant = '%s'", orgA.ID))
				require.NoError(t, err)
				_, err = tx.Exec(ctx, withoutOrg, runA, orgA.RepoID)
				pgErr := requirePgErr(t, err)
				require.Equal(t, "23502", pgErr.Code, "message: %s", pgErr.Message)
				require.Equal(t, "organization_id", pgErr.ColumnName)
			})
		})
	})
}

// =====================================================================
// 5. The key's limit, pinned (22-CONTEXT P3's correction)
// =====================================================================

// TestChunksPartition_TheKeyDoesNotHoldUnderReplicaMode pins the
// fact-check's finding so nobody re-derives the wrong claim: under
// `session_replication_role = replica` a misfiled chunk and a misfiled
// symbol INSERT CLEANLY past their keys, because foreign keys are enforced
// by triggers and replica mode switches them off. The drift check sees both
// before the transaction rolls back. This is why 000017's table comments
// forbid loading either table that way, and why the drift check runs in CI.
//
// As the superuser, in a transaction that is never committed.
func TestChunksPartition_TheKeyDoesNotHoldUnderReplicaMode(t *testing.T) {
	pool := SetupTestDB(t)
	ctx := context.Background()

	WithTwoOrgs(t, pool, func(orgA, orgB *TestOrg) {
		runA := commitRun(t, pool, orgA, "replica")
		symbolID := uuid.NewString()
		var chunkID string

		WithSuperuserConn(t, pool, func(conn *pgx.Conn) {
			tx, err := conn.Begin(ctx)
			require.NoError(t, err)
			defer func() { _ = tx.Rollback(ctx) }()

			// Transaction-scoped, so it cannot outlive this test however the
			// process dies (pkg/auth/testing.go's reasoning). No tenant is
			// set: replica mode disables trg_assert_tenant as well, which
			// the clean inserts below also show.
			_, err = tx.Exec(ctx, `SET LOCAL session_replication_role = replica`)
			require.NoError(t, err)

			require.NoError(t, tx.QueryRow(ctx, TestChunkInsertSQL,
				orgA.ID, runA, orgB.RepoID, "misfiled.go", 1, 1, "misfiled under replica", "h-replica",
			).Scan(&chunkID), "the misfiled chunk must be ACCEPTED under replica mode: the key is a trigger")
			_, err = tx.Exec(ctx, symbolInsertSQL, symbolID, orgA.ID, orgB.RepoID, "misfiled.go", "misfiled", "sha-replica")
			require.NoError(t, err, "the misfiled symbol must be ACCEPTED under replica mode")

			// The chunk is reported twice: its tenant is not its repository's,
			// and its run (A's) is not its repository's (B's) either.
			drifted, err := CheckChunkTenantDrift(ctx, tx)
			require.NoError(t, err)
			require.Equal(t, []string{
				"chunks:" + chunkID + ":run",
				"chunks:" + chunkID + ":tenant",
				"symbols:" + symbolID + ":tenant",
			}, drifted, "the drift check must report exactly the two misfiled rows")
		})

		AssertNoChunkTenantDrift(t, pool)
	})
}

// TestChunksPartition_TheSingleColumnKeysCarryNoTenancy pins PR #49's
// review finding (minor 3) so it is not re-derived as a breach, and so the
// drift check is known to cover it: the single-column keys ingestion_run_id
// and symbol_id are checked per row with row-level security bypassed, so AS
// THE APP ROLE, with no bypass of any kind, tenant A can write a chunk of
// its own repository that cites tenant B's run, or B's symbol. Not a
// boundary breach: A mis-files its own row and B cannot read it. But B
// deleting its run then cascades into A's partition, and B deleting its
// symbol nulls A's chunk, and neither composite key notices, because both
// are keyed on the repository. The drift query's :run and :symbol arms do.
// ISS-036 carries the composite keys that close this when 22.1-01 gives
// symbols a writer.
//
// One transaction on a superuser connection, switched to the app role for
// the writes and back for the check, never committed.
func TestChunksPartition_TheSingleColumnKeysCarryNoTenancy(t *testing.T) {
	pool := SetupTestDB(t)
	ctx := context.Background()

	WithTwoOrgs(t, pool, func(orgA, orgB *TestOrg) {
		runA := commitRun(t, pool, orgA, "single-a")
		runB := commitRun(t, pool, orgB, "single-b")
		symbolB := uuid.NewString()
		tx, err := TenantScope(ctx, pool, orgB.ID)
		require.NoError(t, err)
		_, err = tx.Exec(ctx, symbolInsertSQL, symbolB, orgB.ID, orgB.RepoID, "b.go", "B", "sha-b")
		require.NoError(t, err)
		require.NoError(t, tx.Commit(ctx))

		WithSuperuserConn(t, pool, func(conn *pgx.Conn) {
			tx, err := conn.Begin(ctx)
			require.NoError(t, err)
			defer func() { _ = tx.Rollback(ctx) }()

			// As A, with nothing lifted.
			_, err = tx.Exec(ctx, "SET ROLE "+appRole)
			require.NoError(t, err)
			_, err = tx.Exec(ctx, fmt.Sprintf("SET LOCAL app.current_tenant = '%s'", orgA.ID))
			require.NoError(t, err)

			var citesRunB, citesSymbolB string
			require.NoError(t, tx.QueryRow(ctx, TestChunkInsertSQL,
				orgA.ID, runB, orgA.RepoID, "cites-b-run.go", 1, 1, "cites B's run", "h-b-run",
			).Scan(&citesRunB), "a chunk of A's repository citing B's run is ACCEPTED as A")
			require.NoError(t, tx.QueryRow(ctx, `INSERT INTO chunks
			   (organization_id, ingestion_run_id, repository_id, symbol_id, file_path,
			    start_line, end_line, content, content_hash, embedding, embedding_model)
			 VALUES ($1, $2, $3, $4, 'cites-b-symbol.go', 1, 1, 'cites B''s symbol', 'h-b-symbol', `+TestEmbeddingSQL+`, $5)
			 RETURNING id::text`,
				orgA.ID, runA, orgA.RepoID, symbolB, TestEmbeddingModel,
			).Scan(&citesSymbolB), "a chunk of A's repository citing B's symbol is ACCEPTED as A")

			// Back to the superuser: the drift check sees both, and only both.
			_, err = tx.Exec(ctx, "RESET ROLE")
			require.NoError(t, err)
			want := []string{"chunks:" + citesRunB + ":run", "chunks:" + citesSymbolB + ":symbol"}
			sort.Strings(want)
			drifted, err := CheckChunkTenantDrift(ctx, tx)
			require.NoError(t, err)
			require.Equal(t, want, drifted)

			// The consequence: B, deleting its own run and symbol, reaches
			// A's partition through the cascade and the SET NULL, because
			// referential actions bypass row-level security too.
			_, err = tx.Exec(ctx, "SET ROLE "+appRole)
			require.NoError(t, err)
			_, err = tx.Exec(ctx, fmt.Sprintf("SET LOCAL app.current_tenant = '%s'", orgB.ID))
			require.NoError(t, err)
			tag, err := tx.Exec(ctx, `DELETE FROM ingestion_runs WHERE id = $1`, runB)
			require.NoError(t, err)
			require.EqualValues(t, 1, tag.RowsAffected())
			tag, err = tx.Exec(ctx, `DELETE FROM symbols WHERE id = $1`, symbolB)
			require.NoError(t, err)
			require.EqualValues(t, 1, tag.RowsAffected())
			_, err = tx.Exec(ctx, "RESET ROLE")
			require.NoError(t, err)

			require.Zero(t, countRows(t, tx, `SELECT count(*) FROM chunks WHERE id = $1`, citesRunB),
				"B's run delete removed A's chunk from A's partition")
			var symbol *string
			require.NoError(t, tx.QueryRow(ctx, `SELECT symbol_id::text FROM chunks WHERE id = $1`, citesSymbolB).Scan(&symbol))
			require.Nil(t, symbol, "B's symbol delete nulled A's chunk")
			drifted, err = CheckChunkTenantDrift(ctx, tx)
			require.NoError(t, err)
			require.Empty(t, drifted, "and nothing is left for the check to report")
		})

		AssertNoChunkTenantDrift(t, pool)
	})
}

// =====================================================================
// 6. Cascades
// =====================================================================

// TestChunksPartition_DeletingARepositoryRemovesItsChunksAndSymbols: the
// cascade through chunks_repo_tenant_fk and symbols_repo_tenant_fk, as the
// app role under the tenant, checked without RLS afterwards so "gone" is not
// "invisible".
func TestChunksPartition_DeletingARepositoryRemovesItsChunksAndSymbols(t *testing.T) {
	pool := SetupTestDB(t)
	ctx := context.Background()

	WithTwoOrgs(t, pool, func(orgA, orgB *TestOrg) {
		repo := commitRepository(t, pool, orgA, "doomed")
		tx, err := TenantScope(ctx, pool, orgA.ID)
		require.NoError(t, err)
		var runID, chunkID, symbolID string
		require.NoError(t, tx.QueryRow(ctx,
			`INSERT INTO ingestion_runs (repository_id, commit_sha, branch, status)
			 VALUES ($1, repeat('d', 40), 'main', 'completed') RETURNING id::text`, repo).Scan(&runID))
		require.NoError(t, tx.QueryRow(ctx, TestChunkInsertSQL,
			orgA.ID, runID, repo, "doomed.go", 1, 1, "doomed", "h-doomed").Scan(&chunkID))
		symbolID = uuid.NewString()
		_, err = tx.Exec(ctx, symbolInsertSQL, symbolID, orgA.ID, repo, "doomed.go", "doomed", "sha-doomed")
		require.NoError(t, err)
		require.NoError(t, tx.Commit(ctx))

		// The other tenant's rows, as a control that the cascade is scoped by
		// key and not by anything broader.
		chunkB := commitChunk(t, pool, orgB, "survivor")

		tx, err = TenantScope(ctx, pool, orgA.ID)
		require.NoError(t, err)
		tag, err := tx.Exec(ctx, `DELETE FROM repositories WHERE id = $1`, repo)
		require.NoError(t, err)
		require.EqualValues(t, 1, tag.RowsAffected())
		require.NoError(t, tx.Commit(ctx))

		WithSuperuserConn(t, pool, func(conn *pgx.Conn) {
			require.Zero(t, countRows(t, conn, `SELECT count(*) FROM chunks WHERE id = $1`, chunkID),
				"the chunk must be gone, not merely invisible")
			require.Zero(t, countRows(t, conn, `SELECT count(*) FROM symbols WHERE id = $1`, symbolID),
				"the symbol must be gone")
			require.Zero(t, countRows(t, conn, `SELECT count(*) FROM ingestion_runs WHERE id = $1`, runID))
			require.Equal(t, 1, countRows(t, conn, `SELECT count(*) FROM chunks WHERE id = $1`, chunkB),
				"the other tenant's chunk survives")
		})

		AssertNoChunkTenantDrift(t, pool)
	})
}

// TestChunksPartition_DeletingAnOrganizationCompletes is D5's verification
// item 6 for the new tables: the cascade organizations -> projects ->
// repositories -> chunks and symbols runs to completion as the app role
// under the tenant, with every partition's trigger satisfied on the way.
func TestChunksPartition_DeletingAnOrganizationCompletes(t *testing.T) {
	pool := SetupTestDB(t)
	ctx := context.Background()

	org := createOrg(t, pool, "iso-orgdel-"+shortToken())
	t.Cleanup(func() { cleanupOrg(ctx, pool, org) })

	chunkID := commitChunk(t, pool, org, "org-delete")
	tx, err := TenantScope(ctx, pool, org.ID)
	require.NoError(t, err)
	symbolID := uuid.NewString()
	_, err = tx.Exec(ctx, symbolInsertSQL, symbolID, org.ID, org.RepoID, "x.go", "x", "sha-x")
	require.NoError(t, err)
	require.NoError(t, tx.Commit(ctx))

	tx, err = TenantScope(ctx, pool, org.ID)
	require.NoError(t, err)
	tag, err := tx.Exec(ctx, `DELETE FROM organizations WHERE id = $1`, org.ID)
	require.NoError(t, err, "deleting the organization must complete through every cascade")
	require.EqualValues(t, 1, tag.RowsAffected())
	require.NoError(t, tx.Commit(ctx))

	WithSuperuserConn(t, pool, func(conn *pgx.Conn) {
		require.Zero(t, countRows(t, conn, `SELECT count(*) FROM chunks WHERE id = $1`, chunkID))
		require.Zero(t, countRows(t, conn, `SELECT count(*) FROM symbols WHERE id = $1`, symbolID))
		require.Zero(t, countRows(t, conn, `SELECT count(*) FROM repositories WHERE id = $1`, org.RepoID))
		require.Zero(t, countRows(t, conn, `SELECT count(*) FROM projects WHERE id = $1`, org.ProjectID))
	})

	AssertNoChunkTenantDrift(t, pool)
}

// TestChunksPartition_DeletingASymbolNullsTheChunksThatPointAtIt: P7's
// ON DELETE SET NULL, through the partial index, as the app role.
func TestChunksPartition_DeletingASymbolNullsTheChunksThatPointAtIt(t *testing.T) {
	pool := SetupTestDB(t)
	ctx := context.Background()

	WithTwoOrgs(t, pool, func(orgA, _ *TestOrg) {
		runA := commitRun(t, pool, orgA, "setnull")
		symbolID := uuid.NewString()

		tx, err := TenantScope(ctx, pool, orgA.ID)
		require.NoError(t, err)
		_, err = tx.Exec(ctx, symbolInsertSQL, symbolID, orgA.ID, orgA.RepoID, "f.go", "F", "sha-f")
		require.NoError(t, err)
		var linked, unlinked string
		require.NoError(t, tx.QueryRow(ctx, `INSERT INTO chunks
		   (organization_id, ingestion_run_id, repository_id, symbol_id, file_path,
		    start_line, end_line, content, content_hash, embedding, embedding_model)
		 VALUES ($1, $2, $3, $4, 'f.go', 1, 1, 'func F() {}', 'h-f', `+TestEmbeddingSQL+`, $5)
		 RETURNING id::text`,
			orgA.ID, runA, orgA.RepoID, symbolID, TestEmbeddingModel).Scan(&linked))
		require.NoError(t, tx.QueryRow(ctx, TestChunkInsertSQL,
			orgA.ID, runA, orgA.RepoID, "g.go", 1, 1, "func G() {}", "h-g").Scan(&unlinked))
		require.NoError(t, tx.Commit(ctx))

		tx, err = TenantScope(ctx, pool, orgA.ID)
		require.NoError(t, err)
		tag, err := tx.Exec(ctx, `DELETE FROM symbols WHERE id = $1`, symbolID)
		require.NoError(t, err)
		require.EqualValues(t, 1, tag.RowsAffected())
		require.NoError(t, tx.Commit(ctx))

		tx, err = TenantScope(ctx, pool, orgA.ID)
		require.NoError(t, err)
		defer func() { _ = tx.Rollback(ctx) }()
		var symbol *string
		require.NoError(t, tx.QueryRow(ctx, `SELECT symbol_id::text FROM chunks WHERE id = $1`, linked).Scan(&symbol))
		require.Nil(t, symbol, "the chunk's symbol_id must be NULL after its symbol is deleted")
		require.Equal(t, 1, countRows(t, tx, `SELECT count(*) FROM chunks WHERE id = $1`, linked),
			"the chunk itself survives")
		require.Equal(t, 1, countRows(t, tx, `SELECT count(*) FROM chunks WHERE id = $1`, unlinked))

		AssertNoChunkTenantDrift(t, pool)
	})
}

// =====================================================================
// 7. D1's resurrection upsert, on PostgreSQL 16
// =====================================================================

// TestChunksPartition_SymbolResurrectionUpsert: a symbol that was archived
// and comes back computes the same deterministic id, so a plain INSERT
// collides (23505, measured by D1's third review), and the ingest must
// UPSERT AND UNARCHIVE instead. The statement is D1's, verbatim; this pins
// that it runs on the committed table, under the tenant, as the app role.
func TestChunksPartition_SymbolResurrectionUpsert(t *testing.T) {
	pool := SetupTestDB(t)
	ctx := context.Background()

	WithTwoOrgs(t, pool, func(orgA, _ *TestOrg) {
		symbolID := uuid.NewString()

		tx, err := TenantScope(ctx, pool, orgA.ID)
		require.NoError(t, err)
		_, err = tx.Exec(ctx, symbolInsertSQL, symbolID, orgA.ID, orgA.RepoID, "r.go", "R", "sha-1")
		require.NoError(t, err)
		_, err = tx.Exec(ctx, `UPDATE symbols SET archived_at = NOW() WHERE id = $1`, symbolID)
		require.NoError(t, err)
		require.NoError(t, tx.Commit(ctx))

		// The collision the upsert exists for.
		tx, err = TenantScope(ctx, pool, orgA.ID)
		require.NoError(t, err)
		_, err = tx.Exec(ctx, symbolInsertSQL, symbolID, orgA.ID, orgA.RepoID, "r.go", "R", "sha-2")
		pgErr := requirePgErr(t, err)
		require.Equal(t, "23505", pgErr.Code, "a returning symbol collides on its deterministic id")
		require.Equal(t, "symbols_pkey", pgErr.ConstraintName)
		require.NoError(t, tx.Rollback(ctx))

		// D1's statement.
		tx, err = TenantScope(ctx, pool, orgA.ID)
		require.NoError(t, err)
		_, err = tx.Exec(ctx, `
			INSERT INTO symbols (id, organization_id, repository_id, file_path, symbol_path, kind,
			                     start_line, end_line, span_digest, first_seen_commit, last_seen_commit)
			VALUES ($1, $2, $3, 'r.go', 'R', 'function', 3, 9, 'sha-2', 'commit-2', 'commit-2')
			ON CONFLICT (id) DO UPDATE
			SET archived_at       = NULL,
			    span_digest       = EXCLUDED.span_digest,
			    start_line        = EXCLUDED.start_line,
			    end_line          = EXCLUDED.end_line,
			    last_seen_commit  = EXCLUDED.last_seen_commit`,
			symbolID, orgA.ID, orgA.RepoID)
		require.NoError(t, err)
		require.NoError(t, tx.Commit(ctx))

		tx, err = TenantScope(ctx, pool, orgA.ID)
		require.NoError(t, err)
		defer func() { _ = tx.Rollback(ctx) }()
		var archived *string
		var digest, first, last string
		var start, end int
		require.NoError(t, tx.QueryRow(ctx,
			`SELECT archived_at::text, span_digest, first_seen_commit, last_seen_commit, start_line, end_line
			 FROM symbols WHERE id = $1`, symbolID).Scan(&archived, &digest, &first, &last, &start, &end))
		require.Nil(t, archived, "resurrected: archived_at cleared")
		require.Equal(t, "sha-2", digest)
		require.Equal(t, "commit-1", first, "first_seen_commit is the ORIGINAL sighting")
		require.Equal(t, "commit-2", last)
		require.Equal(t, 3, start)
		require.Equal(t, 9, end)

		AssertNoChunkTenantDrift(t, pool)
	})
}

// =====================================================================
// 8. The drift check itself can fail
// =====================================================================

// TestChunksPartition_DriftCheckDetectsDrift proves the check by removing
// the keys and writing drifted and orphaned rows. In a SCRATCH DATABASE, not
// the shared one: dropping a key takes ACCESS EXCLUSIVE on both tables and
// deadlocked CI once (ISS-032), and a dropped key is exactly the state no
// other package must ever observe. The scratch database is migrated in
// full, owned by the superuser (SuperuserRole: what is proven here is the
// query, not the deployment shape), seeded by hand, and dropped in cleanup.
func TestChunksPartition_DriftCheckDetectsDrift(t *testing.T) {
	pool := SetupTestDB(t)
	ctx := context.Background()

	db := ScratchDatabase(t, pool, SuperuserRole)
	super := connectScratch(t, db.SuperuserDSN)
	require.NoError(t, applyMigrations(db.OwnerDSN, migrationsDir()))

	// Two organizations, one repository each, one run for A; written as the
	// superuser under each organization's tenant (trg_assert_tenant applies
	// to superusers too).
	orgA, orgB := uuid.NewString(), uuid.NewString()
	repoA, repoB := seedScratchRepository(t, super, orgA, "drift-a"), seedScratchRepository(t, super, orgB, "drift-b")
	tx, err := super.Begin(ctx)
	require.NoError(t, err)
	_, err = tx.Exec(ctx, fmt.Sprintf("SET LOCAL app.current_tenant = '%s'", orgA))
	require.NoError(t, err)
	var runA string
	require.NoError(t, tx.QueryRow(ctx,
		`INSERT INTO ingestion_runs (repository_id, commit_sha, branch, status)
		 VALUES ($1, repeat('e', 40), 'main', 'completed') RETURNING id::text`, repoA).Scan(&runA))
	require.NoError(t, tx.Commit(ctx))

	// Clean to begin with.
	drifted, err := CheckChunkTenantDrift(ctx, super)
	require.NoError(t, err)
	require.Empty(t, drifted)

	// The keys go. On the partitioned parent, DROP CONSTRAINT drops the
	// clones on every partition with it.
	_, err = super.Exec(ctx, `ALTER TABLE chunks DROP CONSTRAINT `+chunksTenantFK)
	require.NoError(t, err)
	_, err = super.Exec(ctx, `ALTER TABLE symbols DROP CONSTRAINT `+symbolsTenantFK)
	require.NoError(t, err)

	tx, err = super.Begin(ctx)
	require.NoError(t, err)
	_, err = tx.Exec(ctx, fmt.Sprintf("SET LOCAL app.current_tenant = '%s'", orgA))
	require.NoError(t, err)
	var misfiledChunk string
	require.NoError(t, tx.QueryRow(ctx, TestChunkInsertSQL,
		orgA, runA, repoB, "misfiled.go", 1, 1, "misfiled", "h-misfiled").Scan(&misfiledChunk),
		"with the key gone the misfiled chunk is representable")
	misfiledSymbol := uuid.NewString()
	_, err = tx.Exec(ctx, symbolInsertSQL, misfiledSymbol, orgA, repoB, "misfiled.go", "misfiled", "sha-m")
	require.NoError(t, err, "with the key gone the misfiled symbol is representable")
	require.NoError(t, tx.Commit(ctx))

	// The chunk twice (its tenant, and its run A's while its repository is
	// B's), the symbol once.
	want := []string{
		"chunks:" + misfiledChunk + ":run",
		"chunks:" + misfiledChunk + ":tenant",
		"symbols:" + misfiledSymbol + ":tenant",
	}
	drifted, err = CheckChunkTenantDrift(ctx, super)
	require.NoError(t, err)
	require.Equal(t, want, drifted)

	// And an ORPHAN: the repository the rows still name is deleted out from
	// under them (the single-column keys are dropped too, so the delete does
	// not cascade). A row with no repository has no truth to agree with.
	_, err = super.Exec(ctx, `ALTER TABLE chunks DROP CONSTRAINT chunks_repository_id_fkey`)
	require.NoError(t, err)
	_, err = super.Exec(ctx, `ALTER TABLE symbols DROP CONSTRAINT symbols_repository_id_fkey`)
	require.NoError(t, err)
	tx, err = super.Begin(ctx)
	require.NoError(t, err)
	_, err = tx.Exec(ctx, fmt.Sprintf("SET LOCAL app.current_tenant = '%s'", orgB))
	require.NoError(t, err)
	_, err = tx.Exec(ctx, `DELETE FROM repositories WHERE id = $1`, repoB)
	require.NoError(t, err)
	require.NoError(t, tx.Commit(ctx))

	drifted, err = CheckChunkTenantDrift(ctx, super)
	require.NoError(t, err)
	require.Equal(t, want, drifted, "orphaned rows are drift too")

	// The :run and :symbol arms on their own: a chunk of A's repository
	// citing another run of A's, and one citing a symbol of A's other
	// repository. Same tenant throughout, so the tenant arms stay silent.
	repoA2 := seedScratchRepositoryInOrg(t, super, orgA, "drift-a2")
	tx, err = super.Begin(ctx)
	require.NoError(t, err)
	_, err = tx.Exec(ctx, fmt.Sprintf("SET LOCAL app.current_tenant = '%s'", orgA))
	require.NoError(t, err)
	var runA2, wrongRun, wrongSymbol string
	require.NoError(t, tx.QueryRow(ctx,
		`INSERT INTO ingestion_runs (repository_id, commit_sha, branch, status)
		 VALUES ($1, repeat('f', 40), 'main', 'completed') RETURNING id::text`, repoA2).Scan(&runA2))
	require.NoError(t, tx.QueryRow(ctx, TestChunkInsertSQL,
		orgA, runA2, repoA, "wrong-run.go", 1, 1, "cites a run of A's other repository", "h-wrong-run").Scan(&wrongRun))
	symbolA2 := uuid.NewString()
	_, err = tx.Exec(ctx, symbolInsertSQL, symbolA2, orgA, repoA2, "a2.go", "A2", "sha-a2")
	require.NoError(t, err)
	require.NoError(t, tx.QueryRow(ctx, `INSERT INTO chunks
	   (organization_id, ingestion_run_id, repository_id, symbol_id, file_path,
	    start_line, end_line, content, content_hash, embedding, embedding_model)
	 VALUES ($1, $2, $3, $4, 'wrong-symbol.go', 1, 1, 'cites a symbol of A''s other repository', 'h-wrong-symbol', `+TestEmbeddingSQL+`, $5)
	 RETURNING id::text`,
		orgA, runA, repoA, symbolA2, TestEmbeddingModel).Scan(&wrongSymbol))
	require.NoError(t, tx.Commit(ctx))

	want = append(want, "chunks:"+wrongRun+":run", "chunks:"+wrongSymbol+":symbol")
	sort.Strings(want)
	drifted, err = CheckChunkTenantDrift(ctx, super)
	require.NoError(t, err)
	require.Equal(t, want, drifted, "a run or symbol of another repository is drift, within one tenant too")

	// Under a role subject to row-level security the check refuses to run,
	// rather than reporting "no drift" about rows it cannot see.
	_, err = super.Exec(ctx, `SET ROLE `+appRole)
	require.NoError(t, err)
	_, err = CheckChunkTenantDrift(ctx, super)
	require.ErrorContains(t, err, "bypasses row-level security")
	_, err = super.Exec(ctx, `RESET ROLE`)
	require.NoError(t, err)
}

// =====================================================================
// Helpers
// =====================================================================

// symbolInsertSQL inserts a symbol under the caller's tenant: id,
// organization_id, repository_id, file_path, symbol_path, span_digest. The
// id is random here; D1's deterministic uuid_v5 is generated by the ingest
// from 22.1-01, and nothing in this file depends on how it is derived.
const symbolInsertSQL = `INSERT INTO symbols
   (id, organization_id, repository_id, file_path, symbol_path, kind,
    start_line, end_line, span_digest, first_seen_commit, last_seen_commit)
 VALUES ($1, $2, $3, $4, $5, 'function', 1, 2, $6, 'commit-1', 'commit-1')`

// withTwoOrgsInDistinctPartitions is WithTwoOrgs plus the premise every
// cross-partition test needs: the two organizations hash to different
// partitions. When they collide (1 in 64), organizations are created until
// one does not, and every extra one is cleaned up.
func withTwoOrgsInDistinctPartitions(t *testing.T, pool *pgxpool.Pool, fn func(orgA, orgB *TestOrg)) {
	t.Helper()
	WithTwoOrgs(t, pool, func(orgA, orgB *TestOrg) {
		partA := partitionFor(t, pool, orgA.ID)
		for attempt := 0; partitionFor(t, pool, orgB.ID) == partA; attempt++ {
			require.Less(t, attempt, 16, "sixteen organizations in a row hashed to %s", partA)
			t.Logf("orgB shares %s with orgA; making another", partA)
			extra := createOrg(t, pool, fmt.Sprintf("iso-c-%s-%s", sanitizeTag(t.Name()), shortToken()))
			t.Cleanup(func() { cleanupOrg(context.Background(), pool, extra) })
			orgB = extra
		}
		fn(orgA, orgB)
	})
}

// partitionFor computes, from the hash, which partition of chunks rows of
// the given organization belong in.
func partitionFor(t *testing.T, pool *pgxpool.Pool, orgID string) string {
	t.Helper()
	var remainder int
	require.NoError(t, pool.QueryRow(context.Background(), `
		SELECT r FROM generate_series(0, $2 - 1) AS r
		WHERE satisfies_hash_partition('public.chunks'::regclass, $2, r, $1::uuid)`,
		orgID, chunksPartitions).Scan(&remainder))
	return fmt.Sprintf("chunks_p%d", remainder)
}

// partitionHolding reads which partition a chunk row is physically in.
// Read as the superuser: under RLS another tenant's row is invisible.
func partitionHolding(t *testing.T, conn *pgx.Conn, chunkID string) string {
	t.Helper()
	var name string
	require.NoError(t, conn.QueryRow(context.Background(),
		`SELECT tableoid::regclass::text FROM chunks WHERE id = $1`, chunkID).Scan(&name))
	return name
}

func chunkPartitions(t *testing.T, pool *pgxpool.Pool) []string {
	t.Helper()
	rows, err := pool.Query(context.Background(), `
		SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
		WHERE i.inhparent = 'public.chunks'::regclass ORDER BY c.relname`)
	require.NoError(t, err)
	names, err := pgx.CollectRows(rows, pgx.RowTo[string])
	require.NoError(t, err)
	return names
}

func policyQual(t *testing.T, pool *pgxpool.Pool, table string) string {
	t.Helper()
	var qual string
	require.NoError(t, pool.QueryRow(context.Background(),
		`SELECT qual FROM pg_policies WHERE schemaname = 'public' AND tablename = $1 AND policyname = $2`,
		table, tenantPolicyName).Scan(&qual), "%s: policy %s must exist", table, tenantPolicyName)
	return qual
}

func hasTrigger(t *testing.T, pool *pgxpool.Pool, table, trigger string) bool {
	t.Helper()
	var n int
	require.NoError(t, pool.QueryRow(context.Background(),
		`SELECT count(*) FROM pg_trigger WHERE tgrelid = ('public.' || $1)::regclass AND tgname = $2`,
		table, trigger).Scan(&n))
	return n == 1
}

// commitRun inserts and commits an ingestion run for org's fixture repository.
func commitRun(t *testing.T, pool *pgxpool.Pool, org *TestOrg, marker string) string {
	t.Helper()
	ctx := context.Background()
	tx, err := TenantScope(ctx, pool, org.ID)
	require.NoError(t, err)
	defer func() { _ = tx.Rollback(ctx) }()
	var runID string
	require.NoError(t, tx.QueryRow(ctx,
		`INSERT INTO ingestion_runs (repository_id, commit_sha, branch, status)
		 VALUES ($1, $2, 'main', 'completed') RETURNING id::text`,
		org.RepoID, marker+"-"+shortToken()).Scan(&runID))
	require.NoError(t, tx.Commit(ctx))
	return runID
}

// commitChunk inserts and commits one run and one chunk with the given
// content under org's fixture repository, and returns the chunk id.
func commitChunk(t *testing.T, pool *pgxpool.Pool, org *TestOrg, content string) string {
	t.Helper()
	ctx := context.Background()
	tx, err := TenantScope(ctx, pool, org.ID)
	require.NoError(t, err)
	defer func() { _ = tx.Rollback(ctx) }()
	var runID, chunkID string
	require.NoError(t, tx.QueryRow(ctx,
		`INSERT INTO ingestion_runs (repository_id, commit_sha, branch, status)
		 VALUES ($1, $2, 'main', 'completed') RETURNING id::text`,
		org.RepoID, content+"-"+shortToken()).Scan(&runID))
	require.NoError(t, tx.QueryRow(ctx, TestChunkInsertSQL,
		org.ID, runID, org.RepoID, "x.go", 1, 1, content, "h-"+content).Scan(&chunkID))
	require.NoError(t, tx.Commit(ctx))
	return chunkID
}

// commitRepository adds a second repository to org's default project and
// returns its id. It is deleted by the test, or by cleanupOrg.
func commitRepository(t *testing.T, pool *pgxpool.Pool, org *TestOrg, name string) string {
	t.Helper()
	ctx := context.Background()
	tx, err := TenantScope(ctx, pool, org.ID)
	require.NoError(t, err)
	defer func() { _ = tx.Rollback(ctx) }()
	var id string
	require.NoError(t, tx.QueryRow(ctx,
		`INSERT INTO repositories (project_id, name, git_url) VALUES ($1, $2, $3) RETURNING id::text`,
		org.ProjectID, name, "https://example.test/"+name+"-"+shortToken()+".git").Scan(&id))
	require.NoError(t, tx.Commit(ctx))
	return id
}

// seedScratchRepository writes an organization, its default project and one
// repository into a scratch database as the superuser, and returns the
// repository id.
func seedScratchRepository(t *testing.T, super *pgx.Conn, orgID, slug string) string {
	t.Helper()
	ctx := context.Background()
	_, err := super.Exec(ctx, `INSERT INTO organizations (id, name, slug) VALUES ($1, $2, $2)`, orgID, slug)
	require.NoError(t, err)
	var projectID string
	require.NoError(t, super.QueryRow(ctx,
		`INSERT INTO projects (organization_id, name, slug, is_default) VALUES ($1, 'Default', 'default', true)
		 RETURNING id::text`, orgID).Scan(&projectID))
	return seedScratchRepositoryInProject(t, super, orgID, projectID, slug)
}

// seedScratchRepositoryInOrg adds a second repository to an organization
// seedScratchRepository created, under its default project.
func seedScratchRepositoryInOrg(t *testing.T, super *pgx.Conn, orgID, slug string) string {
	t.Helper()
	var projectID string
	require.NoError(t, super.QueryRow(context.Background(),
		`SELECT id::text FROM projects WHERE organization_id = $1 AND is_default`, orgID).Scan(&projectID))
	return seedScratchRepositoryInProject(t, super, orgID, projectID, slug)
}

func seedScratchRepositoryInProject(t *testing.T, super *pgx.Conn, orgID, projectID, slug string) string {
	t.Helper()
	ctx := context.Background()
	tx, err := super.Begin(ctx)
	require.NoError(t, err)
	defer func() { _ = tx.Rollback(ctx) }()
	_, err = tx.Exec(ctx, fmt.Sprintf("SET LOCAL app.current_tenant = '%s'", orgID))
	require.NoError(t, err)
	var repoID string
	require.NoError(t, tx.QueryRow(ctx,
		`INSERT INTO repositories (project_id, name, git_url) VALUES ($1, $2, $3) RETURNING id::text`,
		projectID, slug, "https://example.test/"+slug+".git").Scan(&repoID))
	require.NoError(t, tx.Commit(ctx))
	return repoID
}

// connectAsAppRole opens a FRESH connection switched to the app role, with
// app.current_tenant genuinely unset, closed when the test ends.
func connectAsAppRole(t *testing.T, pool *pgxpool.Pool) *pgx.Conn {
	t.Helper()
	ctx := context.Background()
	conn, err := pgx.ConnectConfig(ctx, pool.Config().ConnConfig)
	require.NoError(t, err)
	t.Cleanup(func() { _ = conn.Close(context.Background()) })
	switchToAppRole(t, conn)
	var tenant *string
	require.NoError(t, conn.QueryRow(ctx, `SELECT current_setting('app.current_tenant', true)`).Scan(&tenant))
	require.Nil(t, tenant, "premise: a fresh connection has no tenant at all, not ''")
	return conn
}

func countRows(t *testing.T, q Querier, sql string, args ...any) int {
	t.Helper()
	var n int
	require.NoError(t, q.QueryRow(context.Background(), sql, args...).Scan(&n), sql)
	return n
}

func requirePgErr(t *testing.T, err error) *pgconn.PgError {
	t.Helper()
	require.Error(t, err)
	var pgErr *pgconn.PgError
	require.True(t, errors.As(err, &pgErr), "expected *pgconn.PgError, got %T: %v", err, err)
	return pgErr
}

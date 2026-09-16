package isolation_test

// Migration 000013 (21-01): repositories.organization_id is stored, filled by
// trigger, and guaranteed by the composite foreign key
// repositories_project_org_fkey to projects (id, organization_id).
//
// One behaviour per test. Writes run as the app role inside a tenant
// transaction, as production does. The few steps that need more privilege take
// a dedicated superuser connection through isolation.WithSuperuserConn.
//
// NEVER `session_replication_role = replica` in this file, although
// pkg/auth/testing.go uses it for cleanup. It switches off foreign-key
// enforcement, and several of these tests exist to prove a foreign key.
//
// Tenant ids are interpolated into SET LOCAL (which cannot bind a parameter).
// They come from WithTwoOrgs, which reads them back from the database as
// UUIDs, so the interpolation cannot carry anything else.

import (
	"context"
	"errors"
	"fmt"
	"testing"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/stretchr/testify/require"

	"github.com/yourusername/smart-docs-platform/services/backend/pkg/testing/isolation"
)

const (
	// The guard trigger's refusal of a rewrite, verbatim.
	organizationIDRewriteMessage = "organization_id is maintained by the database; move the repository's project instead"

	compositeTenantFK = "repositories_project_org_fkey"
)

// 1. A writer that does not name the column gets its project's organization.
func TestRepositoriesOrganizationID_InsertWithoutTheColumnIsFilledFromTheProject(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		// WithTwoOrgs' own repository insert does not name the column either,
		// and it is one of the writers this migration must leave unchanged.
		_, fixtureOrg := readRepository(t, pool, orgA, orgA.RepoID)
		require.Equal(t, orgA.ID, fixtureOrg, "the fixture's repository must carry its project's organization")

		tx, err := isolation.TenantScope(ctx, pool, orgA.ID)
		require.NoError(t, err)
		defer func() { _ = tx.Rollback(ctx) }()

		var repoID, got string
		require.NoError(t, tx.QueryRow(ctx,
			`INSERT INTO repositories (project_id, name, git_url)
			 VALUES ($1, $2, $3) RETURNING id::text, organization_id::text`,
			orgA.ProjectID, "filled", "https://example.test/filled-"+shortHex()+".git",
		).Scan(&repoID, &got))
		require.Equal(t, orgA.ID, got, "organization_id must be filled from the project")
		require.NoError(t, tx.Commit(ctx))

		isolation.AssertNoRepositoryTenantDrift(t, pool)
	})
}

// 2. A writer that names the WRONG organization is refused with the trigger's
// message, not a bare constraint name.
func TestRepositoriesOrganizationID_InsertNamingAnotherOrganizationIsRejected(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	isolation.WithTwoOrgs(t, pool, func(orgA, orgB *isolation.TestOrg) {
		tx, err := isolation.TenantScope(ctx, pool, orgA.ID)
		require.NoError(t, err)
		defer func() { _ = tx.Rollback(ctx) }()

		_, err = tx.Exec(ctx,
			`INSERT INTO repositories (project_id, organization_id, name, git_url)
			 VALUES ($1, $2, $3, $4)`,
			orgA.ProjectID, orgB.ID, "mislabelled", "https://example.test/mislabelled-"+shortHex()+".git",
		)
		pgErr := requirePgError(t, err)
		require.Equal(t, "42501", pgErr.Code, "message: %s", pgErr.Message)
		require.Contains(t, pgErr.Message,
			fmt.Sprintf("organization_id %s does not match project %s", orgB.ID, orgA.ProjectID))
		require.NoError(t, tx.Rollback(ctx))

		isolation.AssertNoRepositoryTenantDrift(t, pool)
	})
}

// 2b. The trigger is not an existence oracle. Naming a project in ANOTHER
// organization must fail exactly like naming one that does not exist —
// same SQLSTATE, same message, and no word about who owns what.
//
// Why this test exists: `repositories_organization_id_guard` reads
// `projects`, which carries no row-level security, so it can see every
// project in the database. PR #37's review measured the first version of
// the mismatch branch answering "does this project exist, and which
// organization owns it?" from inside another tenant.
func TestRepositoriesOrganizationID_TheTriggerIsNotAnExistenceOracle(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	// A project id that exists in no organization at all.
	const absentProjectID = "00000000-0000-0000-0000-0000000000ff"

	isolation.WithTwoOrgs(t, pool, func(orgA, orgB *isolation.TestOrg) {
		// Each insert gets its own transaction: the first failure aborts it.
		insertAs := func(projectID, organizationID string) *pgconn.PgError {
			tx, err := isolation.TenantScope(ctx, pool, orgA.ID)
			require.NoError(t, err)
			defer func() { _ = tx.Rollback(ctx) }()

			_, err = tx.Exec(ctx,
				`INSERT INTO repositories (project_id, organization_id, name, git_url)
				 VALUES ($1, $2, $3, $4)`,
				projectID, organizationID, "probe", "https://example.test/probe-"+shortHex()+".git",
			)
			return requirePgError(t, err)
		}

		// The probe: org A's own id, with a project id that turns out to
		// belong to org B. The two differ, so this is the branch that used to
		// answer "that project belongs to organization <org B>".
		//
		// Naming org B's id alongside org B's project would prove nothing:
		// the two agree, the mismatch branch never runs, and row-level
		// security refuses the row whatever this trigger does. Measured.
		otherOrg := insertAs(orgB.ProjectID, orgA.ID)
		// The same probe against a project that exists nowhere.
		absent := insertAs(absentProjectID, orgA.ID)

		require.Equal(t, "42501", otherOrg.Code, "message: %s", otherOrg.Message)
		require.Equal(t, absent.Code, otherOrg.Code,
			"a project in another organization must fail with the same SQLSTATE as one that does not exist")
		require.Equal(t, absent.Message, otherOrg.Message,
			"the two must be indistinguishable, or the trigger is a project-existence probe")
		require.NotContains(t, otherOrg.Message, "does not match",
			"the mismatch branch must stay silent about a project outside the caller's tenant")
		require.NotContains(t, otherOrg.Message, orgB.ID,
			"the error must not name the owning organization")

		isolation.AssertNoRepositoryTenantDrift(t, pool)
	})
}

// 3. The guarantee is the foreign key, not the trigger. With the trigger off
// and row-level security bypassed, the mismatched row is still refused.
func TestRepositoriesOrganizationID_CompositeForeignKeyHoldsWithoutTheTrigger(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	isolation.WithTwoOrgs(t, pool, func(orgA, orgB *isolation.TestOrg) {
		isolation.WithSuperuserConn(t, pool, func(conn *pgx.Conn) {
			tx, err := conn.Begin(ctx)
			require.NoError(t, err)
			defer func() { _ = tx.Rollback(ctx) }()

			// DISABLE TRIGGER is durable catalog state once committed, and this
			// container is reused across runs. It is never committed here: the
			// deferred rollback undoes it, and the server aborts the
			// transaction if this process dies first.
			_, err = tx.Exec(ctx, `ALTER TABLE repositories DISABLE TRIGGER trg_repositories_organization_id`)
			require.NoError(t, err)

			// A superuser bypasses row-level security, so RLS cannot be what
			// refuses the row below. trg_assert_tenant still applies, hence the
			// tenant.
			_, err = tx.Exec(ctx, fmt.Sprintf("SET LOCAL app.current_tenant = '%s'", orgA.ID))
			require.NoError(t, err)

			_, err = tx.Exec(ctx,
				`INSERT INTO repositories (project_id, organization_id, name, git_url)
				 VALUES ($1, $2, $3, $4)`,
				orgA.ProjectID, orgB.ID, "unguarded", "https://example.test/unguarded-"+shortHex()+".git",
			)
			pgErr := requirePgError(t, err)
			require.Equal(t, "23503", pgErr.Code, "message: %s", pgErr.Message)
			require.Equal(t, compositeTenantFK, pgErr.ConstraintName)
		})

		isolation.AssertNoRepositoryTenantDrift(t, pool)
	})
}

// 4. The column cannot be rewritten directly, even by the tenant that owns it.
func TestRepositoriesOrganizationID_RewritingTheColumnIsRejected(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	isolation.WithTwoOrgs(t, pool, func(orgA, orgB *isolation.TestOrg) {
		tx, err := isolation.TenantScope(ctx, pool, orgA.ID)
		require.NoError(t, err)
		defer func() { _ = tx.Rollback(ctx) }()

		_, err = tx.Exec(ctx,
			`UPDATE repositories SET organization_id = $1 WHERE id = $2`,
			orgB.ID, orgA.RepoID,
		)
		pgErr := requirePgError(t, err)
		require.Equal(t, "42501", pgErr.Code, "message: %s", pgErr.Message)
		require.Equal(t, organizationIDRewriteMessage, pgErr.Message)
		require.NoError(t, tx.Rollback(ctx))

		_, org := readRepository(t, pool, orgA, orgA.RepoID)
		require.Equal(t, orgA.ID, org, "organization_id must be unchanged")

		isolation.AssertNoRepositoryTenantDrift(t, pool)
	})
}

// 5. Moving a repository to another project in the SAME organization is
// allowed, and organization_id carries over unchanged.
func TestRepositoriesOrganizationID_ReparentWithinTheOrganizationKeepsTheColumn(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		secondProject := createNonDefaultProject(t, pool, orgA)

		tx, err := isolation.TenantScope(ctx, pool, orgA.ID)
		require.NoError(t, err)
		defer func() { _ = tx.Rollback(ctx) }()

		tag, err := tx.Exec(ctx,
			`UPDATE repositories SET project_id = $1 WHERE id = $2`,
			secondProject, orgA.RepoID,
		)
		require.NoError(t, err)
		require.EqualValues(t, 1, tag.RowsAffected())
		require.NoError(t, tx.Commit(ctx))

		project, org := readRepository(t, pool, orgA, orgA.RepoID)
		require.Equal(t, secondProject, project, "the re-parent must have landed")
		require.Equal(t, orgA.ID, org, "organization_id must be unchanged")

		isolation.AssertNoRepositoryTenantDrift(t, pool)
	})
}

// 6. Moving a repository to a project in ANOTHER organization is refused with
// D5's message, and the row is left as it was.
func TestRepositoriesOrganizationID_ReparentAcrossOrganizationsIsRejected(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	isolation.WithTwoOrgs(t, pool, func(orgA, orgB *isolation.TestOrg) {
		tx, err := isolation.TenantScope(ctx, pool, orgA.ID)
		require.NoError(t, err)
		defer func() { _ = tx.Rollback(ctx) }()

		_, err = tx.Exec(ctx,
			`UPDATE repositories SET project_id = $1 WHERE id = $2`,
			orgB.ProjectID, orgA.RepoID,
		)
		pgErr := requirePgError(t, err)
		require.Contains(t, pgErr.Message,
			fmt.Sprintf("cannot move repository %s across organisations; export and re-ingest instead", orgA.RepoID))
		require.NoError(t, tx.Rollback(ctx))

		project, org := readRepository(t, pool, orgA, orgA.RepoID)
		require.Equal(t, orgA.ProjectID, project, "project_id must be unchanged")
		require.Equal(t, orgA.ID, org, "organization_id must be unchanged")

		isolation.AssertNoRepositoryTenantDrift(t, pool)
	})
}

// 7. A project that has repositories cannot be moved to another organization.
// Only the composite foreign key stops this: projects has no row-level
// security and no tenant trigger.
func TestRepositoriesOrganizationID_MovingAProjectWithRepositoriesIsRejected(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	isolation.WithTwoOrgs(t, pool, func(orgA, orgB *isolation.TestOrg) {
		// A non-default project. Moving orgA's DEFAULT project would collide
		// with orgB's default in idx_projects_one_default_per_org first, which
		// is not the guarantee under test.
		project := createNonDefaultProject(t, pool, orgA)
		insertRepository(t, pool, orgA, project, "moving")

		// As the app role, with no tenant set.
		_, err := pool.Exec(ctx,
			`UPDATE projects SET organization_id = $1 WHERE id = $2`,
			orgB.ID, project,
		)
		pgErr := requirePgError(t, err)
		require.Equal(t, "23503", pgErr.Code, "message: %s", pgErr.Message)
		require.Equal(t, compositeTenantFK, pgErr.ConstraintName)

		var owner string
		require.NoError(t, pool.QueryRow(ctx,
			`SELECT organization_id::text FROM projects WHERE id = $1`, project,
		).Scan(&owner))
		require.Equal(t, orgA.ID, owner, "the project must still belong to orgA")

		isolation.AssertNoRepositoryTenantDrift(t, pool)
	})
}

// 8. The drift check itself. Every test above ends with it; these two prove it
// can fail. A check that has never been seen to fail proves nothing.
//
// ⚠ RETRIED ON 40P01 — ISS-032. The `ALTER TABLE ... DROP CONSTRAINT` below
// takes AccessExclusiveLock on `repositories` AND on `projects` (the
// referenced table, whose RI triggers it removes), while every other
// package's fixture cleanup is deleting from those two tables in the other
// order. That is a lock cycle in which this transaction can be picked as
// the victim through no fault of its own, and it fired once in CI's
// package-parallelism step. 21-03 adds more concurrent tests against the
// same two tables, so it is fixed here rather than left to recur.
//
// The retry weakens nothing: each attempt rebuilds its own transaction from
// scratch, the assertions below run on the attempt that completed, and
// three failures still fail — naming 40P01, so the next reader is not left
// guessing.
func TestRepositoriesOrganizationID_DriftCheckDetectsDrift(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	isolation.WithTwoOrgs(t, pool, func(orgA, orgB *isolation.TestOrg) {
		var ids []string
		require.NoError(t, isolation.RetryOnDeadlock(ctx, func() error {
			var attemptErr error
			isolation.WithSuperuserConn(t, pool, func(conn *pgx.Conn) {
				ids, attemptErr = manufactureDriftAndCheck(ctx, conn, orgA, orgB)
			})
			return attemptErr
		}), "manufacturing drift")

		require.Equal(t, []string{orgA.RepoID}, ids)

		isolation.AssertNoRepositoryTenantDrift(t, pool)
	})
}

// manufactureDriftAndCheck removes BOTH guards and writes a drifted row,
// inside a transaction that is never committed, then runs the check over
// it.
//
// It returns an error rather than calling t.Fatal so that the caller can
// retry it: a deadlock here says nothing about the code under test. Every
// statement's error is returned unwrapped, so RetryOnDeadlock can see the
// SQLSTATE.
func manufactureDriftAndCheck(
	ctx context.Context, conn *pgx.Conn, orgA, orgB *isolation.TestOrg,
) ([]string, error) {
	tx, err := conn.Begin(ctx)
	if err != nil {
		return nil, err
	}
	// Never committed: the deferred rollback puts both guards back, and the
	// server aborts the transaction if this process dies first.
	defer func() { _ = tx.Rollback(ctx) }()

	if _, err := tx.Exec(ctx, `ALTER TABLE repositories DROP CONSTRAINT `+compositeTenantFK); err != nil {
		return nil, err
	}
	if _, err := tx.Exec(ctx, `ALTER TABLE repositories DISABLE TRIGGER trg_repositories_organization_id`); err != nil {
		return nil, err
	}
	if _, err := tx.Exec(ctx, fmt.Sprintf("SET LOCAL app.current_tenant = '%s'", orgA.ID)); err != nil {
		return nil, err
	}
	if _, err := tx.Exec(ctx,
		`UPDATE repositories SET organization_id = $1 WHERE id = $2`,
		orgB.ID, orgA.RepoID,
	); err != nil {
		return nil, err
	}
	return isolation.CheckRepositoryTenantDrift(ctx, tx)
}

func TestRepositoriesOrganizationID_DriftCheckRefusesToRunUnderRowLevelSecurity(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	tx, err := pool.Begin(ctx)
	require.NoError(t, err)
	defer func() { _ = tx.Rollback(ctx) }()

	_, err = isolation.CheckRepositoryTenantDrift(ctx, tx)
	require.ErrorContains(t, err, "bypasses row-level security")
}

// 9. The schema's shape.
func TestRepositoriesOrganizationID_SchemaShape(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	var notNull bool
	require.NoError(t, pool.QueryRow(ctx,
		`SELECT attnotnull FROM pg_attribute
		 WHERE attrelid = 'repositories'::regclass
		   AND attname = 'organization_id' AND NOT attisdropped`,
	).Scan(&notNull))
	require.True(t, notNull, "repositories.organization_id must be NOT NULL")

	constraints := []struct{ table, name, def string }{
		{"projects", "projects_id_org_key", "UNIQUE (id, organization_id)"},
		{"repositories", "repositories_id_org_key", "UNIQUE (id, organization_id)"},
		{"repositories", compositeTenantFK,
			"FOREIGN KEY (project_id, organization_id) REFERENCES projects(id, organization_id) ON DELETE CASCADE"},
	}
	for _, c := range constraints {
		var def string
		require.NoError(t, pool.QueryRow(ctx,
			`SELECT pg_get_constraintdef(oid) FROM pg_constraint
			 WHERE conrelid = $1::regclass AND conname = $2`,
			c.table, c.name,
		).Scan(&def), "constraint %s on %s must exist", c.name, c.table)
		require.Equal(t, c.def, def, "constraint %s", c.name)
	}

	triggers := []struct{ name, attachment, function string }{
		{"trg_repositories_organization_id", "BEFORE INSERT OR UPDATE OF organization_id ON", "repositories_organization_id_guard()"},
		// UPDATE-only is load-bearing (D5). Attached for INSERT as well, and
		// without its TG_OP guard, the function raises on every insert.
		{"trg_reject_cross_org_reparent", "BEFORE UPDATE OF project_id ON", "reject_cross_org_reparent()"},
	}
	for _, tr := range triggers {
		var def, enabled string
		require.NoError(t, pool.QueryRow(ctx,
			`SELECT pg_get_triggerdef(oid), tgenabled::text FROM pg_trigger
			 WHERE tgrelid = 'repositories'::regclass AND tgname = $1`,
			tr.name,
		).Scan(&def, &enabled), "trigger %s must exist", tr.name)
		require.Contains(t, def, tr.attachment, "trigger %s attachment", tr.name)
		require.Contains(t, def, tr.function, "trigger %s function", tr.name)
		// Tests here disable triggers only inside transactions they roll back.
		// "O" is enabled; anything else means one of them leaked.
		require.Equal(t, "O", enabled, "trigger %s must be enabled", tr.name)
	}
}

// requirePgError asserts err is a *pgconn.PgError and returns it.
func requirePgError(t *testing.T, err error) *pgconn.PgError {
	t.Helper()
	require.Error(t, err)
	var pgErr *pgconn.PgError
	require.True(t, errors.As(err, &pgErr), "expected *pgconn.PgError, got %T: %v", err, err)
	return pgErr
}

// readRepository reads a repository's project_id and organization_id under the
// owning tenant's scope.
func readRepository(t *testing.T, pool *pgxpool.Pool, org *isolation.TestOrg, repoID string) (projectID, organizationID string) {
	t.Helper()
	ctx := context.Background()
	tx, err := isolation.TenantScope(ctx, pool, org.ID)
	require.NoError(t, err)
	defer func() { _ = tx.Rollback(ctx) }()

	require.NoError(t, tx.QueryRow(ctx,
		`SELECT project_id::text, organization_id::text FROM repositories WHERE id = $1`, repoID,
	).Scan(&projectID, &organizationID))
	return projectID, organizationID
}

// createNonDefaultProject adds a second project to org and removes it on
// cleanup. WithTwoOrgs' cleanup deletes only the fixture's own project.
func createNonDefaultProject(t *testing.T, pool *pgxpool.Pool, org *isolation.TestOrg) string {
	t.Helper()
	ctx := context.Background()

	slug := "extra-" + shortHex()
	var id string
	require.NoError(t, pool.QueryRow(ctx,
		`INSERT INTO projects (organization_id, name, slug, is_default)
		 VALUES ($1, $2, $3, false) RETURNING id::text`,
		org.ID, slug, slug,
	).Scan(&id))

	t.Cleanup(func() {
		// The delete cascades to any repository under the project, and a
		// cascaded delete still fires trg_assert_tenant, hence the scope.
		tx, err := isolation.TenantScope(ctx, pool, org.ID)
		if err != nil {
			return
		}
		defer func() { _ = tx.Rollback(ctx) }()
		if _, err := tx.Exec(ctx, `DELETE FROM projects WHERE id = $1`, id); err != nil {
			return
		}
		_ = tx.Commit(ctx)
	})
	return id
}

// insertRepository inserts and commits a repository under org's scope,
// without naming organization_id.
func insertRepository(t *testing.T, pool *pgxpool.Pool, org *isolation.TestOrg, projectID, name string) string {
	t.Helper()
	ctx := context.Background()
	tx, err := isolation.TenantScope(ctx, pool, org.ID)
	require.NoError(t, err)
	defer func() { _ = tx.Rollback(ctx) }()

	var id string
	require.NoError(t, tx.QueryRow(ctx,
		`INSERT INTO repositories (project_id, name, git_url)
		 VALUES ($1, $2, $3) RETURNING id::text`,
		projectID, name, "https://example.test/"+name+"-"+shortHex()+".git",
	).Scan(&id))
	require.NoError(t, tx.Commit(ctx))
	return id
}

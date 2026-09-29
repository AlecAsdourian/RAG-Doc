package isolation

// THE SEEDED-MIGRATION GATE (22-01; ISS-031, 22-CONTEXT P13).
//
// Every other migration check in this repository applies migrations to an
// EMPTY database, as a SUPERUSER: the harnesses, the Python conftest and
// CI's `migrate up`. ISS-031 is invisible to both halves of that. A
// superuser bypasses row-level security even under FORCE, and on an empty
// database 000013's backfill loop never calls `set_config` at all.
//
// This test runs the migrations the way the first production deploy will:
//
//   - in the DEPLOYMENT SHAPE: a `NOSUPERUSER NOBYPASSRLS` role
//     (DeploymentOwnerRole) owns the database and runs every migration, so
//     FORCE ROW LEVEL SECURITY applies to it;
//   - on a database WITH ROWS: seeded at version 10 (the compose database's
//     version when this was written) with four organizations' rows in every
//     tenant-scoped table, written as the application role under each
//     organization's own tenant;
//   - in ONE SESSION from 12 onward: one migrate instance, one pinned
//     connection, so whatever a migration leaves in a session setting, the
//     next inherits. 10 -> 12 runs separately only because the seed needs
//     000012's `uninstalled_at`; the poison starts at 13, so nothing is lost.
//
// WHAT IT CAUGHT. 000013's loop leaves `app.current_tenant = ''` on the
// migrating session once it commits (ISS-013: nothing short of a new session
// clears it). 000014 then added `ingestion_jobs_repo_tenant_fk` with
// `ALTER TABLE ... ADD CONSTRAINT`, whose validation query reads
// `repositories` through its policy as the owner, evaluates `''::uuid` and
// fails with 22P02, leaving `schema_migrations` at 14, dirty. The fix
// declares the key inside `CREATE TABLE` (000014's section 4 says why).
//
// WHAT IT GUARDS, and by which mechanism. The rule is in 000014's section 4
// and in ISS-031; this is what each half of the gate can see of it.
//
//  1. THE '' LEFTOVER, through the one-session up. A migration after 13 or
//     15 that validates a foreign key or evaluates a policy on the poisoned
//     session fails with 22P02 and leaves the version dirty. This is loud
//     ONLY because 13 and 15 run inside the one session: PR #48's review
//     measured the same ALTER TABLE key reaching 17, clean, when it ran in a
//     fresh session (the shape of every deploy after the first). Keep 13 and
//     15 inside the one-session run; moving the seed forward would silence
//     this half without any test noticing.
//
//  2. THE TENANT AUDIT (installTenantAudit), which does not depend on the
//     session being poisoned. Two event triggers watch every ALTER TABLE
//     from the first migration on and record each foreign key the command
//     left validated that was not validated before it, whenever either:
//       (a) the session's app.current_tenant was non-empty. That is the
//           sentinel the rule forbids, or the tenant an earlier loop left
//           behind. The validation read through the policy as one
//           organization, and no test can tell that from a real check
//           (mutation M3 was a silent pass until this landed);
//       (b) the key's own table has forced row-level security AND held at
//           least one row the key applies to (every key column non-NULL).
//           The owner's validation was then filtered whatever the tenant:
//           NULL sees no rows at all. This is the fresh-session shape from
//           point 1, made visible.
//     It records ALTER TABLE only. A key declared inside CREATE TABLE never
//     runs a validation query (no rows exist), and a key added while its
//     column is still NULL has nothing to check, so neither is flagged: a
//     new-column key added BEFORE a backfill loop (000013's shape since
//     22-01) passes, while the same key added AFTER the loop is flagged by
//     both (a) and (b). A key over existing data added with FORCE lifted
//     for that statement (000012's pattern, the rule's answer for that
//     case) is not flagged either: FORCE is off at that moment, which is
//     the point, and the validation then reads every row (measured: eight
//     violating rows fail it with 23503 where the FORCE-on form passed).
//     SET NOT NULL, CHECK and UNIQUE validations scan the heap directly and
//     are not subject to row-level security, so they are deliberately not
//     the audit's business (measured: on the poisoned session, a violating
//     CHECK failed with 23514 and a violating UNIQUE with 23505, where a
//     policy-filtered read would have raised 22P02 or seen nothing; SET
//     NOT NULL by 21-01).
//     No version is exempt. Before 22-01's review, committed 000013
//     validated repositories_project_org_fkey after its loop, under the last
//     organization's tenant, and the audit flags exactly that (M10). An
//     exemption is where the next instance would hide.
//
// WHAT IT CANNOT SEE. DML in a later migration that inherits the session's
// tenant instead of setting its own: under '' it fails loudly here, but
// under a fresh session it silently matches nothing, and only the outcome
// assertions below (what 000013 and 000015 must have done to every seeded
// row) stand against that. A validating TRIGGER's reads, which the audit does
// not watch either. Both are why the rule is written where the next author
// reads it, not only tested.
//
// MUTATIONS, and why there is an environment variable. The gate migrates
// from RAG_DOC_SEEDED_GATE_MIGRATIONS when it is set: a scratch copy of the
// migrations directory with one file mutated. It is read by this test only,
// never set in CI, and logged loudly when present. The committed migrations
// are never edited to run a mutation.
//
// WHERE: a scratch database inside the shared harness container
// (ScratchDatabase), dropped in t.Cleanup. That is the pattern
// pkg/jobs/backfill_migration_test.go established, it costs a
// `CREATE DATABASE` rather than a container, and it needs no Ryuk.

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/golang-migrate/migrate/v4/database"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/stretchr/testify/require"
)

const (
	// seededGateMigrationsEnv overrides the migrations directory the gate
	// applies, for mutation runs only. Read by TestMigrationsApplyToASeededDatabase
	// and nothing else.
	seededGateMigrationsEnv = "RAG_DOC_SEEDED_GATE_MIGRATIONS"

	// schemaBaselineMigrationsEnv names a second migrations directory whose
	// resulting schema TestMigrationSchemaMatchesBaseline compares with the
	// committed one's. Read by that test and nothing else.
	schemaBaselineMigrationsEnv = "RAG_DOC_SCHEMA_BASELINE_MIGRATIONS"

	seedVersion      = 10 // the version the fixture's columns belong to
	uninstallVersion = 12 // `github_installations.uninstalled_at` arrives here
	pgvectorVersion  = 16 // 000016_enable_pgvector

	// The installation the gate uninstalls at version 12, and its tenant
	// (testdata/seed_at_000010.sql, organization "alpha").
	uninstalledInstallationID = "30000000-0000-4000-8000-0000000000a2"
	uninstalledTenantID       = "10000000-0000-4000-8000-00000000000a"

	// minOrganizationsWithRepositories is the seed's multi-tenant premise. A
	// per-organization backfill that reached one tenant and not another can
	// only leave a row for the assertions to find if more than one tenant
	// has rows to backfill; three, so that "first", "last" and "the one in
	// between" are all represented.
	minOrganizationsWithRepositories = 3
)

// seededRepoOutcome is what 000015 must leave for one seeded repository.
type seededRepoOutcome struct {
	name          string
	wantJob       bool // exactly one queued full_ingest job, or none at all
	wantSyncState string
}

// Every repository in the seed, and what the backfill owes it. The gate
// fails if the seed holds a repository this list does not name, so a row
// added to the fixture cannot go unasserted.
var seededRepoOutcomes = []seededRepoOutcome{
	{name: "a-pending", wantJob: true, wantSyncState: "pending"},
	// Unsyncable: no installation at all.
	{name: "a-pending-no-install", wantJob: false, wantSyncState: "never_synced"},
	// Unsyncable: its installation was uninstalled at version 12.
	{name: "a-pending-uninstalled", wantJob: false, wantSyncState: "never_synced"},
	{name: "a-synced", wantJob: false, wantSyncState: "synced"},
	{name: "b-pending", wantJob: true, wantSyncState: "pending"},
	// `syncing` under a live installation is normalised to `pending` and
	// queued, like `pending`.
	{name: "b-syncing", wantJob: true, wantSyncState: "pending"},
	{name: "b-synced", wantJob: false, wantSyncState: "synced"},
	{name: "c-pending", wantJob: true, wantSyncState: "pending"},
}

func TestMigrationsApplyToASeededDatabase(t *testing.T) {
	ctx := context.Background()
	started := time.Now()
	pool := SetupTestDB(t)

	dir := gateMigrationsDir(t)
	newest := newestMigrationVersion(t, dir)
	require.GreaterOrEqual(t, newest, uint(pgvectorVersion),
		"the gate expects at least 000016 in %s", dir)

	// 1 to 5: the deployment shape, seeded at 10 and again at 12.
	db, super := seedDeploymentShape(t, pool, dir)

	before := snapshotSeededRows(t, super)
	seededChunks := countOf(t, super, `SELECT count(*) FROM chunks`)
	require.Equal(t, 3, seededChunks, "premise: the seed's chunks, in two organizations")
	require.GreaterOrEqual(t, countOf(t, super, `
		SELECT count(DISTINCT p.organization_id)
		FROM repositories r JOIN projects p ON p.id = r.project_id`),
		minOrganizationsWithRepositories,
		"premise: the seed is multi-tenant where the backfills act")

	// 6. ONE `up`: one migrate instance, one session, 12 to the newest.
	upStarted := time.Now()
	if err := applyMigrations(db.OwnerDSN, dir); err != nil {
		version, dirty := migrationVersion(t, super)
		hint := ""
		if sqlStateOf(err) == "22P02" {
			hint = "\n\nTHE ISS-031 CLASS: a migration validated a constraint or evaluated " +
				"a row-level-security policy on a session whose app.current_tenant an " +
				"earlier migration left at ''. Declare foreign keys inside CREATE TABLE, " +
				"and never rely on the session's tenant. See this file's header and ISS-031."
		}
		t.Fatalf("migrating a seeded database from %d to %d as %s, in one session, failed "+
			"and left schema_migrations at %d (dirty=%v): %s%s",
			uninstallVersion, newest, DeploymentOwnerRole, version, dirty,
			describeMigrationError(err), hint)
	}
	upTook := time.Since(upStarted)

	// 7. The assertions, all read as the superuser, so row-level security
	//    cannot hide a gap.
	t.Run("schema_migrations is at the newest version and clean", func(t *testing.T) {
		version, dirty := migrationVersion(t, super)
		require.Equal(t, int64(newest), version)
		require.False(t, dirty)
	})

	t.Run("still the deployment shape after the up", func(t *testing.T) {
		assertDeploymentShape(t, super, db.Name)
	})

	t.Run("no foreign key was validated under a tenant or through row-level security", func(t *testing.T) {
		require.Empty(t, flaggedForeignKeyValidations(t, super),
			"an ALTER TABLE validated a foreign key in a shape the rule forbids; "+
				"see this file's header, 000014's section 4 and ISS-031")
	})

	t.Run("000013 filled every repository's organization_id from its project", func(t *testing.T) {
		drifted, err := CheckRepositoryTenantDrift(ctx, super)
		require.NoError(t, err)
		require.Empty(t, drifted, "repositories whose organization_id is not their project's")
		require.Zero(t, countOf(t, super,
			`SELECT count(*) FROM repositories WHERE organization_id IS NULL`))
		require.Equal(t, len(before["repositories"]), countOf(t, super,
			`SELECT count(*) FROM repositories`), "every seeded repository is still there")
	})

	t.Run("000015 queued each syncable repository once and stood the rest down", func(t *testing.T) {
		require.Equal(t, len(seededRepoOutcomes), countOf(t, super,
			`SELECT count(*) FROM repositories`),
			"seededRepoOutcomes and the seed disagree about which repositories exist")

		wantJobs := 0
		for _, want := range seededRepoOutcomes {
			var repoID, syncState string
			require.NoError(t, super.QueryRow(ctx,
				`SELECT id::text, sync_state FROM repositories WHERE name = $1`,
				want.name).Scan(&repoID, &syncState), "%s: read the repository", want.name)

			var queued, all int
			require.NoError(t, super.QueryRow(ctx, `
				SELECT count(*) FILTER (WHERE state = 'queued' AND job_type = 'full_ingest'),
				       count(*)
				FROM ingestion_jobs WHERE repository_id = $1`, repoID).Scan(&queued, &all))
			if want.wantJob {
				wantJobs++
				require.Equalf(t, 1, queued, "%s: exactly one queued full_ingest job", want.name)
				require.Equalf(t, 1, all, "%s: and no other job", want.name)
			} else {
				require.Zerof(t, all, "%s: no job", want.name)
			}
			require.Equalf(t, want.wantSyncState, syncState, "%s: sync_state", want.name)
		}
		require.Equal(t, wantJobs, countOf(t, super, `SELECT count(*) FROM ingestion_jobs`),
			"jobs for repositories the seed does not hold")

		// ingestion_jobs has no row-level security, so a count that ignores
		// organization_id proves nothing about tenancy. Compare explicitly.
		require.Zero(t, countOf(t, super, `
			SELECT count(*) FROM ingestion_jobs j
			JOIN repositories r ON r.id = j.repository_id
			WHERE j.organization_id <> r.organization_id`),
			"a backfilled job carries the wrong organization")

		// The invariant 000015 states, over the whole table: `pending` means
		// a live job exists.
		require.Zero(t, countOf(t, super, `
			SELECT count(*) FROM repositories r
			WHERE r.sync_state IN ('pending','syncing')
			  AND NOT EXISTS (
				SELECT 1 FROM ingestion_jobs j
				WHERE j.repository_id = r.id AND j.state IN ('queued','running'))`),
			"a repository shows as queued with no job to run it")
	})

	t.Run("000016 installed pgvector", func(t *testing.T) {
		var version string
		require.NoError(t, super.QueryRow(ctx,
			`SELECT extversion FROM pg_extension WHERE extname = 'vector'`).Scan(&version))
		t.Logf("vector %s", version)
	})

	// `chunks` is excluded: 22-02's 000017 drops and recreates it (P1), and
	// adds its own assertions here, including that the seeded retrievals and
	// feedback survive it with a dangling chunk_id (P17).
	t.Run("every seeded row outside chunks survived", func(t *testing.T) {
		after := snapshotSeededRows(t, super)
		for _, table := range sortedKeys(before) {
			missing := missingFrom(before[table], after[table])
			require.Emptyf(t, missing, "%s: seeded rows gone after the migrations", table)
		}
	})

	t.Logf("seeded gate on %s: %d migrations from %d in one session in %s; test total %s",
		db.Name, newest-uninstallVersion, uninstallVersion, upTook.Round(time.Millisecond),
		time.Since(started).Round(time.Millisecond))
}

// TestMigration000016NeedsTheExtensionPreCreated pins the operator
// precondition with the error that distinguishes it. `vector` is untrusted,
// so a non-superuser owner cannot create it; an image WITHOUT pgvector would
// fail differently ("is not available", 0A000), and the premise below rules
// that out, so this failure can only be the privilege.
//
// It also pins the state each failure leaves behind and the documented way
// out of it (docs/local-development.md, "Recovering a dirty version"): a
// version left dirty is the operator's first sight of any of this.
func TestMigration000016NeedsTheExtensionPreCreated(t *testing.T) {
	ctx := context.Background()
	pool := SetupTestDB(t)
	dir := migrationsDir()

	db := ScratchDatabase(t, pool, DeploymentOwnerRole)
	super := connectScratch(t, db.SuperuserDSN)

	var available, installed bool
	require.NoError(t, super.QueryRow(ctx, `
		SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector'),
		       EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')`,
	).Scan(&available, &installed))
	require.True(t, available, "premise: the image ships pgvector")
	require.False(t, installed, "premise: this database has not created it")

	require.NoError(t, applyMigrationsTo(db.OwnerDSN, dir, pgvectorVersion-1))

	// Without the extension: the privilege error, and a dirty version.
	err := applyMigrationsTo(db.OwnerDSN, dir, pgvectorVersion)
	require.Error(t, err, "a non-superuser owner created an untrusted extension")
	require.Equal(t, "42501", sqlStateOf(err), describeMigrationError(err))
	require.Contains(t, err.Error(), `permission denied to create extension "vector"`)
	version, dirty := migrationVersion(t, super)
	require.Equal(t, int64(pgvectorVersion), version)
	require.True(t, dirty)
	require.False(t, extensionInstalled(t, super), "the failed up installed nothing")

	// The operator's way out, as docs/local-development.md gives it: create
	// the extension, force back to 15, and run the migration again, which is
	// now a no-op for the owner.
	_, err = super.Exec(ctx, `CREATE EXTENSION vector`)
	require.NoError(t, err)
	require.NoError(t, forceMigrationVersion(db.OwnerDSN, dir, pgvectorVersion-1))
	require.NoError(t, applyMigrationsTo(db.OwnerDSN, dir, pgvectorVersion),
		"with the extension present, 000016 passes as the non-superuser owner")
	version, dirty = migrationVersion(t, super)
	require.Equal(t, int64(pgvectorVersion), version)
	require.False(t, dirty)

	// And the way back down needs the operator too: the owner does not own
	// the extension it did not create (000016's down says so).
	err = applyMigrationsTo(db.OwnerDSN, dir, pgvectorVersion-1)
	require.Error(t, err)
	require.Equal(t, "42501", sqlStateOf(err), describeMigrationError(err))
	require.Contains(t, err.Error(), "must be owner of extension vector")

	// The state a failed down leaves: 15, dirty, with the extension STILL
	// INSTALLED. golang-migrate records the target version as dirty before
	// it runs the file, and the DROP rolled back with the file.
	version, dirty = migrationVersion(t, super)
	require.Equal(t, int64(pgvectorVersion-1), version, "a failed down records its target version")
	require.True(t, dirty)
	require.True(t, extensionInstalled(t, super), "the failed down dropped nothing")

	// Two documented ways out. Abandon the rollback: the extension stays,
	// and 16 is what the database actually has.
	require.NoError(t, forceMigrationVersion(db.OwnerDSN, dir, pgvectorVersion))
	version, dirty = migrationVersion(t, super)
	require.Equal(t, int64(pgvectorVersion), version)
	require.False(t, dirty)
	require.True(t, extensionInstalled(t, super))

	// Or finish it by hand: the operator drops the extension and records
	// 15, which then IS the state of the database.
	_, err = super.Exec(ctx, `DROP EXTENSION vector`)
	require.NoError(t, err)
	require.NoError(t, forceMigrationVersion(db.OwnerDSN, dir, pgvectorVersion-1))
	version, dirty = migrationVersion(t, super)
	require.Equal(t, int64(pgvectorVersion-1), version)
	require.False(t, dirty)
	require.False(t, extensionInstalled(t, super))
}

// TestMigrationSchemaMatchesBaseline compares the schema two migrations
// directories build, as catalog dumps. It is a proof tool, not a gate: it
// skips unless RAG_DOC_SCHEMA_BASELINE_MIGRATIONS names the second
// directory. 22-01 used it to show that its two edits to applied migrations
// (000013's key moved ahead of its loop, 000014's key declared inside
// CREATE TABLE) leave the schema identical to main's, `convalidated`
// included:
//
//	mkdir -p /tmp/baseline && cp services/backend/migrations/* /tmp/baseline/
//	for f in 000013_repositories_organization_id 000014_ingestion_jobs; do
//	  git show RAG-Doc/main:services/backend/migrations/$f.up.sql > /tmp/baseline/$f.up.sql
//	done
//	RAG_DOC_SCHEMA_BASELINE_MIGRATIONS=/tmp/baseline \
//	  go test ./pkg/testing/isolation -run TestMigrationSchemaMatchesBaseline -v
//
// Both databases are migrated EMPTY, in the deployment shape; the original
// 000014 cannot migrate a seeded one (ISS-031).
func TestMigrationSchemaMatchesBaseline(t *testing.T) {
	baseline := os.Getenv(schemaBaselineMigrationsEnv)
	if baseline == "" {
		t.Skipf("proof tool: set %s to a migrations directory to compare its schema with the committed one's",
			schemaBaselineMigrationsEnv)
	}
	pool := SetupTestDB(t)

	committed := catalogDump(t, migrateEmptyInDeploymentShape(t, pool, migrationsDir()))
	base := catalogDump(t, migrateEmptyInDeploymentShape(t, pool, baseline))

	require.Equal(t, strings.Join(base, "\n"), strings.Join(committed, "\n"),
		"the committed migrations build a different schema from %s", baseline)
	t.Logf("identical: %d catalog lines", len(committed))
}

// =====================================================================
// Steps
// =====================================================================

func gateMigrationsDir(t *testing.T) string {
	t.Helper()
	if dir := os.Getenv(seededGateMigrationsEnv); dir != "" {
		t.Logf("⚠ MUTATION RUN: %s is set; migrating from %s, NOT the committed migrations",
			seededGateMigrationsEnv, dir)
		return dir
	}
	return migrationsDir()
}

// seedDeploymentShape builds the database the gate runs on and returns it
// with a superuser connection: owned and migrated by DeploymentOwnerRole,
// the extension pre-created by the operator, audited from the first
// migration on, seeded at 10 as rag_doc_app and, at 12, with one
// installation uninstalled. The deployment-shape premises are checked before
// it returns.
func seedDeploymentShape(t *testing.T, pool *pgxpool.Pool, dir string) (ScratchDB, *pgx.Conn) {
	t.Helper()
	ctx := context.Background()

	// 1. A database owned by the deployment-shape role.
	db := ScratchDatabase(t, pool, DeploymentOwnerRole)
	super := connectScratch(t, db.SuperuserDSN)

	// 2. The operator's step: `vector` is untrusted, so the owner cannot
	//    create it (TestMigration000016NeedsTheExtensionPreCreated). And the
	//    audit, before any migration runs, so no version is exempt from it.
	_, err := super.Exec(ctx, `CREATE EXTENSION vector`)
	require.NoError(t, err, "create the extension as the operator")
	installTenantAudit(t, super)

	// 3. The schema the fixture was written for, as the owner.
	require.NoError(t, applyMigrationsTo(db.OwnerDSN, dir, seedVersion),
		"migrate to %d as %s", seedVersion, DeploymentOwnerRole)

	// 4. The rows, written the way the application writes them.
	grantAppRoleIn(t, super)
	seed, err := os.ReadFile(filepath.Join("testdata", "seed_at_000010.sql"))
	require.NoError(t, err)
	execAsAppRole(t, db.SuperuserDSN, string(seed))

	// 5. On to 12 in a session of its own, then the one row the fixture
	//    could not hold at 10.
	//
	//    ⚠ THE RE-GRANT IS NOT OPTIONAL. 000012's AFTER UPDATE trigger on
	//    github_installations (not SECURITY DEFINER) writes
	//    github_installation_tenants, a table created after step 4's grants.
	//    Without it the update fails with 42501 (measured by the fact-check).
	require.NoError(t, applyMigrationsTo(db.OwnerDSN, dir, uninstallVersion),
		"migrate to %d as %s", uninstallVersion, DeploymentOwnerRole)
	grantAppRoleIn(t, super)
	markInstallationUninstalled(t, db.SuperuserDSN)

	assertDeploymentShape(t, super, db.Name)
	return db, super
}

func connectScratch(t *testing.T, dsn string) *pgx.Conn {
	t.Helper()
	conn, err := pgx.Connect(context.Background(), dsn)
	require.NoError(t, err)
	// Registered after ScratchDatabase's drop, so it runs before it.
	t.Cleanup(func() { _ = conn.Close(context.Background()) })
	return conn
}

// grantAppRoleIn gives rag_doc_app the harness's grants in the scratch
// database, which cover only the tables that exist when they run.
func grantAppRoleIn(t *testing.T, super *pgx.Conn) {
	t.Helper()
	for _, s := range appRoleGrants {
		_, err := super.Exec(context.Background(), s)
		require.NoError(t, err, "grant: %s", s)
	}
}

// execAsAppRole runs sql on a fresh connection switched to rag_doc_app, the
// role the application writes as: subject to row-level security and to
// trg_assert_tenant, and unable to write anything the application could
// not.
func execAsAppRole(t *testing.T, superuserDSN, sql string) {
	t.Helper()
	ctx := context.Background()
	conn, err := pgx.Connect(ctx, superuserDSN)
	require.NoError(t, err)
	defer func() { _ = conn.Close(ctx) }()

	switchToAppRole(t, conn)
	_, err = conn.Exec(ctx, sql)
	require.NoError(t, err, "load the seed as %s", appRole)
}

// markInstallationUninstalled does what the `installation.deleted` webhook
// does to the row, as the application role under the installation's tenant.
func markInstallationUninstalled(t *testing.T, superuserDSN string) {
	t.Helper()
	ctx := context.Background()
	conn, err := pgx.Connect(ctx, superuserDSN)
	require.NoError(t, err)
	defer func() { _ = conn.Close(ctx) }()
	switchToAppRole(t, conn)

	tx, err := conn.Begin(ctx)
	require.NoError(t, err)
	defer func() { _ = tx.Rollback(ctx) }()
	_, err = tx.Exec(ctx, `SELECT set_config('app.current_tenant', $1, true)`, uninstalledTenantID)
	require.NoError(t, err)
	tag, err := tx.Exec(ctx,
		`UPDATE github_installations SET uninstalled_at = NOW(), updated_at = NOW() WHERE id = $1`,
		uninstalledInstallationID)
	require.NoError(t, err, "uninstall as %s at version %d", appRole, uninstallVersion)
	// A filtered UPDATE matches zero rows and reports success. Without this
	// the "uninstalled" fixture could silently be a live installation, and
	// the gate would assert the wrong thing about it.
	require.Equal(t, int64(1), tag.RowsAffected(), "premise: the installation was uninstalled")
	require.NoError(t, tx.Commit(ctx))
}

func switchToAppRole(t *testing.T, conn *pgx.Conn) {
	t.Helper()
	ctx := context.Background()
	_, err := conn.Exec(ctx, "SET ROLE "+appRole)
	require.NoError(t, err)
	var bypasses bool
	require.NoError(t, conn.QueryRow(ctx,
		`SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user`).Scan(&bypasses))
	require.False(t, bypasses, "premise: %s is subject to row-level security", appRole)
}

// assertDeploymentShape checks the premise the whole gate rests on: the
// database, every table in it and the migration bookkeeping belong to
// DeploymentOwnerRole, and `repositories` forces row-level security on its
// owner. If migrations had run as the superuser, the tables would be the
// superuser's and ISS-031 could not occur.
func assertDeploymentShape(t *testing.T, super *pgx.Conn, dbName string) {
	t.Helper()
	ctx := context.Background()

	var dbOwner string
	require.NoError(t, super.QueryRow(ctx,
		`SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = $1`, dbName).Scan(&dbOwner))
	require.Equal(t, DeploymentOwnerRole, dbOwner, "premise: the database's owner")

	rows, err := super.Query(ctx, `
		SELECT tablename || ' is owned by ' || tableowner FROM pg_tables
		WHERE schemaname = 'public' AND tableowner <> $1 ORDER BY tablename`, DeploymentOwnerRole)
	require.NoError(t, err)
	foreign, err := pgx.CollectRows(rows, pgx.RowTo[string])
	require.NoError(t, err)
	require.Empty(t, foreign, "premise: every table, schema_migrations included, belongs to %s",
		DeploymentOwnerRole)

	var rls, force bool
	require.NoError(t, super.QueryRow(ctx,
		`SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = 'public.repositories'::regclass`,
	).Scan(&rls, &force))
	require.True(t, rls && force, "premise: repositories forces row-level security on its owner")
}

// =====================================================================
// The tenant audit
// =====================================================================

// tenantAuditSQL installs the audit the header describes: two event
// triggers on ALTER TABLE, in a schema of their own so the deployment-shape
// premise on public tables is untouched.
//
// SECURITY DEFINER, owned by the superuser that installs it: the functions
// read catalogs and count rows with row-level security bypassed, and write
// audit tables the migrating owner has no grant on. `app.current_tenant` is
// a session setting, so the definer switch does not change what
// current_setting returns.
//
// gate_audit.snapshot runs before each ALTER TABLE and records which foreign
// keys were already validated. gate_audit.record runs after it and looks at
// each foreign key the command left validated that was not before. It counts
// the key's table WITHOUT `ONLY`, so a key on a partitioned parent sees its
// partitions' rows. golang-migrate records `version = N, dirty` before it
// runs file N, so the version read here is the file that ran the command.
const tenantAuditSQL = `
CREATE SCHEMA gate_audit;

CREATE TABLE gate_audit.fk_before (
  conoid      oid PRIMARY KEY,
  convalidated boolean NOT NULL
);

CREATE TABLE gate_audit.flagged (
  version         bigint,
  relation        text NOT NULL,
  constraint_name text NOT NULL,
  tenant          text,
  force_rls       boolean NOT NULL,
  checkable_rows  bigint NOT NULL,
  reason          text NOT NULL
);

CREATE FUNCTION gate_audit.snapshot() RETURNS event_trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  DELETE FROM gate_audit.fk_before;
  INSERT INTO gate_audit.fk_before
  SELECT oid, convalidated FROM pg_catalog.pg_constraint WHERE contype = 'f';
END $$;

CREATE FUNCTION gate_audit.record() RETURNS event_trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  tenant    text := pg_catalog.current_setting('app.current_tenant', true);
  fk        record;
  cols      text;
  checkable bigint;
BEGIN
  FOR fk IN
    SELECT c.oid, c.conrelid, c.conname, c.conkey, n.nspname, r.relname,
           (r.relrowsecurity AND r.relforcerowsecurity) AS force_rls
    FROM pg_catalog.pg_constraint c
    JOIN pg_catalog.pg_class r ON r.oid = c.conrelid
    JOIN pg_catalog.pg_namespace n ON n.oid = r.relnamespace
    LEFT JOIN gate_audit.fk_before b ON b.conoid = c.oid
    WHERE c.contype = 'f' AND c.convalidated
      AND (b.conoid IS NULL OR NOT b.convalidated)
  LOOP
    SELECT pg_catalog.string_agg(pg_catalog.format('%I IS NOT NULL', a.attname), ' AND ')
      INTO cols
    FROM pg_catalog.pg_attribute a
    WHERE a.attrelid = fk.conrelid AND a.attnum = ANY (fk.conkey);

    EXECUTE pg_catalog.format('SELECT count(*) FROM %I.%I WHERE %s', fk.nspname, fk.relname, cols)
      INTO checkable;

    IF (tenant IS NOT NULL AND tenant <> '') OR (fk.force_rls AND checkable > 0) THEN
      INSERT INTO gate_audit.flagged
      VALUES (
        (SELECT version FROM public.schema_migrations LIMIT 1),
        fk.nspname || '.' || fk.relname, fk.conname, tenant, fk.force_rls, checkable,
        CASE WHEN tenant IS NOT NULL AND tenant <> ''
             THEN 'validated under a tenant'
             ELSE 'validated through forced row-level security with rows to check' END);
    END IF;
  END LOOP;
END $$;

CREATE EVENT TRIGGER gate_audit_snapshot ON ddl_command_start
  WHEN TAG IN ('ALTER TABLE') EXECUTE FUNCTION gate_audit.snapshot();
CREATE EVENT TRIGGER gate_audit_record ON ddl_command_end
  WHEN TAG IN ('ALTER TABLE') EXECUTE FUNCTION gate_audit.record();
`

func installTenantAudit(t *testing.T, super *pgx.Conn) {
	t.Helper()
	_, err := super.Exec(context.Background(), tenantAuditSQL)
	require.NoError(t, err, "install the tenant audit")
}

// flaggedForeignKeyValidations reads the audit: one line per foreign key an
// ALTER TABLE validated under a tenant, or through forced row-level
// security with rows to check. Empty is the only acceptable answer.
func flaggedForeignKeyValidations(t *testing.T, super *pgx.Conn) []string {
	t.Helper()
	rows, err := super.Query(context.Background(), `
		SELECT format('%s: %s on %s %s (tenant=%s, force_rls=%s, checkable_rows=%s)',
		              version, constraint_name, relation, reason,
		              coalesce(tenant, 'NULL'), force_rls, checkable_rows)
		FROM gate_audit.flagged ORDER BY version, relation, constraint_name`)
	require.NoError(t, err)
	flagged, err := pgx.CollectRows(rows, pgx.RowTo[string])
	require.NoError(t, err)
	return flagged
}

// seededTables are the tables the seed writes, with the column that
// identifies a row. `chunks` is counted separately; see the gate.
var seededTables = []struct{ table, key string }{
	{"organizations", "id"},
	{"projects", "id"},
	{"users", "id"},
	{"organization_memberships", "id"},
	{"github_installations", "id"},
	{"github_installation_tenants", "github_installation_id"},
	{"repositories", "id"},
	{"ingestion_runs", "id"},
	{"queries", "id"},
	{"retrievals", "id"},
	{"feedback", "id"},
}

func snapshotSeededRows(t *testing.T, super *pgx.Conn) map[string][]string {
	t.Helper()
	out := make(map[string][]string, len(seededTables))
	for _, st := range seededTables {
		rows, err := super.Query(context.Background(), fmt.Sprintf(
			`SELECT %s::text FROM %s ORDER BY 1`,
			pgx.Identifier{st.key}.Sanitize(), pgx.Identifier{st.table}.Sanitize()))
		require.NoError(t, err)
		keys, err := pgx.CollectRows(rows, pgx.RowTo[string])
		require.NoError(t, err)
		require.NotEmptyf(t, keys, "premise: the seed wrote rows into %s", st.table)
		out[st.table] = keys
	}
	return out
}

// =====================================================================
// Reading migration state and errors
// =====================================================================

func migrationVersion(t *testing.T, super *pgx.Conn) (int64, bool) {
	t.Helper()
	var version int64
	var dirty bool
	require.NoError(t, super.QueryRow(context.Background(),
		`SELECT version, dirty FROM schema_migrations`).Scan(&version, &dirty))
	return version, dirty
}

func extensionInstalled(t *testing.T, super *pgx.Conn) bool {
	t.Helper()
	var installed bool
	require.NoError(t, super.QueryRow(context.Background(),
		`SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')`).Scan(&installed))
	return installed
}

func newestMigrationVersion(t *testing.T, dir string) uint {
	t.Helper()
	files, err := filepath.Glob(filepath.Join(dir, "*.up.sql"))
	require.NoError(t, err)
	require.NotEmpty(t, files, "no migrations in %s", dir)
	var newest uint64
	for _, f := range files {
		prefix, _, _ := strings.Cut(filepath.Base(f), "_")
		n, err := strconv.ParseUint(prefix, 10, 64)
		require.NoError(t, err, "migration file name %s", f)
		if n > newest {
			newest = n
		}
	}
	return uint(newest)
}

// sqlStateOf digs the SQLSTATE out of a golang-migrate failure. The
// postgres driver wraps lib/pq's error in a database.Error value that has
// no Unwrap, so errors.As on the driver's type alone cannot reach it.
func sqlStateOf(err error) string {
	var dbErr database.Error
	if !errors.As(err, &dbErr) {
		return ""
	}
	if s, ok := dbErr.OrigErr.(interface{ SQLState() string }); ok {
		return s.SQLState()
	}
	return ""
}

// describeMigrationError is a failure's SQLSTATE and message WITHOUT the
// migration's full text, which database.Error's own string includes.
//
// It cannot name the statement inside the file: golang-migrate sends a
// whole file as one statement, and an error raised by an internal query
// (a foreign key's validation, say) carries no position. The server log's
// CONTEXT line names it; 22-01-SUMMARY.md quotes the one for ISS-031.
func describeMigrationError(err error) string {
	var dbErr database.Error
	if !errors.As(err, &dbErr) {
		return err.Error()
	}
	desc := fmt.Sprintf("SQLSTATE %s: %s", sqlStateOf(err), dbErr.Err)
	if dbErr.Line > 0 {
		desc += fmt.Sprintf(" (line %d of the migration)", dbErr.Line)
	}
	return desc
}

// =====================================================================
// The schema comparison
// =====================================================================

// migrateEmptyInDeploymentShape builds a scratch database from dir, owned
// and migrated by DeploymentOwnerRole, and returns a superuser connection
// to it.
func migrateEmptyInDeploymentShape(t *testing.T, pool *pgxpool.Pool, dir string) *pgx.Conn {
	t.Helper()
	db := ScratchDatabase(t, pool, DeploymentOwnerRole)
	super := connectScratch(t, db.SuperuserDSN)
	_, err := super.Exec(context.Background(), `CREATE EXTENSION vector`)
	require.NoError(t, err)
	require.NoError(t, applyMigrations(db.OwnerDSN, dir), "migrate %s", dir)
	return super
}

// catalogQueries are the catalog reads compared between two schemas. Each
// is ordered on its own so the dump is deterministic.
//
// ⚠ Internal triggers are compared by the constraint they enforce, never by
// name: their names embed OIDs, which differ between any two databases.
var catalogQueries = []struct{ section, sql string }{
	{"constraint", `
		SELECT conrelid::regclass::text, conname, contype::text, pg_get_constraintdef(oid),
		       convalidated, condeferrable, condeferred
		FROM pg_constraint WHERE connamespace = 'public'::regnamespace
		ORDER BY 1, 2`},
	{"index", `
		SELECT tablename, indexname, indexdef FROM pg_indexes
		WHERE schemaname = 'public' ORDER BY 1, 2`},
	{"column", `
		SELECT table_name, ordinal_position, column_name, data_type, udt_name, is_nullable,
		       column_default, character_maximum_length, numeric_precision, numeric_scale
		FROM information_schema.columns WHERE table_schema = 'public'
		ORDER BY 1, 2`},
	{"trigger", `
		SELECT tgrelid::regclass::text, tgname, pg_get_triggerdef(oid), tgenabled::text
		FROM pg_trigger WHERE NOT tgisinternal
		ORDER BY 1, 2`},
	{"internal trigger", `
		SELECT t.tgrelid::regclass::text, c.conname, t.tgfoid::regproc::text, t.tgtype::text,
		       t.tgenabled::text, t.tgdeferrable, t.tginitdeferred
		FROM pg_trigger t JOIN pg_constraint c ON c.oid = t.tgconstraint
		WHERE t.tgisinternal
		ORDER BY 1, 2, 3, 4`},
	{"policy", `
		SELECT tablename, policyname, permissive, roles::text, cmd, qual, with_check
		FROM pg_policies WHERE schemaname = 'public'
		ORDER BY 1, 2`},
	{"relation", `
		SELECT relname, relkind::text, relrowsecurity, relforcerowsecurity,
		       pg_get_userbyid(relowner), obj_description(oid, 'pg_class')
		FROM pg_class WHERE relnamespace = 'public'::regnamespace
		ORDER BY 1`},
	{"column comment", `
		SELECT c.relname, a.attname, col_description(c.oid, a.attnum)
		FROM pg_class c JOIN pg_attribute a ON a.attrelid = c.oid
		WHERE c.relnamespace = 'public'::regnamespace AND a.attnum > 0
		  AND col_description(c.oid, a.attnum) IS NOT NULL
		ORDER BY 1, 2`},
	// A function body keeps the line endings of the file that created it, so
	// a Windows checkout (CRLF) and `git show` output (LF) differ here with no
	// difference in the schema. Measured in 22-01; carriage returns are
	// stripped before hashing.
	{"function", `
		SELECT p.proname, pg_get_function_identity_arguments(p.oid),
		       md5(replace(pg_get_functiondef(p.oid), E'\r', ''))
		FROM pg_proc p
		WHERE p.pronamespace = 'public'::regnamespace AND p.prokind IN ('f', 'p')
		ORDER BY 1, 2`},
	{"extension", `SELECT extname, extversion FROM pg_extension ORDER BY 1`},
}

func catalogDump(t *testing.T, super *pgx.Conn) []string {
	t.Helper()
	var lines []string
	for _, q := range catalogQueries {
		rows, err := super.Query(context.Background(), q.sql)
		require.NoError(t, err, q.section)
		for rows.Next() {
			values, err := rows.Values()
			require.NoError(t, err, q.section)
			fields := make([]string, len(values))
			for i, v := range values {
				fields[i] = fmt.Sprint(v)
			}
			lines = append(lines, q.section+" | "+strings.Join(fields, " | "))
		}
		require.NoError(t, rows.Err(), q.section)
	}
	return lines
}

func countOf(t *testing.T, conn *pgx.Conn, sql string, args ...any) int {
	t.Helper()
	var n int
	require.NoError(t, conn.QueryRow(context.Background(), sql, args...).Scan(&n), sql)
	return n
}

func sortedKeys(m map[string][]string) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

// missingFrom returns the members of want absent from have.
func missingFrom(want, have []string) []string {
	present := make(map[string]bool, len(have))
	for _, h := range have {
		present[h] = true
	}
	var missing []string
	for _, w := range want {
		if !present[w] {
			missing = append(missing, w)
		}
	}
	return missing
}

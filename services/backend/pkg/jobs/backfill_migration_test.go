package jobs

// Migration 000015 (21-04): a job for every repository the pre-21-04
// webhook path left `pending` with nothing to run it.
//
// WHY THIS TEST NEEDS ITS OWN DATABASE. golang-migrate never re-applies a
// recorded version, and the shared harness container is already at 15 by
// the time any test runs — so the only way to observe what this migration
// DOES is to build a database at 14, put rows in it, and then apply 15.
// The database is created inside the harness container (no second
// container, no port), and dropped afterwards. Nothing in the shared
// `isolation` database is read or written by this file.
//
// ⚠ WHAT THIS SHAPE DOES AND DOES NOT PROVE. Migrations here run as the
// container's superuser, who owns the tables and BYPASSES row-level
// security. So this file proves the eligibility predicate, the tenancy of
// the rows the migration writes, its `ON CONFLICT ... DO NOTHING`
// idempotency, and that `trg_assert_tenant` is satisfied — that one is a
// ROW TRIGGER, which a superuser does not escape, so a missing
// `set_config` still fails here (the `syncing` fixture exists partly to
// make sure a row reaches it). It does NOT exercise FORCE ROW LEVEL
// SECURITY, because a superuser is not subject to it. The deployment
// shape — an RLS-subject role owning the tables — is measured separately
// and recorded in 21-04-SUMMARY.md, the same division 21-01 used for
// 000013.

import (
	"context"
	"fmt"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/golang-migrate/migrate/v4"
	_ "github.com/golang-migrate/migrate/v4/database/postgres" // driver
	_ "github.com/golang-migrate/migrate/v4/source/file"       // source
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/stretchr/testify/require"

	"github.com/yourusername/smart-docs-platform/services/backend/pkg/testing/isolation"
)

const (
	backfillFromVersion = 14
	backfillToVersion   = 15
)

// seededRepo is one fixture row and what the migration must do with it.
type seededRepo struct {
	name string

	// before
	syncState   string
	installed   bool // has an installation_id at all
	uninstalled bool // that installation carries uninstalled_at
	liveJob     bool // already has a queued job

	// after
	wantJob       bool
	wantSyncState string

	id    string
	orgID string
}

func TestBackfillIngestionJobs_GivesAJobToEveryStrandedRepository(t *testing.T) {
	ctx := context.Background()
	pool := isolation.SetupTestDB(t)

	// Owned and migrated by the container SUPERUSER, deliberately: see the
	// header for what that shape proves and what it leaves to the seeded
	// gate in pkg/testing/isolation.
	scratch := isolation.ScratchDatabase(t, pool, isolation.SuperuserRole)
	dsn, dbName := scratch.OwnerDSN, scratch.Name
	m := openMigrator(t, dsn)

	// 1. The schema as it stood before this plan.
	require.NoError(t, m.Migrate(backfillFromVersion))

	conn, err := pgx.Connect(ctx, dsn)
	require.NoError(t, err)
	defer func() { _ = conn.Close(ctx) }()

	// 2. Two organizations, so a job written under the wrong tenant shows
	//    up as wrong rather than merely absent.
	orgA := seedOrganization(t, conn, "backfilla"+shortID())
	orgB := seedOrganization(t, conn, "backfillb"+shortID())

	all := []*seededRepo{
		{orgID: orgA, name: "a-pending", syncState: "pending", installed: true,
			wantJob: true, wantSyncState: "pending"},
		// ⚠ THE TWO ROWS NOTHING CAN EVER INGEST. They get no job — a job
		// for one would clone nothing and dead-letter — and they must NOT
		// be left `pending`, because `sync_state` is what the frontend
		// renders and no handler can reach them: both production writers of
		// `never_synced` key on `installation_id = $1`, which never matches
		// `installation_id IS NULL`, and for the uninstalled case the event
		// that would have fixed it has already been processed. Found by
		// PR #40's review; before it, these two ended `pending` forever.
		{orgID: orgA, name: "a-no-installation", syncState: "pending",
			wantJob: false, wantSyncState: "never_synced"},
		{orgID: orgA, name: "a-uninstalled", syncState: "pending",
			installed: true, uninstalled: true,
			wantJob: false, wantSyncState: "never_synced"},
		// The same condition reached from `syncing` rather than `pending`:
		// statement 2 skips it (no live installation), so statement 3 is
		// the only thing that can move it.
		{orgID: orgA, name: "a-syncing-uninstalled", syncState: "syncing",
			installed: true, uninstalled: true,
			wantJob: false, wantSyncState: "never_synced"},
		{orgID: orgA, name: "a-already-queued", syncState: "pending",
			installed: true, liveJob: true,
			wantJob: true, wantSyncState: "pending"},
		// The `syncing` fixture is what makes a missing `set_config` fail
		// loudly: the UPDATE that normalises it fires `trg_assert_tenant`,
		// a row trigger, which applies to the migrating superuser too.
		{orgID: orgA, name: "a-syncing", syncState: "syncing", installed: true,
			wantJob: true, wantSyncState: "pending"},
		{orgID: orgA, name: "a-synced", syncState: "synced", installed: true,
			wantJob: false, wantSyncState: "synced"},
		// ⚠ THE ROW THAT KEEPS THE STAND-DOWN NARROW, and it was added
		// because a mutation escaped without it: widening statement 3 from
		// `sync_state IN ('pending','syncing')` to `<> 'never_synced'`
		// passed the whole suite, because every other unsyncable fixture is
		// already `pending`. A repository that FINISHED keeps its content
		// and its state; relabelling it `never_synced` because the App went
		// away would throw away the one signal that says it was ingested,
		// and `docs/api-repositories.md` tells clients to expect the
		// opposite.
		{orgID: orgA, name: "a-synced-uninstalled", syncState: "synced",
			installed: true, uninstalled: true,
			wantJob: false, wantSyncState: "synced"},
		// The same boundary from the other side: `failed` is not a request
		// for work, so the stand-down must not touch it either. Its job, if
		// it had one, was dead-lettered or superseded long before this ran.
		{orgID: orgA, name: "a-failed-uninstalled", syncState: "failed",
			installed: true, uninstalled: true,
			wantJob: false, wantSyncState: "failed"},
		{orgID: orgB, name: "b-pending", syncState: "pending", installed: true,
			wantJob: true, wantSyncState: "pending"},
		{orgID: orgB, name: "b-never-synced", syncState: "never_synced", installed: true,
			wantJob: false, wantSyncState: "never_synced"},
	}
	for _, r := range all {
		r.id = seedRepository(t, conn, r)
	}

	before := liveJobIDsByRepository(t, conn)
	require.Len(t, before, 1,
		"exactly one live job exists before the migration: the pre-existing one")

	// 3. Apply it.
	require.NoError(t, m.Migrate(backfillToVersion))

	after := liveJobIDsByRepository(t, conn)
	require.Equal(t, countEligible(all), len(after), "live jobs after the backfill")
	assertBackfill(t, conn, all)

	// The repository that already had a live job keeps THAT job. A backfill
	// is the same work, already queued — so `DO NOTHING`, not the
	// producer's `DO UPDATE SET needs_rerun`, which would buy a second full
	// ingest of a repository being ingested right now.
	for _, r := range all {
		if !r.liveJob {
			continue
		}
		require.Equal(t, before[r.id], after[r.id],
			"%s: the pre-existing job must be left exactly as it is", r.name)
		require.False(t, jobNeedsRerun(t, conn, after[r.id]),
			"%s: a backfill is not new work, so it must not flag a rerun", r.name)
	}

	// 4. IDEMPOTENT. The down is a deliberate no-op, so rolling back and
	//    re-applying runs the up a second time over the rows it produced —
	//    the only way golang-migrate will run it twice, and the shape a
	//    re-run in production would take.
	require.NoError(t, m.Steps(-1))
	require.NoError(t, m.Steps(1))

	again := liveJobIDsByRepository(t, conn)
	require.Equal(t, after, again,
		"re-applying the backfill must create no job and replace none")
	assertBackfill(t, conn, all)

	t.Logf("backfill on %s: %d repositories seeded, %d eligible, "+
		"%d live jobs after one application and %d after two",
		dbName, len(all), countEligible(all), len(after), len(again))
}

// assertBackfill checks every fixture against its expectation, then the
// three properties that have to hold across the whole table.
func assertBackfill(t *testing.T, conn *pgx.Conn, all []*seededRepo) {
	t.Helper()
	for _, r := range all {
		live := countRows(t, conn,
			`SELECT count(*) FROM ingestion_jobs
			 WHERE repository_id = $1 AND state IN ('queued','running')`, r.id)
		if r.wantJob {
			require.Equalf(t, 1, live, "%s: expected exactly one live job", r.name)
		} else {
			require.Zerof(t, live, "%s: expected no job", r.name)
		}
		require.Equalf(t, r.wantSyncState, backfillSyncStateOf(t, conn, r.id),
			"%s: sync_state", r.name)
	}

	// ⚠ TENANCY, COMPARED EXPLICITLY. `ingestion_jobs` has no row-level
	// security, so a count that omits organization_id proves nothing about
	// it. The composite foreign key makes a mismatch unrepresentable; this
	// asserts the guarantee is still standing rather than trusting the
	// statement that wrote the rows.
	require.Zero(t, countRows(t, conn, `
		SELECT count(*) FROM ingestion_jobs j
		JOIN repositories r ON r.id = j.repository_id
		WHERE j.organization_id <> r.organization_id`),
		"a backfilled job carries the wrong organization")

	require.Zero(t, countRows(t, conn, `
		SELECT count(*) FROM ingestion_jobs
		WHERE job_type <> 'full_ingest' OR attempts <> 0 OR needs_rerun`),
		"a backfilled job is an ordinary unstarted full ingest")

	// ⚠ THE INVARIANT, STATED OVER THE WHOLE TABLE rather than fixture by
	// fixture, so a row shape nobody thought of still fails this. It is one
	// sentence: NO REPOSITORY IS LEFT ASKING FOR WORK THAT NOTHING WILL DO.
	//
	// Both halves matter and they fail in opposite directions. A syncable
	// row without a job waits forever with nothing to claim it; an
	// UNSYNCABLE row left `pending` renders as "queued, syncing soon"
	// forever, and no handler can correct it (PR #40's review). The second
	// half is why statement 3 exists.
	require.Zero(t, countRows(t, conn, `
		SELECT count(*) FROM repositories r
		JOIN github_installations gi ON gi.id = r.installation_id
		WHERE r.sync_state IN ('pending','syncing')
		  AND gi.uninstalled_at IS NULL
		  AND NOT EXISTS (
			SELECT 1 FROM ingestion_jobs j
			WHERE j.repository_id = r.id AND j.state IN ('queued','running'))`),
		"a syncable repository is still stranded `pending` with no job")

	require.Zero(t, countRows(t, conn, `
		SELECT count(*) FROM repositories r
		WHERE r.sync_state IN ('pending','syncing')
		  AND NOT EXISTS (
			SELECT 1 FROM github_installations gi
			WHERE gi.id = r.installation_id AND gi.uninstalled_at IS NULL)`),
		"a repository nothing can ingest is still showing as queued, and no "+
			"handler can reach it to say otherwise")

	// And the two together, phrased without reference to installations at
	// all: after this migration, `pending` means "a job exists for it".
	require.Zero(t, countRows(t, conn, `
		SELECT count(*) FROM repositories r
		WHERE r.sync_state IN ('pending','syncing')
		  AND NOT EXISTS (
			SELECT 1 FROM ingestion_jobs j
			WHERE j.repository_id = r.id AND j.state IN ('queued','running'))`),
		"`pending` must mean a live job exists")
}

func countEligible(all []*seededRepo) int {
	n := 0
	for _, r := range all {
		if r.wantJob {
			n++
		}
	}
	return n
}

// =====================================================================
// The migrator
// =====================================================================
//
// The scratch database itself comes from isolation.ScratchDatabase, lifted
// out of this file in 22-01 so the seeded-migration gate shares it.

func openMigrator(t *testing.T, dsn string) *migrate.Migrate {
	t.Helper()
	abs, err := filepath.Abs(migrationsDirForTest())
	require.NoError(t, err)
	m, err := migrate.New("file://"+filepath.ToSlash(abs), dsn)
	require.NoError(t, err)
	// Registered AFTER the scratch database's drop, so it runs BEFORE it:
	// the drop fails while migrate still holds a connection.
	t.Cleanup(func() { _, _ = m.Close() })
	return m
}

func migrationsDirForTest() string {
	_, thisFile, _, _ := runtime.Caller(0)
	// this file: services/backend/pkg/jobs/backfill_migration_test.go
	// target:    services/backend/migrations
	return filepath.Join(filepath.Dir(thisFile), "..", "..", "migrations")
}

// =====================================================================
// Fixtures, written as the superuser that owns the scratch database
// =====================================================================

func seedOrganization(t *testing.T, conn *pgx.Conn, slug string) string {
	t.Helper()
	ctx := context.Background()
	var orgID string
	require.NoError(t, conn.QueryRow(ctx,
		`INSERT INTO organizations (name, slug) VALUES ($1, $1) RETURNING id::text`,
		slug).Scan(&orgID))
	_, err := conn.Exec(ctx,
		`INSERT INTO projects (organization_id, name, slug, is_default)
		 VALUES ($1, $2, $2, true)`, orgID, slug+"-proj")
	require.NoError(t, err)
	return orgID
}

func seedRepository(t *testing.T, conn *pgx.Conn, r *seededRepo) string {
	t.Helper()
	ctx := context.Background()

	// `repositories` AND `github_installations` both carry
	// trg_assert_tenant, which a superuser does not escape — measured here,
	// where the first draft wrote the installation off the bare connection
	// and got 42501. So the fixture scopes itself exactly as production
	// does.
	tx, err := conn.Begin(ctx)
	require.NoError(t, err)
	defer func() { _ = tx.Rollback(ctx) }()
	_, err = tx.Exec(ctx, "SET LOCAL app.current_tenant = '"+r.orgID+"'")
	require.NoError(t, err)

	var installationID *string
	if r.installed {
		uninstalled := "NULL"
		if r.uninstalled {
			uninstalled = "NOW()"
		}
		var id string
		require.NoError(t, tx.QueryRow(ctx, `
			INSERT INTO github_installations
			  (organization_id, github_installation_id, account_login, account_type,
			   repository_selection, uninstalled_at)
			VALUES ($1, $2, 'someone', 'User', 'selected', `+uninstalled+`)
			RETURNING id::text`, r.orgID, nextGitHubInstallationID()).Scan(&id))
		installationID = &id
	}

	var projectID string
	require.NoError(t, tx.QueryRow(ctx,
		`SELECT id::text FROM projects WHERE organization_id = $1 AND is_default`,
		r.orgID).Scan(&projectID))

	var repoID string
	require.NoError(t, tx.QueryRow(ctx, `
		INSERT INTO repositories
		  (project_id, installation_id, github_repo_id, name, git_url, sync_state)
		VALUES ($1, $2, $3, $4, $5, $6)
		RETURNING id::text`,
		projectID, installationID, nextGitHubRepoID(), r.name,
		"https://github.com/someone/"+r.name+".git", r.syncState).Scan(&repoID))

	if r.liveJob {
		_, err = tx.Exec(ctx, `
			INSERT INTO ingestion_jobs (organization_id, repository_id, job_type, state)
			VALUES ($1, $2, 'full_ingest', 'queued')`, r.orgID, repoID)
		require.NoError(t, err)
	}
	require.NoError(t, tx.Commit(ctx))
	return repoID
}

// =====================================================================
// Reads
// =====================================================================

func liveJobIDsByRepository(t *testing.T, conn *pgx.Conn) map[string]string {
	t.Helper()
	rows, err := conn.Query(context.Background(),
		`SELECT repository_id::text, id::text FROM ingestion_jobs
		 WHERE state IN ('queued','running')`)
	require.NoError(t, err)
	byRepo, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) ([2]string, error) {
		var pair [2]string
		err := row.Scan(&pair[0], &pair[1])
		return pair, err
	})
	require.NoError(t, err)

	out := make(map[string]string, len(byRepo))
	for _, pair := range byRepo {
		out[pair[0]] = pair[1]
	}
	require.Len(t, out, len(byRepo),
		"two live jobs for one repository: the partial unique index is gone")
	return out
}

func jobNeedsRerun(t *testing.T, conn *pgx.Conn, jobID string) bool {
	t.Helper()
	var flag bool
	require.NoError(t, conn.QueryRow(context.Background(),
		`SELECT needs_rerun FROM ingestion_jobs WHERE id = $1`, jobID).Scan(&flag))
	return flag
}

func backfillSyncStateOf(t *testing.T, conn *pgx.Conn, repoID string) string {
	t.Helper()
	var state string
	require.NoError(t, conn.QueryRow(context.Background(),
		`SELECT sync_state FROM repositories WHERE id = $1`, repoID).Scan(&state))
	return state
}

func countRows(t *testing.T, conn *pgx.Conn, sql string, args ...any) int {
	t.Helper()
	var n int
	require.NoError(t, conn.QueryRow(context.Background(), sql, args...).Scan(&n),
		"counting rows: %s", firstLineOf(sql))
	return n
}

func firstLineOf(s string) string {
	s = strings.TrimSpace(s)
	if i := strings.IndexByte(s, '\n'); i >= 0 {
		return s[:i]
	}
	return s
}

// =====================================================================
// Ids
// =====================================================================

var (
	backfillInstallationSeq int64 = 8_100_000
	backfillRepoSeq         int64 = 8_200_000
)

func nextGitHubInstallationID() int64 {
	backfillInstallationSeq++
	return backfillInstallationSeq
}

func nextGitHubRepoID() int64 {
	backfillRepoSeq++
	return backfillRepoSeq
}

func shortID() string {
	return fmt.Sprintf("%.8s", strings.ReplaceAll(uuid.NewString(), "-", ""))
}

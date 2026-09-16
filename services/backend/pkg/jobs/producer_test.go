package jobs

// Tests for the Go producer (21-03).
//
// schema_test.go proves the STATEMENTS on PostgreSQL 16. This file proves
// the two functions built on them: validation, de-duplication, the
// `sync_state` projection, the L4 ordering contract, tenant scope, and the
// concurrency the partial unique index exists for.
//
// Same house rules as schema_test.go: one behaviour per test, writes run as
// the app role inside a tenant transaction as production does, and the
// tests that create repositories assert AssertNoRepositoryTenantDrift
// themselves while their rows still exist.

import (
	"context"
	"strings"
	"sync"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/stretchr/testify/require"

	"github.com/yourusername/smart-docs-platform/services/backend/pkg/testing/isolation"
)

// =====================================================================
// Enqueue — the single-repository case
// =====================================================================

func TestEnqueue_CreatesOneQueuedJobAndProjectsPending(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		setSyncState(t, pool, orgA, orgA.RepoID, "never_synced")

		tx, err := isolation.TenantScope(ctx, pool, orgA.ID)
		require.NoError(t, err)
		defer func() { _ = tx.Rollback(ctx) }()

		results, err := Enqueue(ctx, tx, []EnqueueRequest{
			{OrganizationID: orgA.ID, RepositoryID: orgA.RepoID, JobType: JobTypeFullIngest},
		})
		require.NoError(t, err)
		require.Len(t, results, 1)
		require.Equal(t, orgA.RepoID, results[0].RepositoryID)
		require.False(t, results[0].WasExisting, "the first enqueue inserts")
		require.NotEmpty(t, results[0].JobID)
		require.NoError(t, tx.Commit(ctx))

		live := liveJobIDs(t, pool, orgA.RepoID)
		require.Equal(t, []string{results[0].JobID}, live)
		require.Equal(t, "full_ingest", jobTypeOf(t, pool, results[0].JobID))
		require.Equal(t, "queued", jobState(t, pool, results[0].JobID))
		require.Equal(t, "pending", syncStateOf(t, pool, orgA, orgA.RepoID),
			"the producer writes the projection, in the same transaction as the job")

		isolation.AssertNoRepositoryTenantDrift(t, pool)
	})
}

// A push against a live job flags it instead of queueing a second one
// (L7), and the projection is deliberately NOT rewritten: the live job owns
// `sync_state` from the moment it is claimed.
//
// The job is CLAIMED first, which matters: a job that has not started has
// its flag cleared again in the same transaction — see
// TestEnqueue_ClearsARerunFlagOnlyOnAJobThatHasNotStarted — so an unclaimed
// job would be the wrong fixture for the flag half of this.
func TestEnqueue_ASecondEnqueueFlagsTheLiveJobAndLeavesTheProjection(t *testing.T) {
	pool := isolation.SetupTestDB(t)

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		first := enqueueOne(t, pool, orgA, orgA.RepoID, JobTypeFullIngest)
		require.False(t, first.WasExisting)

		// A worker has claimed it since; `syncing` is what the second
		// enqueue must not stomp.
		claim(t, pool, first.JobID, "worker-1")
		setSyncState(t, pool, orgA, orgA.RepoID, "syncing")

		second := enqueueOne(t, pool, orgA, orgA.RepoID, JobTypeIncremental)
		require.True(t, second.WasExisting, "a live job must be flagged, not replaced")
		require.Equal(t, first.JobID, second.JobID, "and its id reported, not a new one")

		require.True(t, needsRerun(t, pool, first.JobID))
		require.Equal(t, []string{first.JobID}, liveJobIDs(t, pool, orgA.RepoID),
			"still exactly one live job")
		require.Equal(t, "full_ingest", jobTypeOf(t, pool, first.JobID),
			"the flag does not rewrite the live job's type")
		require.Equal(t, "syncing", syncStateOf(t, pool, orgA, orgA.RepoID),
			"a flagged enqueue must leave sync_state to the job that is running")

		isolation.AssertNoRepositoryTenantDrift(t, pool)
	})
}

// =====================================================================
// Enqueue — the set form, over W3's scenario
// =====================================================================
//
// 21-02 proved W3 through a generated multi-row VALUES list. Production
// sends `unnest`, so the scenario runs again through the statement that
// actually ships: three repositories, one of them already live, in one
// statement. The withdrawn catch-23505 design lost two of the three while
// reporting success (L8).

func TestEnqueue_BulkRacingALiveJobHandlesEveryRow(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		repos := []string{
			orgA.RepoID,
			insertRepository(t, pool, orgA, "set-2"),
			insertRepository(t, pool, orgA, "set-3"),
		}
		// The relink that got there first, already claimed by a worker.
		live := seedJob(t, pool, orgA, repos[0], "running", jobOpts{LeaseOwner: "worker-1"})
		setSyncState(t, pool, orgA, repos[0], "syncing")
		for _, repo := range repos[1:] {
			setSyncState(t, pool, orgA, repo, "never_synced")
		}

		tx, err := isolation.TenantScope(ctx, pool, orgA.ID)
		require.NoError(t, err)
		defer func() { _ = tx.Rollback(ctx) }()

		reqs := make([]EnqueueRequest, 0, len(repos))
		for _, repo := range repos {
			reqs = append(reqs, EnqueueRequest{
				OrganizationID: orgA.ID, RepositoryID: repo, JobType: JobTypeFullIngest,
			})
		}
		results, err := Enqueue(ctx, tx, reqs)
		require.NoError(t, err)
		require.NoError(t, tx.Commit(ctx))

		require.Len(t, results, len(repos), "every row of the statement must report")
		for i, result := range results {
			require.Equal(t, repos[i], result.RepositoryID,
				"results come back in the caller's order")
		}
		require.True(t, results[0].WasExisting)
		require.Equal(t, live, results[0].JobID)
		require.False(t, results[1].WasExisting)
		require.False(t, results[2].WasExisting)

		for _, repo := range repos {
			require.Len(t, liveJobIDs(t, pool, repo), 1,
				"repository %s must end with exactly one live job", repo)
		}
		require.Equal(t, "syncing", syncStateOf(t, pool, orgA, repos[0]),
			"the repository whose job was merely flagged keeps its state")
		require.Equal(t, "pending", syncStateOf(t, pool, orgA, repos[1]))
		require.Equal(t, "pending", syncStateOf(t, pool, orgA, repos[2]))

		isolation.AssertNoRepositoryTenantDrift(t, pool)
	})
}

// ⚠ `ON CONFLICT DO UPDATE` may not touch the same row twice in one
// statement — it raises 21000 `ON CONFLICT DO UPDATE command cannot affect
// row a second time`. A bulk webhook payload can name a repository twice,
// so Enqueue de-duplicates before the statement runs. Nothing in SQL does
// this for us.
func TestEnqueue_DuplicateRepositoryIDsAreDeDuplicated(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		other := insertRepository(t, pool, orgA, "dedupe-2")

		tx, err := isolation.TenantScope(ctx, pool, orgA.ID)
		require.NoError(t, err)
		defer func() { _ = tx.Rollback(ctx) }()

		results, err := Enqueue(ctx, tx, []EnqueueRequest{
			{OrganizationID: orgA.ID, RepositoryID: orgA.RepoID, JobType: JobTypeFullIngest},
			{OrganizationID: orgA.ID, RepositoryID: other, JobType: JobTypeFullIngest},
			{OrganizationID: orgA.ID, RepositoryID: orgA.RepoID, JobType: JobTypeIncremental},
		})
		require.NoError(t, err,
			"a repeated repository must be de-duplicated, not raise 21000")
		require.NoError(t, tx.Commit(ctx))

		require.Len(t, results, 2, "one result per DISTINCT repository")
		require.Equal(t, orgA.RepoID, results[0].RepositoryID)
		require.Equal(t, other, results[1].RepositoryID)
		require.Equal(t, "full_ingest", jobTypeOf(t, pool, results[0].JobID),
			"the FIRST request for a repository wins")
		require.Len(t, liveJobIDs(t, pool, orgA.RepoID), 1)
		require.Len(t, liveJobIDs(t, pool, other), 1)

		isolation.AssertNoRepositoryTenantDrift(t, pool)
	})
}

// =====================================================================
// SupersedeLive, and L4's ordering contract
// =====================================================================

func TestSupersedeLive_ThenEnqueueInOneTransaction(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		quiet := insertRepository(t, pool, orgA, "no-live-job")
		old := seedJob(t, pool, orgA, orgA.RepoID, "running", jobOpts{LeaseOwner: "worker-1"})
		setSyncState(t, pool, orgA, orgA.RepoID, "syncing")

		tx, err := isolation.TenantScope(ctx, pool, orgA.ID)
		require.NoError(t, err)
		defer func() { _ = tx.Rollback(ctx) }()

		superseded, err := SupersedeLive(ctx, tx, orgA.ID, []string{orgA.RepoID, quiet})
		require.NoError(t, err)
		require.Equal(t, []string{orgA.RepoID}, superseded,
			"only repositories that actually had a live job are reported")

		results, err := Enqueue(ctx, tx, []EnqueueRequest{
			{OrganizationID: orgA.ID, RepositoryID: orgA.RepoID, JobType: JobTypeFullIngest},
		})
		require.NoError(t, err)
		require.False(t, results[0].WasExisting,
			"the superseded job has left the live set, so this one inserts")
		require.NoError(t, tx.Commit(ctx))

		require.Equal(t, "superseded", jobState(t, pool, old))
		require.Equal(t, "queued", jobState(t, pool, results[0].JobID))
		require.Equal(t, []string{results[0].JobID}, liveJobIDs(t, pool, orgA.RepoID))
		require.Equal(t, "pending", syncStateOf(t, pool, orgA, orgA.RepoID))

		// The supersede leaves the lease attached deliberately — it is the
		// only record of which worker was interrupted. That is also why
		// every lease-fenced statement carries `AND state = 'running'`.
		require.Equal(t, "worker-1", leaseOwnerOf(t, pool, old))

		isolation.AssertNoRepositoryTenantDrift(t, pool)
	})
}

// The order in the doc comment, measured. Backwards, nothing raises: the
// upsert flags the job that is about to leave the live set, the supersede
// then takes it out, and the repository ends with NO live job and a
// terminal row carrying a rerun flag nothing will ever read.
//
// This exists so the doc comment's claim is a measurement rather than a
// belief, and so that anyone who "simplifies" the handler by reordering the
// two calls fails a test instead of shipping silence.
func TestSupersedeLive_TheWrongOrderLosesTheJobSilently(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		old := seedJob(t, pool, orgA, orgA.RepoID, "running", jobOpts{LeaseOwner: "worker-1"})

		tx, err := isolation.TenantScope(ctx, pool, orgA.ID)
		require.NoError(t, err)
		defer func() { _ = tx.Rollback(ctx) }()

		results, err := Enqueue(ctx, tx, []EnqueueRequest{
			{OrganizationID: orgA.ID, RepositoryID: orgA.RepoID, JobType: JobTypeFullIngest},
		})
		require.NoError(t, err, "the wrong order raises nothing; that is the problem")
		require.True(t, results[0].WasExisting)
		require.Equal(t, old, results[0].JobID)

		superseded, err := SupersedeLive(ctx, tx, orgA.ID, []string{orgA.RepoID})
		require.NoError(t, err)
		require.Equal(t, []string{orgA.RepoID}, superseded)
		require.NoError(t, tx.Commit(ctx))

		require.Empty(t, liveJobIDs(t, pool, orgA.RepoID),
			"backwards, the repository is left with no live job at all")
		require.Equal(t, "superseded", jobState(t, pool, old))
		require.True(t, needsRerun(t, pool, old),
			"and the rerun flag survives on a terminal row, where it reads as pending work")

		isolation.AssertNoRepositoryTenantDrift(t, pool)
	})
}

// =====================================================================
// Tenant scope
// =====================================================================

// Enqueue IS scoped, by the database: trg_ingestion_jobs_tenant reads
// `repositories`, which carries FORCE ROW LEVEL SECURITY, so another
// tenant's repository is not visible and the insert is refused.
func TestEnqueue_AnotherTenantsRepositoryIsRefusedAndNothingIsWritten(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	isolation.WithTwoOrgs(t, pool, func(orgA, orgB *isolation.TestOrg) {
		for _, tc := range []struct {
			name  string
			orgID string
		}{
			{"claiming org B's repository as org A's own", orgA.ID},
			{"naming org B outright", orgB.ID},
		} {
			t.Run(tc.name, func(t *testing.T) {
				tx, err := isolation.TenantScope(ctx, pool, orgA.ID)
				require.NoError(t, err)
				defer func() { _ = tx.Rollback(ctx) }()

				_, err = Enqueue(ctx, tx, []EnqueueRequest{
					{OrganizationID: tc.orgID, RepositoryID: orgB.RepoID, JobType: JobTypeFullIngest},
				})
				pgErr := requirePgError(t, err)
				require.Equal(t, tenantSQLState, pgErr.Code, "message: %s", pgErr.Message)
				require.Contains(t, pgErr.Message, "does not exist",
					"under row-level security another tenant's repository is not an oracle")
				require.NoError(t, tx.Rollback(ctx))

				require.Empty(t, liveJobIDs(t, pool, orgB.RepoID),
					"nothing may be written for another tenant's repository")
			})
		}

		isolation.AssertNoRepositoryTenantDrift(t, pool)
	})
}

// ⚠ THE ONE STATEMENT IN THIS PACKAGE THE DATABASE DOES NOT SCOPE.
//
// `ingestion_jobs` has no row-level security, and the supersede touches
// neither organization_id nor repository_id, so trg_ingestion_jobs_tenant
// never fires — where the enqueue's BEFORE INSERT trigger refuses a
// mismatched tenant with 42501 (the test above). The `AND organization_id
// = $2` predicate is the whole of the scope, which is why it is tested
// head-on rather than assumed.
//
// It commits, deliberately: a rolled-back transaction would pass even if
// the predicate did nothing.
//
// The case this exists for is 21-04's. A webhook resolves repositories by
// `github_repo_id`, which `idx_repositories_project_github_repo` makes
// unique only PER PROJECT, so a less careful resolution can hand this
// function another tenant's repository. It must cancel nothing.
func TestSupersedeLive_CancelsNothingForAnotherTenant(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	isolation.WithTwoOrgs(t, pool, func(orgA, orgB *isolation.TestOrg) {
		foreign := seedJob(t, pool, orgB, orgB.RepoID, "running", jobOpts{LeaseOwner: "worker-b"})
		own := seedJob(t, pool, orgA, orgA.RepoID, "running", jobOpts{LeaseOwner: "worker-a"})

		tx, err := isolation.TenantScope(ctx, pool, orgA.ID)
		require.NoError(t, err)
		defer func() { _ = tx.Rollback(ctx) }()

		// Both ids in one call, the shape a bulk webhook would produce.
		superseded, err := SupersedeLive(ctx, tx, orgA.ID, []string{orgB.RepoID, orgA.RepoID})
		require.NoError(t, err,
			"a foreign id is not an error; it simply matches nothing")
		require.Equal(t, []string{orgA.RepoID}, superseded,
			"only the caller's own repository may be reported as superseded")
		require.NoError(t, tx.Commit(ctx))

		require.Equal(t, "running", jobState(t, pool, foreign),
			"org B's in-flight ingest must survive a cancellation aimed at it from org A")
		require.Equal(t, []string{foreign}, liveJobIDs(t, pool, orgB.RepoID),
			"and it must still be the live job for its repository")
		require.Equal(t, "superseded", jobState(t, pool, own))

		// The organization id is validated like every other id here.
		tx2, err := isolation.TenantScope(ctx, pool, orgA.ID)
		require.NoError(t, err)
		defer func() { _ = tx2.Rollback(ctx) }()
		_, err = SupersedeLive(ctx, tx2, "urn:uuid:"+orgA.ID, []string{orgA.RepoID})
		require.ErrorContains(t, err, "canonical")

		isolation.AssertNoRepositoryTenantDrift(t, pool)
	})
}

// =====================================================================
// A rerun flag on a job that has not started
// =====================================================================
//
// Found in PR #39's review. Two concurrent connects of a repository with no
// row yet have nothing to serialise on, so both classify as `new` and the
// loser's enqueue takes the upsert's conflict branch against the winner's
// brand-new job — leaving `needs_rerun = true` on a job at `attempts = 0`
// that has not run. 21-05 turns that flag into a SECOND full ingest, so one
// double-clicked Connect costs two.
//
// The L7 upsert is NOT changed: 21-02 proved it and 21-05 ports it to
// Python. The flag is cleared afterwards, in the same transaction, and only
// where clearing it is provably safe.

func TestEnqueue_ClearsARerunFlagOnlyOnAJobThatHasNotStarted(t *testing.T) {
	pool := isolation.SetupTestDB(t)

	cases := []struct {
		name    string
		state   string
		opts    jobOpts
		cleared bool
		why     string
	}{
		{
			name:    "a queued job that has never run",
			state:   "queued",
			opts:    jobOpts{},
			cleared: true,
			why: "it has not read the repository yet, so it will cover this work " +
				"when it is claimed; the flag would only buy a duplicate ingest",
		},
		{
			name:    "a queued job that has already attempted",
			state:   "queued",
			opts:    jobOpts{Attempts: 2},
			cleared: false,
			why: "an earlier attempt may have recorded last_stage and progress that " +
				"a retry resumes from, so it cannot be assumed to re-read the repository",
		},
		{
			name:    "a running job",
			state:   "running",
			opts:    jobOpts{Attempts: 1, LeaseOwner: "worker-1"},
			cleared: false,
			why:     "a worker has the repository open at some commit; everything after it is a real rerun",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
				live := seedJob(t, pool, orgA, orgA.RepoID, tc.state, tc.opts)
				require.False(t, needsRerun(t, pool, live), "the fixture starts unflagged")

				result := enqueueOne(t, pool, orgA, orgA.RepoID, JobTypeIncremental)
				require.True(t, result.WasExisting)
				require.Equal(t, live, result.JobID)

				require.Equal(t, !tc.cleared, needsRerun(t, pool, live), tc.why)
				require.Equal(t, tc.state, jobState(t, pool, live),
					"clearing the flag must not move the job")
				require.Len(t, liveJobIDs(t, pool, orgA.RepoID), 1)

				isolation.AssertNoRepositoryTenantDrift(t, pool)
			})
		})
	}
}

// A flag that was ALREADY set, on a job that has not started, is cleared
// too — and correctly so, by the same argument. It is called out because
// "clear the flag this call just set" is the tempting narrower reading, and
// the statement deliberately does not try to tell the two apart.
func TestEnqueue_ClearsAPreviouslySetFlagOnAnUnstartedJob(t *testing.T) {
	pool := isolation.SetupTestDB(t)

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		live := seedJob(t, pool, orgA, orgA.RepoID, "queued", jobOpts{NeedsRerun: true})

		result := enqueueOne(t, pool, orgA, orgA.RepoID, JobTypeIncremental)
		require.True(t, result.WasExisting)
		require.False(t, needsRerun(t, pool, live),
			"a job that has not started covers every push that arrived before it was claimed")
	})
}

// The clear must not reach a job this call did not flag — a different
// repository's unstarted job with a rerun pending keeps it.
func TestEnqueue_DoesNotClearAnotherRepositorysRerunFlag(t *testing.T) {
	pool := isolation.SetupTestDB(t)

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		other := insertRepository(t, pool, orgA, "bystander")
		bystander := seedJob(t, pool, orgA, other, "queued", jobOpts{NeedsRerun: true})
		mine := seedJob(t, pool, orgA, orgA.RepoID, "queued", jobOpts{})

		result := enqueueOne(t, pool, orgA, orgA.RepoID, JobTypeIncremental)
		require.Equal(t, mine, result.JobID)

		require.True(t, needsRerun(t, pool, bystander),
			"the clear is restricted to the jobs this call flagged")

		isolation.AssertNoRepositoryTenantDrift(t, pool)
	})
}

// =====================================================================
// Validation, before any statement runs
// =====================================================================

func TestEnqueue_RejectsBadInputWithoutTouchingTheDatabase(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		cases := []struct {
			name    string
			req     EnqueueRequest
			wantErr string
		}{
			{
				name:    "an unknown job type",
				req:     EnqueueRequest{OrganizationID: orgA.ID, RepositoryID: orgA.RepoID, JobType: "rebuild"},
				wantErr: "unknown job type",
			},
			{
				name:    "an empty job type",
				req:     EnqueueRequest{OrganizationID: orgA.ID, RepositoryID: orgA.RepoID},
				wantErr: "unknown job type",
			},
			{
				// uuid.Parse is a parser, not a validator: it accepts all
				// four of these and Postgres accepts some and rejects
				// others with 22P02.
				name:    "a URN-form repository id",
				req:     EnqueueRequest{OrganizationID: orgA.ID, RepositoryID: "urn:uuid:" + orgA.RepoID, JobType: JobTypeFullIngest},
				wantErr: "canonical",
			},
			{
				name:    "a brace-form repository id",
				req:     EnqueueRequest{OrganizationID: orgA.ID, RepositoryID: "{" + orgA.RepoID + "}", JobType: JobTypeFullIngest},
				wantErr: "canonical",
			},
			{
				name:    "an uppercase repository id",
				req:     EnqueueRequest{OrganizationID: orgA.ID, RepositoryID: strings.ToUpper(orgA.RepoID), JobType: JobTypeFullIngest},
				wantErr: "canonical",
			},
			{
				name:    "an undashed organization id",
				req:     EnqueueRequest{OrganizationID: strings.ReplaceAll(orgA.ID, "-", ""), RepositoryID: orgA.RepoID, JobType: JobTypeFullIngest},
				wantErr: "canonical",
			},
			{
				name:    "an empty repository id",
				req:     EnqueueRequest{OrganizationID: orgA.ID, JobType: JobTypeFullIngest},
				wantErr: "must not be empty",
			},
			{
				name:    "a repository id that is not a uuid at all",
				req:     EnqueueRequest{OrganizationID: orgA.ID, RepositoryID: "not-a-uuid", JobType: JobTypeFullIngest},
				wantErr: "not a valid uuid",
			},
		}

		for _, tc := range cases {
			t.Run(tc.name, func(t *testing.T) {
				tx, err := isolation.TenantScope(ctx, pool, orgA.ID)
				require.NoError(t, err)
				defer func() { _ = tx.Rollback(ctx) }()

				_, err = Enqueue(ctx, tx, []EnqueueRequest{tc.req})
				require.ErrorContains(t, err, tc.wantErr)

				// The transaction is still usable, which is the point of
				// validating before running anything: a bad id must not
				// abort work the caller has already done.
				var alive int
				require.NoError(t, tx.QueryRow(ctx, `SELECT 1`).Scan(&alive))
				require.Equal(t, 1, alive)
				require.Empty(t, liveJobIDs(t, tx, orgA.RepoID),
					"a rejected request must write nothing")
			})
		}

		t.Run("a valid request alongside an invalid one writes nothing", func(t *testing.T) {
			other := insertRepository(t, pool, orgA, "partial-batch")
			tx, err := isolation.TenantScope(ctx, pool, orgA.ID)
			require.NoError(t, err)
			defer func() { _ = tx.Rollback(ctx) }()

			_, err = Enqueue(ctx, tx, []EnqueueRequest{
				{OrganizationID: orgA.ID, RepositoryID: other, JobType: JobTypeFullIngest},
				{OrganizationID: orgA.ID, RepositoryID: orgA.RepoID, JobType: "rebuild"},
			})
			require.ErrorContains(t, err, "unknown job type")
			require.Empty(t, liveJobIDs(t, tx, other),
				"validation runs over the whole batch before the statement does")
		})

		t.Run("a duplicate does not excuse an invalid job type", func(t *testing.T) {
			tx, err := isolation.TenantScope(ctx, pool, orgA.ID)
			require.NoError(t, err)
			defer func() { _ = tx.Rollback(ctx) }()

			_, err = Enqueue(ctx, tx, []EnqueueRequest{
				{OrganizationID: orgA.ID, RepositoryID: orgA.RepoID, JobType: JobTypeFullIngest},
				{OrganizationID: orgA.ID, RepositoryID: orgA.RepoID, JobType: "rebuild"},
			})
			require.ErrorContains(t, err, "unknown job type",
				"the de-duplicated request is validated too")
		})
	})
}

// A nil transaction is a wiring bug and is reported as one, ahead of
// "nothing to do": a caller that reached here without a transaction is
// broken whether or not this particular batch happened to be empty.
func TestProducer_EmptyInputAndNilTransaction(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	_, err := Enqueue(ctx, nil, nil)
	require.ErrorIs(t, err, ErrNoTransaction)
	_, err = Enqueue(ctx, nil, []EnqueueRequest{{}})
	require.ErrorIs(t, err, ErrNoTransaction)
	_, err = SupersedeLive(ctx, nil, "", nil)
	require.ErrorIs(t, err, ErrNoTransaction)
	_, err = SupersedeLive(ctx, nil, uuid.NewString(), []string{uuid.NewString()})
	require.ErrorIs(t, err, ErrNoTransaction)

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		tx, err := isolation.TenantScope(ctx, pool, orgA.ID)
		require.NoError(t, err)
		defer func() { _ = tx.Rollback(ctx) }()

		results, err := Enqueue(ctx, tx, nil)
		require.NoError(t, err, "an empty batch is not an error")
		require.Empty(t, results)

		superseded, err := SupersedeLive(ctx, tx, orgA.ID, nil)
		require.NoError(t, err)
		require.Empty(t, superseded)
	})
}

func TestSupersedeLive_RejectsANonCanonicalID(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		tx, err := isolation.TenantScope(ctx, pool, orgA.ID)
		require.NoError(t, err)
		defer func() { _ = tx.Rollback(ctx) }()

		_, err = SupersedeLive(ctx, tx, orgA.ID, []string{"urn:uuid:" + orgA.RepoID})
		require.ErrorContains(t, err, "canonical")
	})
}

// =====================================================================
// The race the partial unique index exists for (L8)
// =====================================================================
//
// Sixteen callers, each with its own transaction, released together to
// enqueue the SAME repository. Every one must succeed, exactly one must
// report WasExisting = false, and the repository must end with exactly one
// live job.
//
// Structured after pkg/testing/isolation/container_concurrency_test.go: a
// barrier rather than a hope that Go's scheduler produces contention. The
// transactions are opened BEFORE the barrier, so what is released is the
// enqueue itself and not a queue for pooled connections — which is also why
// this uses its own pool, since the harness pool defaults to
// max(4, NumCPU) and CI's runner has two cores.
//
// FIVE ROUNDS ON A WARM POOL. A single cold round proves nothing: the
// first round pays for connection setup and the callers arrive spread out,
// which is the opposite of the contention being tested (21-CONTEXT).
func TestEnqueue_ConcurrentEnqueuesResolveToOneLiveJob(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	const (
		callers = 16
		rounds  = 5
	)

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		cfg := pool.Config()
		cfg.MaxConns = callers + 2
		racePool, err := pgxpool.NewWithConfig(ctx, cfg)
		require.NoError(t, err)
		defer racePool.Close()

		for round := 1; round <= rounds; round++ {
			// Only this test's own repository is touched — no blanket
			// delete. See asWorker in schema_test.go for why that matters
			// now that producers live in pkg/api/handlers too.
			clearJobsFor(t, pool, orgA.RepoID)
			setSyncState(t, pool, orgA, orgA.RepoID, "never_synced")

			var (
				start sync.WaitGroup
				done  sync.WaitGroup
				mu    sync.Mutex
				fresh int
				errs  []error
			)
			start.Add(1)
			done.Add(callers)

			txs := make([]pgx.Tx, callers)
			for i := 0; i < callers; i++ {
				tx, err := isolation.TenantScope(ctx, racePool, orgA.ID)
				require.NoError(t, err, "round %d: caller %d", round, i)
				txs[i] = tx
			}

			for i := 0; i < callers; i++ {
				go func(tx pgx.Tx) {
					defer done.Done()
					start.Wait() // release them together

					results, err := Enqueue(ctx, tx, []EnqueueRequest{{
						OrganizationID: orgA.ID,
						RepositoryID:   orgA.RepoID,
						JobType:        JobTypeFullIngest,
					}})
					if err == nil {
						err = tx.Commit(ctx)
					}

					mu.Lock()
					defer mu.Unlock()
					if err != nil {
						_ = tx.Rollback(ctx)
						errs = append(errs, err)
						return
					}
					if !results[0].WasExisting {
						fresh++
					}
				}(txs[i])
			}

			start.Done()
			done.Wait()

			require.Emptyf(t, errs, "round %d: %d of %d callers failed; first: %v",
				round, len(errs), callers, firstErr(errs))
			require.Equalf(t, 1, fresh,
				"round %d: exactly one caller may insert; the rest must flag the live job", round)
			require.Lenf(t, liveJobIDs(t, pool, orgA.RepoID), 1,
				"round %d: exactly one live job may survive %d concurrent enqueues", round, callers)
			require.Equalf(t, "pending", syncStateOf(t, pool, orgA, orgA.RepoID),
				"round %d: the one insert projects pending", round)
		}

		isolation.AssertNoRepositoryTenantDrift(t, pool)
	})
}

// =====================================================================
// Helpers
// =====================================================================

func enqueueOne(t *testing.T, pool *pgxpool.Pool, org *isolation.TestOrg, repoID string, jobType JobType) EnqueueResult {
	t.Helper()
	ctx := context.Background()

	tx, err := isolation.TenantScope(ctx, pool, org.ID)
	require.NoError(t, err)
	defer func() { _ = tx.Rollback(ctx) }()

	results, err := Enqueue(ctx, tx, []EnqueueRequest{
		{OrganizationID: org.ID, RepositoryID: repoID, JobType: jobType},
	})
	require.NoError(t, err)
	require.Len(t, results, 1)
	require.NoError(t, tx.Commit(ctx))
	return results[0]
}

// setSyncState writes the projection column directly, which is how a test
// arranges a state the producer must or must not overwrite. `repositories`
// has row-level security, so this runs under the org's tenant scope.
func setSyncState(t *testing.T, pool *pgxpool.Pool, org *isolation.TestOrg, repoID, state string) {
	t.Helper()
	ctx := context.Background()

	tx, err := isolation.TenantScope(ctx, pool, org.ID)
	require.NoError(t, err)
	defer func() { _ = tx.Rollback(ctx) }()

	tag, err := tx.Exec(ctx,
		`UPDATE repositories SET sync_state = $2 WHERE id = $1`, repoID, state)
	require.NoError(t, err)
	require.EqualValues(t, 1, tag.RowsAffected())
	require.NoError(t, tx.Commit(ctx))
}

func syncStateOf(t *testing.T, pool *pgxpool.Pool, org *isolation.TestOrg, repoID string) string {
	t.Helper()
	ctx := context.Background()

	tx, err := isolation.TenantScope(ctx, pool, org.ID)
	require.NoError(t, err)
	defer func() { _ = tx.Rollback(ctx) }()

	var state string
	require.NoError(t, tx.QueryRow(ctx,
		`SELECT sync_state FROM repositories WHERE id = $1`, repoID).Scan(&state))
	return state
}

// claim moves a job to `running` with a lease, the way a worker does
// (21-05). A test that wants to observe a rerun FLAG needs it: an unstarted
// job has that flag cleared again by the enqueue itself.
func claim(t *testing.T, pool *pgxpool.Pool, jobID, owner string) {
	t.Helper()
	tag, err := pool.Exec(context.Background(), `
		UPDATE ingestion_jobs
		SET state = 'running', lease_owner = $2,
		    lease_expires_at = NOW() + INTERVAL '5 minutes',
		    attempts = attempts + 1, updated_at = NOW()
		WHERE id = $1`, jobID, owner)
	require.NoError(t, err)
	require.EqualValues(t, 1, tag.RowsAffected())
}

func jobTypeOf(t *testing.T, q querier, id string) string {
	t.Helper()
	var jobType string
	require.NoError(t, q.QueryRow(context.Background(),
		`SELECT job_type FROM ingestion_jobs WHERE id = $1`, id).Scan(&jobType))
	return jobType
}

func leaseOwnerOf(t *testing.T, q querier, id string) string {
	t.Helper()
	var owner *string
	require.NoError(t, q.QueryRow(context.Background(),
		`SELECT lease_owner FROM ingestion_jobs WHERE id = $1`, id).Scan(&owner))
	require.NotNil(t, owner, "the lease must still be attached")
	return *owner
}

// clearJobsFor removes one repository's jobs between rounds of the race
// test. Scoped to a single repository on purpose: no test in this package
// deletes a row it did not create.
func clearJobsFor(t *testing.T, pool *pgxpool.Pool, repoID string) {
	t.Helper()
	_, err := pool.Exec(context.Background(),
		`DELETE FROM ingestion_jobs WHERE repository_id = $1`, repoID)
	require.NoError(t, err)
}

func firstErr(errs []error) error {
	if len(errs) == 0 {
		return nil
	}
	return errs[0]
}

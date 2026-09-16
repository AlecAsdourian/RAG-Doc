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
func TestEnqueue_ASecondEnqueueFlagsTheLiveJobAndLeavesTheProjection(t *testing.T) {
	pool := isolation.SetupTestDB(t)

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		first := enqueueOne(t, pool, orgA, orgA.RepoID, JobTypeFullIngest)
		require.False(t, first.WasExisting)

		// A worker has claimed it since; `syncing` is what the second
		// enqueue must not stomp.
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

		superseded, err := SupersedeLive(ctx, tx, []string{orgA.RepoID, quiet})
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

		superseded, err := SupersedeLive(ctx, tx, []string{orgA.RepoID})
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

// ⚠ SupersedeLive is NOT scoped, and this test is here to keep that a
// measured fact rather than a surprise. `ingestion_jobs` has no row-level
// security, and the statement touches neither organization_id nor
// repository_id, so trg_ingestion_jobs_tenant never fires.
//
// The obligation the doc comment states — pass only ids the same
// transaction has already read out of `repositories` — is the ONLY thing
// standing between a caller and another tenant's queue. Connect satisfies
// it through its `FOR UPDATE OF r` lookup.
func TestSupersedeLive_IsNotScopedByTheDatabase(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	isolation.WithTwoOrgs(t, pool, func(orgA, orgB *isolation.TestOrg) {
		foreign := seedJob(t, pool, orgB, orgB.RepoID, "queued", jobOpts{})

		tx, err := isolation.TenantScope(ctx, pool, orgA.ID)
		require.NoError(t, err)
		defer func() { _ = tx.Rollback(ctx) }()

		superseded, err := SupersedeLive(ctx, tx, []string{orgB.RepoID})
		require.NoError(t, err,
			"no error: the database does not scope this statement")
		require.Equal(t, []string{orgB.RepoID}, superseded,
			"org B's live job was superseded from inside org A's transaction — "+
				"which is why the caller must only pass ids it read under its own scope")
		require.NoError(t, tx.Rollback(ctx))

		require.Equal(t, "queued", jobState(t, pool, foreign),
			"rolled back, so nothing is left behind by this test")
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
	_, err = SupersedeLive(ctx, nil, nil)
	require.ErrorIs(t, err, ErrNoTransaction)
	_, err = SupersedeLive(ctx, nil, []string{uuid.NewString()})
	require.ErrorIs(t, err, ErrNoTransaction)

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		tx, err := isolation.TenantScope(ctx, pool, orgA.ID)
		require.NoError(t, err)
		defer func() { _ = tx.Rollback(ctx) }()

		results, err := Enqueue(ctx, tx, nil)
		require.NoError(t, err, "an empty batch is not an error")
		require.Empty(t, results)

		superseded, err := SupersedeLive(ctx, tx, nil)
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

		_, err = SupersedeLive(ctx, tx, []string{"urn:uuid:" + orgA.RepoID})
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

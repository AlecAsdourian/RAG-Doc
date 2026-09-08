package auth_test

// Isolation tests for the provisioning path (Phase 19-02).
//
// The webhook itself carries an @skip-isolation-test marker (19-01) —
// it's signature-verified and provisions its own tenant. But the EFFECT
// of provisioning is a brand-new tenant scaffold, and the question that
// matters is whether that new tenant is properly walled off from every
// existing one. These tests answer that using the 17-01 harness.
//
// External test package (auth_test) so it can use the isolation harness
// without dragging pkg/testing/isolation into pkg/auth's import graph.

import (
	"context"
	"fmt"
	"sync"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/stretchr/testify/require"

	"github.com/yourusername/smart-docs-platform/services/backend/pkg/auth"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/testing/isolation"
)

func TestProvisioningIsolation_NewTenantIsWalledOff(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		provisioner := auth.NewUserProvisioner(pool)

		supabaseID := uuid.NewString()
		email := fmt.Sprintf("newbie-%s@example.com", uuid.NewString()[:8])

		user, created, err := provisioner.ProvisionOAuthUser(ctx, "github", email, "New Bie", supabaseID)
		require.NoError(t, err)
		require.True(t, created, "first provisioning must report created=true")
		t.Cleanup(func() { cleanupProvisionedUser(t, pool, user.ID) })

		newOrgID, err := provisioner.CreateOrganizationForUser(ctx, user.ID,
			"New Bie's Organization", "newbie-org-"+uuid.NewString()[:8])
		require.NoError(t, err)
		t.Cleanup(func() { cleanupProvisionedOrg(t, pool, newOrgID) })

		// The freshly-provisioned org must not be able to see orgA's
		// tenant-scoped rows, and vice versa. Insert a chunk under orgA,
		// then try to read it under the new tenant's scope.
		isolation.AssertNoCrossTenantLeak(t, pool, orgA.ID, newOrgID.String(),
			func(tx pgx.Tx) error {
				var runID string
				if err := tx.QueryRow(ctx,
					`INSERT INTO ingestion_runs (repository_id, commit_sha, branch, status)
					 VALUES ($1, $2, $3, $4) RETURNING id`,
					orgA.RepoID, "0000000000000000000000000000000000000000", "main", "completed",
				).Scan(&runID); err != nil {
					return err
				}
				_, err := tx.Exec(ctx,
					`INSERT INTO chunks (ingestion_run_id, repository_id, file_path,
					     start_line, end_line, content, content_hash)
					 VALUES ($1, $2, $3, $4, $5, $6, $7)`,
					runID, orgA.RepoID, "secret.md", 1, 2, "orgA private marmalade", "h-prov-iso")
				return err
			},
			func(tx pgx.Tx) (bool, error) {
				var exists bool
				err := tx.QueryRow(ctx,
					`SELECT EXISTS (SELECT 1 FROM chunks WHERE content = $1)`,
					"orgA private marmalade").Scan(&exists)
				return exists, err
			},
		)
	})
}

func TestProvisioningIsolation_ReplayIsIdempotent(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()
	provisioner := auth.NewUserProvisioner(pool)

	supabaseID := uuid.NewString()
	email := fmt.Sprintf("replay-%s@example.com", uuid.NewString()[:8])

	first, created, err := provisioner.ProvisionOAuthUser(ctx, "github", email, "Replay User", supabaseID)
	require.NoError(t, err)
	require.True(t, created)
	t.Cleanup(func() { cleanupProvisionedUser(t, pool, first.ID) })

	// Same event delivered again — Supabase retries on any non-2xx.
	second, created, err := provisioner.ProvisionOAuthUser(ctx, "github", email, "Replay User", supabaseID)
	require.NoError(t, err)
	require.False(t, created, "replay must report created=false")
	require.Equal(t, first.ID, second.ID, "replay must resolve to the same user row")

	var count int
	require.NoError(t, pool.QueryRow(ctx,
		`SELECT COUNT(*) FROM users WHERE supabase_user_id = $1`, supabaseID).Scan(&count))
	require.Equal(t, 1, count, "replay must not duplicate the user row")
}

func TestProvisioningIsolation_ConcurrentDuplicateWebhooks(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()
	provisioner := auth.NewUserProvisioner(pool)

	supabaseID := uuid.NewString()
	email := fmt.Sprintf("concurrent-%s@example.com", uuid.NewString()[:8])

	const goroutines = 8
	var (
		wg       sync.WaitGroup
		mu       sync.Mutex
		createds int
		userIDs  = map[uuid.UUID]struct{}{}
		errs     []error
	)

	for i := 0; i < goroutines; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			u, created, err := provisioner.ProvisionOAuthUser(ctx, "github", email, "Concurrent User", supabaseID)
			mu.Lock()
			defer mu.Unlock()
			if err != nil {
				errs = append(errs, err)
				return
			}
			if created {
				createds++
			}
			userIDs[u.ID] = struct{}{}
		}()
	}
	wg.Wait()

	require.Empty(t, errs, "no goroutine should error under concurrent replay")
	require.Equal(t, 1, createds, "exactly one goroutine should report created=true")
	require.Len(t, userIDs, 1, "every goroutine must resolve to the same user row")

	for id := range userIDs {
		t.Cleanup(func() { cleanupProvisionedUser(t, pool, id) })
	}

	var count int
	require.NoError(t, pool.QueryRow(ctx,
		`SELECT COUNT(*) FROM users WHERE supabase_user_id = $1`, supabaseID).Scan(&count))
	require.Equal(t, 1, count, "concurrent replay must not duplicate the user row")
}

func TestProvisioningIsolation_SlugCollisionRecovers(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()
	provisioner := auth.NewUserProvisioner(pool)

	// Two users whose email local parts are identical produce the same
	// deterministic slug base. The second must still get an org.
	base := "collide" + uuid.NewString()[:6]
	sharedSlug := base + "-org"

	mk := func(domain string) uuid.UUID {
		u, created, err := provisioner.ProvisionOAuthUser(ctx, "github",
			fmt.Sprintf("%s@%s", base, domain), "Collide User", uuid.NewString())
		require.NoError(t, err)
		require.True(t, created)
		t.Cleanup(func() { cleanupProvisionedUser(t, pool, u.ID) })

		orgID, err := provisioner.CreateOrganizationForUser(ctx, u.ID, "Collide Org", sharedSlug)
		require.NoError(t, err, "slug collision must recover, not fail")
		t.Cleanup(func() { cleanupProvisionedOrg(t, pool, orgID) })
		return orgID
	}

	firstOrg := mk("first.example.com")
	secondOrg := mk("second.example.com")

	require.NotEqual(t, firstOrg, secondOrg, "colliding slugs must produce distinct orgs")

	var slugs []string
	rows, err := pool.Query(ctx,
		`SELECT slug FROM organizations WHERE id = ANY($1)`,
		[]uuid.UUID{firstOrg, secondOrg})
	require.NoError(t, err)
	defer rows.Close()
	for rows.Next() {
		var s string
		require.NoError(t, rows.Scan(&s))
		slugs = append(slugs, s)
	}
	require.NoError(t, rows.Err())
	require.Len(t, slugs, 2)
	require.NotEqual(t, slugs[0], slugs[1], "the second org must have gotten a suffixed slug")
}

func TestProvisioningIsolation_OrgAndMembershipCommitTogether(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()
	provisioner := auth.NewUserProvisioner(pool)

	user, created, err := provisioner.ProvisionOAuthUser(ctx, "github",
		fmt.Sprintf("atomic-%s@example.com", uuid.NewString()[:8]), "Atomic User", uuid.NewString())
	require.NoError(t, err)
	require.True(t, created)
	t.Cleanup(func() { cleanupProvisionedUser(t, pool, user.ID) })

	orgID, err := provisioner.CreateOrganizationForUser(ctx, user.ID,
		"Atomic Org", "atomic-org-"+uuid.NewString()[:8])
	require.NoError(t, err)
	t.Cleanup(func() { cleanupProvisionedOrg(t, pool, orgID) })

	// The org and the owner membership must both exist — a prior version
	// ran them as two independent statements, so a membership failure
	// left an orphaned, unreachable organization.
	var memberships int
	require.NoError(t, pool.QueryRow(ctx,
		`SELECT COUNT(*) FROM organization_memberships
		 WHERE organization_id = $1 AND user_id = $2 AND role = 'owner'`,
		orgID, user.ID).Scan(&memberships))
	require.Equal(t, 1, memberships, "org creation must commit the owner membership atomically")
}

// ---- helpers ----

func cleanupProvisionedUser(t *testing.T, pool *pgxpool.Pool, userID uuid.UUID) {
	t.Helper()
	ctx := context.Background()
	if _, err := pool.Exec(ctx, `DELETE FROM organization_memberships WHERE user_id = $1`, userID); err != nil {
		t.Logf("cleanup memberships for user %s: %v", userID, err)
	}
	if _, err := pool.Exec(ctx, `DELETE FROM users WHERE id = $1`, userID); err != nil {
		t.Logf("cleanup user %s: %v", userID, err)
	}
}

func cleanupProvisionedOrg(t *testing.T, pool *pgxpool.Pool, orgID uuid.UUID) {
	t.Helper()
	ctx := context.Background()
	if _, err := pool.Exec(ctx, `DELETE FROM organization_memberships WHERE organization_id = $1`, orgID); err != nil {
		t.Logf("cleanup memberships for org %s: %v", orgID, err)
	}
	if _, err := pool.Exec(ctx, `DELETE FROM organizations WHERE id = $1`, orgID); err != nil {
		t.Logf("cleanup org %s: %v", orgID, err)
	}
}

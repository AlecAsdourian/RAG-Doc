package auth

import (
	"context"
	"testing"

	"github.com/google/uuid"
	"github.com/stretchr/testify/require"
)

// TestCreateOrganizationForUser_CreatesADefaultProject exercises the
// PRODUCTION path.
//
// This exists because the first version of 20-02's invariant test ran
// against `WithTwoOrgs`, whose fixture sets `is_default = true` as a
// literal — so it asserted the fixture's own constant. Deleting the
// default-project INSERT from CreateOrganizationForUser left the entire
// suite green.
//
// `DefaultProjectID` is how 20-03 resolves which project a new repository
// belongs to, and `repositories.project_id` is NOT NULL, so an
// organization without a default project is one whose owner cannot
// connect a repository at all.
func TestCreateOrganizationForUser_CreatesADefaultProject(t *testing.T) {
	db := SetupTestDB(t)
	defer CleanupTestDB(t, db)

	ctx := context.Background()
	p := NewUserProvisioner(db)

	userID := CreateTestUser(t, db, "defaultproj-"+uuid.NewString()[:8]+"@example.com", "Default Proj")
	slug := "defaultproj-" + uuid.NewString()[:8]

	orgID, err := p.CreateOrganizationForUser(ctx, userID, "Default Proj Org", slug)
	require.NoError(t, err)

	t.Run("exactly one default project exists", func(t *testing.T) {
		var count int
		require.NoError(t, db.QueryRow(ctx,
			`SELECT count(*) FROM projects WHERE organization_id = $1 AND is_default`,
			orgID).Scan(&count))
		require.Equal(t, 1, count,
			"CreateOrganizationForUser must leave exactly one default project")
	})

	t.Run("DefaultProjectID finds it", func(t *testing.T) {
		projectID, derr := p.DefaultProjectID(ctx, orgID)
		require.NoError(t, derr,
			"20-03 resolves a repository's project through this; it must not error "+
				"for a normally-provisioned organization")
		require.NotEqual(t, uuid.Nil, projectID)

		var isDefault bool
		var ownerOrg uuid.UUID
		require.NoError(t, db.QueryRow(ctx,
			`SELECT is_default, organization_id FROM projects WHERE id = $1`,
			projectID).Scan(&isDefault, &ownerOrg))
		require.True(t, isDefault)
		require.Equal(t, orgID, ownerOrg,
			"the default project must belong to the organization it was asked about")
	})

	t.Run("a repository can actually be created under it", func(t *testing.T) {
		// The point of the whole invariant: project_id is NOT NULL.
		projectID, derr := p.DefaultProjectID(ctx, orgID)
		require.NoError(t, derr)

		repoID := CreateTestRepository(t, db, projectID, "connected-repo",
			"https://example.test/connected.git")
		require.NotEqual(t, uuid.Nil, repoID)
	})
}

// TestDefaultProjectID_ReportsAMissingDefaultRatherThanZero.
//
// Every organization is supposed to have one — from CreateOrganizationForUser
// for new ones and migration 000010's backfill for existing ones. A
// missing default is therefore a data-integrity problem, and returning
// uuid.Nil with a nil error would push it downstream into an FK violation
// on an unrelated insert.
func TestDefaultProjectID_ReportsAMissingDefaultRatherThanZero(t *testing.T) {
	db := SetupTestDB(t)
	defer CleanupTestDB(t, db)

	ctx := context.Background()

	// An organization created WITHOUT going through provisioning, which
	// is the only way to reach this state.
	orgID := CreateTestOrg(t, db, "No Default Org", "nodefault-"+uuid.NewString()[:8])

	projectID, err := NewUserProvisioner(db).DefaultProjectID(ctx, orgID)
	require.Error(t, err, "a missing default project must be reported, not returned as nil")
	require.Equal(t, uuid.Nil, projectID)
	require.Contains(t, err.Error(), "no default project")
}

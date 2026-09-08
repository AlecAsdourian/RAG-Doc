package auth

import (
	"context"
	"net/http"
	"testing"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// ListOwnerOrgAssignments is the query behind cmd/backfill-org-claims,
// which is the ONLY thing that repairs a user whose org-context push
// failed. Until these tests existed it had no automated coverage at all —
// the entire remediation for the 19-03 blocker rested on a function
// verified once, by hand.
//
// The scenario each test builds is the real failure: provision a user
// through the actual webhook with an admin client that fails, so the user
// ends up in our database with an organization and in Supabase without a
// claim. That is precisely the state the backfill has to find.

// TestListOwnerOrgAssignments_FindsUserWhoseOrgPushFailed is the core
// case. If this regresses, a stranded user is unrecoverable and nothing
// else in the suite notices.
func TestListOwnerOrgAssignments_FindsUserWhoseOrgPushFailed(t *testing.T) {
	db := SetupTestDB(t)
	defer CleanupTestDB(t, db)

	// An admin client that fails every push — the incident this repairs.
	failing := &fakeAdmin{failWith: assert.AnError}
	handler := NewWebhookHandler(db, "test-secret-key", failing)

	supabaseID := uuid.NewString()
	rr := deliverUserCreated(t, handler, supabaseID, "stranded@example.com")

	// The webhook still reports success: the push is non-fatal by design,
	// which is exactly why the user can be stranded silently.
	require.Equal(t, http.StatusAccepted, rr.Code,
		"a failed push must not fail the webhook — that is what makes this silent")
	require.Len(t, failing.calls, 1, "the push must have been attempted and failed")

	assignments, err := NewUserProvisioner(db).ListOwnerOrgAssignments(context.Background())
	require.NoError(t, err)

	var found *OwnerOrgAssignment
	for i := range assignments {
		if assignments[i].SupabaseUserID.String() == supabaseID {
			found = &assignments[i]
			break
		}
	}

	require.NotNil(t, found,
		"the stranded user must be discoverable, or backfill-org-claims cannot repair them")
	assert.Equal(t, "stranded@example.com", found.Email)
	assert.NotEqual(t, uuid.Nil, found.OrganizationID,
		"an assignment without an organization id would push a meaningless claim")
	assert.NotEqual(t, uuid.Nil, found.UserID)
}

// TestListOwnerOrgAssignments_ReturnsOneRowPerUser guards the DISTINCT ON.
//
// A user who somehow owns two organizations must yield exactly one
// assignment, and it must be the oldest — the same organization
// UserOwnerOrgID resolves for the webhook. If the two disagreed, a
// backfill run would overwrite the claim the webhook had just written,
// silently moving the user between tenants.
func TestListOwnerOrgAssignments_ReturnsOneRowPerUser(t *testing.T) {
	db := SetupTestDB(t)
	defer CleanupTestDB(t, db)

	userID := CreateTestUser(t, db, "twoorgs@example.com", "Two Orgs")

	firstOrg := CreateTestOrg(t, db, "First Org", "first-org-"+uuid.NewString()[:8])
	AddUserToOrg(t, db, userID, firstOrg, "owner")
	secondOrg := CreateTestOrg(t, db, "Second Org", "second-org-"+uuid.NewString()[:8])
	AddUserToOrg(t, db, userID, secondOrg, "owner")

	assignments, err := NewUserProvisioner(db).ListOwnerOrgAssignments(context.Background())
	require.NoError(t, err)

	var mine []OwnerOrgAssignment
	for _, a := range assignments {
		if a.UserID == userID {
			mine = append(mine, a)
		}
	}

	require.Len(t, mine, 1, "DISTINCT ON must collapse multiple owner memberships to one row")

	// Must agree with what the webhook would choose for the same user.
	expected, ok, err := NewUserProvisioner(db).UserOwnerOrgID(context.Background(), userID)
	require.NoError(t, err)
	require.True(t, ok)
	assert.Equal(t, expected, mine[0].OrganizationID,
		"backfill and the webhook must resolve the SAME organization, or a backfill "+
			"run would move the user between tenants")
}

// TestListOwnerOrgAssignments_ExcludesNonOwners keeps the backfill from
// stamping "owner" onto someone who is only a member.
//
// The push writes organization_role: "owner" unconditionally, so a
// non-owner appearing here would be silently promoted in their JWT — a
// privilege escalation delivered by the repair tool.
func TestListOwnerOrgAssignments_ExcludesNonOwners(t *testing.T) {
	db := SetupTestDB(t)
	defer CleanupTestDB(t, db)

	memberID := CreateTestUser(t, db, "member-only@example.com", "Member Only")
	org := CreateTestOrg(t, db, "Someone Elses Org", "someone-else-"+uuid.NewString()[:8])
	AddUserToOrg(t, db, memberID, org, "member")

	assignments, err := NewUserProvisioner(db).ListOwnerOrgAssignments(context.Background())
	require.NoError(t, err)

	for _, a := range assignments {
		assert.NotEqual(t, memberID, a.UserID,
			"a non-owner must never be returned — the push would stamp organization_role=owner "+
				"onto their token")
	}
}

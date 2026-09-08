package handlers_test

// TestUserOrgsIsolation exercises GET /api/user/organizations and
// POST /api/user/select-organization through the real router.
//
// These endpoints are the multi-org story's authorization boundary. The
// switch endpoint's membership check is the ONLY thing standing between an
// authenticated user and any organization they care to name — 19-03
// removed the header a caller could use to redirect their own tenant, and
// this endpoint would hand it straight back if it trusted the request
// body. Scenario 1 is therefore the most important test in this file.
//
// Note on identity: these tests sign tokens with the SUPABASE user ids
// (`org.OwnerSupabaseID`), not the internal `users.id`. Both are real, but
// only the Supabase id appears in a production token's `sub` claim, and
// these handlers resolve `sub` back to a user row. Signing with the
// internal id — which is what every earlier isolation test does, harmlessly,
// because nothing read `sub` until now — makes every query here return zero
// rows.

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/yourusername/smart-docs-platform/services/backend/pkg/api"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/api/handlers"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/client"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/testing/isolation"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/testing/isolation/testjwt"
)

// recordingAdmin captures what would be written to Supabase.
type recordingAdmin struct {
	mu    sync.Mutex
	calls []recordedPush
}

type recordedPush struct {
	userID string
	meta   map[string]any
}

func (a *recordingAdmin) UpdateUserAppMetadata(_ context.Context, userID string, meta map[string]any) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	copied := make(map[string]any, len(meta))
	for k, v := range meta {
		copied[k] = v
	}
	a.calls = append(a.calls, recordedPush{userID: userID, meta: copied})
	return nil
}

func (a *recordingAdmin) snapshot() []recordedPush {
	a.mu.Lock()
	defer a.mu.Unlock()
	out := make([]recordedPush, len(a.calls))
	copy(out, a.calls)
	return out
}

func TestUserOrgsIsolation(t *testing.T) {
	pool := isolation.SetupTestDB(t)

	isolation.WithTwoOrgs(t, pool, func(orgA, orgB *isolation.TestOrg) {
		admin := &recordingAdmin{}

		// The RAG client is unused by these endpoints but the router needs
		// one; point it at a server that fails loudly, so a test that
		// accidentally reaches the search path is obvious rather than
		// silently green.
		deadRAG := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			t.Errorf("user-org endpoints must never call the RAG service; got %s", r.URL.Path)
			http.Error(w, "unexpected", http.StatusInternalServerError)
		}))
		t.Cleanup(deadRAG.Close)

		router := api.NewRouterWithValidatorAndAdmin(
			pool, client.NewRAGClient(deadRAG.URL), testjwt.NewValidator(), admin,
			api.Config{LogLevel: slog.LevelWarn},
		)
		server := httptest.NewServer(router)
		t.Cleanup(server.Close)

		ownerAToken := testjwt.Sign(orgA.OwnerSupabaseID, orgA.ID, "owner")
		ownerBToken := testjwt.Sign(orgB.OwnerSupabaseID, orgB.ID, "owner")

		t.Run("Scenario1_CannotSelectAnOrgYouDoNotBelongTo", func(t *testing.T) {
			// The authorization test. orgA's owner names orgB — a real
			// organization, just not theirs.
			before := len(admin.snapshot())

			status, body := doSelectOrg(t, server.URL, ownerAToken, orgB.ID)
			require.Equal(t, http.StatusForbidden, status,
				"a user must not be able to join an organization by naming it; body=%s", body)

			require.Len(t, admin.snapshot(), before,
				"Supabase must not be touched on a denied switch — a write here would "+
					"put the caller into another tenant on their next token refresh")
		})

		t.Run("Scenario2_CanSwitchBetweenOrgsYouBelongTo", func(t *testing.T) {
			// Make orgA's owner a genuine member of orgB, with a LOWER role
			// than they hold in orgA. That difference is the point: the
			// claim written must describe orgB, not carry over "owner".
			isolation.AddMembership(t, pool, orgA.OwnerID, orgB.ID, "member")

			before := len(admin.snapshot())
			status, body := doSelectOrg(t, server.URL, ownerAToken, orgB.ID)
			require.Equal(t, http.StatusAccepted, status, "body=%s", body)

			calls := admin.snapshot()
			require.Len(t, calls, before+1, "exactly one Supabase write per accepted switch")
			got := calls[len(calls)-1]

			require.Equal(t, orgA.OwnerSupabaseID, got.userID,
				"the claim must be written onto the CALLER's Supabase user")
			require.Equal(t, orgB.ID, got.meta["organization_id"])

			// The regression guard for the escalation this plan originally
			// contained. Supabase MERGES app_metadata, so writing only
			// organization_id would leave organization_role at "owner" —
			// the role this user holds in orgA — while their active org is
			// orgB, where they are only a member. Verified against the live
			// project on 2026-09-08.
			require.Equal(t, "member", got.meta["organization_role"],
				"the role must come from the TARGET org's membership; carrying over the "+
					"caller's role in another org is a privilege escalation")

			// And the response must tell the client its current token is stale.
			require.Contains(t, body, "refreshSession",
				"the caller's existing token still carries the old org; the response must say so")
		})

		t.Run("Scenario3_ListReturnsOnlyYourOwnMemberships", func(t *testing.T) {
			// orgB's owner belongs to orgB alone. orgA must not appear.
			resp := doListOrgs(t, server.URL, ownerBToken)

			require.Len(t, resp.Organizations, 1,
				"orgB's owner belongs to exactly one organization")
			require.Equal(t, orgB.ID, resp.Organizations[0].ID)
			require.Equal(t, "owner", resp.Organizations[0].Role)
			require.True(t, resp.Organizations[0].IsActive,
				"the org named in the caller's token claim must be flagged active")

			for _, o := range resp.Organizations {
				require.NotEqual(t, orgA.ID, o.ID, "cross-tenant leak: orgA appeared for orgB's owner")
			}
			require.NotNil(t, resp.ActiveOrganizationID)
			require.Equal(t, orgB.ID, *resp.ActiveOrganizationID)
		})

		t.Run("Scenario4_UserWithNoOrgClaimCanStillList", func(t *testing.T) {
			// A token with no organization claim — the state a user is in
			// between signup and a successful org-context push, and the
			// state they stay in permanently if that push failed.
			//
			// These endpoints are the way OUT of that state, so they must
			// not be gated on being in it. If this returns 403, the routes
			// have drifted behind TenantMiddleware.
			claimless := testjwt.SignWithoutOrg(orgA.OwnerSupabaseID)

			resp := doListOrgs(t, server.URL, claimless)
			require.Nil(t, resp.ActiveOrganizationID,
				"no claim means no active org, reported explicitly rather than guessed")
			require.NotEmpty(t, resp.Organizations,
				"the user's memberships exist in our database regardless of what their token claims")

			// And they can select their way back into a valid state.
			status, body := doSelectOrg(t, server.URL, claimless, orgA.ID)
			require.Equal(t, http.StatusAccepted, status,
				"a claim-less user must be able to select an org they belong to; body=%s", body)
		})

		t.Run("Scenario5_ConcurrentListsDoNotLeakAcrossCallers", func(t *testing.T) {
			// Both owners list at once against the same pool and router.
			// Each response must contain only its own caller's orgs — a
			// shared-state bug (a cached tenant, a reused connection with
			// leftover GUCs) would show up here and nowhere else.
			const rounds = 8
			var wg sync.WaitGroup
			errs := make(chan error, rounds*2)

			for i := 0; i < rounds; i++ {
				wg.Add(1)
				go func() {
					defer wg.Done()
					resp := doListOrgs(t, server.URL, ownerBToken)
					for _, o := range resp.Organizations {
						if o.ID == orgA.ID {
							errs <- fmt.Errorf("orgB's owner saw orgA under concurrency")
						}
					}
				}()
				wg.Add(1)
				go func() {
					defer wg.Done()
					resp := doListOrgs(t, server.URL, ownerAToken)
					// orgA's owner is a member of orgB by now (scenario 2),
					// so both are legitimate here. What must never appear
					// is an org they belong to neither of — there isn't
					// one in this fixture, so assert the bound instead.
					if len(resp.Organizations) > 2 {
						errs <- fmt.Errorf("orgA's owner saw %d orgs, expected at most 2",
							len(resp.Organizations))
					}
				}()
			}
			wg.Wait()
			close(errs)
			for err := range errs {
				t.Error(err)
			}
		})

		t.Run("Scenario6_MalformedTargetIsRefusedNotCrashed", func(t *testing.T) {
			// A non-UUID target must be a clean 4xx. It reaches a uuid
			// comparison in Postgres, and an unvalidated value there is a
			// driver error surfaced as 500 — which reads as "our bug"
			// rather than "your request".
			for _, target := range []string{
				"not-a-uuid",
				"' OR '1'='1",
				"",
			} {
				status, body := doSelectOrg(t, server.URL, ownerAToken, target)
				require.Truef(t, status == http.StatusBadRequest || status == http.StatusForbidden,
					"target %q should be refused with 400 or 403, got %d; body=%s", target, status, body)
			}
		})
	})
}

func doListOrgs(t *testing.T, baseURL, token string) handlers.OrgListResponse {
	t.Helper()
	req, err := http.NewRequest(http.MethodGet, baseURL+"/api/user/organizations", nil)
	require.NoError(t, err)
	req.Header.Set("Authorization", "Bearer "+token)

	resp, err := http.DefaultClient.Do(req)
	require.NoError(t, err)
	defer resp.Body.Close()

	raw, err := io.ReadAll(resp.Body)
	require.NoError(t, err)
	require.Equal(t, http.StatusOK, resp.StatusCode, "body=%s", raw)

	var out handlers.OrgListResponse
	require.NoError(t, json.Unmarshal(raw, &out), "body=%s", raw)
	return out
}

func doSelectOrg(t *testing.T, baseURL, token, orgID string) (int, string) {
	t.Helper()
	body := fmt.Sprintf(`{"organization_id":%q}`, orgID)
	req, err := http.NewRequest(http.MethodPost,
		baseURL+"/api/user/select-organization", strings.NewReader(body))
	require.NoError(t, err)
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Content-Type", "application/json")

	resp, err := http.DefaultClient.Do(req)
	require.NoError(t, err)
	defer resp.Body.Close()

	raw, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, string(raw)
}

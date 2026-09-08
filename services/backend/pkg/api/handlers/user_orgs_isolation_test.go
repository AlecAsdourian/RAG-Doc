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
			// per-request-state leak (a cached tenant, a connection reused
			// with leftover GUCs) shows up here and nowhere else, because
			// every other scenario runs one request at a time.
			//
			// Three things about the mechanics below are deliberate, and
			// the first is not optional:
			//
			//  1. Failures are collected under a mutex, never sent to a
			//     channel. An earlier version used a buffered channel sized
			//     to the goroutine count and sent one error PER LEAKED ROW.
			//     A real leak produces more rows than goroutines, so the
			//     senders blocked forever, `wg.Done` never ran, `wg.Wait`
			//     never returned, and the package died on the 90s test
			//     timeout — taking scenario 6 with it and skipping
			//     t.Cleanup, which leaked fixture rows into the reused
			//     container. A test that HANGS on the bug it exists to
			//     catch is worse than no test: a timeout in CI reads as
			//     infrastructure flake and gets re-run.
			//
			//  2. No require/assert inside the goroutines. testify's
			//     require calls t.FailNow, which is runtime.Goexit, which
			//     is documented misuse outside the test goroutine — it
			//     would abandon the WaitGroup rather than fail cleanly.
			//
			//  3. A start barrier, so the requests actually overlap.
			//     Without it the goroutines trickle out as they are
			//     scheduled and may never be in flight together, which is
			//     the only condition under which this scenario can observe
			//     anything scenario 3 doesn't.
			const rounds = 8

			var (
				mu       sync.Mutex
				failures []string
			)
			fail := func(format string, args ...any) {
				mu.Lock()
				defer mu.Unlock()
				failures = append(failures, fmt.Sprintf(format, args...))
			}

			start := make(chan struct{})
			var wg sync.WaitGroup

			check := func(token, callerLabel string, allowed map[string]bool) {
				defer wg.Done()
				<-start

				resp, err := listOrgsNoFail(server.URL, token)
				if err != nil {
					fail("%s: %v", callerLabel, err)
					return
				}
				for _, o := range resp.Organizations {
					if !allowed[o.ID] {
						fail("%s saw organization %s under concurrency — cross-caller leak",
							callerLabel, o.ID)
						return // one report per goroutine is enough
					}
				}
			}

			// orgA's owner was added to orgB in scenario 2, so both are
			// legitimate for them. orgB's owner belongs to orgB alone —
			// that asymmetry is what makes their side load-bearing.
			allowedForA := map[string]bool{orgA.ID: true, orgB.ID: true}
			allowedForB := map[string]bool{orgB.ID: true}

			for i := 0; i < rounds; i++ {
				wg.Add(2)
				go check(ownerAToken, "orgA's owner", allowedForA)
				go check(ownerBToken, "orgB's owner", allowedForB)
			}

			close(start) // release them together
			wg.Wait()

			for _, f := range failures {
				t.Error(f)
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

				// The rejection must not describe our internals. The raw
				// validator error reads "Key: 'SelectOrgRequest.
				// OrganizationID' Error:Field validation for
				// 'OrganizationID' failed on the 'uuid' tag" — Go struct
				// and tag names, useless to the caller and informative to
				// everyone else.
				require.NotContainsf(t, body, "SelectOrgRequest",
					"error body for %q leaks Go struct internals: %s", target, body)
			}
		})

		t.Run("Scenario7_MalformedSubjectClaimIsRejectedAtTheTokenBoundary", func(t *testing.T) {
			// The other identifier that reaches a uuid column: the `sub`
			// claim, which both handlers compare against
			// users.supabase_user_id.
			//
			// This was a real 500 before review caught it. A comment in
			// callerRoleIn claimed both `sub` and the target org were
			// parsed; only the target was, and the target was already
			// covered by the struct validator — so the guard was on the
			// input that didn't need it while the one that did went
			// straight to the driver.
			//
			// Rejection belongs at the token boundary (auth.ExtractUserID),
			// not in a handler, so every route inherits it. That makes this
			// a 401: the token is malformed, not the request.
			for _, sub := range []string{
				"not-a-uuid",
				"'; SELECT 1; --",
				"12345",
			} {
				t.Run(sub, func(t *testing.T) {
					bad := testjwt.Sign(sub, orgA.ID, "owner")

					req, err := http.NewRequest(http.MethodGet,
						server.URL+"/api/user/organizations", nil)
					require.NoError(t, err)
					req.Header.Set("Authorization", "Bearer "+bad)
					resp, err := http.DefaultClient.Do(req)
					require.NoError(t, err)
					defer resp.Body.Close()
					raw, _ := io.ReadAll(resp.Body)

					require.Equal(t, http.StatusUnauthorized, resp.StatusCode,
						"a non-UUID subject must be refused at the token boundary, "+
							"not passed to a uuid column and returned as 500; body=%s", raw)

					status, body := doSelectOrg(t, server.URL, bad, orgA.ID)
					require.Equal(t, http.StatusUnauthorized, status,
						"same for the mutating endpoint; body=%s", body)
				})
			}
		})

		t.Run("Scenario9_NonCanonicalSubjectResolvesToTheSameIdentity", func(t *testing.T) {
			// The narrow door the first version of the `sub` guard left
			// open. `uuid.Parse` is more permissive than Postgres: it
			// accepts `urn:uuid:...`, which Postgres's uuid type rejects,
			// plus braced and unhyphenated forms it accepts. Validating
			// without canonicalizing meant the value that reached the
			// database was still whatever the token said — so the guard
			// narrowed the 500 rather than closing it, and four textually
			// different subjects mapped to one identity by accident rather
			// than by design.
			//
			// ExtractUserID now returns the canonical form. Each variant
			// below must produce exactly the same response as the plain
			// one: accepted, and resolved to the same user.
			canonical := doListOrgs(t, server.URL, ownerAToken)
			require.NotEmpty(t, canonical.Organizations,
				"precondition: the canonical form must resolve to memberships")

			plain := orgA.OwnerSupabaseID
			for name, variant := range map[string]string{
				"urn":        "urn:uuid:" + plain,
				"braced":     "{" + plain + "}",
				"uppercase":  strings.ToUpper(plain),
				"unhyphened": strings.ReplaceAll(plain, "-", ""),
			} {
				t.Run(name, func(t *testing.T) {
					got := doListOrgs(t, server.URL, testjwt.Sign(variant, orgA.ID, "owner"))
					require.Len(t, got.Organizations, len(canonical.Organizations),
						"%s form of the subject must resolve to the same identity", name)
					for i := range got.Organizations {
						require.Equal(t, canonical.Organizations[i].ID, got.Organizations[i].ID)
					}
				})
			}
		})

		t.Run("Scenario8_OversizedBodyIsRefusedWithoutAllocating", func(t *testing.T) {
			// A valid body here is ~55 bytes. Without a limit the decoder
			// materializes whatever an authenticated caller sends before
			// the validator rejects it — measured at roughly 7x the wire
			// size in allocations, repeatable at will.
			lenBeforeOversized := len(admin.snapshot())
			huge := fmt.Sprintf(`{"organization_id":%q}`, strings.Repeat("A", 2<<20))
			req, err := http.NewRequest(http.MethodPost,
				server.URL+"/api/user/select-organization", strings.NewReader(huge))
			require.NoError(t, err)
			req.Header.Set("Authorization", "Bearer "+ownerAToken)
			req.Header.Set("Content-Type", "application/json")

			resp, err := http.DefaultClient.Do(req)
			require.NoError(t, err)
			defer resp.Body.Close()
			raw, _ := io.ReadAll(resp.Body)

			require.Equal(t, http.StatusBadRequest, resp.StatusCode,
				"an oversized body must be refused; body=%s", raw)
			require.Len(t, admin.snapshot(), lenBeforeOversized,
				"no Supabase write may result from a rejected body")

			// Assert on the REASON, not just the status. Without the limit
			// the oversized string decodes successfully and the validator
			// rejects it for not being a UUID — also a 400. Checking only
			// the status made this test pass with the guard removed, which
			// is exactly the vacuous-test failure this suite is supposed to
			// have stopped making.
			require.Contains(t, string(raw), "too large",
				"the body must be refused by the size limit before it is decoded, not by "+
					"the validator after the whole thing has been materialized")
		})
	})
}

// listOrgsNoFail is doListOrgs for use from a goroutine: it returns errors
// instead of calling t.FailNow, which is runtime.Goexit and must not be
// invoked outside the test goroutine.
func listOrgsNoFail(baseURL, token string) (handlers.OrgListResponse, error) {
	var out handlers.OrgListResponse

	req, err := http.NewRequest(http.MethodGet, baseURL+"/api/user/organizations", nil)
	if err != nil {
		return out, err
	}
	req.Header.Set("Authorization", "Bearer "+token)

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return out, err
	}
	defer resp.Body.Close()

	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		return out, err
	}
	if resp.StatusCode != http.StatusOK {
		return out, fmt.Errorf("status %d: %s", resp.StatusCode, raw)
	}
	if err := json.Unmarshal(raw, &out); err != nil {
		return out, fmt.Errorf("decode %s: %w", raw, err)
	}
	return out, nil
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

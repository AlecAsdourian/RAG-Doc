package handlers_test

// Isolation and authorization tests for the GitHub App installation flow.
//
// The flow's security rests on one claim: an installation can only be
// linked by someone who started the flow, and only to the organization
// they started it from. Every scenario here attacks that claim from a
// different side — a forged state, a replayed state, a missing state, an
// installation GitHub does not know, and one another tenant already owns.
//
// The GitHub client is stubbed and the assertions are on the RESULTING
// ROW and on whether the client was called at all, not only on status
// codes. Scenario 6 is the clearest case for why: refusing a cross-tenant
// installation AFTER calling GitHub still spends our credentials and our
// rate-limit budget, and still leaks existence through timing, so the
// status code alone cannot tell a safe implementation from an unsafe one.

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/stretchr/testify/require"

	"github.com/yourusername/smart-docs-platform/services/backend/pkg/api"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/api/handlers"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/auth"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/client"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/db"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/github"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/testing/isolation"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/testing/isolation/testjwt"
)

// safeBuffer collects log output from the server's goroutines.
type safeBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *safeBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *safeBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

// memoryStates is an in-memory InstallStateStore.
//
// Real Redis is not needed to prove the ORDER of the callback's checks,
// and requiring it would mean these tests only run where Redis does.
// ConsumeState is atomic here for the same reason it is in Redis.
type memoryStates struct {
	mu     sync.Mutex
	values map[string]string
}

func newMemoryStates() *memoryStates {
	return &memoryStates{values: map[string]string{}}
}

func (m *memoryStates) StoreStateValue(_ context.Context, state, value string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.values[state] = value
	return nil
}

func (m *memoryStates) ConsumeState(_ context.Context, state string) (string, bool, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	v, ok := m.values[state]
	if !ok {
		return "", false, nil
	}
	delete(m.values, state)
	return v, true, nil
}

// stubInstallClient records whether GitHub was reached.
type stubInstallClient struct {
	mu sync.Mutex

	installation *github.Installation
	getErr       error
	// userAuthOff simulates an App with no client credentials.
	userAuthOff bool
	// controlsErr is what VerifyUserControlsInstallation returns; nil
	// means the caller demonstrably controls the installation.
	controlsErr error
	verifyCalls int
	lastCode    string
	repos       []github.Repository
	hasNext     bool
	listErr     error

	getCalls  int
	listCalls int
}

func (s *stubInstallClient) GetInstallation(
	_ context.Context, id int64,
) (*github.Installation, error) {
	s.mu.Lock()
	s.getCalls++
	s.mu.Unlock()
	if s.getErr != nil {
		return nil, s.getErr
	}
	inst := *s.installation
	inst.ID = id
	return &inst, nil
}

func (s *stubInstallClient) UserAuthConfigured() bool { return !s.userAuthOff }

func (s *stubInstallClient) VerifyUserControlsInstallation(
	_ context.Context, code string, _ int64,
) error {
	s.mu.Lock()
	s.verifyCalls++
	s.lastCode = code
	s.mu.Unlock()
	return s.controlsErr
}

func (s *stubInstallClient) ListInstallationRepositoriesPage(
	_ context.Context, _ int64, _, _ int,
) ([]github.Repository, bool, error) {
	s.mu.Lock()
	s.listCalls++
	s.mu.Unlock()
	if s.listErr != nil {
		return nil, false, s.listErr
	}
	return s.repos, s.hasNext, nil
}

func (s *stubInstallClient) calls() (get, list int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.getCalls, s.listCalls
}

func (s *stubInstallClient) verifications() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.verifyCalls
}

func githubInstallation(login string) *github.Installation {
	inst := &github.Installation{
		RepositorySelection: "selected",
		AccountLogin:        login,
		AccountType:         "Organization",
	}
	inst.Account.Login = login
	inst.Account.Type = "Organization"
	return inst
}

// installServer stands up the real router with both dependencies stubbed.
func installServer(
	t *testing.T, pool *pgxpool.Pool, states handlers.InstallStateStore,
	gh handlers.GitHubInstallationClient,
) string {
	t.Helper()
	deadRAG := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Errorf("the install flow must never call the RAG service; got %s", r.URL.Path)
		http.Error(w, "unexpected", http.StatusInternalServerError)
	}))
	t.Cleanup(deadRAG.Close)

	router := api.NewRouterWithValidatorAndAdmin(
		pool, client.NewRAGClient(deadRAG.URL), testjwt.NewValidator(), nil,
		api.Config{
			LogLevel:            slog.LevelWarn,
			GitHubRepositories:  &stubLister{},
			GitHubInstallations: gh,
			InstallStates:       states,
			GitHubAppSlug:       "rag-doc-test",
			FrontendURL:         "https://app.example.test/settings",
		},
	)
	server := httptest.NewServer(router)
	t.Cleanup(server.Close)
	return server.URL
}

// noRedirect keeps the 302 rather than following it to github.com.
func noRedirectClient() *http.Client {
	return &http.Client{
		CheckRedirect: func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
}

func getNoRedirect(t *testing.T, url, token string) (int, string) {
	t.Helper()
	req, err := http.NewRequest(http.MethodGet, url, nil)
	require.NoError(t, err)
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := noRedirectClient().Do(req)
	require.NoError(t, err)
	defer resp.Body.Close()
	return resp.StatusCode, resp.Header.Get("Location")
}

// resultOf pulls github_result out of a callback's redirect target.
func resultOf(t *testing.T, location string) string {
	t.Helper()
	u, err := url.Parse(location)
	require.NoError(t, err, "callback must redirect somewhere parseable; got %q", location)
	return u.Query().Get("github_result")
}

// installationsOf lists (github_installation_id) visible to org.
func installationsOf(t *testing.T, pool *pgxpool.Pool, orgID string) []int64 {
	t.Helper()
	scoper := db.NewTenantScoper(pool)
	ctx := auth.ContextWithOrgID(context.Background(), orgID)
	var out []int64
	require.NoError(t, scoper.InTenantTx(ctx, func(tx pgx.Tx) error {
		rows, err := tx.Query(context.Background(),
			`SELECT github_installation_id FROM github_installations ORDER BY github_installation_id`)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var id int64
			if err := rows.Scan(&id); err != nil {
				return err
			}
			out = append(out, id)
		}
		return rows.Err()
	}))
	return out
}

func TestGitHubInstallFlow(t *testing.T) {
	pool := isolation.SetupTestDB(t)

	isolation.WithTwoOrgs(t, pool, func(orgA, orgB *isolation.TestOrg) {
		tokenA := testjwt.Sign(orgA.OwnerSupabaseID, orgA.ID, "owner")
		tokenB := testjwt.Sign(orgB.OwnerSupabaseID, orgB.ID, "owner")

		t.Run("Scenario1_InstallRedirectsToGitHubWithAState", func(t *testing.T) {
			states := newMemoryStates()
			url := installServer(t, pool, states, &stubInstallClient{})

			status, location := getNoRedirect(t, url+"/api/github/install", tokenA)
			require.Equal(t, http.StatusFound, status)
			require.True(t, strings.HasPrefix(location,
				"https://github.com/apps/rag-doc-test/installations/new?state="),
				"unexpected redirect target %q", location)

			// The organization is bound SERVER-SIDE. It must not appear in
			// the URL, where the user could edit it before following it.
			require.NotContains(t, location, orgA.ID,
				"the organization must travel in the state token, not the redirect URL")

			token := mustStateToken(t, location)
			raw, ok, err := states.ConsumeState(context.Background(), token)
			require.NoError(t, err)
			require.True(t, ok, "the state token must have been stored before redirecting")
			var payload struct {
				OrganizationID string `json:"organization_id"`
			}
			require.NoError(t, json.Unmarshal([]byte(raw), &payload))
			require.Equal(t, orgA.ID, payload.OrganizationID)
		})

		t.Run("Scenario2_InstallRequiresAnOrganizationClaim", func(t *testing.T) {
			url := installServer(t, pool, newMemoryStates(), &stubInstallClient{})
			claimless := testjwt.SignWithoutOrg(orgA.OwnerSupabaseID)

			status, _ := getNoRedirect(t, url+"/api/github/install", claimless)
			require.Equal(t, http.StatusForbidden, status)

			unauth, _ := getNoRedirect(t, url+"/api/github/install", "")
			require.Equal(t, http.StatusUnauthorized, unauth)
		})

		t.Run("Scenario3_CallbackIsReachableWithoutAJWT", func(t *testing.T) {
			// The regression guard for the mount point. GitHub's redirect
			// carries no Authorization header; if this route ever moves
			// back inside the authenticated group it returns 401 to every
			// real installation, and nothing else in the suite would say so.
			url := installServer(t, pool, newMemoryStates(), &stubInstallClient{})

			status, location := getNoRedirect(t, url+"/api/github/callback?installation_id=1", "")
			require.NotEqual(t, http.StatusUnauthorized, status,
				"the callback must not sit behind JWTAuthMiddleware")
			require.Equal(t, http.StatusFound, status)
			require.Equal(t, "missing_state", resultOf(t, location),
				"no state means refuse, never default to somebody's organization")
		})

		t.Run("Scenario4_ForgedAndReplayedStatesAreRefused", func(t *testing.T) {
			states := newMemoryStates()
			gh := &stubInstallClient{installation: githubInstallation("acme")}
			url := installServer(t, pool, states, gh)

			// Forged.
			_, location := getNoRedirect(t,
				url+"/api/github/callback?installation_id=90001&state=not-a-real-token", "")
			require.Equal(t, "invalid_state", resultOf(t, location))

			getCalls, _ := gh.calls()
			require.Zero(t, getCalls,
				"a bad state must be refused BEFORE GitHub is called")

			// Genuine, then replayed.
			_, redirect := getNoRedirect(t, url+"/api/github/install", tokenA)
			token := mustStateToken(t, redirect)

			_, first := getNoRedirect(t,
				fmt.Sprintf("%s/api/github/callback?installation_id=90002&state=%s", url, token), "")
			require.Equal(t, "connected", resultOf(t, first))

			_, second := getNoRedirect(t,
				fmt.Sprintf("%s/api/github/callback?installation_id=90003&state=%s", url, token), "")
			require.Equal(t, "invalid_state", resultOf(t, second),
				"a state token must work exactly once")

			// And the replay wrote nothing.
			require.NotContains(t, installationsOf(t, pool, orgA.ID), int64(90003),
				"a replayed state must not link a second installation")
		})

		t.Run("Scenario5_UnreachableInstallationWritesNothing", func(t *testing.T) {
			states := newMemoryStates()
			gh := &stubInstallClient{getErr: fmt.Errorf("github: 404 not found")}
			url := installServer(t, pool, states, gh)

			_, redirect := getNoRedirect(t, url+"/api/github/install", tokenA)
			token := mustStateToken(t, redirect)

			before := installationsOf(t, pool, orgA.ID)
			_, location := getNoRedirect(t,
				fmt.Sprintf("%s/api/github/callback?installation_id=91001&state=%s", url, token), "")
			require.Equal(t, "invalid_installation", resultOf(t, location))
			require.Equal(t, before, installationsOf(t, pool, orgA.ID),
				"an installation we cannot reach must not be persisted")
		})

		t.Run("Scenario6_AnotherTenantsInstallationIsRefusedBeforeGitHubIsCalled", func(t *testing.T) {
			// orgB owns the installation; orgA asks to list its repositories.
			instB := seedInstallation(t, pool, orgB.ID, 92001)

			gh := &stubInstallClient{repos: []github.Repository{{ID: 5, Name: "secret"}}}
			url := installServer(t, pool, newMemoryStates(), gh)

			status, _ := getNoRedirect(t,
				url+"/api/github/installations/"+instB+"/repositories", tokenA)
			require.Equal(t, http.StatusNotFound, status)

			_, listCalls := gh.calls()
			require.Zero(t, listCalls,
				"ownership must be proven BEFORE GitHub is called; refusing afterwards still "+
					"spends our credentials and leaks existence through timing")

			// And the owner can still read it, so the refusal above is
			// about tenancy rather than the endpoint being broken.
			ownerStatus, _ := getNoRedirect(t,
				url+"/api/github/installations/"+instB+"/repositories", tokenB)
			require.Equal(t, http.StatusOK, ownerStatus)
			_, afterOwner := gh.calls()
			require.Equal(t, 1, afterOwner)
		})

		t.Run("Scenario7_AnInstallationCannotBeLinkedToTwoOrganizations", func(t *testing.T) {
			const shared = int64(93001)
			states := newMemoryStates()
			gh := &stubInstallClient{installation: githubInstallation("shared-account")}
			url := installServer(t, pool, states, gh)

			// orgA links it.
			_, redirectA := getNoRedirect(t, url+"/api/github/install", tokenA)
			tokenAState := mustStateToken(t, redirectA)
			_, first := getNoRedirect(t,
				fmt.Sprintf("%s/api/github/callback?installation_id=%d&state=%s", url, shared, tokenAState), "")
			require.Equal(t, "connected", resultOf(t, first))

			// orgB tries to take it.
			_, redirectB := getNoRedirect(t, url+"/api/github/install", tokenB)
			tokenBState := mustStateToken(t, redirectB)
			_, second := getNoRedirect(t,
				fmt.Sprintf("%s/api/github/callback?installation_id=%d&state=%s", url, shared, tokenBState), "")

			require.Equal(t, "already_connected", resultOf(t, second),
				"a collision is a comprehensible situation, not a 500")

			// The message must not name the other tenant.
			loc, err := url2(second)
			require.NoError(t, err)
			message := loc.Query().Get("github_message")
			require.NotContains(t, message, orgA.ID)
			require.NotContains(t, message, orgA.Slug)

			// Assert on the ROWS: orgA keeps it, orgB never gets it.
			require.Contains(t, installationsOf(t, pool, orgA.ID), shared,
				"the original owner must keep the installation")
			require.NotContains(t, installationsOf(t, pool, orgB.ID), shared,
				"a losing claimant must not end up linked")
		})

		t.Run("Scenario10_ListingInstallationsIsTenantScopedAndClosesTheLoop", func(t *testing.T) {
			// The gap that made Task 3's endpoint unreachable: a UI needs
			// an installation id to ask what a repository picker should
			// show, and before this the only place one appeared was on an
			// existing repository — which a user who has just installed
			// does not have.
			const fresh = int64(96001)
			states := newMemoryStates()
			gh := &stubInstallClient{installation: githubInstallation("loop-account")}
			srv := installServer(t, pool, states, gh)

			_, redirect := getNoRedirect(t, srv+"/api/github/install", tokenA)
			_, location := getNoRedirect(t, fmt.Sprintf(
				"%s/api/github/callback?installation_id=%d&state=%s",
				srv, fresh, mustStateToken(t, redirect)), "")
			require.Equal(t, "connected", resultOf(t, location))

			// The success redirect hands the id straight back.
			loc, err := url.Parse(location)
			require.NoError(t, err)
			fromRedirect := loc.Query().Get("installation_id")
			require.NotEmpty(t, fromRedirect,
				"a just-installed user must not have to go looking for their own installation id")

			// And listing agrees with it, scoped to the caller.
			status, body := doRepoRequest(t, srv, http.MethodGet, "/api/github/installations", tokenA, "")
			require.Equal(t, http.StatusOK, status, "body=%s", body)
			var listed handlers.InstallationListResponse
			require.NoError(t, json.Unmarshal([]byte(body), &listed))

			ids := map[string]bool{}
			for _, i := range listed.Installations {
				ids[i.ID] = true
			}
			require.True(t, ids[fromRedirect],
				"the id from the redirect must appear in the caller's own list")

			// orgB sees none of orgA's, and the response never carries
			// GitHub's numeric id.
			statusB, bodyB := doRepoRequest(t, srv, http.MethodGet, "/api/github/installations", tokenB, "")
			require.Equal(t, http.StatusOK, statusB)
			require.NotContains(t, bodyB, fromRedirect,
				"cross-tenant leak: orgB listed orgA's installation")
			require.NotContains(t, body, "96001",
				"GitHub's numeric installation id must not be handed to clients")
		})

		t.Run("Scenario11_AnUnlinkedInstallationCannotBeClaimedByAStranger", func(t *testing.T) {
			// THE TAKEOVER. Found in review, and the reason STEP 3 exists.
			//
			// `GetInstallation` authenticates as the APP, so it succeeds
			// for every installation of our App and proves nothing about
			// who is asking. Installations sit unlinked routinely — GitHub's
			// own "Install App" button sends no state, so we refuse it and
			// leave the installation live — and before the user-authorization
			// check, any authenticated user could claim one by naming its
			// id, then read the owner's private repositories through
			// POST /api/repositories.
			const victimInstallation = int64(880042)
			states := newMemoryStates()

			// The stub answers exactly as GitHub does for the app-level
			// endpoint: this installation is real. Only the user-level
			// check can tell the attacker apart from the owner.
			gh := &stubInstallClient{
				installation: githubInstallation("victim-org"),
				controlsErr:  fmt.Errorf("github: the authorizing user does not have access"),
			}
			srv := installServer(t, pool, states, gh)

			// The attacker starts a legitimate flow of their OWN, so the
			// state token is genuine and bound to their organization.
			_, redirect := getNoRedirect(t, srv+"/api/github/install", tokenB)
			state := mustStateToken(t, redirect)

			_, location := getNoRedirect(t, fmt.Sprintf(
				"%s/api/github/callback?installation_id=%d&state=%s&code=stolen",
				srv, victimInstallation, state), "")

			require.Equal(t, "invalid_installation", resultOf(t, location),
				"a stranger must not be able to claim an unlinked installation")
			require.NotContains(t, installationsOf(t, pool, orgB.ID), victimInstallation,
				"TAKEOVER: the attacker's organization was linked to an installation it does not control")

			// And the app-level lookup must not even have run: the user
			// check gates it.
			getCalls, _ := gh.calls()
			require.Zero(t, getCalls,
				"the app-level lookup must come after the user check, not before")
			require.Equal(t, 1, gh.verifications())
		})

		t.Run("Scenario12_LinkingRefusesWhenUserAuthorizationCannotBeVerified", func(t *testing.T) {
			// Fail closed. An App without client credentials cannot prove
			// anything about the caller, and linking without that proof is
			// the vulnerability — so it refuses rather than falling back.
			states := newMemoryStates()
			gh := &stubInstallClient{
				installation: githubInstallation("unverifiable"),
				userAuthOff:  true,
			}
			srv := installServer(t, pool, states, gh)

			// It refuses at the front door too, rather than sending the
			// user to GitHub for an installation it could not finish.
			status, _ := getNoRedirect(t, srv+"/api/github/install", tokenA)
			require.Equal(t, http.StatusServiceUnavailable, status,
				"do not start a flow that cannot be completed")

			// And if a callback arrives anyway (an install begun before the
			// credentials were removed), it must not link.
			states.StoreStateValue(context.Background(), "handmade",
				fmt.Sprintf(`{"organization_id":%q}`, orgA.ID))
			_, location := getNoRedirect(t, fmt.Sprintf(
				"%s/api/github/callback?installation_id=881001&state=handmade&code=x", srv), "")
			require.Equal(t, "unavailable", resultOf(t, location))
			require.NotContains(t, installationsOf(t, pool, orgA.ID), int64(881001))
		})

		t.Run("Scenario13_UnconfiguredGitHubStillAnswers404ForAnotherTenant", func(t *testing.T) {
			// The 20-03 enumeration oracle, one endpoint further on.
			// Moving the `h.github == nil` check above the ownership check
			// survives every other scenario, because they all supply a
			// client — so the ordering only becomes observable with no
			// client at all: a real installation belonging to someone else
			// would answer 503 while a made-up id answered 404, and the
			// difference says which ids exist.
			instB := seedInstallation(t, pool, orgB.ID, 97001)
			srv := installServer(t, pool, newMemoryStates(), nil)

			mine, _ := getNoRedirect(t, srv+"/api/github/installations/"+instB+"/repositories", tokenA)
			fake, _ := getNoRedirect(t,
				srv+"/api/github/installations/99999999-9999-9999-9999-999999999999/repositories", tokenA)

			require.Equal(t, http.StatusNotFound, mine,
				"another tenant's installation must 404 before availability is considered")
			require.Equal(t, fake, mine,
				"'not yours' and 'does not exist' must be indistinguishable")

			// The owner gets the 503, because for them the question is
			// answerable and the service genuinely is not available.
			owner, _ := getNoRedirect(t, srv+"/api/github/installations/"+instB+"/repositories", tokenB)
			require.Equal(t, http.StatusServiceUnavailable, owner)
		})

		t.Run("Scenario14_SuspendedAndMismatchedInstallationsAreRefused", func(t *testing.T) {
			states := newMemoryStates()
			suspended := githubInstallation("suspended-account")
			at := time.Now().Add(-time.Hour)
			suspended.SuspendedAt = &at
			gh := &stubInstallClient{installation: suspended}
			srv := installServer(t, pool, states, gh)

			_, redirect := getNoRedirect(t, srv+"/api/github/install", tokenA)
			_, location := getNoRedirect(t, fmt.Sprintf(
				"%s/api/github/callback?installation_id=98001&state=%s&code=c",
				srv, mustStateToken(t, redirect)), "")

			require.Equal(t, "suspended", resultOf(t, location),
				"a suspended installation must not be linked")
			require.NotContains(t, installationsOf(t, pool, orgA.ID), int64(98001))
		})

		t.Run("Scenario15_CallbackCredentialsDoNotReachTheLog", func(t *testing.T) {
			// The `code` is the credential the whole takeover fix rests on,
			// and the `missing_state` path refuses BEFORE exchanging it —
			// so a victim's code stays valid for its full lifetime. Written
			// to a log, it is replayable by anyone who can read that log.
			//
			// Asserted on CAPTURED LOG OUTPUT rather than on the scrubbing
			// function, because the thing that must hold is "no logger
			// renders it", not "we blanked the field we think it reads".
			const secretCode = "SECRETOAUTHCODEABC123"
			const secretState = "SECRETSTATETOKENXYZ789"

			var captured safeBuffer
			router := api.NewRouterWithValidatorAndAdmin(
				pool, client.NewRAGClient("http://127.0.0.1:1"), testjwt.NewValidator(), nil,
				api.Config{
					LogLevel:            slog.LevelDebug,
					LogWriter:           &captured,
					GitHubRepositories:  &stubLister{},
					GitHubInstallations: &stubInstallClient{installation: githubInstallation("x")},
					InstallStates:       newMemoryStates(),
					GitHubAppSlug:       "rag-doc-test",
					FrontendURL:         "https://app.example.test/settings",
				},
			)
			logged := httptest.NewServer(router)
			t.Cleanup(logged.Close)

			getNoRedirect(t, fmt.Sprintf(
				"%s/api/github/callback?installation_id=99001&code=%s&state=%s&setup_action=install",
				logged.URL, secretCode, secretState), "")

			out := captured.String()
			require.NotEmpty(t, out, "nothing was logged; the assertion below would be vacuous")
			require.NotContains(t, out, secretCode,
				"the GitHub authorization code reached the log; it is replayable")
			require.NotContains(t, out, secretState,
				"the state token reached the log")
			require.Contains(t, out, "/api/github/callback",
				"the path itself should still be logged")
		})

		t.Run("Scenario16_AnEmptySlugRefusesInsteadOfRedirectingToNowhere", func(t *testing.T) {
			// Scenario 12 trips the credentials guard first, so the slug
			// branch was never exercised by it — review measured the check
			// as unpinned. Everything else here is configured, so only the
			// slug can refuse.
			deadRAG := httptest.NewServer(http.HandlerFunc(
				func(w http.ResponseWriter, r *http.Request) {}))
			t.Cleanup(deadRAG.Close)
			router := api.NewRouterWithValidatorAndAdmin(
				pool, client.NewRAGClient(deadRAG.URL), testjwt.NewValidator(), nil,
				api.Config{
					LogLevel:            slog.LevelWarn,
					GitHubRepositories:  &stubLister{},
					GitHubInstallations: &stubInstallClient{installation: githubInstallation("x")},
					InstallStates:       newMemoryStates(),
					GitHubAppSlug:       "", // the whole point
					FrontendURL:         "https://app.example.test/settings",
				},
			)
			srv := httptest.NewServer(router)
			t.Cleanup(srv.Close)

			status, location := getNoRedirect(t, srv.URL+"/api/github/install", tokenA)
			require.Equal(t, http.StatusServiceUnavailable, status,
				"an empty slug must refuse, not 302 to github.com/apps//installations/new")
			require.Empty(t, location)
		})

		t.Run("Scenario9_TheOrganizationComesFromTheTokenAndNothingElse", func(t *testing.T) {
			// The load-bearing property of the whole flow, and the one a
			// status code cannot show: the installation must land in the
			// organization the STATE TOKEN named, no matter what the
			// request says.
			//
			// The callback below carries orgB's id as a query parameter AND
			// a valid orgB bearer token, against a state token minted by
			// orgA. Every one of those is a plausible place for an
			// implementation to look, and each would land the installation
			// in the wrong tenant.
			const contested = int64(95001)
			states := newMemoryStates()
			gh := &stubInstallClient{installation: githubInstallation("contested-account")}
			srv := installServer(t, pool, states, gh)

			_, redirect := getNoRedirect(t, srv+"/api/github/install", tokenA)
			state := mustStateToken(t, redirect)

			callback := fmt.Sprintf(
				"%s/api/github/callback?installation_id=%d&state=%s&organization_id=%s&org=%s",
				srv, contested, state, orgB.ID, orgB.Slug)
			_, location := getNoRedirect(t, callback, tokenB)
			require.Equal(t, "connected", resultOf(t, location))

			require.Contains(t, installationsOf(t, pool, orgA.ID), contested,
				"the installation must land in the organization that STARTED the flow")
			require.NotContains(t, installationsOf(t, pool, orgB.ID), contested,
				"neither a query parameter nor a bearer token may redirect the link")
		})

		t.Run("Scenario8_ReconnectingYourOwnInstallationRefreshesIt", func(t *testing.T) {
			const own = int64(94001)
			states := newMemoryStates()
			gh := &stubInstallClient{installation: githubInstallation("first-name")}
			url := installServer(t, pool, states, gh)

			_, r1 := getNoRedirect(t, url+"/api/github/install", tokenA)
			_, res1 := getNoRedirect(t,
				fmt.Sprintf("%s/api/github/callback?installation_id=%d&state=%s", url, own, mustStateToken(t, r1)), "")
			require.Equal(t, "connected", resultOf(t, res1))

			gh.installation = githubInstallation("renamed-account")
			_, r2 := getNoRedirect(t, url+"/api/github/install", tokenA)
			_, res2 := getNoRedirect(t,
				fmt.Sprintf("%s/api/github/callback?installation_id=%d&state=%s", url, own, mustStateToken(t, r2)), "")
			require.Equal(t, "connected", resultOf(t, res2),
				"re-installing into the SAME organization must refresh, not collide")

			scoper := db.NewTenantScoper(pool)
			ctx := auth.ContextWithOrgID(context.Background(), orgA.ID)
			var login string
			require.NoError(t, scoper.InTenantTx(ctx, func(tx pgx.Tx) error {
				return tx.QueryRow(context.Background(),
					`SELECT account_login FROM github_installations WHERE github_installation_id = $1`,
					own).Scan(&login)
			}))
			require.Equal(t, "renamed-account", login, "metadata must be refreshed")
		})
	})
}

func mustStateToken(t *testing.T, location string) string {
	t.Helper()
	u, err := url.Parse(location)
	require.NoError(t, err)
	token := u.Query().Get("state")
	require.NotEmpty(t, token, "install redirect carried no state token: %q", location)
	return token
}

func url2(location string) (*url.URL, error) { return url.Parse(location) }

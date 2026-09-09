package handlers_test

// Tests for the GitHub webhook receiver.
//
// The signature is this endpoint's ENTIRE authentication story, so the
// first group attacks that; everything after it assumes a valid signature
// and asserts on the DATABASE END STATE rather than the status code,
// because every one of these handlers answers 202 and the interesting
// question is always what it wrote.
//
// FIXTURE PROVENANCE, which matters here more than usual. The
// `installation` payloads in testdata/github are REAL deliveries captured
// from the live App on 2026-09-08 — envelope, headers and body. The plan
// was explicit that hand-written fixtures are how a suite ends up
// agreeing with itself and disagreeing with the sender.
//
// The `push` and `installation_repositories` payloads were NOT captured:
// no such delivery has ever been made to a capture server. They are built
// here from GitHub's documentation, and `TestGitHubWebhook_UnverifiedShapes`
// says so out loud. See 20-05-SUMMARY.md; capturing them is a real task,
// not a formality — capturing the installation payloads is what corrected
// three specs in 20-02.

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/stretchr/testify/require"

	"github.com/yourusername/smart-docs-platform/services/backend/pkg/api"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/auth"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/client"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/db"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/testing/isolation"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/testing/isolation/testjwt"
)

// capturedDelivery is the envelope our capture server recorded.
type capturedDelivery struct {
	Method  string            `json:"method"`
	Path    string            `json:"path"`
	Headers map[string]string `json:"headers"`
	Body    json.RawMessage   `json:"body"`
}

func loadCaptured(t *testing.T, name string) capturedDelivery {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("testdata", "github", name+".json"))
	require.NoError(t, err, "captured fixture %s is missing", name)
	var d capturedDelivery
	require.NoError(t, json.Unmarshal(raw, &d))
	require.NotEmpty(t, d.Body, "fixture %s has no body", name)
	return d
}

// header pulls a captured header case-insensitively.
func (d capturedDelivery) header(name string) string {
	for k, v := range d.Headers {
		if strings.EqualFold(k, name) {
			return v
		}
	}
	return ""
}

// uniqueDelivery makes a delivery id that is fresh on every run.
//
// The isolation harness REUSES its container between `go test`
// invocations, and `github_webhook_deliveries` is not tenant-scoped so
// nothing cleans it. A test that reuses a fixture's real delivery id
// therefore processes on the first run and reports "duplicate" on every
// run after — passing or failing depending on how recently someone ran it.
func uniqueDelivery(prefix string) string {
	return fmt.Sprintf("%s-%s", prefix, uuid.NewString())
}

func signPayload(secret string, body []byte) string {
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write(body)
	return "sha256=" + hex.EncodeToString(mac.Sum(nil))
}

// webhookServer stands up the real router.
func webhookServer(t *testing.T, pool *pgxpool.Pool) string {
	t.Helper()
	deadRAG := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Errorf("the webhook must never call the RAG service; got %s", r.URL.Path)
	}))
	t.Cleanup(deadRAG.Close)

	router := api.NewRouterWithValidatorAndAdmin(
		pool, client.NewRAGClient(deadRAG.URL), testjwt.NewValidator(), nil,
		api.Config{
			LogLevel:            slog.LevelWarn,
			GitHubWebhookSecret: TestGitHubWebhookSecret,
			GitHubRepositories:  &stubLister{},
			GitHubInstallations: &stubInstallClient{installation: githubInstallation("x")},
			InstallStates:       newMemoryStates(),
			GitHubAppSlug:       "rag-doc-test",
			FrontendURL:         "https://app.example.test/settings",
		},
	)
	srv := httptest.NewServer(router)
	t.Cleanup(srv.Close)
	return srv.URL
}

// deliver posts a webhook. An empty signature means "sign it correctly".
func deliver(t *testing.T, baseURL, event, deliveryID string, body []byte, signature string) (int, string) {
	t.Helper()
	if signature == "" {
		signature = signPayload(TestGitHubWebhookSecret, body)
	}
	req, err := http.NewRequest(http.MethodPost, baseURL+"/webhooks/github", strings.NewReader(string(body)))
	require.NoError(t, err)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-GitHub-Event", event)
	if deliveryID != "" {
		req.Header.Set("X-GitHub-Delivery", deliveryID)
	}
	if signature != "-" {
		req.Header.Set("X-Hub-Signature-256", signature)
	}

	resp, err := http.DefaultClient.Do(req)
	require.NoError(t, err)
	defer resp.Body.Close()
	buf := make([]byte, 2048)
	n, _ := resp.Body.Read(buf)
	return resp.StatusCode, string(buf[:n])
}

// seedLinkedInstallation creates an installation owned by org.
func seedLinkedInstallation(t *testing.T, pool *pgxpool.Pool, orgID string, ghID int64) string {
	return seedInstallation(t, pool, orgID, ghID)
}

func repoSyncState(t *testing.T, pool *pgxpool.Pool, orgID, repoID string) (string, *string) {
	t.Helper()
	scoper := db.NewTenantScoper(pool)
	ctx := auth.ContextWithOrgID(context.Background(), orgID)
	var state string
	var installation *string
	require.NoError(t, scoper.InTenantTx(ctx, func(tx pgx.Tx) error {
		return tx.QueryRow(context.Background(),
			`SELECT sync_state, installation_id::text FROM repositories WHERE id = $1`,
			repoID).Scan(&state, &installation)
	}))
	return state, installation
}

// installationRow reads an installation inside org's own scope.
//
// `github_installations` carries RLS, so an unscoped read returns zero
// rows on a fresh connection and SQLSTATE 22P02 on a previously-scoped
// one (ISS-013). The first draft of this file read it straight off the
// pool and hit exactly that.
func installationRow(t *testing.T, pool *pgxpool.Pool, orgID, instID string) (
	uninstalledAt *string, suspendedAt *string, accountLogin string, ownerOrg string,
) {
	t.Helper()
	scoper := db.NewTenantScoper(pool)
	ctx := auth.ContextWithOrgID(context.Background(), orgID)
	require.NoError(t, scoper.InTenantTx(ctx, func(tx pgx.Tx) error {
		return tx.QueryRow(context.Background(), `
			SELECT uninstalled_at::text, suspended_at::text, account_login, organization_id::text
			FROM github_installations WHERE id = $1`, instID).
			Scan(&uninstalledAt, &suspendedAt, &accountLogin, &ownerOrg)
	}))
	return
}

// visibleInstallationCount counts installations with this GitHub id that
// org can see. Used across BOTH orgs to stand in for an unscoped count.
func visibleInstallationCount(t *testing.T, pool *pgxpool.Pool, orgID string, ghID int64) int {
	t.Helper()
	scoper := db.NewTenantScoper(pool)
	ctx := auth.ContextWithOrgID(context.Background(), orgID)
	var n int
	require.NoError(t, scoper.InTenantTx(ctx, func(tx pgx.Tx) error {
		return tx.QueryRow(context.Background(),
			`SELECT count(*) FROM github_installations WHERE github_installation_id = $1`,
			ghID).Scan(&n)
	}))
	return n
}

// deliveryCount reads github_webhook_deliveries, which is deliberately
// NOT tenant-scoped (see migration 000012) — so an unscoped read is
// correct here, unlike everywhere else in this file.
func deliveryCount(t *testing.T, pool *pgxpool.Pool, deliveryID string) int {
	t.Helper()
	var n int
	require.NoError(t, pool.QueryRow(context.Background(),
		`SELECT count(*) FROM github_webhook_deliveries WHERE delivery_id = $1`, deliveryID).Scan(&n))
	return n
}

func TestGitHubWebhook(t *testing.T) {
	pool := isolation.SetupTestDB(t)

	isolation.WithTwoOrgs(t, pool, func(orgA, orgB *isolation.TestOrg) {
		srv := webhookServer(t, pool)

		t.Run("Signature_RejectsEverythingButAValidOne", func(t *testing.T) {
			body := []byte(`{"action":"ping"}`)
			good := signPayload(TestGitHubWebhookSecret, body)

			cases := []struct {
				name string
				sig  string
				want int
			}{
				{"valid", good, http.StatusAccepted},
				{"absent", "-", http.StatusUnauthorized},
				{"empty", "", http.StatusUnauthorized}, // replaced below
				{"wrong secret", signPayload("not-the-secret", body), http.StatusUnauthorized},
				{"truncated", good[:len(good)-2], http.StatusUnauthorized},
				{"no sha256 prefix", strings.TrimPrefix(good, "sha256="), http.StatusUnauthorized},
				{"not hex", "sha256=zzzz", http.StatusUnauthorized},
				{"sha1 legacy only", "sha1=" + strings.TrimPrefix(good, "sha256="), http.StatusUnauthorized},
			}
			for i, tc := range cases {
				sig := tc.sig
				if tc.name == "empty" {
					sig = "sha256="
				}
				status, _ := deliver(t, srv, "ping", uniqueDelivery(fmt.Sprintf("sig-%d", i)), body, sig)
				require.Equalf(t, tc.want, status, "signature case %q", tc.name)
			}
		})

		t.Run("Signature_IsCheckedOverTheRawBodyBeforeParsing", func(t *testing.T) {
			// A body that is not JSON at all. If parsing came first this
			// would be a 400; the signature must decide, and it must decide
			// against an unsigned caller.
			garbage := []byte("this is not json")
			unsigned, _ := deliver(t, srv, "push", uniqueDelivery("raw1"), garbage, "-")
			require.Equal(t, http.StatusUnauthorized, unsigned,
				"an unsigned non-JSON body must be refused by the signature, not the parser")

			signed, _ := deliver(t, srv, "push", uniqueDelivery("raw2"), garbage, "")
			require.Equal(t, http.StatusBadRequest, signed,
				"a correctly signed non-JSON body is a bad request, which proves the "+
					"signature was computed over the raw bytes")
		})

		t.Run("MissingDeliveryIdIsRefused", func(t *testing.T) {
			body := []byte(`{"action":"ping"}`)
			status, _ := deliver(t, srv, "ping", "", body, "")
			require.Equal(t, http.StatusBadRequest, status,
				"without a delivery id there is no idempotency key")
		})

		t.Run("PingIsAccepted", func(t *testing.T) {
			// GitHub sends this when the App is created. A failure here is
			// the first red line in the delivery log, which is the first
			// place anyone looks when webhooks seem broken.
			status, body := deliver(t, srv, "ping", uniqueDelivery("ping"), []byte(`{"zen":"Design for failure."}`), "")
			require.Equal(t, http.StatusAccepted, status, "body=%s", body)
			require.Contains(t, body, "pong")
		})

		t.Run("UnknownEventIsAcceptedNotRefused", func(t *testing.T) {
			status, body := deliver(t, srv, "star", uniqueDelivery("star"), []byte(`{"action":"created"}`), "")
			require.Equal(t, http.StatusAccepted, status,
				"4xx-ing events we do not care about fills the delivery log with red; body=%s", body)
		})

		t.Run("InstallationCreated_RealPayload_IsNotAdoptedWhenUnknown", func(t *testing.T) {
			// THE ORPHAN DECISION, against a real captured delivery.
			//
			// This installation belongs to no organization of ours. Adopting
			// it — linking it to whichever org acted most recently — is a
			// cross-tenant bug, and 20-04's review demonstrated that exact
			// attack in its non-webhook form.
			d := loadCaptured(t, "installation-created")
			require.Equal(t, "installation", d.header("X-GitHub-Event"))

			status, _ := deliver(t, srv, "installation", uniqueDelivery("orphan"), d.Body, "")
			require.Equal(t, http.StatusAccepted, status)

			// Nothing was created, and NEITHER org gained an installation.
			// Counted through both tenant scopes rather than unscoped: these
			// are the only two organizations, and an unscoped read of an RLS
			// table is ISS-013.
			ghID := installationIDOf(t, d.Body)
			require.Zero(t, visibleInstallationCount(t, pool, orgA.ID, ghID),
				"an orphan installation must not be adopted by orgA")
			require.Zero(t, visibleInstallationCount(t, pool, orgB.ID, ghID),
				"an orphan installation must not be adopted by orgB")
		})

		t.Run("InstallationCreated_RefreshesOneWeAlreadyOwn", func(t *testing.T) {
			d := loadCaptured(t, "installation-created")
			ghID := installationIDOf(t, d.Body)
			instID := seedLinkedInstallation(t, pool, orgA.ID, ghID)

			status, _ := deliver(t, srv, "installation", uniqueDelivery("created-known"), d.Body, "")
			require.Equal(t, http.StatusAccepted, status)

			// Refreshed in place, and STILL orgA's — a webhook must never
			// be able to move the tenancy link.
			_, _, login, ownerOrg := installationRow(t, pool, orgA.ID, instID)
			require.Equal(t, orgA.ID, ownerOrg, "the webhook moved an installation between tenants")
			require.NotEmpty(t, login)
			require.Zero(t, visibleInstallationCount(t, pool, orgB.ID, ghID),
				"orgB must not see an installation refreshed for orgA")
		})

		t.Run("InstallationDeleted_KeepsTheRowAndTheRepositories", func(t *testing.T) {
			d := loadCaptured(t, "installation-deleted")
			ghID := installationIDOf(t, d.Body)
			instID := seedLinkedInstallation(t, pool, orgA.ID, ghID)
			repoID := seedRepoUnder(t, pool, orgA, instID, 771001, "pending")

			status, _ := deliver(t, srv, "installation", uniqueDelivery("deleted"), d.Body, "")
			require.Equal(t, http.StatusAccepted, status)

			// The installation row survives, marked uninstalled.
			uninstalled, _, _, _ := installationRow(t, pool, orgA.ID, instID)
			require.NotNil(t, uninstalled, "the row must be kept and marked, not deleted")

			// And so does the repository, with its ingested history.
			state, installation := repoSyncState(t, pool, orgA.ID, repoID)
			require.Equal(t, "never_synced", state, "a queued repo must stand down, not fail")
			require.NotNil(t, installation, "the link must survive so a reinstall can recover")
		})

		t.Run("SuspendAndUnsuspend", func(t *testing.T) {
			ghID := int64(772001)
			instID := seedLinkedInstallation(t, pool, orgA.ID, ghID)
			payload := fmt.Sprintf(`{"action":"suspend","installation":{"id":%d,
				"account":{"login":"x","type":"User"},"repository_selection":"selected"}}`, ghID)

			status, _ := deliver(t, srv, "installation", uniqueDelivery("susp"), []byte(payload), "")
			require.Equal(t, http.StatusAccepted, status)
			_, suspended, _, _ := installationRow(t, pool, orgA.ID, instID)
			require.NotNil(t, suspended, "suspend must be recorded")

			payload = strings.Replace(payload, `"suspend"`, `"unsuspend"`, 1)
			status, _ = deliver(t, srv, "installation", uniqueDelivery("unsusp"), []byte(payload), "")
			require.Equal(t, http.StatusAccepted, status)
			_, cleared, _, _ := installationRow(t, pool, orgA.ID, instID)
			require.Nil(t, cleared, "unsuspend must clear it")
		})

		t.Run("Idempotency_ASecondDeliveryChangesNothing", func(t *testing.T) {
			ghID := int64(773001)
			instID := seedLinkedInstallation(t, pool, orgA.ID, ghID)
			payload := fmt.Sprintf(`{"action":"suspend","installation":{"id":%d,
				"account":{"login":"x","type":"User"},"repository_selection":"selected"}}`, ghID)

			dupeID := uniqueDelivery("dupe")
			first, _ := deliver(t, srv, "installation", dupeID, []byte(payload), "")
			require.Equal(t, http.StatusAccepted, first)
			_, suspended, _, _ := installationRow(t, pool, orgA.ID, instID)
			require.NotNil(t, suspended)

			// Now unsuspend out of band, then REDELIVER the suspend. If the
			// duplicate were reprocessed it would re-suspend.
			// Scoped, like every other write to this table. An unscoped
			// UPDATE here matches nothing (or raises 22P02) — ISS-013.
			scoper := db.NewTenantScoper(pool)
			require.NoError(t, scoper.InTenantTx(
				auth.ContextWithOrgID(context.Background(), orgA.ID),
				func(tx pgx.Tx) error {
					_, e := tx.Exec(context.Background(),
						`UPDATE github_installations SET suspended_at = NULL WHERE id = $1`, instID)
					return e
				}))

			second, body := deliver(t, srv, "installation", dupeID, []byte(payload), "")
			require.Equal(t, http.StatusAccepted, second, "a redelivery is success, not an error")
			require.Contains(t, body, "duplicate")
			_, again, _, _ := installationRow(t, pool, orgA.ID, instID)
			require.Nil(t, again, "the duplicate delivery was reprocessed")
			require.Equal(t, 1, deliveryCount(t, pool, dupeID))
		})

		t.Run("Idempotency_ConcurrentDuplicatesProduceOneEffect", func(t *testing.T) {
			// Measured, not reasoned about: GitHub can and does deliver the
			// same event more than once concurrently.
			ghID := int64(774001)
			seedLinkedInstallation(t, pool, orgA.ID, ghID)
			payload := []byte(fmt.Sprintf(`{"action":"suspend","installation":{"id":%d,
				"account":{"login":"x","type":"User"},"repository_selection":"selected"}}`, ghID))

			raceID := uniqueDelivery("race")
			const racers = 12
			var (
				start    = make(chan struct{})
				wg       sync.WaitGroup
				mu       sync.Mutex
				accepted int
				dupes    int
			)
			for i := 0; i < racers; i++ {
				wg.Add(1)
				go func() {
					defer wg.Done()
					<-start
					status, body := deliver(t, srv, "installation", raceID, payload, "")
					mu.Lock()
					defer mu.Unlock()
					if status == http.StatusAccepted {
						accepted++
						if strings.Contains(body, "duplicate") {
							dupes++
						}
					}
				}()
			}
			close(start)
			wg.Wait()

			require.Equal(t, racers, accepted, "every delivery must be answered 202")
			require.Equal(t, racers-1, dupes, "exactly one racer may do the work")
			require.Equal(t, 1, deliveryCount(t, pool, raceID))
		})

		// ---------------------------------------------------------------
		// UNVERIFIED SHAPES.
		//
		// Everything below drives `push` and `installation_repositories`
		// with payloads built from GitHub's DOCUMENTATION, not captured
		// from the live App — no such delivery has ever reached a capture
		// server. The plan was explicit that this is how a suite ends up
		// agreeing with itself and disagreeing with the sender, and 20-02
		// is the evidence: capturing the installation payloads corrected
		// three specs, including a size field that was out by 1000x.
		//
		// So these tests hold the HANDLER LOGIC honestly and say nothing
		// trustworthy about the payload SHAPE. Capturing both is recorded
		// as the first task of 20-05's follow-up.
		// ---------------------------------------------------------------

		t.Run("UNVERIFIED_PushOnTheDefaultBranchQueuesTheRepository", func(t *testing.T) {
			ghID := int64(776000)
			inst := seedLinkedInstallation(t, pool, orgA.ID, ghID)
			repo := seedRepoUnder(t, pool, orgA, inst, 776001, "synced")

			body := fmt.Sprintf(`{"ref":"refs/heads/main",
				"repository":{"id":776001,"name":"r","full_name":"o/r","default_branch":"main"},
				"installation":{"id":%d,"account":{"login":"x","type":"User"},
				"repository_selection":"selected"}}`, ghID)

			status, _ := deliver(t, srv, "push", uniqueDelivery("push-main"), []byte(body), "")
			require.Equal(t, http.StatusAccepted, status)
			state, _ := repoSyncState(t, pool, orgA.ID, repo)
			require.Equal(t, "pending", state)
		})

		t.Run("UNVERIFIED_PushToAFeatureBranchIsIgnored", func(t *testing.T) {
			ghID := int64(777000)
			inst := seedLinkedInstallation(t, pool, orgA.ID, ghID)
			repo := seedRepoUnder(t, pool, orgA, inst, 777001, "synced")

			body := fmt.Sprintf(`{"ref":"refs/heads/feature/x",
				"repository":{"id":777001,"name":"r","full_name":"o/r","default_branch":"main"},
				"installation":{"id":%d,"account":{"login":"x","type":"User"},
				"repository_selection":"selected"}}`, ghID)

			status, body2 := deliver(t, srv, "push", uniqueDelivery("push-feature"), []byte(body), "")
			require.Equal(t, http.StatusAccepted, status)
			require.Contains(t, body2, "not the default branch")
			state, _ := repoSyncState(t, pool, orgA.ID, repo)
			require.Equal(t, "synced", state,
				"ingesting every feature branch is not the product")
		})

		t.Run("UNVERIFIED_PushDoesNotRequeueARunInFlight", func(t *testing.T) {
			// ISS-016: re-queueing a repository that is mid-sync makes two
			// writers believe they own it. The webhook is the other place
			// that could happen.
			ghID := int64(778000)
			inst := seedLinkedInstallation(t, pool, orgA.ID, ghID)
			repo := seedRepoUnder(t, pool, orgA, inst, 778001, "syncing")

			body := fmt.Sprintf(`{"ref":"refs/heads/main",
				"repository":{"id":778001,"name":"r","full_name":"o/r","default_branch":"main"},
				"installation":{"id":%d,"account":{"login":"x","type":"User"},
				"repository_selection":"selected"}}`, ghID)

			status, _ := deliver(t, srv, "push", uniqueDelivery("push-syncing"), []byte(body), "")
			require.Equal(t, http.StatusAccepted, status)
			state, _ := repoSyncState(t, pool, orgA.ID, repo)
			require.Equal(t, "syncing", state, "a run in flight must not be re-queued")
		})

		t.Run("UNVERIFIED_RepositoriesRemovedStandsDownWithoutDeleting", func(t *testing.T) {
			// Losing access is not the same as the repository being gone.
			// Deleting would destroy ingested history because someone
			// narrowed a permission scope.
			ghID := int64(779000)
			inst := seedLinkedInstallation(t, pool, orgA.ID, ghID)
			repo := seedRepoUnder(t, pool, orgA, inst, 779001, "synced")

			body := fmt.Sprintf(`{"action":"removed",
				"installation":{"id":%d,"account":{"login":"x","type":"User"},
				"repository_selection":"selected"},
				"repositories_removed":[{"id":779001,"name":"r","full_name":"o/r","private":true}]}`, ghID)

			status, _ := deliver(t, srv, "installation_repositories",
				uniqueDelivery("repos-removed"), []byte(body), "")
			require.Equal(t, http.StatusAccepted, status)

			state, installation := repoSyncState(t, pool, orgA.ID, repo)
			require.Equal(t, "never_synced", state)
			require.Nil(t, installation, "access was lost, so the link is cleared")
		})

		t.Run("UNVERIFIED_RepositoriesAddedDoesNotInventARow", func(t *testing.T) {
			// The payload's repository shape is REDUCED — no default_branch,
			// which is NOT NULL. Inserting would mean inventing one, and
			// would put a repository in the product nobody asked to connect.
			ghID := int64(780000)
			seedLinkedInstallation(t, pool, orgA.ID, ghID)
			before := countRepositories(t, pool, orgA)

			body := fmt.Sprintf(`{"action":"added",
				"installation":{"id":%d,"account":{"login":"x","type":"User"},
				"repository_selection":"selected"},
				"repositories_added":[{"id":780001,"name":"new","full_name":"o/new","private":true}]}`, ghID)

			status, _ := deliver(t, srv, "installation_repositories",
				uniqueDelivery("repos-added"), []byte(body), "")
			require.Equal(t, http.StatusAccepted, status)
			require.Equal(t, before, countRepositories(t, pool, orgA),
				"a webhook cannot populate a repositories row; it lacks default_branch")
		})

		t.Run("CrossTenant_AWebhookCannotTouchAnotherOrgsRepositories", func(t *testing.T) {
			// orgA owns the installation; orgB has a repository with the
			// SAME github_repo_id. A push through orgA's installation must
			// not queue orgB's row.
			const sharedRepoID = int64(775001)
			ghID := int64(775000)
			instA := seedLinkedInstallation(t, pool, orgA.ID, ghID)
			repoA := seedRepoUnder(t, pool, orgA, instA, sharedRepoID, "synced")

			instB := seedLinkedInstallation(t, pool, orgB.ID, 775100)
			repoB := seedRepoUnder(t, pool, orgB, instB, sharedRepoID, "synced")

			payload := fmt.Sprintf(`{"ref":"refs/heads/main",
				"repository":{"id":%d,"name":"r","full_name":"o/r","default_branch":"main"},
				"installation":{"id":%d,"account":{"login":"x","type":"User"},
				"repository_selection":"selected"}}`, sharedRepoID, ghID)

			status, _ := deliver(t, srv, "push", uniqueDelivery("cross"), []byte(payload), "")
			require.Equal(t, http.StatusAccepted, status)

			stateA, _ := repoSyncState(t, pool, orgA.ID, repoA)
			stateB, _ := repoSyncState(t, pool, orgB.ID, repoB)
			require.Equal(t, "pending", stateA, "the owning tenant's repository must be queued")
			require.Equal(t, "synced", stateB,
				"cross-tenant leak: a push through orgA's installation queued orgB's repository")
		})
	})
}

func installationIDOf(t *testing.T, body []byte) int64 {
	t.Helper()
	var p struct {
		Installation struct {
			ID int64 `json:"id"`
		} `json:"installation"`
	}
	require.NoError(t, json.Unmarshal(body, &p))
	require.NotZero(t, p.Installation.ID)
	return p.Installation.ID
}

// seedRepoUnder adds a repository to org's default project.
func seedRepoUnder(t *testing.T, pool *pgxpool.Pool, org *isolation.TestOrg,
	installationID string, githubRepoID int64, state string) string {
	t.Helper()
	scoper := db.NewTenantScoper(pool)
	ctx := auth.ContextWithOrgID(context.Background(), org.ID)
	var id string
	require.NoError(t, scoper.InTenantTx(ctx, func(tx pgx.Tx) error {
		return tx.QueryRow(context.Background(), `
			INSERT INTO repositories
			  (project_id, installation_id, github_repo_id, name, git_url, sync_state)
			VALUES ($1, $2, $3, $4, $5, $6)
			RETURNING id::text`,
			org.ProjectID, installationID, githubRepoID,
			fmt.Sprintf("r%d", githubRepoID),
			fmt.Sprintf("https://github.com/%s/r%d.git", org.Slug, githubRepoID),
			state).Scan(&id)
	}))
	return id
}

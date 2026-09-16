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
// here from GitHub's documentation. See 20-05-SUMMARY.md; capturing them is
// a real task, not a formality — capturing the installation payloads is
// what corrected three specs in 20-02.
//
// THE CONVENTION, STATED EXACTLY, because the looser version of it was
// claimed and was not true. Per-event subtests that drive an unverified
// payload carry an `UNVERIFIED_` prefix. FIVE tests drive one without the
// prefix — `AddedOnlyTouchesTheOwningOrganization` and
// `CrossTenant_AWebhookCannotTouchAnotherOrgsRepositories` (both from
// 20-05), and 21-04's `RedeliveryOfAFailedDeliveryCreatesNoSecondLiveJob`,
// `TestGitHubWebhook_BulkAddedRacingARelinkQueuesEveryRepository` and
// `TestGitHubWebhook_DeliveriesNeverTouchAnotherOrgsJobs`. All five are
// about a property that spans events — tenancy, redelivery, the queue —
// rather than about one event's behaviour, so each carries the caveat in
// its own comment instead. Renaming them was considered and rejected: the
// prefix earns its place by marking the per-event cases a reader would
// otherwise take as evidence about the SHAPE, and spreading it over every
// test that happens to send a `push` body would make it mean nothing.
//
// (An earlier version of this header pointed at a
// `TestGitHubWebhook_UnverifiedShapes` that has never existed anywhere in
// the repo.)

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
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
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/api/handlers"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/auth"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/client"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/db"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/github"
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

// deliveryRecord reads a delivery's outcome and recorded tenant.
func deliveryRecord(t *testing.T, pool *pgxpool.Pool, deliveryID string) (string, *string) {
	t.Helper()
	var outcome string
	var org *string
	require.NoError(t, pool.QueryRow(context.Background(),
		`SELECT outcome, organization_id::text FROM github_webhook_deliveries
		 WHERE delivery_id = $1`, deliveryID).Scan(&outcome, &org))
	return outcome, org
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

		t.Run("InstallationCreated_ClearsBothStaleMarkers", func(t *testing.T) {
			// A fresh `created` means the App is installed and active, so
			// neither uninstalled_at nor suspended_at can still be true.
			// Clearing only the first left a reinstalled App looking
			// suspended — and Phase 21 is told to check that column before
			// attempting a sync, so it would refuse to sync forever.
			// A synthetic payload with its own id, deliberately: the
			// captured fixture's installation id is already seeded by
			// another subtest, and two seeds of the same id collide on
			// 000010's UNIQUE constraint. Passed alone this test was green
			// and in the suite it was not — an order dependence, which is
			// its own small lesson about running a new test both ways.
			const ghID = int64(788000)
			instID := seedLinkedInstallation(t, pool, orgA.ID, ghID)
			payload := fmt.Sprintf(`{"action":"created","installation":{"id":%d,
				"account":{"login":"acme","type":"Organization"},
				"repository_selection":"selected"}}`, ghID)

			scoper := db.NewTenantScoper(pool)
			require.NoError(t, scoper.InTenantTx(
				auth.ContextWithOrgID(context.Background(), orgA.ID),
				func(tx pgx.Tx) error {
					_, e := tx.Exec(context.Background(), `
						UPDATE github_installations
						SET suspended_at = NOW(), uninstalled_at = NOW() WHERE id = $1`, instID)
					return e
				}))

			status, _ := deliver(t, srv, "installation", uniqueDelivery("recreated"), []byte(payload), "")
			require.Equal(t, http.StatusAccepted, status)

			uninstalled, suspended, _, _ := installationRow(t, pool, orgA.ID, instID)
			require.Nil(t, uninstalled, "a reinstall must clear uninstalled_at")
			require.Nil(t, suspended, "a reinstall must clear suspended_at too")
		})

		t.Run("InstallationDeleted_KeepsTheRowAndTheRepositories", func(t *testing.T) {
			d := loadCaptured(t, "installation-deleted")
			ghID := installationIDOf(t, d.Body)
			instID := seedLinkedInstallation(t, pool, orgA.ID, ghID)

			// Four repositories, one per state an uninstall has to handle.
			stranded := seedRepoUnder(t, pool, orgA, instID, 771001, "pending")
			running := seedRepoUnder(t, pool, orgA, instID, 771002, "syncing")
			seedRunningJob(t, pool, orgA.ID, running, "worker-uninstall-771")
			// ⚠ THE CASE THE OLD FILTER WALKED PAST. Under 21-05's
			// projection a repository whose job is RETRYING reads `failed`
			// — the state machine has no `failed` job state, so "currently
			// failing" is `queued AND attempts > 0`. The pre-21-04
			// stand-down matched only `pending` and `syncing`, so this row
			// would have kept `failed` after its job was cancelled: a
			// repository that looks like it is retrying and never will,
			// which is exactly what this handler's own comment forbids.
			retrying := seedRepoUnder(t, pool, orgA, instID, 771003, "failed")
			seedRetryingJob(t, pool, orgA.ID, retrying, 2)
			done := seedRepoUnder(t, pool, orgA, instID, 771004, "synced")

			status, _ := deliver(t, srv, "installation", uniqueDelivery("deleted"), d.Body, "")
			require.Equal(t, http.StatusAccepted, status)

			// The installation row survives, marked uninstalled.
			uninstalled, _, _, _ := installationRow(t, pool, orgA.ID, instID)
			require.NotNil(t, uninstalled, "the row must be kept and marked, not deleted")

			// And so do the repositories, with their ingested history.
			state, installation := repoSyncState(t, pool, orgA.ID, stranded)
			require.Equal(t, "never_synced", state, "a queued repo must stand down, not fail")
			require.NotNil(t, installation, "the link must survive so a reinstall can recover")

			require.Empty(t, liveJobsFor(t, pool, running),
				"an uninstall must stop the run in flight, not race it")
			require.Equal(t, "never_synced", syncStateOfRepo(t, pool, orgA.ID, running))

			require.Empty(t, liveJobsFor(t, pool, retrying),
				"the retrying job must be cancelled")
			require.Equal(t, "never_synced", syncStateOfRepo(t, pool, orgA.ID, retrying),
				"a repository whose job was just cancelled must not be left looking "+
					"like it is still retrying")

			require.Equal(t, "synced", syncStateOfRepo(t, pool, orgA.ID, done),
				"a repository that finished is not re-synced, and is not stood down either")
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

			status, resp := deliver(t, srv, "push", uniqueDelivery("push-main"), []byte(body), "")
			require.Equal(t, http.StatusAccepted, status)
			require.Contains(t, resp, "queued")

			// THE WORK ITEM, not the projection. `sync_state` is asserted
			// too, but as the thing the producer WROTE — the queue is what
			// a worker reads, and a test that only checked the column would
			// pass for a handler that still used it as a queue.
			live := liveJobsFor(t, pool, repo)
			require.Len(t, live, 1, "a push must create exactly one job")
			require.Equal(t, "queued", live[0].State)
			require.Equal(t, "incremental", live[0].JobType,
				"a push changed part of a repository we have already seen")
			require.Equal(t, orgA.ID, live[0].OrganizationID)
			require.False(t, live[0].NeedsRerun)

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
			require.Empty(t, jobsFor(t, pool, repo), "a feature branch must not queue work")
			state, _ := repoSyncState(t, pool, orgA.ID, repo)
			require.Equal(t, "synced", state,
				"ingesting every feature branch is not the product")
		})

		t.Run("UNVERIFIED_PushAgainstARunningJobJoinsItRatherThanQueueingASecond", func(t *testing.T) {
			// ISS-016: re-queueing a repository that is mid-sync makes two
			// writers believe they own it. The webhook is the other place
			// that could happen, and this is the case 21-CONTEXT L7 is
			// about — close to all steady-state volume, since people push
			// repeatedly and an ingest takes minutes.
			//
			// THE OLD ANSWER WAS TO DROP THE PUSH. `sync_state <> 'syncing'`
			// stopped the second writer by losing the work. The upsert is
			// the third answer: the live job absorbs it and `needs_rerun`
			// makes 21-05 re-queue once on completion.
			ghID := int64(778000)
			inst := seedLinkedInstallation(t, pool, orgA.ID, ghID)
			repo := seedRepoUnder(t, pool, orgA, inst, 778001, "syncing")
			seedRunningJob(t, pool, orgA.ID, repo, "worker-push-778")

			body := fmt.Sprintf(`{"ref":"refs/heads/main",
				"repository":{"id":778001,"name":"r","full_name":"o/r","default_branch":"main"},
				"installation":{"id":%d,"account":{"login":"x","type":"User"},
				"repository_selection":"selected"}}`, ghID)

			status, resp := deliver(t, srv, "push", uniqueDelivery("push-syncing"), []byte(body), "")
			require.Equal(t, http.StatusAccepted, status)
			require.Contains(t, resp, "joined the live job")

			all := jobsFor(t, pool, repo)
			require.Len(t, all, 1, "a push against a live job must not create a second one")
			require.Equal(t, "running", all[0].State)
			require.True(t, all[0].NeedsRerun,
				"a job that has already read the repository must be told to run again")

			state, _ := repoSyncState(t, pool, orgA.ID, repo)
			require.Equal(t, "syncing", state,
				"a running job owns its repository's projected state")
		})

		t.Run("UNVERIFIED_RepositoriesRemovedStandsDownWithoutDeleting", func(t *testing.T) {
			// Losing access is not the same as the repository being gone.
			// Deleting would destroy ingested history because someone
			// narrowed a permission scope.
			ghID := int64(779000)
			inst := seedLinkedInstallation(t, pool, orgA.ID, ghID)
			repo := seedRepoUnder(t, pool, orgA, inst, 779001, "synced")
			// A run in flight for a repository we are about to lose access
			// to. It would fail on its next GitHub call anyway; L4's point
			// is that it stops PROMPTLY rather than racing this handler to
			// write the final state.
			seedRunningJob(t, pool, orgA.ID, repo, "worker-removed-779")

			body := fmt.Sprintf(`{"action":"removed",
				"installation":{"id":%d,"account":{"login":"x","type":"User"},
				"repository_selection":"selected"},
				"repositories_removed":[{"id":779001,"name":"r","full_name":"o/r","private":true}]}`, ghID)

			status, resp := deliver(t, srv, "installation_repositories",
				uniqueDelivery("repos-removed"), []byte(body), "")
			require.Equal(t, http.StatusAccepted, status)
			require.Contains(t, resp, "1 live job superseded")

			state, installation := repoSyncState(t, pool, orgA.ID, repo)
			require.Equal(t, "never_synced", state)
			require.Nil(t, installation, "access was lost, so the link is cleared")

			require.Empty(t, liveJobsFor(t, pool, repo),
				"a job for a repository the App can no longer read must be stopped")
			all := jobsFor(t, pool, repo)
			require.Len(t, all, 1)
			require.Equal(t, "superseded", all[0].State)
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

		t.Run("UNVERIFIED_RepositoriesAddedRepointsAndQueuesWhatWeAlreadyTrack", func(t *testing.T) {
			// The POSITIVE assertion for `added`, and the reason it matters:
			// the only other test for this event asserts a NEGATIVE (no row
			// created), which a handler that does nothing at all satisfies
			// perfectly. Review demonstrated exactly that — a total no-op
			// survived the whole suite.
			oldInst := seedLinkedInstallation(t, pool, orgA.ID, 781000)
			newInst := seedLinkedInstallation(t, pool, orgA.ID, 781100)
			repo := seedRepoUnder(t, pool, orgA, oldInst, 781001, "synced")

			body := fmt.Sprintf(`{"action":"added",
				"installation":{"id":781100,"account":{"login":"x","type":"User"},
				"repository_selection":"selected"},
				"repositories_added":[{"id":781001,"name":"r","full_name":"o/r","private":true}]}`)

			status, resp := deliver(t, srv, "installation_repositories",
				uniqueDelivery("repos-added-positive"), []byte(body), "")
			require.Equal(t, http.StatusAccepted, status)
			require.Contains(t, resp, "1 already known, 1 queued")

			state, installation := repoSyncState(t, pool, orgA.ID, repo)
			require.Equal(t, "pending", state, "a repository we regained access to must be queued")
			require.NotNil(t, installation)
			require.Equal(t, newInst, *installation,
				"the repository must be re-pointed at the installation that now covers it")

			live := liveJobsFor(t, pool, repo)
			require.Len(t, live, 1)
			require.Equal(t, "full_ingest", live[0].JobType,
				"regaining access to a repository asks for the whole thing, not a diff")
			require.Equal(t, orgA.ID, live[0].OrganizationID)
		})

		t.Run("UNVERIFIED_AddedWithAChangedInstallationSupersedesTheRunInFlight", func(t *testing.T) {
			// The relink case, arriving as a webhook rather than as a
			// connect. The job that is running holds a token for an
			// installation that no longer covers this repository, so it is
			// taken out of the live set BEFORE its replacement is queued —
			// backwards, the upsert flags the job that is about to leave and
			// the repository ends with no live job at all, silently.
			oldInst := seedLinkedInstallation(t, pool, orgA.ID, 790000)
			newInst := seedLinkedInstallation(t, pool, orgA.ID, 790100)
			repo := seedRepoUnder(t, pool, orgA, oldInst, 790001, "syncing")
			seedRunningJob(t, pool, orgA.ID, repo, "worker-added-790")

			body := `{"action":"added",
				"installation":{"id":790100,"account":{"login":"x","type":"User"},
				"repository_selection":"selected"},
				"repositories_added":[{"id":790001,"name":"r","full_name":"o/r","private":true}]}`

			status, resp := deliver(t, srv, "installation_repositories",
				uniqueDelivery("added-supersede"), []byte(body), "")
			require.Equal(t, http.StatusAccepted, status)
			require.Contains(t, resp, "1 superseded")

			all := jobsFor(t, pool, repo)
			require.Len(t, all, 2, "the old job is kept as a record, not deleted")
			live := liveJobsFor(t, pool, repo)
			require.Len(t, live, 1, "exactly one live job, which is the ISS-016 guarantee")
			require.Equal(t, "queued", live[0].State)
			require.Equal(t, "full_ingest", live[0].JobType)
			require.False(t, live[0].NeedsRerun)

			superseded := scanJobs(t, pool,
				`repository_id = $1 AND state = 'superseded'`, repo)
			require.Len(t, superseded, 1)
			require.NotNil(t, superseded[0].LeaseOwner,
				"the supersede keeps the lease, so the row records which worker was running")

			_, installation := repoSyncState(t, pool, orgA.ID, repo)
			require.NotNil(t, installation)
			require.Equal(t, newInst, *installation)
		})

		t.Run("UNVERIFIED_AddedForAnUnchangedInstallationJoinsTheLiveJob", func(t *testing.T) {
			// The other half, and the one that is easy to get wrong in the
			// direction that costs a second ingest: this installation
			// ALREADY covered the repository, so nothing is superseded —
			// its running ingest holds credentials that are still valid.
			inst := seedLinkedInstallation(t, pool, orgA.ID, 791000)
			repo := seedRepoUnder(t, pool, orgA, inst, 791001, "syncing")
			seedRunningJob(t, pool, orgA.ID, repo, "worker-added-791")

			body := `{"action":"added",
				"installation":{"id":791000,"account":{"login":"x","type":"User"},
				"repository_selection":"selected"},
				"repositories_added":[{"id":791001,"name":"r","full_name":"o/r","private":true}]}`

			status, resp := deliver(t, srv, "installation_repositories",
				uniqueDelivery("added-unchanged"), []byte(body), "")
			require.Equal(t, http.StatusAccepted, status)
			require.Contains(t, resp, "0 superseded")
			require.Contains(t, resp, "1 joined a live job")

			all := jobsFor(t, pool, repo)
			require.Len(t, all, 1, "a re-offer of a link we already have is not a new run")
			require.Equal(t, "running", all[0].State)
			require.True(t, all[0].NeedsRerun)
			require.Equal(t, "syncing", syncStateOfRepo(t, pool, orgA.ID, repo),
				"the running job keeps its repository's projected state")
		})

		t.Run("AddedOnlyTouchesTheOwningOrganization", func(t *testing.T) {
			// The project join inside recordAddedRepositories is the second
			// layer under RLS. Review found dropping it survived the suite.
			const shared = int64(782001)
			instA := seedLinkedInstallation(t, pool, orgA.ID, 782000)
			repoA := seedRepoUnder(t, pool, orgA, instA, shared, "synced")

			instB := seedLinkedInstallation(t, pool, orgB.ID, 782100)
			repoB := seedRepoUnder(t, pool, orgB, instB, shared, "synced")

			body := fmt.Sprintf(`{"action":"added",
				"installation":{"id":782000,"account":{"login":"x","type":"User"},
				"repository_selection":"selected"},
				"repositories_added":[{"id":%d,"name":"r","full_name":"o/r","private":true}]}`, shared)

			status, _ := deliver(t, srv, "installation_repositories",
				uniqueDelivery("added-cross"), []byte(body), "")
			require.Equal(t, http.StatusAccepted, status)

			stateA, _ := repoSyncState(t, pool, orgA.ID, repoA)
			stateB, _ := repoSyncState(t, pool, orgB.ID, repoB)
			require.Equal(t, "pending", stateA)
			require.Equal(t, "synced", stateB,
				"cross-tenant leak: added for orgA's installation queued orgB's repository")

			require.Len(t, liveJobsFor(t, pool, repoA), 1)
			require.Empty(t, jobsFor(t, pool, repoB),
				"cross-tenant leak: orgB's repository got a job from orgA's delivery")
			require.Zero(t, countJobs(t, pool,
				`repository_id = $1 AND organization_id = $2`, repoA, orgB.ID),
				"no job for orgA's repository may carry orgB's organization id")
		})

		t.Run("RemovedOnlyTouchesTheNamedInstallation", func(t *testing.T) {
			// standDownRepositories filters on installation_id as well as
			// the repo ids. Review found dropping that filter survived —
			// it would stand down same-org repositories under a DIFFERENT
			// installation.
			instOne := seedLinkedInstallation(t, pool, orgA.ID, 783000)
			instTwo := seedLinkedInstallation(t, pool, orgA.ID, 783100)
			target := seedRepoUnder(t, pool, orgA, instOne, 783001, "synced")
			bystander := seedRepoUnder(t, pool, orgA, instTwo, 783002, "synced")

			body := `{"action":"removed",
				"installation":{"id":783000,"account":{"login":"x","type":"User"},
				"repository_selection":"selected"},
				"repositories_removed":[
					{"id":783001,"name":"a","full_name":"o/a","private":true},
					{"id":783002,"name":"b","full_name":"o/b","private":true}]}`

			status, _ := deliver(t, srv, "installation_repositories",
				uniqueDelivery("removed-scoped"), []byte(body), "")
			require.Equal(t, http.StatusAccepted, status)

			targetState, targetInst := repoSyncState(t, pool, orgA.ID, target)
			byState, byInst := repoSyncState(t, pool, orgA.ID, bystander)
			require.Equal(t, "never_synced", targetState)
			require.Nil(t, targetInst)
			require.Equal(t, "synced", byState,
				"a repository under a different installation must be untouched")
			require.NotNil(t, byInst)
		})

		t.Run("DeliveryRecordsItsOutcomeAndTenant", func(t *testing.T) {
			// Review found recordOutcome could be disabled entirely with the
			// suite still green — which is also what made a poisoned
			// 'processing' row invisible. And migration 000012 described an
			// organization_id column that no code wrote.
			ghID := int64(784000)
			seedLinkedInstallation(t, pool, orgA.ID, ghID)
			id := uniqueDelivery("outcome")
			body := fmt.Sprintf(`{"action":"suspend","installation":{"id":%d,
				"account":{"login":"x","type":"User"},"repository_selection":"selected"}}`, ghID)

			status, _ := deliver(t, srv, "installation", id, []byte(body), "")
			require.Equal(t, http.StatusAccepted, status)

			outcome, org := deliveryRecord(t, pool, id)
			require.Equal(t, "suspended", outcome,
				"the delivery must record what was done, not stay 'processing'")
			require.NotNil(t, org, "the discovered tenant must be recorded")
			require.Equal(t, orgA.ID, *org)
		})

		t.Run("AnUnfinishedDeliveryCanBeReprocessed", func(t *testing.T) {
			// A delivery that died mid-flight — a panic, an OOM, a deploy
			// restart — leaves 'processing' behind. Treating that as a
			// duplicate meant GitHub's redelivery was answered 202 and
			// dropped, permanently, with no way back.
			ghID := int64(785000)
			instID := seedLinkedInstallation(t, pool, orgA.ID, ghID)
			id := uniqueDelivery("poisoned")

			// The corpse of a previous attempt — and it must be OLD.
			// A row that is still 'processing' recently is a delivery some
			// other worker is handling RIGHT NOW, and re-claiming that is
			// how twelve concurrent redeliveries all end up processing.
			_, err := pool.Exec(context.Background(), `
				INSERT INTO github_webhook_deliveries
				  (delivery_id, event, action, outcome, received_at)
				VALUES ($1, 'installation', 'suspend', 'processing', NOW() - INTERVAL '1 hour')`, id)
			require.NoError(t, err)

			body := fmt.Sprintf(`{"action":"suspend","installation":{"id":%d,
				"account":{"login":"x","type":"User"},"repository_selection":"selected"}}`, ghID)
			status, resp := deliver(t, srv, "installation", id, []byte(body), "")
			require.Equal(t, http.StatusAccepted, status)
			require.NotContains(t, resp, "duplicate",
				"an unfinished delivery must be re-claimable, not dropped")

			_, suspended, _, _ := installationRow(t, pool, orgA.ID, instID)
			require.NotNil(t, suspended, "the redelivery must actually have been processed")

			// And once finished, it IS a duplicate.
			_, again := deliver(t, srv, "installation", id, []byte(body), "")
			require.Contains(t, again, "duplicate")
		})

		t.Run("AFailedDeliveryIsReclaimableImmediately", func(t *testing.T) {
			// The half that matters most in practice, and the half that had
			// no test: an ordinary 500 writes 'failed', while 'processing'
			// only survives a crash. Deleting the failed disjunct entirely
			// used to leave the suite green.
			//
			// No staleness window applies here: a failed attempt is provably
			// finished, so there is no live worker to overtake.
			ghID := int64(787000)
			instID := seedLinkedInstallation(t, pool, orgA.ID, ghID)
			id := uniqueDelivery("failed-half")

			_, err := pool.Exec(context.Background(), `
				INSERT INTO github_webhook_deliveries (delivery_id, event, action, outcome)
				VALUES ($1, 'installation', 'suspend', 'failed')`, id)
			require.NoError(t, err)

			body := fmt.Sprintf(`{"action":"suspend","installation":{"id":%d,
				"account":{"login":"x","type":"User"},"repository_selection":"selected"}}`, ghID)
			status, resp := deliver(t, srv, "installation", id, []byte(body), "")
			require.Equal(t, http.StatusAccepted, status)
			require.NotContains(t, resp, "duplicate",
				"a failed delivery must be re-claimable at once, with no waiting period")

			_, suspended, _, _ := installationRow(t, pool, orgA.ID, instID)
			require.NotNil(t, suspended, "the retry must actually have been processed")

			outcome, _ := deliveryRecord(t, pool, id)
			require.Equal(t, "suspended", outcome, "the retry must overwrite 'failed'")
		})

		t.Run("ADeliveryBeingProcessedRightNowIsNotReclaimable", func(t *testing.T) {
			// The other half of the re-claim rule, and the half CI caught
			// and local runs did not: a FRESH 'processing' row belongs to
			// a worker that is still going. Re-claiming it means two
			// workers process the same event.
			ghID := int64(786000)
			instID := seedLinkedInstallation(t, pool, orgA.ID, ghID)
			id := uniqueDelivery("in-flight")

			_, err := pool.Exec(context.Background(), `
				INSERT INTO github_webhook_deliveries (delivery_id, event, action, outcome)
				VALUES ($1, 'installation', 'suspend', 'processing')`, id)
			require.NoError(t, err)

			body := fmt.Sprintf(`{"action":"suspend","installation":{"id":%d,
				"account":{"login":"x","type":"User"},"repository_selection":"selected"}}`, ghID)
			status, resp := deliver(t, srv, "installation", id, []byte(body), "")
			require.Equal(t, http.StatusAccepted, status)
			require.Contains(t, resp, "duplicate",
				"a delivery still in flight must not be re-claimed")

			_, suspended, _, _ := installationRow(t, pool, orgA.ID, instID)
			require.Nil(t, suspended, "the in-flight delivery was processed a second time")
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

			// ⚠ ASSERTED WITH AN EXPLICIT organization_id FILTER, because
			// `ingestion_jobs` has NO row-level security: a query on this
			// table that omits one proves nothing about tenancy.
			require.Len(t, liveJobsFor(t, pool, repoA), 1)
			require.Empty(t, jobsFor(t, pool, repoB),
				"cross-tenant leak: orgB's repository got a job from orgA's push")
			require.Zero(t, countJobs(t, pool, `organization_id = $1 AND repository_id = $2`,
				orgB.ID, repoA), "no job may carry the wrong tenant")
		})

		t.Run("RedeliveryOfAFailedDeliveryCreatesNoSecondLiveJob", func(t *testing.T) {
			// UNVERIFIED SHAPE (ISS-019): the `push` body below is
			// documentation-derived. No `UNVERIFIED_` prefix, because this
			// is about redelivery rather than about what `push` does — see
			// the convention in this file's header.
			//
			// ⚠ EVERY HANDLER HERE RUNS TWICE. `claimDelivery` re-claims a
			// 'failed' row immediately, so GitHub redelivering an event we
			// answered 500 to runs the whole handler again — which the
			// comment in github_webhook.go denied until this plan. The
			// producers have to be re-entrant, and this is the assertion
			// that says so about the queue rather than about the column.
			ghID := int64(792000)
			inst := seedLinkedInstallation(t, pool, orgA.ID, ghID)
			repo := seedRepoUnder(t, pool, orgA, inst, 792001, "synced")

			id := uniqueDelivery("redeliver-push")
			body := fmt.Sprintf(`{"ref":"refs/heads/main",
				"repository":{"id":792001,"name":"r","full_name":"o/r","default_branch":"main"},
				"installation":{"id":%d,"account":{"login":"x","type":"User"},
				"repository_selection":"selected"}}`, ghID)

			status, _ := deliver(t, srv, "push", id, []byte(body), "")
			require.Equal(t, http.StatusAccepted, status)
			require.Len(t, liveJobsFor(t, pool, repo), 1)

			// The delivery is recorded 'failed', as a 500 would leave it.
			_, err := pool.Exec(context.Background(),
				`UPDATE github_webhook_deliveries SET outcome = 'failed' WHERE delivery_id = $1`, id)
			require.NoError(t, err)

			status, resp := deliver(t, srv, "push", id, []byte(body), "")
			require.Equal(t, http.StatusAccepted, status)
			require.NotContains(t, resp, "duplicate",
				"a failed delivery must be re-claimed, which is what makes re-entrancy matter")

			require.Len(t, liveJobsFor(t, pool, repo), 1,
				"a redelivery must join the live job, never queue a second one")
			require.Len(t, jobsFor(t, pool, repo), 1,
				"and it must not leave a second row behind at all")
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

// seedRetryingJob is a job that has FAILED at least once and is waiting
// for its next attempt.
//
// The state machine has five states and `failed` is not one of them
// (decision O2): a failed attempt goes back to `queued` with `run_after`
// in the future and `attempts` incremented, so "this repository is
// currently failing" is `state = 'queued' AND attempts > 0`. It is still
// LIVE — the partial unique index covers `queued` — so an uninstall has to
// supersede it, and the repository it belongs to projects as `failed`,
// which is the state markUninstalled's old filter walked straight past.
func seedRetryingJob(t *testing.T, pool *pgxpool.Pool, orgID, repoID string, attempts int) {
	t.Helper()
	ctx := context.Background()
	scoper := db.NewTenantScoper(pool)
	require.NoError(t, scoper.InTenantTx(auth.ContextWithOrgID(ctx, orgID), func(tx pgx.Tx) error {
		_, err := tx.Exec(ctx, `
			INSERT INTO ingestion_jobs
			  (organization_id, repository_id, job_type, state, attempts,
			   run_after, last_error)
			VALUES ($1, $2, 'full_ingest', 'queued', $3,
			        NOW() + INTERVAL '10 minutes', 'clone failed')`,
			orgID, repoID, attempts)
		return err
	}))
}

// webhookAndConnectServer stands up the real router with BOTH producers
// mounted: `POST /webhooks/github` and `POST /api/repositories`.
//
// The barrier test below needs them on one router because it races them
// against each other. `webhookServer` gives the router a stub lister that
// reports nothing, which is right for every test that never connects and
// wrong for this one.
// buildWebhookRequest signs and builds a delivery WITHOUT sending it, so a
// barrier can release the send rather than the signing.
//
// It is `deliver` split in half. The halves are kept next to each other
// deliberately: if the headers ever diverge, the barrier test stops
// exercising the real receiver and nothing would say so.
func buildWebhookRequest(t *testing.T, baseURL, event, deliveryID string, body []byte) *http.Request {
	t.Helper()
	req, err := http.NewRequest(http.MethodPost, baseURL+"/webhooks/github",
		strings.NewReader(string(body)))
	require.NoError(t, err)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-GitHub-Event", event)
	req.Header.Set("X-GitHub-Delivery", deliveryID)
	req.Header.Set("X-Hub-Signature-256", signPayload(TestGitHubWebhookSecret, body))
	return req
}

// buildConnectRequest is the same split for `POST /api/repositories`.
func buildConnectRequest(t *testing.T, baseURL, token, body string) *http.Request {
	t.Helper()
	req, err := http.NewRequest(http.MethodPost, baseURL+"/api/repositories",
		strings.NewReader(body))
	require.NoError(t, err)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+token)
	return req
}

// send performs a pre-built request. This is the only thing the barrier
// releases.
func send(t *testing.T, req *http.Request) (int, string) {
	t.Helper()
	resp, err := http.DefaultClient.Do(req)
	require.NoError(t, err)
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, string(raw)
}

func webhookAndConnectServer(
	t *testing.T, pool *pgxpool.Pool, lister handlers.InstallationRepositoryLister,
) string {
	t.Helper()
	deadRAG := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Errorf("neither producer may call the RAG service; got %s", r.URL.Path)
	}))
	t.Cleanup(deadRAG.Close)

	router := api.NewRouterWithValidatorAndAdmin(
		pool, client.NewRAGClient(deadRAG.URL), testjwt.NewValidator(), nil,
		api.Config{
			LogLevel:            slog.LevelWarn,
			GitHubWebhookSecret: TestGitHubWebhookSecret,
			GitHubRepositories:  lister,
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

// ⚠ THE BULK CASE THAT LOST TWO REPOSITORIES OF THREE (21-CONTEXT L8).
//
// An `installation_repositories.added` for three repositories, racing a
// relink of one of them through a barrier. The design this replaced caught
// 23505 from a plain INSERT and returned success — correct for two
// reconnects of ONE repository, and wrong the moment a statement touches a
// set: two of the three were never queued and the handler reported a win.
// Measured in review, which is why L8 was rewritten around a per-row
// upsert and why this is a test rather than an argument.
//
// WHAT IT ASSERTS is the only thing that distinguishes the two designs:
// after both actors finish, EVERY repository has exactly one live job. The
// status codes alone would pass under the broken design, which is the
// point.
//
// ⚠ WHAT ACTUALLY KILLS THE REGRESSION IS THE BULK SHAPE, NOT THE RACE,
// and that is worth saying because the name says otherwise. Mutation 18 —
// enqueue only the first of the three and report success, which is the
// original bug — fails this test and NOTHING ELSE in the module: every
// other `added` test carries one repository and cannot see the difference.
// The race is what makes the fixture realistic (a relink landing on one of
// the three mid-delivery, which is how the bug was found in review) and it
// is a second, weaker thing this test buys. PR #40's review made the
// distinction; the barrier below was tightened in the same round so that
// the second claim is at least honest.
//
// FIVE ROUNDS ON A WARM SERVER. A single cold round proves nothing: the
// first pays for connection setup and the two actors arrive spread out,
// which is the opposite of the contention being tested. The servers share
// a pool with MaxConns raised, because pgxpool defaults to
// max(4, NumCPU) and CI's runner has two cores — two HTTP requests that
// each open two transactions can otherwise queue for connections instead
// of racing.
//
// UNVERIFIED SHAPE: the `installation_repositories.added` body below is
// documentation-derived, like every other one in this file (ISS-019). The
// name carries no `UNVERIFIED_` prefix because this is a top-level test
// about the queue rather than one of the per-event cases the prefix marks;
// the caveat is here instead.
func TestGitHubWebhook_BulkAddedRacingARelinkQueuesEveryRepository(t *testing.T) {
	pool := isolation.SetupTestDB(t)
	ctx := context.Background()

	const (
		rounds = 5
		// The repository the relink and the bulk add both touch.
		racedGitHubID = int64(795002)
	)
	githubIDs := []int64{795001, racedGitHubID, 795003}

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		cfg := pool.Config()
		cfg.MaxConns = 12
		racePool, err := pgxpool.NewWithConfig(ctx, cfg)
		require.NoError(t, err)
		defer racePool.Close()

		token := testjwt.Sign(orgA.OwnerSupabaseID, orgA.ID, "owner")

		// Three installations: where the repositories start, where the
		// bulk `added` re-points them, and where the concurrent relink
		// sends the one it touches.
		origin := seedLinkedInstallation(t, pool, orgA.ID, 795100)
		bulkTarget := seedLinkedInstallation(t, pool, orgA.ID, 795200)
		relinkTarget := seedLinkedInstallation(t, pool, orgA.ID, 795300)

		repoIDs := make([]string, 0, len(githubIDs))
		for _, ghID := range githubIDs {
			repoIDs = append(repoIDs, seedRepoUnder(t, pool, orgA, origin, ghID, "synced"))
		}

		lister := &stubLister{repos: []github.Repository{{
			ID:            racedGitHubID,
			Name:          fmt.Sprintf("r%d", racedGitHubID),
			FullName:      "someone/raced",
			Private:       true,
			Visibility:    "private",
			SizeKB:        12,
			DefaultBranch: "main",
			CloneURL: fmt.Sprintf("https://github.com/%s/r%d.git",
				orgA.Slug, racedGitHubID),
		}}}
		srv := webhookAndConnectServer(t, racePool, lister)

		addedBody := fmt.Sprintf(`{"action":"added",
			"installation":{"id":795200,"account":{"login":"x","type":"User"},
			"repository_selection":"selected"},
			"repositories_added":[
				{"id":%d,"name":"a","full_name":"o/a","private":true},
				{"id":%d,"name":"b","full_name":"o/b","private":true},
				{"id":%d,"name":"c","full_name":"o/c","private":true}]}`,
			githubIDs[0], githubIDs[1], githubIDs[2])

		for round := 1; round <= rounds; round++ {
			// Reset to the starting shape. Only this test's own rows are
			// touched — no blanket delete, because the claim tests in
			// pkg/jobs rely on nothing removing rows it did not create.
			for _, repoID := range repoIDs {
				setInstallation(t, pool, orgA.ID, repoID, origin)
				clearJobsFor(t, pool, repoID)
			}
			// One of the three is already being ingested, which is what
			// makes the bulk enqueue take the upsert's conflict branch for
			// that row and the insert branch for the other two.
			seedRunningJob(t, pool, orgA.ID, repoIDs[0], "worker-bulk-race")

			// ⚠ THE BARRIER RELEASES THE `Do`, NOT THE GOROUTINE.
			//
			// An earlier version released `start` and THEN built and signed
			// each request inside the goroutine, so everything before the
			// send — HMAC signing, dialling, routing, `claimDelivery`,
			// `resolveInstallation`, JWT validation on the connect side —
			// happened after the barrier and the two actors reached their
			// transactions at genuinely different times. Caught by PR #40's
			// review. Each request is now fully built first; `ready` says
			// so, and only then are the two sends released together.
			webhookReq := buildWebhookRequest(t, srv, "installation_repositories",
				uniqueDelivery(fmt.Sprintf("bulk-race-%d", round)), []byte(addedBody))
			connectReq := buildConnectRequest(t, srv, token,
				fmt.Sprintf(`{"github_repo_id":%d,"installation_id":%q}`,
					racedGitHubID, relinkTarget))

			var (
				ready       sync.WaitGroup
				done        sync.WaitGroup
				release     = make(chan struct{})
				webhookCode int
				webhookBody string
				connectCode int
				connectResp string
			)
			ready.Add(2)
			done.Add(2)

			go func() {
				defer done.Done()
				ready.Done()
				<-release
				webhookCode, webhookBody = send(t, webhookReq)
			}()
			go func() {
				defer done.Done()
				ready.Done()
				<-release
				connectCode, connectResp = send(t, connectReq)
			}()
			ready.Wait()
			close(release)
			done.Wait()

			require.Equalf(t, http.StatusAccepted, webhookCode,
				"round %d: the bulk add must be accepted; body=%s", round, webhookBody)
			require.Equalf(t, http.StatusCreated, connectCode,
				"round %d: the concurrent relink must succeed; body=%s", round, connectResp)

			for i, repoID := range repoIDs {
				live := liveJobsFor(t, pool, repoID)
				require.Lenf(t, live, 1,
					"round %d: repository %d (github id %d) must end with exactly one live "+
						"job; a bulk enqueue that reports success while losing rows is the "+
						"L8 failure this test exists for", round, i, githubIDs[i])
				require.Equalf(t, orgA.ID, live[0].OrganizationID, "round %d", round)
			}

			// And every repository still points at one of the two
			// installations this round asked for — never NULL, never stale.
			for i, repoID := range repoIDs {
				_, installation := repoSyncState(t, pool, orgA.ID, repoID)
				require.NotNilf(t, installation, "round %d: repository %d lost its link",
					round, i)
				require.Containsf(t, []string{bulkTarget, relinkTarget}, *installation,
					"round %d: repository %d", round, i)
			}
		}

		for _, repoID := range repoIDs {
			clearJobsFor(t, pool, repoID)
		}
		isolation.AssertNoRepositoryTenantDrift(t, pool)
	})
}

// ⚠ THE TENANT BOUNDARY, ON THE TABLE THE DATABASE WILL NOT DEFEND.
//
// `ingestion_jobs` has NO row-level security (21-CONTEXT L5), and
// `jobs.SupersedeLive`'s `AND organization_id = $2` is the only scope that
// statement has — its UPDATE touches neither `organization_id` nor
// `repository_id`, so the tenant trigger never fires. The case that makes
// that predicate matter is exactly this one: a webhook resolves
// repositories by `github_repo_id`, which
// `idx_repositories_project_github_repo` makes unique only PER PROJECT,
// never globally. Two organizations holding the same GitHub repository id
// is ordinary, not contrived.
//
// So both orgs get a repository with the SAME github_repo_id, both have a
// live job, and only orgA's installations send deliveries. Nothing of
// orgB's may be created, flagged or cancelled.
//
// UNVERIFIED SHAPES (ISS-019): the `installation_repositories.added` body
// is documentation-derived; the `installation.deleted` one is the shape of
// a real capture. No `UNVERIFIED_` prefix, because this is about tenancy
// rather than about either event — see this file's header.
func TestGitHubWebhook_DeliveriesNeverTouchAnotherOrgsJobs(t *testing.T) {
	pool := isolation.SetupTestDB(t)

	const shared = int64(796001)

	isolation.WithTwoOrgs(t, pool, func(orgA, orgB *isolation.TestOrg) {
		srv := webhookServer(t, pool)

		instA := seedLinkedInstallation(t, pool, orgA.ID, 796100)
		repoA := seedRepoUnder(t, pool, orgA, instA, shared, "syncing")
		seedRunningJob(t, pool, orgA.ID, repoA, "worker-a")

		instB := seedLinkedInstallation(t, pool, orgB.ID, 796200)
		repoB := seedRepoUnder(t, pool, orgB, instB, shared, "syncing")
		seedRunningJob(t, pool, orgB.ID, repoB, "worker-b")

		// A relink of the shared id through one of orgA's installations:
		// the handler supersedes and re-enqueues, which is the pair of
		// writes that could reach across if the resolution were careless.
		relinked := seedLinkedInstallation(t, pool, orgA.ID, 796300)
		added := `{"action":"added",
			"installation":{"id":796300,"account":{"login":"x","type":"User"},
			"repository_selection":"selected"},
			"repositories_added":[{"id":796001,"name":"r","full_name":"o/r","private":true}]}`
		status, _ := deliver(t, srv, "installation_repositories",
			uniqueDelivery("tenant-added"), []byte(added), "")
		require.Equal(t, http.StatusAccepted, status)

		// And an uninstall of orgA's original installation, which
		// supersedes every repository still under it.
		deleted := `{"action":"deleted",
			"installation":{"id":796100,"account":{"login":"x","type":"User"},
			"repository_selection":"selected"}}`
		status, _ = deliver(t, srv, "installation", uniqueDelivery("tenant-deleted"),
			[]byte(deleted), "")
		require.Equal(t, http.StatusAccepted, status)

		// orgB is untouched: its job is still running, unflagged, and it
		// gained no second one.
		jobsB := jobsFor(t, pool, repoB)
		require.Len(t, jobsB, 1, "orgB must have gained no job from orgA's deliveries")
		require.Equal(t, "running", jobsB[0].State,
			"cross-tenant cancellation: orgA's delivery superseded orgB's in-flight ingest")
		require.False(t, jobsB[0].NeedsRerun,
			"cross-tenant flag: orgA's delivery asked orgB's job to run again")
		require.Equal(t, orgB.ID, jobsB[0].OrganizationID)
		require.Equal(t, "syncing", syncStateOfRepo(t, pool, orgB.ID, repoB))

		// And no job for either row carries the other's tenant.
		require.Zero(t, countJobs(t, pool,
			`repository_id = $1 AND organization_id <> $2`, repoA, orgA.ID))
		require.Zero(t, countJobs(t, pool,
			`repository_id = $1 AND organization_id <> $2`, repoB, orgB.ID))

		// orgA's own repository did move: the relink superseded its running
		// job and queued the replacement, and the later uninstall of the
		// ORIGINAL installation left it alone, because the relink had
		// already re-pointed it away.
		require.Len(t, liveJobsFor(t, pool, repoA), 1)
		require.Equal(t, relinked, mustInstallationOf(t, pool, orgA.ID, repoA))

		isolation.AssertNoRepositoryTenantDrift(t, pool)
	})
}

func mustInstallationOf(t *testing.T, pool *pgxpool.Pool, orgID, repoID string) string {
	t.Helper()
	_, installation := repoSyncState(t, pool, orgID, repoID)
	require.NotNil(t, installation)
	return *installation
}

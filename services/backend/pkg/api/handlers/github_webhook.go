package handlers

// The GitHub webhook receiver.
//
// This endpoint has no JWT and no tenant on its context — GitHub holds no
// token of ours — so the HMAC signature is the entire authentication
// story, and everything downstream of it is only as trustworthy as that
// check. It is verified over the RAW body before a byte is parsed.
//
// Everything here RECORDS INTENT. Phase 21 owns the queue that acts on
// it. Nothing in this file starts a goroutine to do work: a goroutine
// begun in a webhook handler dies with the process and takes the only
// record of the work with it, which is precisely the failure mode 19-03
// spent a review round on.

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"strings"

	"github.com/go-chi/render"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/yourusername/smart-docs-platform/services/backend/pkg/db"
)

// MaxGitHubWebhookBodyBytes bounds the request body.
//
// GitHub caps webhook payloads at 25MB. A push to a repository with a
// large number of changed files is genuinely big, so this is generous —
// but it is bounded, because the body is read into memory before the
// signature can be checked and an unbounded read is a free memory
// exhaustion for an unauthenticated endpoint.
const MaxGitHubWebhookBodyBytes = 25 << 20

// GitHubWebhookHandler receives events from the GitHub App.
//
// It holds BOTH a pool and a scoper, and the split is the whole design:
//
//   - The POOL is for `github_webhook_deliveries`, which has no RLS
//     deliberately (see migration 000012), and for the one SECURITY
//     DEFINER lookup that discovers which tenant an installation belongs
//     to. A webhook arrives with no tenant, so that discovery cannot
//     itself be tenant-scoped.
//   - The SCOPER is for everything else. `github_installations` and
//     `repositories` are FORCE RLS, so every read and write of them
//     happens inside a tenant transaction built from the discovered
//     organization.
//
// The first draft of this file used the pool for everything and an
// earlier version of this comment referred to a `withInstallationTenant`
// helper that was never written. Every UPDATE silently matched zero rows,
// because that is what an unscoped write to a FORCE-RLS table does.
type GitHubWebhookHandler struct {
	pool   *pgxpool.Pool
	scoper *db.TenantScoper
	secret string
}

// NewGitHubWebhookHandler builds the handler.
//
// Panics on an empty secret. The 19-01 rule, and this file's precedent is
// `pkg/auth/webhook.go`, whose earlier versions carried an
// `if secret == "" { return true }` branch for "development convenience"
// that became a production vulnerability the moment the variable was
// unset. There is no bypass here and no way to construct one.
func NewGitHubWebhookHandler(
	pool *pgxpool.Pool, scoper *db.TenantScoper, secret string,
) *GitHubWebhookHandler {
	if strings.TrimSpace(secret) == "" {
		panic("handlers.NewGitHubWebhookHandler: secret is empty; set GITHUB_WEBHOOK_SECRET " +
			"before constructing the router")
	}
	return &GitHubWebhookHandler{pool: pool, scoper: scoper, secret: secret}
}

// verifySignature checks X-Hub-Signature-256 over the raw body.
//
// The header is `sha256=<64 hex chars>` — verified 2026-09-08 against
// real deliveries, not taken from the documentation. GitHub also sends a
// legacy `X-Hub-Signature` (SHA-1); it is deliberately ignored, because
// accepting either means an attacker picks the weaker one.
func (h *GitHubWebhookHandler) verifySignature(body []byte, header string) bool {
	const prefix = "sha256="
	if !strings.HasPrefix(header, prefix) {
		return false
	}
	want, err := hex.DecodeString(strings.TrimPrefix(header, prefix))
	if err != nil {
		return false
	}

	mac := hmac.New(sha256.New, []byte(h.secret))
	mac.Write(body)
	// hmac.Equal is constant-time. A plain `==` on the hex strings leaks
	// how many leading bytes matched, which is enough to forge a signature
	// one byte at a time given enough attempts against an endpoint that,
	// by design, anyone can reach.
	return hmac.Equal(want, mac.Sum(nil))
}

// githubWebhookEnvelope is the subset of every payload this handler reads.
//
// Shapes confirmed against real deliveries captured 2026-09-08 (see
// testdata/github). `Repositories` carries a REDUCED repository shape —
// id, node_id, name, full_name, private, and nothing else. No `size`, no
// `default_branch`, no `visibility`, no `archived`. A webhook therefore
// CANNOT populate a repositories row on its own.
type githubWebhookEnvelope struct {
	Action       string `json:"action"`
	Installation *struct {
		ID      int64 `json:"id"`
		Account struct {
			Login string `json:"login"`
			Type  string `json:"type"`
		} `json:"account"`
		RepositorySelection string `json:"repository_selection"`
	} `json:"installation"`

	// installation / installation_repositories
	Repositories        []githubWebhookRepo `json:"repositories"`
	RepositoriesAdded   []githubWebhookRepo `json:"repositories_added"`
	RepositoriesRemoved []githubWebhookRepo `json:"repositories_removed"`

	// push
	Ref        string `json:"ref"`
	Repository *struct {
		ID            int64  `json:"id"`
		Name          string `json:"name"`
		FullName      string `json:"full_name"`
		DefaultBranch string `json:"default_branch"`
	} `json:"repository"`
}

type githubWebhookRepo struct {
	ID       int64  `json:"id"`
	Name     string `json:"name"`
	FullName string `json:"full_name"`
	Private  bool   `json:"private"`
}

// Receive handles POST /webhooks/github.
func (h *GitHubWebhookHandler) Receive(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()

	// 1. Bound the body BEFORE reading it.
	r.Body = http.MaxBytesReader(w, r.Body, MaxGitHubWebhookBodyBytes)
	body, err := io.ReadAll(r.Body)
	if err != nil {
		var tooLarge *http.MaxBytesError
		if errors.As(err, &tooLarge) {
			render.Status(r, http.StatusRequestEntityTooLarge)
			render.JSON(w, r, map[string]string{"status": "error", "error": "payload too large"})
			return
		}
		render.Status(r, http.StatusBadRequest)
		render.JSON(w, r, map[string]string{"status": "error", "error": "could not read body"})
		return
	}

	// 2. Verify BEFORE parsing. Parsing attacker-controlled JSON is work
	//    done on behalf of someone who has not yet proved they are GitHub.
	if !h.verifySignature(body, r.Header.Get("X-Hub-Signature-256")) {
		slog.Warn("github webhook: signature rejected",
			slog.String("event", r.Header.Get("X-GitHub-Event")),
			slog.String("delivery", r.Header.Get("X-GitHub-Delivery")))
		render.Status(r, http.StatusUnauthorized)
		render.JSON(w, r, map[string]string{"status": "error", "error": "invalid signature"})
		return
	}

	event := r.Header.Get("X-GitHub-Event")
	delivery := r.Header.Get("X-GitHub-Delivery")
	if delivery == "" {
		// Every genuine delivery carries one. Refusing is safer than
		// inventing an id, which would make the request unrepeatable and
		// silently defeat idempotency.
		render.Status(r, http.StatusBadRequest)
		render.JSON(w, r, map[string]string{"status": "error", "error": "missing X-GitHub-Delivery"})
		return
	}

	var payload githubWebhookEnvelope
	if err := json.Unmarshal(body, &payload); err != nil {
		render.Status(r, http.StatusBadRequest)
		render.JSON(w, r, map[string]string{"status": "error", "error": "malformed payload"})
		return
	}

	var installationID int64
	if payload.Installation != nil {
		installationID = payload.Installation.ID
	}

	// 3. Claim the delivery. First writer wins; everyone else is a
	//    duplicate and does nothing.
	claimed, err := h.claimDelivery(ctx, delivery, event, payload.Action, installationID)
	if err != nil {
		slog.Error("github webhook: could not record delivery",
			slog.String("delivery", delivery), slog.String("error", err.Error()))
		// 500 so GitHub retries: we genuinely do not know whether this was
		// processed, and a redelivery is safe by construction.
		render.Status(r, http.StatusInternalServerError)
		render.JSON(w, r, map[string]string{"status": "error", "error": "could not record delivery"})
		return
	}
	if !claimed {
		slog.Info("github webhook: duplicate delivery ignored",
			slog.String("delivery", delivery), slog.String("event", event))
		h.accept(w, r, "duplicate")
		return
	}

	outcome, tenant, err := h.dispatch(ctx, event, &payload)
	if err != nil {
		slog.Error("github webhook: handler failed",
			slog.String("event", event), slog.String("delivery", delivery),
			slog.String("error", err.Error()))
		// The delivery row stays. GitHub will redeliver with the SAME id,
		// which the claim above will then reject as a duplicate — so a
		// failure here is not automatically retried, by design. See
		// `docs/api-github-webhooks.md`: recovery is a resync, not a
		// redelivery, because a partially-applied event replayed is worse
		// than one recorded as failed.
		h.recordOutcome(ctx, delivery, "failed", tenant)
		render.Status(r, http.StatusInternalServerError)
		render.JSON(w, r, map[string]string{"status": "error", "error": "handler failed"})
		return
	}

	h.recordOutcome(ctx, delivery, outcome, tenant)
	h.accept(w, r, outcome)
}

func (h *GitHubWebhookHandler) accept(w http.ResponseWriter, r *http.Request, outcome string) {
	render.Status(r, http.StatusAccepted)
	render.JSON(w, r, map[string]string{"status": "accepted", "outcome": outcome})
}

// abandonedProcessingAfter is how long a delivery may sit 'processing'
// before we assume the attempt died — a panic, an OOM, a deploy restart.
//
// Long enough that a slow-but-live handler is never overtaken (these do a
// handful of indexed UPDATEs; a second would be remarkable), short enough
// that GitHub's own redelivery schedule can recover a genuinely lost
// event without anyone intervening.
const abandonedProcessingAfter = "5 minutes"

// claimDelivery claims a delivery, returning false if it is a duplicate of
// one already FINISHED.
//
// The UNIQUE constraint is the lock. Two concurrent redeliveries both
// reach this, one wins, and only the winner proceeds — which is why it is
// an INSERT rather than a SELECT-then-INSERT. Tested concurrently rather
// than reasoned about.
//
// A 'failed' delivery is re-claimable immediately, and a 'processing' one
// becomes re-claimable once it is older than abandonedProcessingAfter.
// That is a correction: the first version treated any existing row as a
// duplicate, justified by "replaying a partially-applied event is worse
// than one recorded as failed" — but no handler here can be partially
// applied (each writes in one tenant transaction, each is an idempotent
// UPDATE by key), so the cost was real and the benefit did not exist.
//
// THE AGE CHECK IS NOT OPTIONAL, and its absence was caught by CI rather
// than locally. Without it, a row is re-claimable for the whole time the
// winner is still working on it — so twelve concurrent redeliveries all
// re-claim each other and all process. Locally the winner finished fast
// enough to hide it; on a slower runner every racer won.
func (h *GitHubWebhookHandler) claimDelivery(
	ctx context.Context, delivery, event, action string, installationID int64,
) (bool, error) {
	var installation *int64
	if installationID != 0 {
		installation = &installationID
	}
	var actionValue *string
	if action != "" {
		actionValue = &action
	}

	// The DO UPDATE ... WHERE is what makes an unfinished delivery
	// re-claimable: when the WHERE is false the update does nothing, no
	// row is returned, and the caller sees a duplicate. ON CONFLICT takes
	// a row lock, so concurrent racers still serialise to exactly one
	// winner.
	var id string
	err := h.pool.QueryRow(ctx, `
		INSERT INTO github_webhook_deliveries
		  (delivery_id, event, action, github_installation_id, outcome)
		VALUES ($1, $2, $3, $4, 'processing')
		ON CONFLICT (delivery_id) DO UPDATE
		  SET outcome = 'processing', received_at = NOW()
		  WHERE github_webhook_deliveries.outcome = 'failed'
		     OR (github_webhook_deliveries.outcome = 'processing'
		         AND github_webhook_deliveries.received_at < NOW() - $5::interval)
		RETURNING id::text
`, delivery, event, actionValue, installation, abandonedProcessingAfter).Scan(&id)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return true, nil
}

// recordOutcome finishes a delivery, annotating it with the tenant if one
// was discovered.
//
// `organization_id` is written HERE and nowhere else. Migration 000012
// describes it as "filled in when the handler works out which tenant an
// event belonged to"; for one commit that was a description of something
// no code did.
func (h *GitHubWebhookHandler) recordOutcome(
	ctx context.Context, delivery, outcome string, orgID *string,
) {
	if _, err := h.pool.Exec(ctx, `
		UPDATE github_webhook_deliveries
		SET outcome = $2,
		    organization_id = COALESCE($3::uuid, organization_id)
		WHERE delivery_id = $1
	`, delivery, outcome, orgID); err != nil {
		// Not fatal: the work was done, only the annotation is missing.
		slog.Warn("github webhook: could not record outcome",
			slog.String("delivery", delivery), slog.String("error", err.Error()))
	}
}

// dispatch routes an event to its handler and returns what was done.
//
// An unrecognised event is ACCEPTED, not refused. We subscribe to a small
// set; GitHub sends what the App is configured for, and 4xx-ing anything
// else fills the App's delivery log with red for events nobody cares
// about — which is the first place someone looks when webhooks appear
// broken, so filling it with noise has a real cost.
// dispatch returns the outcome, the tenant it discovered (nil when it did
// not discover one), and an error.
func (h *GitHubWebhookHandler) dispatch(
	ctx context.Context, event string, p *githubWebhookEnvelope,
) (string, *string, error) {
	switch event {
	case "ping":
		// Sent once when the App is created. No action, no installation.
		// Answering anything but success makes the very first entry in the
		// delivery log a failure.
		return "pong", nil, nil
	case "installation":
		return h.handleInstallation(ctx, p)
	case "installation_repositories":
		return h.handleInstallationRepositories(ctx, p)
	case "push":
		return h.handlePush(ctx, p)
	default:
		return "ignored", nil, nil
	}
}

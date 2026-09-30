// Package internalapi is the backend's SECOND listener: routes for the
// worker process, on an address that is never published.
//
// WHY A SECOND LISTENER AND NOT A ROUTE GROUP. The one route here finds a
// job by its id and its lease owner BEFORE it knows which tenant the job
// belongs to. That is a queue-wide read over `ingestion_jobs`, a table
// with no row-level security by decision (21-CONTEXT L5), and
// docs/api-ingestion-jobs.md keeps the rule that such SQL never reaches a
// public handler. Putting the route on pkg/api's router, behind any
// middleware, would make that rule a matter of middleware ordering. A
// separate listener on a separate address makes it a matter of network
// reachability, which Phase 24's deployment can enforce and audit.
//
// WHAT AUTHENTICATES A CALLER. The job's `lease_owner`: a UUID4 the
// worker generated ONCE, when its process started, and uses for every job
// that process claims; no API returns it (21-07 deliberately left it out
// of the job response), and every terminal write is already fenced on it.
// A caller presenting a job id and the lease owner of that job, while the
// job is `running` under a live lease, is the worker running that job — or
// holds the worker's database access, which is the residual risk P10
// states rather than hides: such a process could ask for a token for any
// CURRENTLY RUNNING job's repository, and the token it gets is still one
// repository, read-only, and gone in an hour. That is far narrower than
// the App private key, which never leaves this process. The worker's logs
// also carry the lease owner today; ISS-039 tracks that for Phase 24.
//
// THE LEASE GATES ISSUANCE, NOT VALIDITY (PR #52's review, L1, measured).
// Once the lease expires this route refuses, but a token it already issued
// stays valid for GitHub's full hour; nothing here can shorten it. The
// worker revokes ITS OWN token — `DELETE /installation/token`,
// authenticated by the token being revoked, no App key needed — when its
// fetch ends, on every path, so the life of the token the worker holds is
// the fetch, not the hour. That covers only the worker's token: a token
// this route mints for anyone else presenting a live lease is not the
// worker's to revoke, and lives GitHub's hour (PR #58's review, A-M1).
//
// WHAT THE NETWORK POSITION IS WORTH. On a private compose network the
// lease owner plus reachability is the whole authentication, and the
// review ruled that sufficient for v1. If the worker and the backend ever
// sit on different hosts, the route needs a bearer secret or mTLS in
// front of it; Phase 24 carries that.
//
// EVERY RESPONSE FROM THE ROUTE CARRIES `X-Rag-Internal: repository-token/1`,
// and the 404 body is fixed. The worker treats a 404 as "the lease is not
// mine" ONLY when the marker is present; a 404 from anything else — chi's
// on the public router when INTERNAL_API_URL is misconfigured, a proxy's,
// a wrong path on this listener — is unmarked and the worker fails loudly
// instead of dying quietly after five lease expiries with `last_error`
// NULL. The marker is set by the handler, not by router middleware, so
// that a wrong path on this listener is unmarked too.
package internalapi

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/yourusername/smart-docs-platform/services/backend/pkg/auth"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/db"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/github"
)

const (
	// DefaultAddr is where the internal listener binds when INTERNAL_ADDR
	// is unset: loopback, so a default deployment cannot expose it by
	// accident. Compose overrides it to the container's interface, on the
	// compose network only.
	DefaultAddr = "127.0.0.1:8081"

	// MarkerHeader and MarkerValue are on every response the token route
	// writes, whatever the status. The worker refuses to interpret a
	// response without them (see the package comment).
	MarkerHeader = "X-Rag-Internal"
	MarkerValue  = "repository-token/1"

	// RefusedBody is THE 404. Byte-identical for an unknown id, a
	// malformed id, the wrong lease owner, an expired lease and a job that
	// is not `running` (superseded, completed, dead, queued): 21-07's rule,
	// because anything else is an oracle over other tenants' job ids.
	RefusedBody = `{"error":"no_live_lease"}`

	// The two 409 reasons. 22-05 maps them onto Phase 21's endings — a
	// suspension DEFERS with the attempt handed back, an uninstall ABANDONS
	// — so they must stay distinct and must never collapse into a generic
	// failure that would end a healthy repository `dead`.
	ReasonSuspended   = "installation_suspended"
	ReasonUninstalled = "installation_uninstalled"

	// A request body is `{"lease_owner": "<uuid>"}`; anything larger is not
	// this protocol.
	maxBodyBytes = 4096

	// Two GitHub round trips at the client's 15-second timeout each, with
	// room for the database.
	routeTimeout = 45 * time.Second
)

// TokenMinter is the slice of the GitHub App client this package needs.
//
// An interface for the reason every handler seam in pkg/api gives: the
// tests here stand up a REAL github.Client against a fake GitHub (through
// github.WithBaseURL) so they can decode what the mint request actually
// carried, but a narrow interface is also what keeps a nil *github.Client
// from arriving here as a non-nil interface holding a nil pointer.
type TokenMinter interface {
	RepositoryToken(ctx context.Context, installationID, githubRepoID int64) (github.ScopedToken, error)
}

// Handler serves POST /internal/jobs/{id}/repository-token.
//
// It holds BOTH a pool and a scoper, and that is deliberate and visible
// rather than an omission (20-01-DESIGN.md's rule). The lease lookup is
// pre-tenant by construction — the tenant is on the row it is looking
// for — so it runs on the pool; the installation check that follows runs
// inside a tenant transaction for the organization the row named.
type Handler struct {
	pool   *pgxpool.Pool
	scoper *db.TenantScoper
	minter TokenMinter
	logger *slog.Logger
}

// NewHandler builds the handler. logger may be nil, meaning slog.Default.
func NewHandler(pool *pgxpool.Pool, minter TokenMinter, logger *slog.Logger) *Handler {
	if pool == nil {
		panic("internalapi.NewHandler: pool is nil")
	}
	if minter == nil {
		panic("internalapi.NewHandler: minter is nil; without GitHub App credentials the " +
			"internal listener must not start at all")
	}
	if logger == nil {
		logger = slog.Default()
	}
	return &Handler{
		pool:   pool,
		scoper: db.NewTenantScoper(pool),
		minter: minter,
		logger: logger,
	}
}

// NewRouter builds the internal listener's router: the token route and
// nothing else. No CORS, no request logging middleware (the handler logs
// one line per outcome and the body carries a credential), no health
// route — anything answered here that is not the token route is answered
// by chi, unmarked.
func NewRouter(pool *pgxpool.Pool, minter TokenMinter, logger *slog.Logger) chi.Router {
	h := NewHandler(pool, minter, logger)

	r := chi.NewRouter()
	r.Use(middleware.RequestID)
	r.Use(middleware.Recoverer)

	// The route. Its isolation test is repository_token_isolation_test.go,
	// written deliberately: the CI scanner asks for one because this is a
	// POST, but what makes it necessary is that `ingestion_jobs` has no
	// row-level security, so the statement's own predicate is the whole
	// tenant boundary — and its cross-tenant case is mutation-checked.
	r.With(middleware.Timeout(routeTimeout)).Post("/internal/jobs/{id}/repository-token", h.Mint)

	return r
}

// liveLeaseSQL finds the job the caller claims to be running.
//
// ⚠ THIS PREDICATE IS THE WHOLE AUTHORIZATION CHECK. `ingestion_jobs` has
// no row-level security (21-CONTEXT L5), so nothing beneath this statement
// refuses anything: `AND lease_owner = $2` is what makes the lease a
// credential, `state = 'running'` is what turns a superseded row (whose
// lease stays attached, deliberately) into a miss, and
// `lease_expires_at > NOW()` is what makes an expired lease someone else's
// to reclaim rather than this caller's to keep minting against. It is the
// terminal-write fence every transition in workers/jobs/transitions.py
// carries, plus a LIVE lease. Delete any one clause and a caller holding
// nothing gets a repository token.
//
// The organization comes OUT of this statement, not into it. That is the
// pre-tenant read the package comment is about, and why this runs on the
// pool rather than inside a tenant transaction.
const liveLeaseSQL = `
SELECT organization_id::text, repository_id::text
FROM ingestion_jobs
WHERE id = $1 AND lease_owner = $2 AND state = 'running'
  AND lease_expires_at > NOW()`

// installationSQL reads the repository's CURRENT installation. TENANT-SCOPED:
// both tables carry FORCE ROW LEVEL SECURITY, so this runs inside a tenant
// transaction for the organization the job named, and an unscoped version
// would return nothing — which would look exactly like "uninstalled".
//
// The same shape as the worker's claim-time read (INSTALLATION_SQL in
// workers/jobs/runtime.py). The worker checked at claim time; this closes
// the window between that check and now. LEFT JOIN, so a repository whose
// installation row is gone still yields a row and lands on the
// `uninstalled` branch rather than being indistinguishable from a
// repository that does not exist.
const installationSQL = `
SELECT r.github_repo_id, gi.github_installation_id, gi.suspended_at, gi.uninstalled_at
FROM repositories r
LEFT JOIN github_installations gi ON gi.id = r.installation_id
WHERE r.id = $1`

// tokenResponse is the 200 body. The token is copied here from
// github.ScopedToken explicitly: that struct excludes it from JSON, and
// this is the one place it belongs on the wire.
type tokenResponse struct {
	Token         string    `json:"token"`
	ExpiresAt     time.Time `json:"expires_at"`
	FullName      string    `json:"full_name"`
	DefaultBranch string    `json:"default_branch"`
}

// Mint handles POST /internal/jobs/{id}/repository-token with the body
// {"lease_owner": "..."}.
//
// The order of the checks is the order of the responses below, and the
// first thing it does is set the marker, so that every exit — including a
// timeout or a recovered panic that writes its own status — carries it.
func (h *Handler) Mint(w http.ResponseWriter, r *http.Request) {
	w.Header().Set(MarkerHeader, MarkerValue)
	ctx := r.Context()

	// The body. A malformed one is 400, NOT 404: it is a worker bug or a
	// misconfiguration, and disguising it as "the lease is gone" would make
	// the job die quietly after five lease expiries with nothing recorded.
	leaseOwner, ok := parseLeaseBody(r.Body)
	if !ok {
		writeJSON(w, http.StatusBadRequest, `{"error":"bad_request"}`)
		return
	}

	// The id. A malformed spelling is the same 404 as an unknown id: this
	// route must not say which strings are job ids.
	id, ok := canonicalUUID(chi.URLParam(r, "id"))
	if !ok {
		refuse(w)
		return
	}

	// 1. The lease. Pre-tenant, on the pool: see liveLeaseSQL.
	var orgID, repoID string
	err := h.pool.QueryRow(ctx, liveLeaseSQL, id, leaseOwner).Scan(&orgID, &repoID)
	if errors.Is(err, pgx.ErrNoRows) {
		refuse(w)
		return
	}
	if err != nil {
		h.logger.Error("repository token: lease lookup failed",
			slog.String("job", id), slog.String("error", err.Error()))
		writeJSON(w, http.StatusInternalServerError, `{"error":"internal"}`)
		return
	}

	// 2. The installation, inside the job's tenant.
	var (
		githubRepoID   *int64
		installationID *int64
		suspendedAt    *time.Time
		uninstalledAt  *time.Time
		found          = true
	)
	err = h.scoper.InTenantTx(auth.ContextWithOrgID(ctx, orgID), func(tx pgx.Tx) error {
		scanErr := tx.QueryRow(ctx, installationSQL, repoID).Scan(
			&githubRepoID, &installationID, &suspendedAt, &uninstalledAt)
		if errors.Is(scanErr, pgx.ErrNoRows) {
			found = false
			return nil
		}
		return scanErr
	})
	if err != nil {
		h.logger.Error("repository token: installation lookup failed",
			slog.String("job", id), slog.String("organization", orgID),
			slog.String("repository", repoID), slog.String("error", err.Error()))
		writeJSON(w, http.StatusInternalServerError, `{"error":"internal"}`)
		return
	}
	// `uninstalled_at` is tested before `suspended_at`, as the worker does:
	// a reinstall clears both, so a row carrying each is uninstalled, not
	// suspended (21-06).
	switch {
	case !found, githubRepoID == nil, installationID == nil, uninstalledAt != nil:
		h.logger.Info("repository token refused: installation uninstalled or missing",
			slog.String("job", id), slog.String("organization", orgID),
			slog.String("repository", repoID))
		writeJSON(w, http.StatusConflict, `{"reason":"`+ReasonUninstalled+`"}`)
		return
	case suspendedAt != nil:
		h.logger.Info("repository token refused: installation suspended",
			slog.String("job", id), slog.String("organization", orgID),
			slog.String("repository", repoID),
			slog.Time("suspended_at", *suspendedAt))
		writeJSON(w, http.StatusConflict, `{"reason":"`+ReasonSuspended+`"}`)
		return
	}

	// 3. Mint. The client's error string has already been through its
	// redaction (github.Client.request), which is the one redaction on
	// this path; the test over captured log output is what holds that.
	scoped, err := h.minter.RepositoryToken(ctx, *installationID, *githubRepoID)
	if err != nil {
		h.logger.Error("repository token: mint failed",
			slog.String("job", id), slog.String("organization", orgID),
			slog.String("repository", repoID),
			slog.Int64("github_repo_id", *githubRepoID),
			slog.Int64("installation", *installationID),
			slog.String("error", err.Error()))
		writeJSON(w, http.StatusBadGateway, `{"error":"github_unavailable"}`)
		return
	}

	// 4. One line per mint. Never the token. Since 22-05 it carries the
	// scope GitHub REPORTED in the mint reply -- repository ids and
	// permission name:level pairs, as the client's fail-closed checks
	// accepted them -- so what GitHub granted is on record, not only what
	// was asked for.
	h.logger.Info("repository token minted",
		slog.String("job", id), slog.String("organization", orgID),
		slog.String("repository", repoID),
		slog.Int64("github_repo_id", *githubRepoID),
		slog.Int64("installation", *installationID),
		slog.String("full_name", scoped.FullName),
		slog.Time("expires_at", scoped.ExpiresAt),
		slog.String("reported_repository_ids", scoped.ReportedRepositoryIDList()),
		slog.String("reported_permissions", scoped.ReportedPermissionList()))

	out, err := json.Marshal(tokenResponse{
		Token:         scoped.Token,
		ExpiresAt:     scoped.ExpiresAt,
		FullName:      scoped.FullName,
		DefaultBranch: scoped.DefaultBranch,
	})
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, `{"error":"internal"}`)
		return
	}
	writeJSON(w, http.StatusOK, string(out))
}

// parseLeaseBody accepts exactly `{"lease_owner": "<non-empty string>"}`
// and nothing else: no other key, no repeated key, no trailing bytes.
//
// A plain json.Decoder accepts trailing data and takes the last of two
// `lease_owner` keys, which is harmless here and still not this protocol
// (PR #52's review, N4). Walking the tokens costs nothing and makes the
// accepted shape exactly the documented one.
func parseLeaseBody(r io.Reader) (string, bool) {
	dec := json.NewDecoder(io.LimitReader(r, maxBodyBytes))
	if tok, err := dec.Token(); err != nil || tok != json.Delim('{') {
		return "", false
	}
	var owner string
	seen := false
	for dec.More() {
		keyTok, err := dec.Token()
		if err != nil {
			return "", false
		}
		key, isString := keyTok.(string)
		if !isString || key != "lease_owner" || seen {
			return "", false
		}
		seen = true
		// Decode into a string: a number, null-as-empty, an object or an
		// array all fail here or below.
		if err := dec.Decode(&owner); err != nil {
			return "", false
		}
	}
	if tok, err := dec.Token(); err != nil || tok != json.Delim('}') {
		return "", false
	}
	if _, err := dec.Token(); err != io.EOF {
		// Anything after the object — a second object, garbage — is not
		// this protocol either.
		return "", false
	}
	if !seen || strings.TrimSpace(owner) == "" {
		return "", false
	}
	return owner, true
}

// refuse writes THE 404. One function so there is one body.
func refuse(w http.ResponseWriter) {
	writeJSON(w, http.StatusNotFound, RefusedBody)
}

func writeJSON(w http.ResponseWriter, status int, body string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = io.WriteString(w, body)
}

// canonicalUUID accepts exactly the lowercase 8-4-4-4-12 spelling.
//
// The same helper pkg/api/handlers keeps, for the same reason: uuid.Parse
// is a parser, not a validator, and accepts braces, URNs, uppercase and
// undashed forms that Postgres may or may not, so without this the
// malformed cases would split into 404s and 500s by spelling.
func canonicalUUID(raw string) (string, bool) {
	parsed, err := uuid.Parse(raw)
	if err != nil {
		return "", false
	}
	return parsed.String(), parsed.String() == raw
}

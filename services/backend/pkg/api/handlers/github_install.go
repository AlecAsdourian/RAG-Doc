package handlers

// The GitHub App installation flow.
//
// This file is where a GitHub-side fact — "someone installed our App on
// an account" — becomes a tenant-side fact: that installation belongs to
// this organization and no other. That makes it an authorization
// boundary, not plumbing, and the ordering inside Callback is the whole
// security argument rather than a style preference.

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/url"
	"strconv"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/render"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	"github.com/yourusername/smart-docs-platform/services/backend/pkg/auth"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/db"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/github"
)

// InstallStateStore is the slice of auth.StateStore this flow needs.
//
// An interface for the reason 20-03 learned the hard way: a handler that
// holds a concrete client whose transport is unexported cannot have its
// success path executed by a test, and an untested success path is where
// the bugs live. Redis is also the one dependency a unit test should not
// need in order to prove the ORDERING of these checks.
type InstallStateStore interface {
	StoreStateValue(ctx context.Context, state, value string) error
	ConsumeState(ctx context.Context, state string) (string, bool, error)
}

// GitHubInstallationClient is the slice of the GitHub App client this
// flow needs.
type GitHubInstallationClient interface {
	GetInstallation(ctx context.Context, installationID int64) (*github.Installation, error)
	ListInstallationRepositoriesPage(
		ctx context.Context, installationID int64, page, perPage int,
	) ([]github.Repository, bool, error)
}

// installState is what the state token carries across the round trip
// through GitHub.
//
// The organization is bound HERE, server-side, at the moment the user
// starts the flow. It is deliberately not in the redirect URL, where the
// user could edit it, and deliberately not re-read from the caller's
// claim at callback time, where a user who switched organizations
// mid-flow would link the installation to the wrong tenant.
type installState struct {
	OrganizationID string    `json:"organization_id"`
	UserID         string    `json:"user_id"`
	IssuedAt       time.Time `json:"issued_at"`
}

// GitHubInstallHandler serves the installation flow.
type GitHubInstallHandler struct {
	scoper      *db.TenantScoper
	states      InstallStateStore
	github      GitHubInstallationClient
	appSlug     string
	frontendURL string
}

// NewGitHubInstallHandler builds the handler.
//
// appSlug and frontendURL are required and validated by the caller — see
// router.go, which panics when the slug is missing. A router that starts
// without one serves a redirect to a GitHub App that does not exist, and
// serves it as a 302 rather than an error, so nothing surfaces until a
// user reports a 404 on github.com. The 19-01 fail-closed pattern
// applies: refuse at construction, not at request time.
//
// states and githubClient may be nil; the endpoints that need them then
// refuse rather than reporting a success they did not perform.
func NewGitHubInstallHandler(
	scoper *db.TenantScoper,
	states InstallStateStore,
	githubClient GitHubInstallationClient,
	appSlug string,
	frontendURL string,
) *GitHubInstallHandler {
	return &GitHubInstallHandler{
		scoper:      scoper,
		states:      states,
		github:      githubClient,
		appSlug:     appSlug,
		frontendURL: frontendURL,
	}
}

// newStateToken returns a 256-bit URL-safe random token.
//
// The error is returned rather than swallowed. pkg/auth's equivalent
// ignores rand.Read's error, which on a failing entropy source yields a
// token of 32 zero bytes — a predictable CSRF token, which is no CSRF
// token at all.
func newStateToken() (string, error) {
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		return "", fmt.Errorf("generate state token: %w", err)
	}
	return base64.RawURLEncoding.EncodeToString(b), nil
}

// Install handles GET /api/github/install.
//
// Authenticated and tenant-scoped. Mints a state token bound to the
// caller's organization and redirects to GitHub.
func (h *GitHubInstallHandler) Install(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()

	orgID, ok := auth.OrgIDFromContext(ctx)
	if !ok || orgID == "" {
		// TenantMiddleware should have refused this already; belt and
		// braces, because this value is what authorises the link.
		render.Render(w, r, ErrForbidden())
		return
	}
	// Recorded for the audit line only; the ORGANIZATION is what
	// authorises the link, and it comes from the claim above.
	userID, _ := ctx.Value(auth.UserIDKey).(string)

	if h.states == nil {
		render.Render(w, r, ErrServiceUnavailable(errors.New(
			"state store unavailable; cannot start an installation safely")))
		return
	}

	token, err := newStateToken()
	if err != nil {
		render.Render(w, r, ErrInternal(err))
		return
	}

	payload, err := json.Marshal(installState{
		OrganizationID: orgID,
		UserID:         userID,
		IssuedAt:       time.Now().UTC(),
	})
	if err != nil {
		render.Render(w, r, ErrInternal(fmt.Errorf("encode install state: %w", err)))
		return
	}
	if err := h.states.StoreStateValue(ctx, token, string(payload)); err != nil {
		// Refuse rather than redirect without a state. A flow started
		// without one cannot be completed — the callback requires it — so
		// sending the user to GitHub would waste a real installation.
		render.Render(w, r, ErrServiceUnavailable(fmt.Errorf("store install state: %w", err)))
		return
	}

	target := fmt.Sprintf("https://github.com/apps/%s/installations/new?state=%s",
		url.PathEscape(h.appSlug), url.QueryEscape(token))
	http.Redirect(w, r, target, http.StatusFound)
}

// Callback handles GET /api/github/callback.
//
// MOUNTED OUTSIDE JWTAuthMiddleware, and that is load-bearing rather than
// an oversight. Verified 2026-09-08 against the live App: a browser
// following GitHub's redirect sends no Authorization header, so a
// callback inside the authenticated group returns 401 to every real
// installation — it fails 100% of the time, not intermittently.
//
// The state token is therefore the ONLY credential on this request, which
// is exactly why it must be single-use and organization-bound.
func (h *GitHubInstallHandler) Callback(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	q := r.URL.Query()

	// A missing state is REFUSED, never defaulted.
	//
	// Verified: installing from GitHub's own "Install App" button produces
	// a redirect carrying installation_id and setup_action but NO state.
	// Falling back to "the caller's current organization" would link an
	// installation to whoever happened to be logged in — and on a public
	// route there is no caller at all, so the fallback would have to
	// invent one.
	state := q.Get("state")
	if state == "" {
		h.redirectResult(w, r, "missing_state",
			"start the installation from the button in the app, so we know which workspace to connect")
		return
	}

	if h.states == nil || h.github == nil {
		h.redirectResult(w, r, "unavailable",
			"the GitHub integration is not configured on this server")
		return
	}

	// STEP 1 — consume the state token FIRST.
	//
	// Atomic get-and-delete, so a replayed token loses the race rather
	// than winning it. Invalid, expired and already-used are one answer on
	// purpose.
	raw, ok, err := h.states.ConsumeState(ctx, state)
	if err != nil {
		slog.Error("install callback: state store unreachable", slog.String("error", err.Error()))
		h.redirectResult(w, r, "unavailable", "could not verify the installation request; try again")
		return
	}
	if !ok {
		h.redirectResult(w, r, "invalid_state",
			"that installation link has expired or was already used; start again from the app")
		return
	}

	var st installState
	if err := json.Unmarshal([]byte(raw), &st); err != nil || st.OrganizationID == "" {
		slog.Error("install callback: unreadable state payload")
		h.redirectResult(w, r, "invalid_state",
			"that installation link was not usable; start again from the app")
		return
	}

	// STEP 2 — the installation id is an attacker-supplied integer until
	// GitHub says otherwise.
	installationID, err := strconv.ParseInt(q.Get("installation_id"), 10, 64)
	if err != nil || installationID <= 0 {
		h.redirectResult(w, r, "invalid_installation", "GitHub did not send a usable installation id")
		return
	}

	// STEP 3 — prove the installation is real and that we can authenticate
	// to it, BEFORE writing anything. This is the step that turns a number
	// in a query string into a fact.
	inst, err := h.github.GetInstallation(ctx, installationID)
	if err != nil {
		slog.Warn("install callback: installation not reachable",
			slog.Int64("installation_id", installationID),
			slog.String("error", err.Error()))
		h.redirectResult(w, r, "invalid_installation",
			"we could not reach that installation on GitHub")
		return
	}
	if inst.SuspendedAt != nil {
		h.redirectResult(w, r, "suspended",
			"that installation is suspended on GitHub; un-suspend it and try again")
		return
	}

	// STEP 4 — persist, into the organization the TOKEN named.
	orgCtx := auth.ContextWithOrgID(ctx, st.OrganizationID)
	var internalID string
	err = h.scoper.InTenantTx(orgCtx, func(tx pgx.Tx) error {
		return tx.QueryRow(ctx, `
			INSERT INTO github_installations
			  (organization_id, github_installation_id, account_login,
			   account_type, repository_selection)
			VALUES ($1, $2, $3, $4, $5)
			ON CONFLICT (github_installation_id) DO UPDATE SET
			  account_login = EXCLUDED.account_login,
			  account_type = EXCLUDED.account_type,
			  repository_selection = EXCLUDED.repository_selection,
			  updated_at = NOW()
			WHERE github_installations.organization_id = $1
			RETURNING id::text
		`, st.OrganizationID, inst.ID, inst.AccountLogin,
			inst.AccountType, inst.RepositorySelection).Scan(&internalID)
	})

	switch {
	case errors.Is(err, pgx.ErrNoRows):
		// The WHERE on the DO UPDATE is what makes this reachable: the row
		// exists and it belongs to somebody else. A conflicting
		// installation is a COMPREHENSIBLE situation — the App was
		// installed on a GitHub account another tenant already connected —
		// so it is a 409-equivalent result, not a 500.
		//
		// Both tenants go to the log because an operator needs to know
		// which two are involved. Neither is named to the user: telling
		// them WHICH workspace holds it confirms that workspace exists and
		// uses this product.
		slog.Warn("install callback: installation already linked to another organization",
			slog.Int64("github_installation_id", inst.ID),
			slog.String("requesting_organization_id", st.OrganizationID),
			slog.String("account_login", inst.AccountLogin))
		h.redirectResult(w, r, "already_connected",
			"that GitHub account is already connected to another workspace")
		return
	case err != nil:
		var pgErr *pgconn.PgError
		if errors.As(err, &pgErr) && pgErr.Code == "23505" {
			// Belt and braces: a unique violation that escapes the ON
			// CONFLICT arbiter is the same situation and must not be a 500.
			slog.Warn("install callback: unique violation linking installation",
				slog.Int64("github_installation_id", inst.ID),
				slog.String("requesting_organization_id", st.OrganizationID),
				slog.String("constraint", pgErr.ConstraintName))
			h.redirectResult(w, r, "already_connected",
				"that GitHub account is already connected to another workspace")
			return
		}
		slog.Error("install callback: persist failed",
			slog.Int64("github_installation_id", inst.ID),
			slog.String("error", err.Error()))
		h.redirectResult(w, r, "error", "we could not save that installation")
		return
	}

	slog.Info("github app installation linked",
		slog.String("installation_id", internalID),
		slog.Int64("github_installation_id", inst.ID),
		slog.String("organization_id", st.OrganizationID),
		slog.String("repository_selection", inst.RepositorySelection))

	// The id goes back with the redirect so the UI can offer repository
	// selection immediately. Without it a user who has just installed has
	// no way to name their own installation: the only other place the id
	// appears is on a repository, and they have none yet.
	h.redirectResultWith(w, r, "connected", "", map[string]string{
		"installation_id": internalID,
	})
}

// redirectResult sends the browser back to the frontend with a result.
//
// A redirect rather than a JSON body because the caller is a browser
// following GitHub's redirect chain, not a fetch(). The frontend reads
// github_result; github_message is a human-readable sentence that is safe
// to display verbatim — it never names another tenant.
func (h *GitHubInstallHandler) redirectResult(w http.ResponseWriter, r *http.Request, result, message string) {
	h.redirectResultWith(w, r, result, message, nil)
}

func (h *GitHubInstallHandler) redirectResultWith(
	w http.ResponseWriter, r *http.Request, result, message string, extra map[string]string,
) {
	target, err := url.Parse(h.frontendURL)
	if err != nil {
		// Cannot redirect anywhere useful; say what happened in plain text
		// rather than sending the browser to a malformed URL.
		http.Error(w, "github: "+result, http.StatusBadRequest)
		return
	}
	q := target.Query()
	q.Set("github_result", result)
	if message != "" {
		q.Set("github_message", message)
	}
	for k, v := range extra {
		q.Set(k, v)
	}
	target.RawQuery = q.Encode()
	http.Redirect(w, r, target.String(), http.StatusFound)
}

// InstallationRepositoryListResponse is one page of what an installation
// can see on GitHub.
type InstallationRepositoryListResponse struct {
	Repositories []github.Repository `json:"repositories"`
	Page         int                 `json:"page"`
	HasNext      bool                `json:"has_next"`
}

func (r *InstallationRepositoryListResponse) Render(http.ResponseWriter, *http.Request) error {
	return nil
}

// ListRepositories handles GET /api/github/installations/{id}/repositories.
//
// What this installation can see, for a picker to offer before calling
// POST /api/repositories.
func (h *GitHubInstallHandler) ListRepositories(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()

	id, ok := canonicalUUID(chi.URLParam(r, "id"))
	if !ok {
		render.Render(w, r, ErrNotFound())
		return
	}

	page, err := parsePositiveInt(r.URL.Query().Get("page"), 1)
	if err != nil {
		render.Render(w, r, ErrInvalidRequest(errors.New("page must be a positive integer")))
		return
	}
	perPage, err := parsePositiveInt(r.URL.Query().Get("per_page"), 30)
	if err != nil || perPage > 100 {
		render.Render(w, r, ErrInvalidRequest(errors.New("per_page must be between 1 and 100")))
		return
	}

	// OWNERSHIP IS PROVEN BEFORE GITHUB IS CALLED, and the order is the
	// point. Checking afterwards would still refuse the response, but it
	// would already have spent an installation token, our rate-limit
	// budget and a measurable round trip on someone else's installation —
	// turning this endpoint into an oracle that answers "does this
	// installation exist" in the timing even while it 404s in the body.
	var githubInstallationID int64
	err = h.scoper.InTenantTx(ctx, func(tx pgx.Tx) error {
		return tx.QueryRow(ctx,
			`SELECT github_installation_id FROM github_installations WHERE id = $1`,
			id).Scan(&githubInstallationID)
	})
	if errors.Is(err, pgx.ErrNoRows) {
		// Same 404 for "not yours" and "does not exist", for the reason
		// docs/api-repositories.md gives at length.
		render.Render(w, r, ErrNotFound())
		return
	}
	if err != nil {
		render.Render(w, r, ErrInternal(fmt.Errorf("resolve installation: %w", err)))
		return
	}

	// Availability is checked AFTER authorization — 20-03's finding. The
	// reverse order turns a 503-vs-404 difference into an enumeration
	// oracle whenever GitHub credentials are missing.
	if h.github == nil {
		render.Render(w, r, ErrServiceUnavailable(errors.New(
			"github app credentials not configured")))
		return
	}

	repos, hasNext, err := h.github.ListInstallationRepositoriesPage(
		ctx, githubInstallationID, page, perPage)
	if err != nil {
		render.Render(w, r, ErrServiceUnavailable(
			fmt.Errorf("list installation repositories: %w", err)))
		return
	}
	if repos == nil {
		repos = []github.Repository{}
	}

	render.Render(w, r, &InstallationRepositoryListResponse{
		Repositories: repos,
		Page:         page,
		HasNext:      hasNext,
	})
}

// InstallationSummary is one of the caller's GitHub App installations.
type InstallationSummary struct {
	ID                  string    `json:"id"`
	AccountLogin        string    `json:"account_login"`
	AccountType         string    `json:"account_type"`
	RepositorySelection string    `json:"repository_selection"`
	CreatedAt           time.Time `json:"created_at"`
}

// InstallationListResponse is the envelope for ListInstallations.
type InstallationListResponse struct {
	Installations []InstallationSummary `json:"installations"`
}

func (r *InstallationListResponse) Render(http.ResponseWriter, *http.Request) error { return nil }

// ListInstallations handles GET /api/github/installations.
//
// Added in 20-04 after the flow was found not to compose: the repository
// picker takes an installation id in its path, and before this the only
// place an id appeared was on an existing repository — so a user who had
// just installed the App, and therefore had none, could not reach the
// picker that exists to help them add their first one.
//
// `github_installation_id` is deliberately NOT returned. It is GitHub's
// number, it is not needed by a UI that addresses installations by our
// uuid, and it is the value an attacker would want in order to talk to
// GitHub about someone else's installation.
func (h *GitHubInstallHandler) ListInstallations(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()

	var out []InstallationSummary
	err := h.scoper.InTenantTx(ctx, func(tx pgx.Tx) error {
		rows, qerr := tx.Query(ctx, `
			SELECT id::text, account_login, account_type, repository_selection, created_at
			FROM github_installations
			ORDER BY created_at ASC, id ASC`)
		if qerr != nil {
			return qerr
		}
		defer rows.Close()
		for rows.Next() {
			var s InstallationSummary
			if serr := rows.Scan(&s.ID, &s.AccountLogin, &s.AccountType,
				&s.RepositorySelection, &s.CreatedAt); serr != nil {
				return serr
			}
			out = append(out, s)
		}
		return rows.Err()
	})
	if err != nil {
		render.Render(w, r, ErrInternal(fmt.Errorf("list installations: %w", err)))
		return
	}
	if out == nil {
		out = []InstallationSummary{}
	}

	render.Render(w, r, &InstallationListResponse{Installations: out})
}

func parsePositiveInt(raw string, def int) (int, error) {
	if raw == "" {
		return def, nil
	}
	n, err := strconv.Atoi(raw)
	if err != nil || n < 1 {
		return 0, errors.New("not a positive integer")
	}
	return n, nil
}

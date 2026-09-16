package handlers

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/render"
	"github.com/go-playground/validator/v10"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/yourusername/smart-docs-platform/services/backend/pkg/db"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/github"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/jobs"
)

// MaxRepositoryBodyBytes bounds the connect-repository request body. A
// valid body is two ids — about 100 bytes.
const MaxRepositoryBodyBytes = 4 << 10

// defaultPageSize / maxPageSize bound the list endpoint.
const (
	defaultPageSize = 25
	maxPageSize     = 100
)

// InstallationRepositoryLister is the slice of the GitHub client that
// Connect needs.
//
// An interface rather than *github.Client because the concrete client's
// baseURL is unexported, so a test in this package cannot point it at a
// stub — which meant the whole persist path of Connect (the project join,
// the upsert, the twelve-column scan) had never been executed by any test
// in the repository. The same seam `auth.TokenValidator` and
// `auth.AdminClient` already provide, for the same reason.
type InstallationRepositoryLister interface {
	ListInstallationRepositories(ctx context.Context, installationID int64) ([]github.Repository, error)
}

// RepositoriesHandler serves the repository CRUD surface.
//
// It holds a *db.TenantScoper and NOT a *pgxpool.Pool. Every table it
// touches (`repositories`) is RLS-scoped, and an unscoped read of one
// returns zero rows on some connections and a 500 on others depending on
// that connection's history (ISS-013). Not having a pool is what makes
// that mistake unavailable rather than merely discouraged — see
// 20-01-DESIGN.md.
type RepositoriesHandler struct {
	scoper   *db.TenantScoper
	github   InstallationRepositoryLister
	validate *validator.Validate
}

// NewRepositoriesHandler builds the handler.
//
// githubClient may be nil (the router runs degraded without App
// credentials). List, get and delete still work; connect refuses with 503
// rather than reporting a success it did not perform.
//
// CALLERS MUST PASS A LITERAL nil, not a nil *github.Client. A nil pointer
// stored in an interface makes the interface itself non-nil, so the `==
// nil` check below would pass and Connect would dereference it. See the
// guard in router.go.
func NewRepositoriesHandler(
	scoper *db.TenantScoper,
	githubClient InstallationRepositoryLister,
	validate *validator.Validate,
) *RepositoriesHandler {
	return &RepositoriesHandler{scoper: scoper, github: githubClient, validate: validate}
}

// Repository is one row of the repositories API.
type Repository struct {
	ID             string     `json:"id"`
	Name           string     `json:"name"`
	GitURL         string     `json:"git_url"`
	DefaultBranch  string     `json:"default_branch"`
	GitHubRepoID   *int64     `json:"github_repo_id"`
	InstallationID *string    `json:"installation_id"`
	Visibility     *string    `json:"visibility"`
	SizeKB         *int64     `json:"size_kb"`
	Archived       bool       `json:"archived"`
	SyncState      string     `json:"sync_state"`
	LastSyncedAt   *time.Time `json:"last_synced_at"`
	CreatedAt      time.Time  `json:"created_at"`
}

// RepositoryListResponse is the paginated list envelope.
//
// `next_cursor` is null on the last page. An object rather than a bare
// array so pagination can exist at all, and so fields can be added later
// without breaking clients.
type RepositoryListResponse struct {
	Repositories []Repository `json:"repositories"`
	NextCursor   *string      `json:"next_cursor"`
}

func (r *RepositoryListResponse) Render(http.ResponseWriter, *http.Request) error { return nil }

func (r *Repository) Render(http.ResponseWriter, *http.Request) error { return nil }

// ConnectRepositoryRequest is the body of POST /api/repositories.
//
// Deliberately NOT a git URL. GitHub's numeric repository id is stable
// across renames and transfers, and accepting a URL would invite someone
// to name a repository their installation cannot reach — which the
// handler would then have to reject anyway, less clearly.
type ConnectRepositoryRequest struct {
	GitHubRepoID   int64  `json:"github_repo_id" validate:"required,gt=0"`
	InstallationID string `json:"installation_id" validate:"required,uuid"`
}

// List handles GET /api/repositories.
func (h *RepositoriesHandler) List(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()

	limit, err := parsePageSize(r.URL.Query().Get("limit"))
	if err != nil {
		render.Render(w, r, ErrInvalidRequest(err))
		return
	}
	cursor, err := decodeCursor(r.URL.Query().Get("cursor"))
	if err != nil {
		render.Render(w, r, ErrInvalidRequest(err))
		return
	}

	// Collected here and rendered after the transaction closes. Writing
	// the response inside the callback would commit-after-respond: the
	// client gets its body before the commit that could still fail.
	var repos []Repository

	if err := h.scoper.InTenantTx(ctx, func(tx pgx.Tx) error {
		// Cursor pagination on (created_at, id), NOT OFFSET.
		//
		// Offset pagination skips and duplicates ALREADY-VISIBLE rows when
		// the set shifts under it, and this set shifts without the user
		// doing anything: the 20-05 webhook inserts repositories while
		// someone is paging through them. Keyset pagination fixes that.
		//
		// It does NOT fix commit-order skew, and an earlier version of this
		// comment claimed it did. `created_at DEFAULT NOW()` is the
		// TRANSACTION START time, but a row only becomes visible at commit,
		// so a transaction that began before the client's cursor and commits
		// after it lands a row permanently behind the cursor. Measured: a
		// late-committing insert is invisible to a resumed page and stays
		// invisible.
		//
		// Nothing cheap fixes that — Postgres exposes no commit-order
		// column — so the contract says so instead: a client watching for
		// new repositories re-polls from the first page rather than
		// trusting a held cursor to surface them. See
		// docs/api-repositories.md.
		//
		// `id` breaks ties, so two repositories created in the same
		// microsecond still order deterministically.
		//
		// limit+1 to learn whether another page exists without a second
		// query.
		const q = `
			SELECT id::text, name, git_url, default_branch, github_repo_id,
			       installation_id::text, visibility, size_kb, archived,
			       sync_state, last_synced_at, created_at
			FROM repositories
			WHERE ($1::timestamptz IS NULL OR (created_at, id) > ($1::timestamptz, $2::uuid))
			ORDER BY created_at ASC, id ASC
			LIMIT $3`

		rows, qerr := tx.Query(ctx, q, cursor.createdAt, cursor.id, limit+1)
		if qerr != nil {
			return qerr
		}
		defer rows.Close()

		for rows.Next() {
			var repo Repository
			if serr := rows.Scan(
				&repo.ID, &repo.Name, &repo.GitURL, &repo.DefaultBranch, &repo.GitHubRepoID,
				&repo.InstallationID, &repo.Visibility, &repo.SizeKB, &repo.Archived,
				&repo.SyncState, &repo.LastSyncedAt, &repo.CreatedAt,
			); serr != nil {
				return serr
			}
			repos = append(repos, repo)
		}
		return rows.Err()
	}); err != nil {
		render.Render(w, r, ErrInternal(fmt.Errorf("list repositories: %w", err)))
		return
	}

	var next *string
	if len(repos) > limit {
		repos = repos[:limit]
		c := encodeCursor(repos[len(repos)-1].CreatedAt, repos[len(repos)-1].ID)
		next = &c
	}
	if repos == nil {
		// Non-nil so an empty page marshals as [] rather than null.
		repos = []Repository{}
	}

	render.Render(w, r, &RepositoryListResponse{Repositories: repos, NextCursor: next})
}

// canonicalUUID accepts only the one spelling of a UUID this API emits.
//
// `uuid.Parse` is a PARSER, NOT A VALIDATOR: it also accepts
// `urn:uuid:<v>`, `{<v>}`, unhyphenated hex and uppercase. Postgres
// accepts some of those and rejects others, so handing its output through
// unexamined turns a malformed id into SQLSTATE 22P02 — an unhandled 500
// — for exactly the inputs Postgres happens to dislike. Measured on the
// cursor path before this existed: `urn:uuid:…` → 500, `{…}` → 200.
//
// pkg/db/tenant.go reaches the same conclusion from the other direction
// (there the value is interpolated, because SET LOCAL cannot bind). One
// rule for both: a UUID from outside is canonical or it is refused.
func canonicalUUID(raw string) (string, bool) {
	parsed, err := uuid.Parse(raw)
	if err != nil {
		return "", false
	}
	return parsed.String(), parsed.String() == raw
}

// Get handles GET /api/repositories/{id}.
func (h *RepositoriesHandler) Get(w http.ResponseWriter, r *http.Request) {
	id, ok := canonicalUUID(chi.URLParam(r, "id"))
	if !ok {
		// 404, not 400. "Malformed id" and "not yours" should be
		// indistinguishable for the same reason as below.
		render.Render(w, r, ErrNotFound())
		return
	}

	var repo Repository
	err := h.scoper.InTenantTx(r.Context(), func(tx pgx.Tx) error {
		return tx.QueryRow(r.Context(), `
			SELECT id::text, name, git_url, default_branch, github_repo_id,
			       installation_id::text, visibility, size_kb, archived,
			       sync_state, last_synced_at, created_at
			FROM repositories WHERE id = $1`, id,
		).Scan(
			&repo.ID, &repo.Name, &repo.GitURL, &repo.DefaultBranch, &repo.GitHubRepoID,
			&repo.InstallationID, &repo.Visibility, &repo.SizeKB, &repo.Archived,
			&repo.SyncState, &repo.LastSyncedAt, &repo.CreatedAt,
		)
	})

	if errors.Is(err, pgx.ErrNoRows) {
		// Deliberately identical to "does not exist". RLS already made
		// another tenant's repository invisible, so this branch covers both
		// — and telling the two apart would turn the endpoint into an
		// existence oracle for other tenants' repository ids.
		render.Render(w, r, ErrNotFound())
		return
	}
	if err != nil {
		render.Render(w, r, ErrInternal(fmt.Errorf("get repository: %w", err)))
		return
	}

	render.Render(w, r, &repo)
}

// DeleteRepositoryResponse reports what a delete actually removed.
type DeleteRepositoryResponse struct {
	Status           string `json:"status"`
	RepositoryID     string `json:"repository_id"`
	ChunksDeleted    int64  `json:"chunks_deleted"`
	IngestionsGone   int64  `json:"ingestion_runs_deleted"`
	FeedbackDeleted  int64  `json:"feedback_deleted"`
	IrreversibleWarn string `json:"note"`
}

func (d *DeleteRepositoryResponse) Render(http.ResponseWriter, *http.Request) error { return nil }

// Delete handles DELETE /api/repositories/{id}.
//
// THIS DELETES INGESTED DATA. The real cascade, all ON DELETE CASCADE
// across migrations 000002-000005:
//
//	repositories ─┬─> ingestion_runs ─> chunks
//	              └─> chunks ─> retrievals ─> feedback
//
// `chunks` hangs off `repositories` DIRECTLY as well as through
// `ingestion_runs` (000003 denormalizes `repository_id` for query
// performance), and the chain does not stop at `retrievals` — user-written
// `feedback` goes too. `queries` survive; only the retrievals that cited
// this repository's chunks are removed.
//
// That is the intended behaviour rather than an accident of the schema.
// A "disconnect" that left the chunks in place would keep a repository's
// contents searchable after the user removed it — which is the wrong
// answer for a product whose whole job is answering questions from that
// content, and worse if the repository was disconnected because it should
// not have been indexed.
//
// The response reports the counts so a client can show what was lost
// rather than a bare 204.
func (h *RepositoriesHandler) Delete(w http.ResponseWriter, r *http.Request) {
	id, ok := canonicalUUID(chi.URLParam(r, "id"))
	if !ok {
		render.Render(w, r, ErrNotFound())
		return
	}

	ctx := r.Context()
	var chunks, runs, feedback int64
	var found bool

	if err := h.scoper.InTenantTx(ctx, func(tx pgx.Tx) error {
		// Count before deleting — afterwards the rows are gone and the
		// cascade reports nothing.
		if cerr := tx.QueryRow(ctx,
			`SELECT count(*) FROM chunks WHERE repository_id = $1`, id).Scan(&chunks); cerr != nil {
			return cerr
		}
		if cerr := tx.QueryRow(ctx,
			`SELECT count(*) FROM ingestion_runs WHERE repository_id = $1`, id).Scan(&runs); cerr != nil {
			return cerr
		}
		// Feedback is USER-AUTHORED and two edges down the cascade
		// (chunks → retrievals → feedback), so it was being destroyed
		// without appearing in the response. Counted separately because it
		// is the one thing here a user cannot regenerate by re-ingesting.
		if cerr := tx.QueryRow(ctx, `
			SELECT count(*)
			FROM feedback f
			JOIN retrievals rt ON rt.id = f.retrieval_id
			JOIN chunks c ON c.id = rt.chunk_id
			WHERE c.repository_id = $1`, id).Scan(&feedback); cerr != nil {
			return cerr
		}

		tag, derr := tx.Exec(ctx, `DELETE FROM repositories WHERE id = $1`, id)
		if derr != nil {
			return derr
		}
		found = tag.RowsAffected() > 0
		return nil
	}); err != nil {
		render.Render(w, r, ErrInternal(fmt.Errorf("delete repository: %w", err)))
		return
	}

	if !found {
		// RLS matched nothing, which covers both "no such repository" and
		// "belongs to another tenant". Note this is why the check is on
		// RowsAffected rather than on the statement erroring: a
		// cross-tenant DELETE does not error, it simply matches no rows.
		render.Render(w, r, ErrNotFound())
		return
	}

	render.Render(w, r, &DeleteRepositoryResponse{
		Status:          "deleted",
		RepositoryID:    id,
		ChunksDeleted:   chunks,
		IngestionsGone:  runs,
		FeedbackDeleted: feedback,
		IrreversibleWarn: "this removed the repository, everything ingested from it, and any " +
			"feedback left on answers that cited it; reconnecting requires a full re-ingestion",
	})
}

// errNoDefaultProject means the caller's organization predates migration
// 000010's one-default-project-per-organization guarantee. A server-side
// problem, not a client one, so it must not be folded into the 404s.
var errNoDefaultProject = errors.New(
	"organization has no default project; it predates the provisioning that creates one")

// connectDecision is what a connect turned out to be. Three outcomes, one
// value, decided in Go — see the comment at STEP 3 for why it is no longer
// two SQL `CASE` expressions.
type connectDecision string

const (
	// connectNew: no row for this repository anywhere in the organization.
	connectNew connectDecision = "new"
	// connectRelink: the installation changed, or the row has never
	// carried a `github_repo_id`. Either way it has to be fetched again,
	// and any job already in flight is superseded first.
	connectRelink connectDecision = "relink"
	// connectUnchanged: a metadata refresh. It must NOT queue work — a
	// re-connect is not a retry, and re-queueing here is what ISS-016 was.
	connectUnchanged connectDecision = "unchanged"
)

// existingRepositoryLookupSQL finds the row a connect should adopt, from
// ANYWHERE in the caller's organization, and locks it.
//
// Two rows are adoptable, and both were shipping as duplicates:
//
//  1. The same `github_repo_id` in a NON-DEFAULT project. Migration
//     000011's index is per-project, and an organization may hold several
//     projects, so the schema cannot express "once per organization".
//     Without this, a repository already connected under an older project
//     got a second row and would be ingested twice.
//
//  2. A row with NO `github_repo_id` whose `git_url` matches — anything
//     connected before this API existed. It is the same repository;
//     leaving it alone produced a permanent duplicate that could never be
//     synced, because nothing else ever sets `github_repo_id`.
//
// ⚠ `FOR UPDATE OF r`, NOT A BARE `FOR UPDATE`. A bare one locks every
// table in the FROM clause, and this one joins `projects` — so it would
// take a row lock on the organization's default project and SERIALISE
// EVERY CONNECT IN THE ORGANIZATION behind whichever one is in flight.
// Nothing would fail; connects would just queue up one at a time, for as
// long as the slowest transaction takes. Pinned by
// TestRepositoriesConnect_ConcurrentConnectsOfDifferentRepositoriesDoNotBlock,
// which runs this same statement in a second transaction.
//
// SCOPING. `repositories` carries RLS, so this SELECT is already
// tenant-scoped before the join is considered — measured: with another
// tenant holding a row of identical `github_repo_id` AND `git_url`, RLS
// alone reduces the match set to ours. The join to `organization_id` is a
// SECOND layer, and it is what keeps adoption safe if the policy ever
// regresses. (An earlier comment here called the join "what scopes this",
// which overstated it.)
//
// ORDER BY is load-bearing, not cosmetic. A real GitHub-id match must beat
// a URL match: adopting the legacy row while an id-match exists in the same
// project makes the adopt UPDATE collide with
// idx_repositories_project_github_repo — a reachable 500.
const existingRepositoryLookupSQL = `
SELECT r.id::text, r.installation_id::text, r.github_repo_id
FROM repositories r
JOIN projects p ON p.id = r.project_id
WHERE p.organization_id = $1
  AND (r.github_repo_id = $2
       OR (r.github_repo_id IS NULL AND r.git_url = $3))
ORDER BY (r.github_repo_id IS NULL), r.created_at
LIMIT 1
FOR UPDATE OF r`

// Connect handles POST /api/repositories.
func (h *RepositoriesHandler) Connect(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()

	r.Body = http.MaxBytesReader(w, r.Body, MaxRepositoryBodyBytes)

	var req ConnectRepositoryRequest
	dec := json.NewDecoder(r.Body)
	if err := dec.Decode(&req); err != nil {
		render.Render(w, r, ErrInvalidRequest(err))
		return
	}
	// Decode reads ONE value and stops, so `{...} <<<GARBAGE>>>` was being
	// accepted as a valid body. Anything after the object means the client
	// sent something other than what it thinks it sent.
	if dec.More() {
		render.Render(w, r, ErrInvalidRequest(errors.New(
			"request body must contain exactly one JSON object")))
		return
	}
	if err := h.validate.Struct(req); err != nil {
		// Not the raw validator error — it names Go struct fields and tags.
		render.Render(w, r, ErrInvalidRequest(errors.New(
			"github_repo_id must be a positive integer and installation_id a valid UUID")))
		return
	}

	// CANONICALISE THE INSTALLATION ID BEFORE COMPARING IT TO ANYTHING.
	//
	// The `uuid` validator tag accepts uppercase hex, and Postgres accepts
	// an uppercase UUID literal, so every query below works either way.
	// What does NOT work either way is the Go string comparison that
	// classifies a relink: `installation_id::text` comes back lowercase,
	// so an uppercase request would look like a changed installation and
	// re-queue an ingest on every call. The SQL `CASE` this replaced used
	// `IS DISTINCT FROM $2::uuid`, which compared UUIDs rather than text
	// and had no such edge.
	parsedInstallation, perr := uuid.Parse(req.InstallationID)
	if perr != nil {
		render.Render(w, r, ErrInvalidRequest(errors.New(
			"installation_id must be a valid UUID")))
		return
	}
	installationID := parsedInstallation.String()

	// STEP 1 — resolve the installation INSIDE a tenant transaction.
	//
	// This is the application-layer half of the guard migration 000010
	// adds as a trigger. The scoped read means another tenant's
	// installation is invisible, so a caller naming one gets 404 rather
	// than a foreign-key success. The trigger is the backstop; this is
	// what makes the failure legible.
	var ghInstallationID int64
	err := h.scoper.InTenantTx(ctx, func(tx pgx.Tx) error {
		return tx.QueryRow(ctx,
			`SELECT github_installation_id FROM github_installations WHERE id = $1`,
			installationID).Scan(&ghInstallationID)
	})
	if errors.Is(err, pgx.ErrNoRows) {
		render.Render(w, r, ErrNotFound())
		return
	}
	if err != nil {
		render.Render(w, r, ErrInternal(fmt.Errorf("resolve installation: %w", err)))
		return
	}

	// AVAILABILITY IS CHECKED AFTER AUTHORIZATION, deliberately.
	//
	// The obvious ordering — refuse early when the App is unconfigured —
	// makes this endpoint an enumeration oracle whenever GitHub
	// credentials are missing: a caller naming a REAL installation id
	// belonging to another tenant would get 503, and a made-up one 404.
	// The difference tells them which ids exist.
	//
	// A test caught this: scenario 4 got 503 where it expected 404.
	if h.github == nil {
		render.Render(w, r, ErrServiceUnavailable(errors.New(
			"github app credentials not configured; cannot connect repositories")))
		return
	}

	// STEP 2 — ask GitHub, OUTSIDE any transaction.
	//
	// A network round-trip inside a transaction pins a pooled connection
	// for its duration. Splitting means the two steps are not atomic; the
	// upsert on (project_id, github_repo_id) is what makes a duplicate
	// connect harmless. (It used to say "the unique index on
	// (installation_id, github_repo_id)" — migration 000011 drops that
	// index, and the comment outlived it by one commit.)
	repos, err := h.github.ListInstallationRepositories(ctx, ghInstallationID)
	if err != nil {
		render.Render(w, r, ErrServiceUnavailable(fmt.Errorf("list installation repositories: %w", err)))
		return
	}

	var match *github.Repository
	for i := range repos {
		if repos[i].ID == req.GitHubRepoID {
			match = &repos[i]
			break
		}
	}
	if match == nil {
		// The installation cannot see it. Same 404 as "no such
		// installation" — a caller should not learn which repository ids
		// exist by probing.
		render.Render(w, r, ErrNotFound())
		return
	}

	// STEP 3 — persist, and decide whether this call created work.
	//
	// ISS-016 lived here. `sync_state` was the queue: a status column with
	// no owner, no lease and no attempt counter, stamped 'pending' by two
	// SQL `CASE` expressions. A relink re-queued a repository whose run was
	// still in flight, and whichever writer finished last won.
	//
	// Since 21-02 the work item is a row in `ingestion_jobs` and
	// `sync_state` is a projection of it. A relink SUPERSEDES the live job
	// and enqueues its replacement, in that order and in this transaction
	// (21-CONTEXT L4), and `idx_ingestion_jobs_one_live_per_repo` makes two
	// live jobs for one repository unrepresentable.
	var (
		created    Repository
		orgID      string
		decision   connectDecision
		superseded []string
		enqueued   []jobs.EnqueueResult
	)
	err = h.scoper.InTenantTx(ctx, func(tx pgx.Tx) error {
		// Re-read the installation inside THIS transaction rather than
		// trusting step 1: the GitHub round-trip happened in between, and
		// the installation can have been deleted since. Its own 404 rather
		// than the project lookup's, so "your installation went away"
		// and "your organization has no default project" stop sharing one
		// opaque 500.
		if ierr := tx.QueryRow(ctx,
			`SELECT organization_id::text FROM github_installations WHERE id = $1`,
			installationID).Scan(&orgID); ierr != nil {
			return fmt.Errorf("re-resolve installation: %w", ierr)
		}

		// The project comes from the caller's own default. `projects` has
		// no RLS, so it is scoped by the organization_id read above — off
		// a row RLS already proved is ours.
		var projectID string
		if perr := tx.QueryRow(ctx,
			`SELECT id::text FROM projects WHERE organization_id = $1 AND is_default`,
			orgID).Scan(&projectID); perr != nil {
			if errors.Is(perr, pgx.ErrNoRows) {
				// Not a 404: the caller did nothing wrong. Migration 000010
				// guarantees one default project per organization, so this
				// means an organization created before that ran.
				return errNoDefaultProject
			}
			return fmt.Errorf("resolve default project: %w", perr)
		}

		// Lock the existing row, if any, and read what the decision below
		// turns on.
		var (
			existingID           string
			existingInstallation *string
			existingGitHubRepoID *int64
		)
		aerr := tx.QueryRow(ctx, existingRepositoryLookupSQL, orgID, match.ID, match.CloneURL).
			Scan(&existingID, &existingInstallation, &existingGitHubRepoID)
		if aerr != nil && !errors.Is(aerr, pgx.ErrNoRows) {
			return fmt.Errorf("resolve existing repository: %w", aerr)
		}

		// CLASSIFY IN GO, NOT IN SQL.
		//
		// This used to be two `CASE` expressions buried in the writes
		// below — one in the adopt UPDATE, one in the INSERT's DO UPDATE —
		// each deciding whether to stamp the projection to `pending`. They
		// could disagree, neither reported what it had decided, and
		// "already pending" and "just re-queued" came back identical
		// because `RETURNING sync_state` says only what the column now
		// holds. The decision is a value here, it is logged, and the tests
		// can assert on it.
		switch {
		case errors.Is(aerr, pgx.ErrNoRows):
			decision = connectNew
		case existingInstallation == nil || *existingInstallation != installationID,
			existingGitHubRepoID == nil:
			// Either the App this repository is reached through changed,
			// or the row has never carried a GitHub id — a row connected
			// before this API existed, which has never been fetched AS
			// this repository. Both need a fresh full ingest.
			decision = connectRelink
		default:
			decision = connectUnchanged
		}

		if aerr == nil {
			// Adopt. `sync_state` is deliberately absent: it is a
			// projection of the job, written by pkg/jobs below.
			if uerr := tx.QueryRow(ctx, `
				UPDATE repositories SET
				  installation_id = $2,
				  github_repo_id = $3,
				  name = $4,
				  git_url = $5,
				  default_branch = $6,
				  visibility = $7,
				  size_kb = $8,
				  archived = $9,
				  updated_at = NOW()
				WHERE id = $1
				RETURNING id::text, name, git_url, default_branch, github_repo_id,
				          installation_id::text, visibility, size_kb, archived,
				          last_synced_at, created_at
			`,
				existingID, installationID, match.ID, match.Name, match.CloneURL,
				match.DefaultBranch, match.Visibility, match.SizeKB, match.Archived,
			).Scan(
				&created.ID, &created.Name, &created.GitURL, &created.DefaultBranch,
				&created.GitHubRepoID, &created.InstallationID, &created.Visibility,
				&created.SizeKB, &created.Archived,
				&created.LastSyncedAt, &created.CreatedAt,
			); uerr != nil {
				return fmt.Errorf("adopt repository: %w", uerr)
			}
		} else {
			// Upsert on (project_id, github_repo_id) — migration 000011.
			//
			// NOT on the installation: that is a credential, and it is
			// exactly what changes when the App is uninstalled and
			// reinstalled. Keying on it meant the documented recovery path
			// raised 23505 against `UNIQUE (project_id, git_url)` and
			// surfaced as a 500. Keying on GitHub's stable repository id
			// makes the same call RELINK the orphaned row instead.
			//
			// Still an upsert, not a plain insert, even though the lookup
			// above has just run: two concurrent connects can both miss
			// and both insert, and ON CONFLICT is what keeps that at one
			// row and two 201s rather than a 23505. The loser's Enqueue
			// then flags the winner's job instead of creating a second
			// one, so the pair still ends with exactly one live job.
			//
			// `sync_state` is absent from both halves and takes its column
			// default ('never_synced', migration 000010) on insert. The
			// enqueue below is what moves it to 'pending', in this same
			// transaction.
			if ierr := tx.QueryRow(ctx, `
				INSERT INTO repositories
				  (project_id, installation_id, github_repo_id, name, git_url,
				   default_branch, visibility, size_kb, archived)
				VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
				ON CONFLICT (project_id, github_repo_id)
				  WHERE github_repo_id IS NOT NULL
				DO UPDATE SET
				  installation_id = EXCLUDED.installation_id,
				  name = EXCLUDED.name,
				  git_url = EXCLUDED.git_url,
				  default_branch = EXCLUDED.default_branch,
				  visibility = EXCLUDED.visibility,
				  size_kb = EXCLUDED.size_kb,
				  archived = EXCLUDED.archived,
				  updated_at = NOW()
				RETURNING id::text, name, git_url, default_branch, github_repo_id,
				          installation_id::text, visibility, size_kb, archived,
				          last_synced_at, created_at
			`,
				projectID, installationID, match.ID, match.Name, match.CloneURL,
				match.DefaultBranch, match.Visibility, match.SizeKB, match.Archived,
			).Scan(
				&created.ID, &created.Name, &created.GitURL, &created.DefaultBranch,
				&created.GitHubRepoID, &created.InstallationID, &created.Visibility,
				&created.SizeKB, &created.Archived,
				&created.LastSyncedAt, &created.CreatedAt,
			); ierr != nil {
				return fmt.Errorf("insert repository: %w", ierr)
			}
		}

		// ⚠ SUPERSEDE, THEN ENQUEUE. In that order, in this transaction
		// (21-CONTEXT L4). Backwards raises NOTHING: the enqueue upsert
		// flags `needs_rerun` on the job that is about to leave the live
		// set, the supersede then removes it, and the repository ends with
		// no live job at all. `idx_ingestion_jobs_one_live_per_repo` —
		// `UNIQUE (repository_id) WHERE state IN ('queued','running')` —
		// is what makes two live jobs unrepresentable; it is not what
		// enforces the order, because through the upsert there is no
		// collision to raise.
		//
		// SupersedeLive is not tenant-scoped by the database. It is safe
		// here because created.ID came out of `repositories` — which is —
		// in this same transaction. See its doc comment.
		if decision == connectRelink {
			var serr error
			if superseded, serr = jobs.SupersedeLive(ctx, tx, []string{created.ID}); serr != nil {
				return fmt.Errorf("supersede live ingestion job: %w", serr)
			}
		}
		if decision == connectNew || decision == connectRelink {
			var eerr error
			if enqueued, eerr = jobs.Enqueue(ctx, tx, []jobs.EnqueueRequest{{
				OrganizationID: orgID,
				RepositoryID:   created.ID,
				JobType:        jobs.JobTypeFullIngest,
			}}); eerr != nil {
				return fmt.Errorf("enqueue ingestion job: %w", eerr)
			}
		}

		// `sync_state` is read LAST, so the response carries the value
		// this transaction is about to commit rather than the one the row
		// write returned. The producer's projection runs between the two.
		return tx.QueryRow(ctx,
			`SELECT sync_state FROM repositories WHERE id = $1`, created.ID).
			Scan(&created.SyncState)
	})
	switch {
	case errors.Is(err, pgx.ErrNoRows):
		// The installation vanished between the two transactions. Same 404
		// as "not yours" — from the caller's side nothing distinguishes them.
		render.Render(w, r, ErrNotFound())
		return
	case errors.Is(err, errNoDefaultProject):
		render.Render(w, r, ErrInternal(errNoDefaultProject))
		return
	case err != nil:
		render.Render(w, r, ErrInternal(fmt.Errorf("connect repository: %w", err)))
		return
	}

	// AFTER the commit, so nothing claims a job that was rolled back. Ids
	// only — never a token, an installation secret or a clone URL that
	// might carry one.
	logConnectOutcome(orgID, created.ID, decision, superseded, enqueued)

	render.Status(r, http.StatusCreated)
	render.Render(w, r, &created)
}

// logConnectOutcome records what a connect did to the queue.
//
// "already pending" and "just re-queued" used to be indistinguishable from
// outside this handler, because `RETURNING sync_state` reports only what
// the column holds. The decision and the producer's `was_existing` say
// which happened, and a superseded job is the one event here worth being
// able to find afterwards: it interrupted a run someone was waiting on.
func logConnectOutcome(
	orgID, repoID string,
	decision connectDecision,
	superseded []string,
	enqueued []jobs.EnqueueResult,
) {
	attrs := []any{
		slog.String("organization_id", orgID),
		slog.String("repository_id", repoID),
		slog.String("decision", string(decision)),
	}
	if len(superseded) > 0 {
		attrs = append(attrs, slog.Bool("superseded_live_job", true))
	}
	if len(enqueued) == 0 {
		slog.Info("connect repository: no ingestion job needed", attrs...)
		return
	}
	attrs = append(attrs,
		slog.String("job_id", enqueued[0].JobID),
		// true means a job was already live and has been flagged
		// needs_rerun instead — the L7 push-against-a-live-job case.
		slog.Bool("flagged_existing_job", enqueued[0].WasExisting))
	slog.Info("connect repository: ingestion job enqueued", attrs...)
}

// --- pagination helpers -------------------------------------------------

type listCursor struct {
	createdAt *time.Time
	id        *string
}

// encodeCursor packs the last row's sort key.
//
// Opaque to the client on purpose: it is a position in an ordering, not a
// stable identifier, and a client that parses it will break when the
// ordering changes.
func encodeCursor(createdAt time.Time, id string) string {
	return base64.RawURLEncoding.EncodeToString(
		[]byte(createdAt.UTC().Format(time.RFC3339Nano) + "|" + id))
}

func decodeCursor(raw string) (listCursor, error) {
	if raw == "" {
		return listCursor{}, nil
	}
	decoded, err := base64.RawURLEncoding.DecodeString(raw)
	if err != nil {
		return listCursor{}, errors.New("cursor is not valid; use the next_cursor from a previous response")
	}
	parts := strings.SplitN(string(decoded), "|", 2)
	if len(parts) != 2 {
		return listCursor{}, errors.New("cursor is not valid; use the next_cursor from a previous response")
	}
	ts, err := time.Parse(time.RFC3339Nano, parts[0])
	if err != nil {
		return listCursor{}, errors.New("cursor is not valid; use the next_cursor from a previous response")
	}
	id, ok := canonicalUUID(parts[1])
	if !ok {
		return listCursor{}, errors.New("cursor is not valid; use the next_cursor from a previous response")
	}
	return listCursor{createdAt: &ts, id: &id}, nil
}

func parsePageSize(raw string) (int, error) {
	if raw == "" {
		return defaultPageSize, nil
	}
	n, err := strconv.Atoi(raw)
	if err != nil || n < 1 {
		return 0, fmt.Errorf("limit must be a positive integer, at most %d", maxPageSize)
	}
	if n > maxPageSize {
		return 0, fmt.Errorf("limit must be at most %d", maxPageSize)
	}
	return n, nil
}

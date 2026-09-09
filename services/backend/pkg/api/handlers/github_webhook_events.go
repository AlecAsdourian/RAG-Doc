package handlers

// Event handlers for the GitHub webhook.
//
// Every one of these RECORDS INTENT. None of them fetches from GitHub and
// none of them starts work. Two reasons, and the second is the one that
// decided it:
//
//   1. Fetching inline makes webhook processing depend on GitHub being
//      reachable at delivery time — a different reliability posture than
//      recording what happened and letting a queue retry.
//   2. A goroutine started here dies with the process and takes the only
//      record of the work with it.
//
// So the output of this file is rows: `sync_state = 'pending'` on the
// repositories that need attention, and enough identity to find them
// again. Phase 21 reads that.

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"

	"github.com/jackc/pgx/v5"

	"github.com/yourusername/smart-docs-platform/services/backend/pkg/auth"
)

// handleInstallation covers created / deleted / suspend / unsuspend.
func (h *GitHubWebhookHandler) handleInstallation(
	ctx context.Context, p *githubWebhookEnvelope,
) (string, error) {
	if p.Installation == nil {
		return "ignored: no installation", nil
	}
	id := p.Installation.ID

	switch p.Action {
	case "created":
		// ORPHAN INSTALLATIONS ARE NOT ADOPTED. This is the decision the
		// plan asked to be made explicitly, and it is the same one 20-04's
		// callback makes for the same reason.
		//
		// A user can install from GitHub's directory without ever passing
		// through our install flow, so this event routinely arrives for an
		// installation that belongs to no organization. The tempting fix —
		// link it to the organization of whoever most recently did
		// something — is a cross-tenant bug: it hands one customer's GitHub
		// account to another customer, and 20-04's review demonstrated
		// exactly that attack in its non-webhook form.
		//
		// There is nothing in this payload that identifies one of OUR
		// users. `sender` is a GitHub login; we do not map those to
		// accounts, and mapping them would be an authorization decision
		// made from an unauthenticated request.
		//
		// So: if we already know this installation, refresh it. If we do
		// not, record the delivery and do nothing else. The user completes
		// the flow from inside the app, where their organization is known
		// from a verified claim, and 20-04's callback links it.
		_, orgID, known, err := h.resolveInstallation(ctx, id)
		if err != nil {
			return "", err
		}
		var updated bool
		if known {
			if updated, err = h.refreshInstallation(ctx, orgID, p); err != nil {
				return "", err
			}
		}
		if !updated {
			slog.Info("github webhook: installation created outside our flow; not adopted",
				slog.Int64("github_installation_id", id),
				slog.String("account", p.Installation.Account.Login))
			return "unlinked: awaiting in-app connect", nil
		}
		return "installation refreshed", nil

	case "deleted":
		// KEEP THE ROW AND KEEP THE REPOSITORIES.
		//
		// An uninstall means access was lost, not that the user asked us to
		// forget what we ingested — and uninstalling by accident is easy.
		// `repositories.installation_id` is ON DELETE SET NULL (000010), so
		// deleting the installation row would silently orphan every
		// repository under it and lose the link that makes reconnection
		// work.
		//
		// Repositories are marked unsyncable instead, which is the state
		// docs/api-repositories.md already tells clients to expect.
		return h.markUninstalled(ctx, id)

	case "suspend":
		return h.setSuspended(ctx, id, true)
	case "unsuspend":
		return h.setSuspended(ctx, id, false)

	case "new_permissions_accepted":
		// Nothing stored depends on the permission set today.
		return "ignored: permissions", nil
	default:
		return "ignored: " + p.Action, nil
	}
}

// refreshInstallation updates an installation we already know about,
// reporting whether it existed.
//
// Scoped by `github_installation_id`, which is UNIQUE (000010) and is the
// only identity a webhook carries. It deliberately does NOT touch
// `organization_id`: that is the tenancy link, it was established by an
// authenticated caller, and a webhook must never be able to move it.
func (h *GitHubWebhookHandler) refreshInstallation(
	ctx context.Context, orgID string, p *githubWebhookEnvelope,
) (bool, error) {
	var affected int64
	err := h.inTenant(ctx, orgID, func(tx pgx.Tx) error {
		tag, err := tx.Exec(ctx, `
			UPDATE github_installations SET
			  account_login = $2,
			  account_type = $3,
			  repository_selection = $4,
			  uninstalled_at = NULL,
			  updated_at = NOW()
			WHERE github_installation_id = $1
		`, p.Installation.ID, p.Installation.Account.Login,
			p.Installation.Account.Type, p.Installation.RepositorySelection)
		affected = tag.RowsAffected()
		return err
	})
	if err != nil {
		return false, fmt.Errorf("refresh installation: %w", err)
	}
	return affected > 0, nil
}

func (h *GitHubWebhookHandler) markUninstalled(ctx context.Context, id int64) (string, error) {
	internalID, orgID, ok, err := h.resolveInstallation(ctx, id)
	if err != nil {
		return "", err
	}
	if !ok {
		return "ignored: unknown installation", nil
	}

	var stoodDown int64
	err = h.inTenant(ctx, orgID, func(tx pgx.Tx) error {
		if _, uerr := tx.Exec(ctx, `
			UPDATE github_installations
			SET uninstalled_at = NOW(), updated_at = NOW()
			WHERE id = $1
		`, internalID); uerr != nil {
			return uerr
		}
		// The repositories keep their rows and their ingested content;
		// they simply cannot be synced until the App is reinstalled.
		// 'failed' is deliberately not used — nothing failed, and Phase 21
		// must not retry these.
		tag, rerr := tx.Exec(ctx, `
			UPDATE repositories SET sync_state = 'never_synced', updated_at = NOW()
			WHERE installation_id = $1 AND sync_state IN ('pending', 'syncing')
		`, internalID)
		stoodDown = tag.RowsAffected()
		return rerr
	})
	if err != nil {
		return "", fmt.Errorf("mark uninstalled: %w", err)
	}

	slog.Info("github webhook: installation uninstalled",
		slog.Int64("github_installation_id", id),
		slog.Int64("repositories_stood_down", stoodDown))
	return "uninstalled", nil
}

func (h *GitHubWebhookHandler) setSuspended(
	ctx context.Context, id int64, suspended bool,
) (string, error) {
	var clause string
	if suspended {
		clause = "suspended_at = NOW()"
	} else {
		clause = "suspended_at = NULL"
	}
	internalID, orgID, ok, err := h.resolveInstallation(ctx, id)
	if err != nil {
		return "", err
	}
	if !ok {
		return "ignored: unknown installation", nil
	}
	if err := h.inTenant(ctx, orgID, func(tx pgx.Tx) error {
		_, e := tx.Exec(ctx,
			"UPDATE github_installations SET "+clause+", updated_at = NOW() WHERE id = $1",
			internalID)
		return e
	}); err != nil {
		return "", fmt.Errorf("set suspended: %w", err)
	}
	if suspended {
		return "suspended", nil
	}
	return "unsuspended", nil
}

// handleInstallationRepositories covers added / removed.
func (h *GitHubWebhookHandler) handleInstallationRepositories(
	ctx context.Context, p *githubWebhookEnvelope,
) (string, error) {
	if p.Installation == nil {
		return "ignored: no installation", nil
	}

	internalID, orgID, ok, err := h.resolveInstallation(ctx, p.Installation.ID)
	if err != nil {
		return "", err
	}
	if !ok {
		return "ignored: unknown installation", nil
	}

	switch p.Action {
	case "added":
		return h.recordAddedRepositories(ctx, internalID, orgID, p.RepositoriesAdded)
	case "removed":
		// REMOVED MEANS WE LOST ACCESS, NOT THAT THE REPOSITORY IS GONE.
		// Deleting the rows would destroy ingested history because the
		// owner narrowed a permission scope — recoverable only by a full
		// re-ingest, and only if they notice.
		return h.standDownRepositories(ctx, internalID, orgID, p.RepositoriesRemoved)
	default:
		return "ignored: " + p.Action, nil
	}
}

// recordAddedRepositories notes repositories we can now see.
//
// **A webhook cannot populate a repositories row on its own** — verified
// 2026-09-08. The payload's repository shape is REDUCED: id, node_id,
// name, full_name, private, and nothing else. No `default_branch`, which
// is NOT NULL; no `size_kb`, `visibility` or `archived`.
//
// So this does not insert. It records the ids against the installation and
// leaves the connect to `POST /api/repositories`, which has an
// installation token and can fetch the full shape. Inserting a half-row
// with invented defaults would put a repository in the product that
// nobody asked to connect, and it would carry a made-up default branch.
func (h *GitHubWebhookHandler) recordAddedRepositories(
	ctx context.Context, installationID, orgID string, repos []githubWebhookRepo,
) (string, error) {
	if len(repos) == 0 {
		return "no repositories added", nil
	}

	// Existing rows for these ids DO get re-pointed at this installation
	// and re-queued: this is the "repository we already know, access
	// restored" case, and it is the one a user notices.
	ids := make([]int64, 0, len(repos))
	names := make([]string, 0, len(repos))
	for _, r := range repos {
		ids = append(ids, r.ID)
		names = append(names, r.FullName)
	}

	var affected int64
	err := h.inTenant(ctx, orgID, func(tx pgx.Tx) error {
		// RLS already restricts this to the organization's own rows; the
		// project join is the second layer, and it is what keeps the write
		// correct if the policy ever regresses.
		tag, e := tx.Exec(ctx, `
			UPDATE repositories r
			SET installation_id = $1,
			    sync_state = 'pending',
			    updated_at = NOW()
			FROM projects p
			WHERE p.id = r.project_id
			  AND p.organization_id = $2
			  AND r.github_repo_id = ANY($3::bigint[])
		`, installationID, orgID, ids)
		affected = tag.RowsAffected()
		return e
	})
	if err != nil {
		return "", fmt.Errorf("re-point known repositories: %w", err)
	}

	slog.Info("github webhook: repositories added to installation",
		slog.String("installation_id", installationID),
		slog.Int("offered", len(repos)),
		slog.Int64("already_known_requeued", affected),
		slog.String("repositories", strings.Join(names, ",")))

	return fmt.Sprintf("added: %d offered, %d already known and re-queued",
		len(repos), affected), nil
}

// standDownRepositories marks repositories we can no longer reach.
func (h *GitHubWebhookHandler) standDownRepositories(
	ctx context.Context, installationID, orgID string, repos []githubWebhookRepo,
) (string, error) {
	if len(repos) == 0 {
		return "no repositories removed", nil
	}
	ids := make([]int64, 0, len(repos))
	for _, r := range repos {
		ids = append(ids, r.ID)
	}

	// installation_id is set to NULL, matching what an uninstall does and
	// what docs/api-repositories.md already documents: the repository and
	// everything ingested from it are kept, and it cannot be re-synced
	// until access is restored.
	var affected int64
	err := h.inTenant(ctx, orgID, func(tx pgx.Tx) error {
		tag, e := tx.Exec(ctx, `
			UPDATE repositories
			SET installation_id = NULL,
			    sync_state = 'never_synced',
			    updated_at = NOW()
			WHERE installation_id = $1 AND github_repo_id = ANY($2::bigint[])
		`, installationID, ids)
		affected = tag.RowsAffected()
		return e
	})
	if err != nil {
		return "", fmt.Errorf("stand down repositories: %w", err)
	}
	return fmt.Sprintf("removed: %d repositories stood down", affected), nil
}

// handlePush marks a repository as needing a sync.
func (h *GitHubWebhookHandler) handlePush(
	ctx context.Context, p *githubWebhookEnvelope,
) (string, error) {
	if p.Repository == nil {
		return "ignored: no repository", nil
	}
	if p.Installation == nil {
		return "ignored: no installation", nil
	}

	// DEFAULT BRANCH ONLY. Ingesting every feature branch is not the
	// product, and it would multiply the ingestion cost by the number of
	// open branches. The comparison is against the branch GitHub reports
	// on the payload, not a stored value, so a repository whose default
	// branch changed is handled without us noticing the change.
	if p.Repository.DefaultBranch == "" {
		return "ignored: no default branch on payload", nil
	}
	if p.Ref != "refs/heads/"+p.Repository.DefaultBranch {
		return "ignored: not the default branch", nil
	}

	internalID, orgID, ok, err := h.resolveInstallation(ctx, p.Installation.ID)
	if err != nil {
		return "", err
	}
	if !ok {
		return "ignored: unknown installation", nil
	}

	// Only repositories we already track. A push to a repository nobody
	// connected is not work — connecting is a deliberate act.
	//
	// `syncing` is left alone: re-queueing a run already in flight is
	// ISS-016, and this is the other place it would happen.
	var affected int64
	if err := h.inTenant(ctx, orgID, func(tx pgx.Tx) error {
		tag, e := tx.Exec(ctx, `
			UPDATE repositories
			SET sync_state = 'pending', updated_at = NOW()
			WHERE installation_id = $1
			  AND github_repo_id = $2
			  AND sync_state <> 'syncing'
		`, internalID, p.Repository.ID)
		affected = tag.RowsAffected()
		return e
	}); err != nil {
		return "", fmt.Errorf("queue push: %w", err)
	}
	if affected == 0 {
		return "ignored: repository not connected", nil
	}
	return "queued", nil
}

// resolveInstallation maps GitHub's numeric id to our row.
//
// Reads `github_installations` WITHOUT a tenant scope, deliberately: the
// whole point is to discover which tenant this event belongs to, so
// scoping the lookup by the answer would be circular. It is safe because
// the only input is a numeric id GitHub signed for, and the only outputs
// are used to scope the writes that follow.
func (h *GitHubWebhookHandler) resolveInstallation(
	ctx context.Context, githubInstallationID int64,
) (internalID string, orgID string, ok bool, err error) {
	// github_installation_owner is SECURITY DEFINER (migration 000012).
	// It is the ONE sanctioned crossing of the tenant boundary in this
	// file, and it exists because the boundary is what we are trying to
	// find: `github_installations` is FORCE RLS, so a direct read here
	// returns zero rows rather than an error — verified, not assumed.
	err = h.pool.QueryRow(ctx,
		"SELECT installation_id::text, organization_id::text FROM github_installation_owner($1)",
		githubInstallationID).Scan(&internalID, &orgID)
	if errors.Is(err, pgx.ErrNoRows) {
		return "", "", false, nil
	}
	if err != nil {
		return "", "", false, fmt.Errorf("resolve installation: %w", err)
	}
	return internalID, orgID, true, nil
}

// inTenant runs fn inside a transaction scoped to org.
//
// Every write below goes through this. The organization comes from
// resolveInstallation, never from the payload — GitHub tells us which
// installation, and our own table tells us whose it is.
func (h *GitHubWebhookHandler) inTenant(
	ctx context.Context, orgID string, fn func(pgx.Tx) error,
) error {
	return h.scoper.InTenantTx(auth.ContextWithOrgID(ctx, orgID), fn)
}

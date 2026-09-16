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
// So the output of this file is WORK ITEMS: a row in `ingestion_jobs` for
// every repository a delivery says needs attention, written through
// `pkg/jobs` and nothing else. Until 21-04 the output was
// `sync_state = 'pending'` — a status column used as a queue, which is
// what ISS-016 was. `sync_state` is now a projection the producer writes
// and the UI reads; no handler in this file sets it to ask for work.
//
// THREE RULES EVERY PRODUCER CALL HERE HONOURS, all from `pkg/jobs/doc.go`:
//
//   1. SUPERSEDE BEFORE ENQUEUE, in one transaction. The reverse order
//      raises NOTHING: the upsert flags `needs_rerun` on the job that is
//      about to leave the live set, the supersede then removes it, and the
//      repository ends with no live job at all.
//   2. `jobs.SupersedeLive` TAKES THE ORGANIZATION, and that predicate is
//      the whole of its tenant scope — `ingestion_jobs` has no row-level
//      security and the UPDATE fires no tenant trigger. It is the
//      organization `resolveInstallation` returned, never a payload field:
//      `idx_repositories_project_github_repo` makes `github_repo_id`
//      unique only PER PROJECT, so a careless resolution by that id hands
//      the statement another tenant's repositories.
//   3. EVERY CALL HERE RUNS TWICE. A failed delivery is re-claimed on
//      redelivery (`claimDelivery`), so a handler is re-entrant or it is
//      wrong. The enqueue upsert is; so is every UPDATE below, which is
//      keyed rather than incremental.

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"

	"github.com/jackc/pgx/v5"

	"github.com/yourusername/smart-docs-platform/services/backend/pkg/auth"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/jobs"
)

// handleInstallation covers created / deleted / suspend / unsuspend.
func (h *GitHubWebhookHandler) handleInstallation(
	ctx context.Context, p *githubWebhookEnvelope,
) (string, *string, error) {
	if p.Installation == nil {
		return "ignored: no installation", nil, nil
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
			return "", nil, err
		}
		var updated bool
		var tenant *string
		if known {
			tenant = &orgID
			if updated, err = h.refreshInstallation(ctx, orgID, p); err != nil {
				return "", tenant, err
			}
		}
		if !updated {
			slog.Info("github webhook: installation created outside our flow; not adopted",
				slog.Int64("github_installation_id", id),
				slog.String("account", p.Installation.Account.Login))
			return "unlinked: awaiting in-app connect", tenant, nil
		}
		return "installation refreshed", tenant, nil

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
		return "ignored: permissions", nil, nil
	default:
		return "ignored: " + p.Action, nil, nil
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
			  -- A fresh created event means the App is installed and active,
			  -- so neither marker can still be true. Clearing only
			  -- uninstalled_at left a reinstalled App looking suspended.
			  uninstalled_at = NULL,
			  suspended_at = NULL,
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

func (h *GitHubWebhookHandler) markUninstalled(ctx context.Context, id int64) (string, *string, error) {
	internalID, orgID, ok, err := h.resolveInstallation(ctx, id)
	if err != nil {
		return "", nil, err
	}
	if !ok {
		return "ignored: unknown installation", nil, nil
	}
	tenant := &orgID

	var (
		stoodDown  int64
		superseded []string
		covered    int
	)
	err = h.inTenant(ctx, orgID, func(tx pgx.Tx) error {
		if _, uerr := tx.Exec(ctx, `
			UPDATE github_installations
			SET uninstalled_at = NOW(), updated_at = NOW()
			WHERE id = $1
		`, internalID); uerr != nil {
			return uerr
		}

		// Every repository under this installation, read under the tenant
		// scope this transaction already holds. A job for a repository the
		// App can no longer read will fail on its first clone anyway; the
		// point of stopping it here is that it fails PROMPTLY and knowingly
		// rather than racing the stand-down to write the final state (L4).
		rows, qerr := tx.Query(ctx,
			`SELECT id::text FROM repositories WHERE installation_id = $1 ORDER BY id`,
			internalID)
		if qerr != nil {
			return qerr
		}
		repoIDs, cerr := pgx.CollectRows(rows, pgx.RowTo[string])
		if cerr != nil {
			return cerr
		}
		covered = len(repoIDs)

		var serr error
		if superseded, serr = jobs.SupersedeLive(ctx, tx, orgID, repoIDs); serr != nil {
			return serr
		}
		// Never nil going into the UPDATE below. pgx encodes a nil slice as
		// SQL NULL, and `id = ANY(NULL::uuid[])` is NULL rather than false —
		// which happens to behave here, because NULL OR true is true, and
		// would stop behaving the moment someone reorders the predicate.
		if superseded == nil {
			superseded = []string{}
		}

		// The repositories keep their rows and their ingested content;
		// they simply cannot be synced until the App is reinstalled.
		// 'failed' is deliberately not used — nothing failed, and the queue
		// must not retry these.
		//
		// ⚠ THE `id = ANY(...)` DISJUNCT IS WHAT KEEPS THAT PROMISE TRUE
		// under 21-05's projection. A repository whose job is RETRYING
		// projects as `failed` — the state machine has no `failed` job
		// state, so "currently failing" is `queued AND attempts > 0` — and
		// the two sync states below would walk straight past it, leaving a
		// repository that looks like it is retrying when its job has just
		// been cancelled. Driving off the ids `SupersedeLive` RETURNED is
		// what covers every job it actually took out of the live set,
		// whatever the repository's projected state happened to be.
		//
		// The `pending` and `syncing` states are kept for the rows stranded
		// by the pre-21-04 webhook path, which have no job to supersede.
		tag, rerr := tx.Exec(ctx, `
			UPDATE repositories SET sync_state = 'never_synced', updated_at = NOW()
			WHERE installation_id = $1
			  AND (sync_state IN ('pending', 'syncing') OR id = ANY($2::uuid[]))
		`, internalID, superseded)
		stoodDown = tag.RowsAffected()
		return rerr
	})
	if err != nil {
		return "", tenant, fmt.Errorf("mark uninstalled: %w", err)
	}

	slog.Info("github webhook: installation uninstalled",
		slog.Int64("github_installation_id", id),
		slog.Int("repositories_covered", covered),
		slog.Int("live_jobs_superseded", len(superseded)),
		slog.Int64("repositories_stood_down", stoodDown))
	return "uninstalled", tenant, nil
}

func (h *GitHubWebhookHandler) setSuspended(
	ctx context.Context, id int64, suspended bool,
) (string, *string, error) {
	var clause string
	if suspended {
		clause = "suspended_at = NOW()"
	} else {
		clause = "suspended_at = NULL"
	}
	internalID, orgID, ok, err := h.resolveInstallation(ctx, id)
	if err != nil {
		return "", nil, err
	}
	if !ok {
		return "ignored: unknown installation", nil, nil
	}
	tenant := &orgID
	if err := h.inTenant(ctx, orgID, func(tx pgx.Tx) error {
		_, e := tx.Exec(ctx,
			"UPDATE github_installations SET "+clause+", updated_at = NOW() WHERE id = $1",
			internalID)
		return e
	}); err != nil {
		return "", tenant, fmt.Errorf("set suspended: %w", err)
	}
	if suspended {
		return "suspended", tenant, nil
	}
	return "unsuspended", tenant, nil
}

// handleInstallationRepositories covers added / removed.
func (h *GitHubWebhookHandler) handleInstallationRepositories(
	ctx context.Context, p *githubWebhookEnvelope,
) (string, *string, error) {
	if p.Installation == nil {
		return "ignored: no installation", nil, nil
	}

	internalID, orgID, ok, err := h.resolveInstallation(ctx, p.Installation.ID)
	if err != nil {
		return "", nil, err
	}
	if !ok {
		return "ignored: unknown installation", nil, nil
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
		return "ignored: " + p.Action, &orgID, nil
	}
}

// recordAddedRepositories notes repositories we can now see.
//
// **A webhook cannot populate a repositories row on its own** — verified
// 2026-09-08. The payload's repository shape is REDUCED: id, node_id,
// name, full_name, private, and nothing else. No `default_branch`, no
// `size_kb`, `visibility` or `archived`.
//
// The reason is not that the schema would refuse the insert — an earlier
// version of this comment said `default_branch` is NOT NULL and would
// stop us, and that is wrong: 000001 declares it
// `NOT NULL DEFAULT 'main' `, so a half-row would be accepted and would
// silently claim the default branch is `main`. Being accepted is what
// makes it dangerous.
//
// The real reason is a product one: connecting a repository is a
// deliberate act, and a webhook firing because someone widened a
// permission scope is not that act. So this records the ids against the
// installation and leaves the connect to `POST /api/repositories`,
// which has an installation token and can fetch the real shape.
//
// ⚠ THIS IS 21-CONTEXT L8's CASE, AND IT IS THE ONE THAT FAILED SILENTLY.
// An earlier design caught 23505 from a plain INSERT and returned success;
// three repositories racing a relink left TWO OF THE THREE NEVER QUEUED
// while the handler reported a win. One `jobs.Enqueue` over the whole set
// is what removes the failure mode rather than handling it: the upsert
// either inserts a job or flags the live one, per row, and reports which.
func (h *GitHubWebhookHandler) recordAddedRepositories(
	ctx context.Context, installationID, orgID string, repos []githubWebhookRepo,
) (string, *string, error) {
	if len(repos) == 0 {
		return "no repositories added", &orgID, nil
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

	var (
		matched    []string
		changed    []string
		superseded []string
		enqueued   []jobs.EnqueueResult
	)
	err := h.inTenant(ctx, orgID, func(tx pgx.Tx) error {
		// 1. THE ROWS WE ALREADY KNOW, AND WHAT THEIR LINK IS NOW.
		//
		// RLS already restricts this to the organization's own rows; the
		// project join is the second layer, and it is what keeps the write
		// correct if the policy ever regresses. Review found dropping it
		// survived the whole suite once.
		//
		// `FOR UPDATE OF r` — of the repositories alias, never bare. A bare
		// `FOR UPDATE` would also lock the joined `projects` row, which
		// every repository in the organization hangs off, turning a
		// per-repository lock into a per-organization one (21-03). The lock
		// is what serialises this delivery against a concurrent relink of
		// one of these repositories, so that the two do not interleave
		// between the supersede and the enqueue.
		rows, qerr := tx.Query(ctx, `
			SELECT r.id::text, r.installation_id::text
			FROM repositories r
			JOIN projects p ON p.id = r.project_id
			WHERE p.organization_id = $1
			  AND r.github_repo_id = ANY($2::bigint[])
			ORDER BY r.id
			FOR UPDATE OF r
		`, orgID, ids)
		if qerr != nil {
			return qerr
		}
		type knownRepo struct {
			ID           string
			Installation *string
		}
		known, cerr := pgx.CollectRows(rows, pgx.RowToStructByPos[knownRepo])
		if cerr != nil {
			return cerr
		}

		// 2. WHICH LINKS ACTUALLY CHANGE, decided in Go rather than in SQL.
		//
		// Only those need their in-flight job cancelled: a repository this
		// installation already covered is being re-offered, not re-pointed,
		// and its running ingest holds credentials that are still valid.
		matched = matched[:0]
		changed = changed[:0]
		for _, k := range known {
			matched = append(matched, k.ID)
			if k.Installation == nil || *k.Installation != installationID {
				changed = append(changed, k.ID)
			}
		}
		if len(matched) == 0 {
			return nil
		}

		// 3. Re-point every matched row, as before this plan — minus the
		//    `sync_state` write, which is the producer's now.
		if _, uerr := tx.Exec(ctx, `
			UPDATE repositories r
			SET installation_id = $1, updated_at = NOW()
			FROM projects p
			WHERE p.id = r.project_id
			  AND p.organization_id = $2
			  AND r.id = ANY($3::uuid[])
		`, installationID, orgID, matched); uerr != nil {
			return uerr
		}

		// 4. SUPERSEDE, THEN ENQUEUE — in that order, in this transaction.
		//    Backwards raises nothing and loses the job (pkg/jobs/doc.go).
		//    orgID is the tenant `resolveInstallation` returned and this
		//    transaction is scoped to, not a payload field.
		var serr error
		if superseded, serr = jobs.SupersedeLive(ctx, tx, orgID, changed); serr != nil {
			return serr
		}

		// 5. One Enqueue over EVERY matched row, not only the changed ones.
		//    A repository whose link did not change still had its access
		//    re-offered and is worth a fresh full ingest; if it already has
		//    a live job the upsert joins that job instead of duplicating it.
		reqs := make([]jobs.EnqueueRequest, 0, len(matched))
		for _, repoID := range matched {
			reqs = append(reqs, jobs.EnqueueRequest{
				OrganizationID: orgID,
				RepositoryID:   repoID,
				JobType:        jobs.JobTypeFullIngest,
			})
		}
		var eerr error
		enqueued, eerr = jobs.Enqueue(ctx, tx, reqs)
		return eerr
	})
	if err != nil {
		return "", &orgID, fmt.Errorf("re-point known repositories: %w", err)
	}

	queued, joined := countEnqueued(enqueued)
	slog.Info("github webhook: repositories added to installation",
		slog.String("installation_id", installationID),
		slog.Int("offered", len(repos)),
		slog.Int("already_known", len(matched)),
		slog.Int("installation_changed", len(changed)),
		slog.Int("jobs_queued", queued),
		slog.Int("joined_live_job", joined),
		slog.Int("live_jobs_superseded", len(superseded)),
		slog.String("repositories", strings.Join(names, ",")))

	return fmt.Sprintf(
		"added: %d offered, %d already known, %d queued, %d joined a live job, %d superseded",
		len(repos), len(matched), queued, joined, len(superseded)), &orgID, nil
}

// countEnqueued splits a producer result into "got a new job" and "joined
// the one that was already live".
//
// ⚠ `WasExisting` IS NOT "A RERUN IS NOW PENDING", which is why neither
// name here says rerun. It means a live job existed and this call joined
// it. If that job had not started — `queued` at `attempts = 0` — the flag
// the upsert set is cleared again inside the same transaction, because a
// job that has not read the repository yet will clone at whatever HEAD is
// current when it is claimed and so already covers the new work. See
// clearRerunOnUnstartedSQL in pkg/jobs.
func countEnqueued(results []jobs.EnqueueResult) (queued, joined int) {
	for _, r := range results {
		if r.WasExisting {
			joined++
		} else {
			queued++
		}
	}
	return queued, joined
}

// standDownRepositories marks repositories we can no longer reach.
func (h *GitHubWebhookHandler) standDownRepositories(
	ctx context.Context, installationID, orgID string, repos []githubWebhookRepo,
) (string, *string, error) {
	if len(repos) == 0 {
		return "no repositories removed", &orgID, nil
	}
	ids := make([]int64, 0, len(repos))
	for _, r := range repos {
		ids = append(ids, r.ID)
	}

	// installation_id is set to NULL: the repository and everything
	// ingested from it are kept, and it cannot be re-synced until access
	// is restored — the state docs/api-repositories.md documents.
	//
	// This is deliberately NOT what an uninstall does. An uninstall KEEPS
	// the link, because the installation row survives and a reinstall
	// relinks through it. Here the installation is still live and simply
	// no longer covers this repository, so the link is what became false.
	// (An earlier comment claimed the two behaved identically. They do
	// not, and the tests assert both.)
	var (
		stoodDown  []string
		superseded []string
	)
	err := h.inTenant(ctx, orgID, func(tx pgx.Tx) error {
		rows, e := tx.Query(ctx, `
			UPDATE repositories
			SET installation_id = NULL,
			    sync_state = 'never_synced',
			    updated_at = NOW()
			WHERE installation_id = $1 AND github_repo_id = ANY($2::bigint[])
			RETURNING id::text
		`, installationID, ids)
		if e != nil {
			return e
		}
		if stoodDown, e = pgx.CollectRows(rows, pgx.RowTo[string]); e != nil {
			return e
		}

		// STOP THE RUN IN FLIGHT (L4). A job for a repository the App can
		// no longer read fails on its first clone anyway; superseding it
		// makes that failure prompt and knowing instead of a worker racing
		// this stand-down to write the final state.
		//
		// Nothing is enqueued here, so the supersede-then-enqueue order has
		// nothing to order against — but `RETURNING id` above is still what
		// supplies the ids, rather than a second lookup by
		// `github_repo_id`, which is unique only per project.
		var serr error
		superseded, serr = jobs.SupersedeLive(ctx, tx, orgID, stoodDown)
		return serr
	})
	if err != nil {
		return "", &orgID, fmt.Errorf("stand down repositories: %w", err)
	}

	slog.Info("github webhook: repositories removed from installation",
		slog.String("installation_id", installationID),
		slog.Int("offered", len(repos)),
		slog.Int("repositories_stood_down", len(stoodDown)),
		slog.Int("live_jobs_superseded", len(superseded)))

	return fmt.Sprintf("removed: %d repositories stood down, %d live jobs superseded",
		len(stoodDown), len(superseded)), &orgID, nil
}

// handlePush marks a repository as needing a sync.
func (h *GitHubWebhookHandler) handlePush(
	ctx context.Context, p *githubWebhookEnvelope,
) (string, *string, error) {
	if p.Repository == nil {
		return "ignored: no repository", nil, nil
	}
	if p.Installation == nil {
		return "ignored: no installation", nil, nil
	}

	// DEFAULT BRANCH ONLY. Ingesting every feature branch is not the
	// product, and it would multiply the ingestion cost by the number of
	// open branches. The comparison is against the branch GitHub reports
	// on the payload, not a stored value, so a repository whose default
	// branch changed is handled without us noticing the change.
	if p.Repository.DefaultBranch == "" {
		return "ignored: no default branch on payload", nil, nil
	}
	if p.Ref != "refs/heads/"+p.Repository.DefaultBranch {
		return "ignored: not the default branch", nil, nil
	}

	internalID, orgID, ok, err := h.resolveInstallation(ctx, p.Installation.ID)
	if err != nil {
		return "", nil, err
	}
	if !ok {
		return "ignored: unknown installation", nil, nil
	}

	// Only repositories we already track. A push to a repository nobody
	// connected is not work — connecting is a deliberate act.
	//
	// ⚠ THE `sync_state <> 'syncing'` GUARD IS GONE, AND SO IS THE WRITE IT
	// GUARDED. It was this file's half of ISS-016: with `sync_state` as the
	// queue, stamping `pending` over `syncing` gave a repository two
	// writers, and refusing to stamp it dropped the push instead. Neither
	// is needed now. The enqueue upsert is a third answer (L7): if a job is
	// already live the push JOINS it rather than queueing a second one,
	// which the partial unique index would refuse anyway, and the live job
	// picks the new commits up — either because it has not started and will
	// clone at the current HEAD, or because it is running and `needs_rerun`
	// makes 21-05 re-queue it once on completion.
	var (
		repoIDs  []string
		enqueued []jobs.EnqueueResult
	)
	if err := h.inTenant(ctx, orgID, func(tx pgx.Tx) error {
		rows, e := tx.Query(ctx, `
			SELECT id::text FROM repositories
			WHERE installation_id = $1 AND github_repo_id = $2
			ORDER BY id
		`, internalID, p.Repository.ID)
		if e != nil {
			return e
		}
		if repoIDs, e = pgx.CollectRows(rows, pgx.RowTo[string]); e != nil {
			return e
		}
		if len(repoIDs) == 0 {
			return nil
		}

		// An INCREMENTAL job: a push changed part of a repository we have
		// already seen. Nothing supersedes here — a push does not
		// invalidate the run in flight, it adds to it.
		reqs := make([]jobs.EnqueueRequest, 0, len(repoIDs))
		for _, repoID := range repoIDs {
			reqs = append(reqs, jobs.EnqueueRequest{
				OrganizationID: orgID,
				RepositoryID:   repoID,
				JobType:        jobs.JobTypeIncremental,
			})
		}
		var eerr error
		enqueued, eerr = jobs.Enqueue(ctx, tx, reqs)
		return eerr
	}); err != nil {
		return "", &orgID, fmt.Errorf("queue push: %w", err)
	}
	if len(repoIDs) == 0 {
		return "ignored: repository not connected", &orgID, nil
	}

	queued, joined := countEnqueued(enqueued)
	slog.Info("github webhook: push queued",
		slog.String("organization_id", orgID),
		slog.String("repositories", strings.Join(repoIDs, ",")),
		slog.Int("jobs_queued", queued),
		slog.Int("joined_live_job", joined))

	// ⚠ THE OUTCOME DOES NOT SAY "RERUN FLAGGED", deliberately. The plan
	// offered that wording for the `WasExisting` case and it would be
	// wrong: a live job at `queued` / `attempts = 0` has its flag cleared
	// again inside the producer's own transaction, so for the commonest
	// shape of this case there is no rerun pending at all. Reading the row
	// back to tell the two apart would buy a distinction that changes
	// nothing an operator does — either way the push is covered by the job
	// that is already live, and that is what this says.
	if queued == 0 {
		return "joined the live job", &orgID, nil
	}
	return "queued", &orgID, nil
}

// resolveInstallation maps GitHub's numeric id to our row.
//
// Reads the MIRROR — `github_installation_tenants` — without a tenant
// scope, deliberately: the whole point is to discover which tenant this
// event belongs to, so scoping the lookup by the answer would be
// circular. It is safe because the only input is a numeric id GitHub
// signed for, and the only outputs are used to scope the writes that
// follow.
//
// (This line said `github_installations` until review caught it — in the
// one function whose reading-the-wrong-table behaviour was the original
// blocker on this PR.)
func (h *GitHubWebhookHandler) resolveInstallation(
	ctx context.Context, githubInstallationID int64,
) (internalID string, orgID string, ok bool, err error) {
	// github_installation_tenants is the discovery index (migration
	// 000012): an ordinary table with no RLS, maintained by a trigger on
	// github_installations so it cannot drift.
	//
	// It exists because the tenant boundary is what we are trying to find.
	// github_installations is FORCE RLS, so a direct read here returns
	// zero rows — verified under a NOSUPERUSER NOBYPASSRLS owner, which is
	// the deployment shape this repo documents. An earlier version used a
	// SECURITY DEFINER function and was verified only in the test harness,
	// where migrations run as a superuser; in production it would have
	// returned nothing and the receiver would have 202'd every event while
	// doing nothing.
	err = h.pool.QueryRow(ctx,
		"SELECT installation_id::text, organization_id::text "+
			"FROM github_installation_tenants WHERE github_installation_id = $1",
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

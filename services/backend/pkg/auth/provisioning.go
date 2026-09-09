package auth

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type UserProvisioner struct {
	db *pgxpool.Pool
}

func NewUserProvisioner(db *pgxpool.Pool) *UserProvisioner {
	return &UserProvisioner{db: db}
}

type User struct {
	ID             uuid.UUID `json:"id"`
	SupabaseUserID uuid.UUID `json:"supabase_user_id"`
	Email          string    `json:"email"`
	FullName       string    `json:"full_name"`
}

// maxSlugCollisionRetries bounds how many times CreateOrganizationForUser
// re-rolls a slug suffix when the deterministic base collides. Two users
// with the same email local part (alice@a.com, alice@b.com) both generate
// "alice-org"; a retry with random hex makes a second collision
// astronomically unlikely.
const maxSlugCollisionRetries = 3

// ErrEmailOwnedByAnotherIdentity is returned by ProvisionOAuthUser when
// the incoming address is already registered to a DIFFERENT Supabase
// user id. Callers must not treat the situation as a replay — mapping
// the new identity onto the existing row would hand it someone else's
// account and organization.
var ErrEmailOwnedByAnotherIdentity = errors.New("email registered to a different supabase identity")

// ProvisionOAuthUser inserts the user identified by supabaseUserID, or
// returns the existing row if a prior webhook already provisioned them.
//
// Returns (user, created, error). `created` is false when the insert hit
// a uniqueness conflict — that's the replay signal the webhook uses to
// skip organization creation.
//
// supabaseUserID is the caller's Supabase Auth UUID and becomes the
// `users.supabase_user_id` column verbatim. An earlier version of this
// function accepted the parameter and then ignored it, generating a
// fresh `uuid.New()` instead — meaning our users row never matched the
// real Supabase identity, and any lookup keyed on the JWT `sub` claim
// (as Phase 19-03's Auth Hook does) would find nothing. That is fixed
// here; the parameter is now load-bearing.
//
// The `provider` parameter is retained for call-site clarity and future
// per-provider handling but is not persisted — the schema has no
// provider column today.
func (p *UserProvisioner) ProvisionOAuthUser(
	ctx context.Context,
	provider, email, fullName, supabaseUserID string,
) (*User, bool, error) {
	parsedID, err := uuid.Parse(supabaseUserID)
	if err != nil {
		return nil, false, fmt.Errorf("invalid supabase user id %q: %w", supabaseUserID, err)
	}

	var user User

	// Bare ON CONFLICT DO NOTHING (no column list) catches a conflict on
	// EITHER unique constraint — supabase_user_id or email. Naming only
	// supabase_user_id would let an email collision surface as a raw
	// constraint error instead of the replay path.
	err = p.db.QueryRow(ctx, `
		INSERT INTO users (supabase_user_id, email, full_name)
		VALUES ($1, $2, $3)
		ON CONFLICT DO NOTHING
		RETURNING id, supabase_user_id, email, full_name
	`, parsedID, email, fullName).Scan(
		&user.ID, &user.SupabaseUserID, &user.Email, &user.FullName,
	)
	if err == nil {
		return &user, true, nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return nil, false, fmt.Errorf("failed to create user: %w", err)
	}

	// Conflict. Fetch the existing row, preferring a supabase_user_id
	// match (the stable Supabase identity) over an email match — a user
	// who changed their email in Supabase should resolve to their
	// original row, not a stranger who since claimed the address.
	err = p.db.QueryRow(ctx, `
		SELECT id, supabase_user_id, email, full_name
		FROM users
		WHERE supabase_user_id = $1 OR email = $2
		ORDER BY (supabase_user_id = $1) DESC
		LIMIT 1
	`, parsedID, email).Scan(
		&user.ID, &user.SupabaseUserID, &user.Email, &user.FullName,
	)
	if err != nil {
		return nil, false, fmt.Errorf("failed to fetch existing user after conflict: %w", err)
	}

	// The conflict may have been on email rather than supabase_user_id —
	// i.e. a DIFFERENT Supabase identity already owns this address (they
	// deleted and re-created their Supabase account, or an address was
	// recycled). Returning that row would hand the new identity someone
	// else's account and organization. Refuse instead of silently
	// aliasing the two.
	if user.SupabaseUserID != parsedID {
		return nil, false, fmt.Errorf(
			"%w: address %q is registered to supabase user %s, not %s",
			ErrEmailOwnedByAnotherIdentity, email, user.SupabaseUserID, parsedID,
		)
	}

	return &user, false, nil
}

// UserHasOwnerOrg reports whether userID already owns an organization.
//
// Used by the webhook to decide whether a starter org still needs
// creating. Gating that on "did we just create the user row" is wrong:
// if org creation fails after the user row commits, any later run sees an
// existing user, skips org creation, and returns 202 — leaving the user
// permanently organization-less.
func (p *UserProvisioner) UserHasOwnerOrg(ctx context.Context, userID uuid.UUID) (bool, error) {
	_, found, err := p.UserOwnerOrgID(ctx, userID)
	return found, err
}

// OwnerOrgAssignment pairs a user's Supabase identity with the
// organization they own. It is what an org-context push needs and nothing
// more.
type OwnerOrgAssignment struct {
	UserID         uuid.UUID
	SupabaseUserID uuid.UUID
	Email          string
	OrganizationID uuid.UUID
}

// ListOwnerOrgAssignments returns every user who owns an organization,
// oldest membership first.
//
// This exists for `cmd/backfill-org-claims`, which is the ONLY repair path
// for a user whose org-context push failed. Supabase database webhooks
// fire once and never retry, and the trigger behind them is AFTER INSERT
// on auth.users, so a user gets exactly one automatic attempt at an
// organization claim in their entire lifetime. When that attempt fails —
// a transient 5xx from the admin API, a network blip, a deploy without
// SUPABASE_SERVICE_ROLE_KEY set — nothing in the request path repairs it
// and the user is locked out of every tenant-scoped route with a 403.
//
// Enumerating from OUR database rather than from Supabase is deliberate:
// our database is the source of truth for org ownership, and this
// project's Supabase admin READ endpoints currently return 500 anyway.
//
// The same query doubles as the migration step for users provisioned
// before the claim existed at all.
func (p *UserProvisioner) ListOwnerOrgAssignments(ctx context.Context) ([]OwnerOrgAssignment, error) {
	rows, err := p.db.Query(ctx, `
		SELECT DISTINCT ON (u.id)
		       u.id, u.supabase_user_id, u.email, om.organization_id
		FROM users u
		JOIN organization_memberships om ON om.user_id = u.id
		WHERE om.role = 'owner'
		ORDER BY u.id, om.created_at ASC
	`)
	if err != nil {
		return nil, fmt.Errorf("list owner org assignments: %w", err)
	}
	defer rows.Close()

	var out []OwnerOrgAssignment
	for rows.Next() {
		var a OwnerOrgAssignment
		if err := rows.Scan(&a.UserID, &a.SupabaseUserID, &a.Email, &a.OrganizationID); err != nil {
			return nil, fmt.Errorf("scan owner org assignment: %w", err)
		}
		out = append(out, a)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate owner org assignments: %w", err)
	}
	return out, nil
}

// UserOwnerOrgID returns the organization userID owns, if any.
//
// The webhook needs the id on BOTH the fresh-provision and replay paths:
// org context must be pushed to Supabase whenever the handler runs, not
// only when the org was just created, because a prior run may have created
// the org and then failed the Supabase push. Returning the id rather than
// a bare boolean lets the handler do that without a second query.
//
// If a user somehow owns more than one organization, the oldest wins.
// That's the one provisioning created, and it keeps the choice
// deterministic across runs rather than flapping between orgs — which
// matters for backfill-org-claims, whose whole job is to be re-runnable.
func (p *UserProvisioner) UserOwnerOrgID(ctx context.Context, userID uuid.UUID) (uuid.UUID, bool, error) {
	var orgID uuid.UUID
	err := p.db.QueryRow(ctx, `
		SELECT organization_id
		FROM organization_memberships
		WHERE user_id = $1 AND role = 'owner'
		ORDER BY created_at ASC
		LIMIT 1
	`, userID).Scan(&orgID)
	if errors.Is(err, pgx.ErrNoRows) {
		return uuid.Nil, false, nil
	}
	if err != nil {
		return uuid.Nil, false, fmt.Errorf("look up owner org: %w", err)
	}
	return orgID, true, nil
}

// CreateOrganizationForUser creates an organization and makes userID its
// owner, in one transaction. Returns the organization id.
//
// The org and the membership are committed together — a prior version
// ran them as two independent statements, so a failed membership insert
// left an orphaned organization with no members and no way to reach it.
//
// orgSlug is a deterministic base derived from the user's email (see
// generateOrgSlugFromEmail). Because two users can share an email local
// part, the base can collide; on conflict this retries with a random
// hex suffix.
func (p *UserProvisioner) CreateOrganizationForUser(
	ctx context.Context,
	userID uuid.UUID,
	orgName, orgSlug string,
) (uuid.UUID, error) {
	tx, err := p.db.Begin(ctx)
	if err != nil {
		return uuid.Nil, fmt.Errorf("begin org creation tx: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()

	var orgID uuid.UUID
	slug := orgSlug

	for attempt := 0; attempt < maxSlugCollisionRetries; attempt++ {
		err = tx.QueryRow(ctx, `
			INSERT INTO organizations (name, slug)
			VALUES ($1, $2)
			ON CONFLICT (slug) DO NOTHING
			RETURNING id
		`, orgName, slug).Scan(&orgID)

		if err == nil {
			break
		}
		if !errors.Is(err, pgx.ErrNoRows) {
			return uuid.Nil, fmt.Errorf("failed to create organization: %w", err)
		}
		// Slug taken. ON CONFLICT DO NOTHING does not poison the tx, so
		// we can retry inline with a fresh suffix.
		suffix, sErr := randomHex(3)
		if sErr != nil {
			return uuid.Nil, fmt.Errorf("generate slug suffix: %w", sErr)
		}
		slug = orgSlug + "-" + suffix
		orgID = uuid.Nil
	}

	if orgID == uuid.Nil {
		return uuid.Nil, fmt.Errorf(
			"failed to create organization: slug %q still colliding after %d attempts",
			orgSlug, maxSlugCollisionRetries,
		)
	}

	// Idempotent membership insert — a replayed webhook that somehow
	// reaches this path must not fail on the (user_id, organization_id)
	// unique constraint.
	_, err = tx.Exec(ctx, `
		INSERT INTO organization_memberships (user_id, organization_id, role)
		VALUES ($1, $2, 'owner')
		ON CONFLICT (user_id, organization_id) DO NOTHING
	`, userID, orgID)
	if err != nil {
		return uuid.Nil, fmt.Errorf("failed to add user to organization: %w", err)
	}

	// Default project, in the same transaction as the organization.
	//
	// `repositories.project_id` is NOT NULL, so without this a freshly
	// provisioned user has an organization they cannot connect a
	// repository to. Nothing in production created a project before
	// 20-02 — the only inserts were test helpers — which is why
	// migration 000010 also backfills one for every existing
	// organization.
	//
	// In this transaction rather than a follow-up write, for the same
	// reason the membership is: an organization that exists without its
	// project is a state no code handles, and 19-02 already learned what
	// a partially-created organization costs.
	//
	// ON CONFLICT DO NOTHING for replay safety. The partial unique index
	// idx_projects_one_default_per_org guarantees at most one default per
	// organization, so a retry cannot produce a second.
	_, err = tx.Exec(ctx, `
		INSERT INTO projects (organization_id, name, slug, is_default)
		VALUES ($1, 'Default', 'default', true)
		ON CONFLICT (organization_id, slug) DO NOTHING
	`, orgID)
	if err != nil {
		return uuid.Nil, fmt.Errorf("failed to create default project: %w", err)
	}

	if err := tx.Commit(ctx); err != nil {
		return uuid.Nil, fmt.Errorf("commit org creation: %w", err)
	}

	return orgID, nil
}

// DefaultProjectID returns the organization's default project.
//
// 20-03's `POST /api/repositories` needs it: the caller names a
// repository and an installation, not a project, so the handler resolves
// the project itself.
//
// Every organization has exactly one, guaranteed by
// idx_projects_one_default_per_org plus creation in
// CreateOrganizationForUser and the 000010 backfill. A missing default is
// therefore a data-integrity problem rather than a normal absence, and is
// reported as an error rather than a zero value.
func (p *UserProvisioner) DefaultProjectID(ctx context.Context, orgID uuid.UUID) (uuid.UUID, error) {
	var projectID uuid.UUID
	err := p.db.QueryRow(ctx, `
		SELECT id FROM projects
		WHERE organization_id = $1 AND is_default
	`, orgID).Scan(&projectID)

	if errors.Is(err, pgx.ErrNoRows) {
		return uuid.Nil, fmt.Errorf(
			"organization %s has no default project; migration 000010 backfills one for "+
				"every organization and CreateOrganizationForUser creates one for each new "+
				"organization, so this means neither ran for it", orgID)
	}
	if err != nil {
		return uuid.Nil, fmt.Errorf("look up default project: %w", err)
	}
	return projectID, nil
}

// randomHex returns n random bytes hex-encoded (2n characters).
func randomHex(n int) (string, error) {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return hex.EncodeToString(b), nil
}

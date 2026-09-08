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
// if org creation fails after the user row commits, the Supabase retry
// sees an existing user, skips org creation, and returns 202 — leaving
// the user permanently organization-less with no further retries.
func (p *UserProvisioner) UserHasOwnerOrg(ctx context.Context, userID uuid.UUID) (bool, error) {
	var exists bool
	err := p.db.QueryRow(ctx, `
		SELECT EXISTS (
			SELECT 1 FROM organization_memberships
			WHERE user_id = $1 AND role = 'owner'
		)
	`, userID).Scan(&exists)
	if err != nil {
		return false, fmt.Errorf("check existing owner org: %w", err)
	}
	return exists, nil
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

	if err := tx.Commit(ctx); err != nil {
		return uuid.Nil, fmt.Errorf("commit org creation: %w", err)
	}

	return orgID, nil
}

// randomHex returns n random bytes hex-encoded (2n characters).
func randomHex(n int) (string, error) {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return hex.EncodeToString(b), nil
}

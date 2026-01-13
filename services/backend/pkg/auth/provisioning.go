package auth

import (
	"context"
	"fmt"

	"github.com/google/uuid"
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

// ProvisionOAuthUser creates or updates user from OAuth provider
func (p *UserProvisioner) ProvisionOAuthUser(ctx context.Context, provider, email, fullName, providerUserID string) (*User, bool, error) {
	// Check if user exists by email
	var user User
	err := p.db.QueryRow(ctx,
		`SELECT id, supabase_user_id, email, full_name FROM users WHERE email = $1`,
		email,
	).Scan(&user.ID, &user.SupabaseUserID, &user.Email, &user.FullName)

	if err == nil {
		// User exists, return
		return &user, false, nil
	}

	// User doesn't exist, create new user
	// Generate new UUID for supabase_user_id (temporary - will be real Supabase ID later)
	supabaseUserID := uuid.New()

	err = p.db.QueryRow(ctx,
		`INSERT INTO users (supabase_user_id, email, full_name)
         VALUES ($1, $2, $3)
         RETURNING id, supabase_user_id, email, full_name`,
		supabaseUserID, email, fullName,
	).Scan(&user.ID, &user.SupabaseUserID, &user.Email, &user.FullName)

	if err != nil {
		return nil, false, fmt.Errorf("failed to create user: %w", err)
	}

	return &user, true, nil
}

// CreateOrganizationForUser creates org and adds user as owner
func (p *UserProvisioner) CreateOrganizationForUser(ctx context.Context, userID uuid.UUID, orgName, orgSlug string) (uuid.UUID, error) {
	var orgID uuid.UUID

	// Create organization
	err := p.db.QueryRow(ctx,
		`INSERT INTO organizations (name, slug)
         VALUES ($1, $2)
         RETURNING id`,
		orgName, orgSlug,
	).Scan(&orgID)

	if err != nil {
		return uuid.Nil, fmt.Errorf("failed to create organization: %w", err)
	}

	// Add user as owner
	_, err = p.db.Exec(ctx,
		`INSERT INTO organization_memberships (user_id, organization_id, role)
         VALUES ($1, $2, 'owner')`,
		userID, orgID,
	)

	if err != nil {
		return uuid.Nil, fmt.Errorf("failed to add user to organization: %w", err)
	}

	return orgID, nil
}

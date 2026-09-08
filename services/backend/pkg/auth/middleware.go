package auth

import (
	"context"
	"net/http"
	"strings"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/lestrrat-go/jwx/v3/jwt"
)

type contextKey string

const (
	UserIDKey  contextKey = "user_id"
	OrgIDKey   contextKey = "org_id"
	OrgRoleKey contextKey = "org_role"
	// TokenKey carries the validated jwt.Token so middleware downstream of
	// JWTAuthMiddleware can read claims without re-parsing or re-verifying.
	TokenKey contextKey = "jwt_token"
)

// JWTAuthMiddleware validates JWT and extracts user_id.
//
// Takes a TokenValidator interface so tests can inject an HS256 validator
// while production uses *JWTValidator against Supabase JWKS.
func JWTAuthMiddleware(validator TokenValidator) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			// Extract Bearer token from Authorization header
			authHeader := r.Header.Get("Authorization")
			if authHeader == "" {
				http.Error(w, "Missing authorization header", http.StatusUnauthorized)
				return
			}

			parts := strings.SplitN(authHeader, " ", 2)
			if len(parts) != 2 || parts[0] != "Bearer" {
				http.Error(w, "Invalid authorization header format", http.StatusUnauthorized)
				return
			}

			tokenString := parts[1]

			// Validate JWT
			token, err := validator.ValidateToken(r.Context(), tokenString)
			if err != nil {
				http.Error(w, "Invalid token", http.StatusUnauthorized)
				return
			}

			// Extract user_id from subject
			userID, err := ExtractUserID(token)
			if err != nil {
				http.Error(w, "Invalid token claims", http.StatusUnauthorized)
				return
			}

			// Stash both the user id and the whole validated token.
			// TenantMiddleware reads organization claims off the token, and
			// re-parsing there would mean validating the signature twice.
			ctx := context.WithValue(r.Context(), UserIDKey, userID)
			ctx = context.WithValue(ctx, TokenKey, token)

			next.ServeHTTP(w, r.WithContext(ctx))
		})
	}
}

// TenantMiddleware reads the caller's organization out of the validated
// JWT and stores it on the request context for downstream handlers.
//
// Tenant identity comes exclusively from `app_metadata.organization_id`,
// a Supabase-issued, signature-verified claim. There is deliberately NO
// header fallback: until Phase 19-03 this function read
// `X-Organization-ID`, which any authenticated caller could set to any
// value — meaning a valid login for one organization could read another
// organization's data. That path is gone, not deprecated.
//
// The claim is trusted wholesale rather than re-checked against
// organization_memberships on every request. The membership check happens
// where the claim is WRITTEN (the webhook's org-context push, and 19-04's
// select-organization endpoint), and only something holding Supabase's
// signing key can mint a token at all. Re-querying per request would add
// a database round-trip to the hot path to defend against an attacker who,
// by construction, would already have to control token issuance.
//
// Note on the db argument: earlier drafts of this middleware tried to
// SET LOCAL app.current_tenant on a pool-acquired connection here. That
// was broken twice over — SET LOCAL outside an explicit transaction is a
// no-op, and pgx's extended protocol rejects parameterized SET — so it
// crashed every request with 500. It has been removed. RLS-scoped queries
// must open their own transaction via isolation.TenantScope (or a Phase
// 17-03 request-scoped-tx equivalent); the pool argument is kept so a
// future request-tx design can wire itself in without a middleware-chain
// signature change.
func TenantMiddleware(db *pgxpool.Pool) func(http.Handler) http.Handler {
	_ = db // TODO(17-03/ISS-008): wire request-scoped tenant tx here
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			token, ok := r.Context().Value(TokenKey).(jwt.Token)
			if !ok {
				// JWTAuthMiddleware must run first; if it didn't, that's a
				// wiring bug rather than anything the caller did.
				http.Error(w, "Missing token context", http.StatusInternalServerError)
				return
			}

			orgID, err := ExtractOrganizationID(token)
			if err != nil {
				http.Error(w, "No active organization for this user", http.StatusForbidden)
				return
			}

			// Role is advisory today — no handler gates on it yet — so a
			// token carrying an org but no role is allowed through with an
			// empty role rather than being refused outright.
			role, _ := ExtractOrganizationRole(token)

			ctx := context.WithValue(r.Context(), OrgIDKey, orgID)
			ctx = context.WithValue(ctx, OrgRoleKey, role)
			next.ServeHTTP(w, r.WithContext(ctx))
		})
	}
}

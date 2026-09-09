package auth

import (
	"context"
	"net/http"
	"strings"

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
// This middleware does NOT open a database transaction, and takes no pool.
//
// It did once, briefly: an early draft called SET LOCAL app.current_tenant
// on a pool-acquired connection here, which was broken twice over — SET
// LOCAL outside an explicit transaction is a no-op, and pgx's extended
// protocol rejects a parameterized SET — and crashed every request with a
// 500. After that it kept an unused `db *pgxpool.Pool` parameter reserving
// the spot for ISS-008.
//
// ISS-008 was resolved in 20-01, and NOT here. Scoping every authenticated
// request would hold a pooled connection and an open transaction for the
// life of the request, including `/api/chat/stream`, whose life is
// measured in minutes — and including the majority of requests, which
// never touch the database at all. Handlers that read tenant-scoped tables
// use db.TenantScoper instead; see 20-01-DESIGN.md for the full
// comparison, and docs/isolation.md for which handlers need it.
//
// The parameter is gone rather than ignored. An unused pool argument is an
// invitation to wire something into the wrong layer.
func TenantMiddleware() func(http.Handler) http.Handler {
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

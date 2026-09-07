package auth

import (
	"context"
	"net/http"
	"strings"

	"github.com/jackc/pgx/v5/pgxpool"
)

type contextKey string

const (
	UserIDKey contextKey = "user_id"
	OrgIDKey  contextKey = "org_id"
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

			// Add to context
			ctx := context.WithValue(r.Context(), UserIDKey, userID)

			next.ServeHTTP(w, r.WithContext(ctx))
		})
	}
}

// TenantMiddleware extracts the caller's organization and stores it on the
// request context under OrgIDKey so downstream handlers can propagate it
// (currently to the RAG client; later, to per-request DB transactions).
//
// The tenant id comes from the X-Organization-ID header today. Phase 19-03
// moves the source-of-truth to a JWT custom claim + membership check; that
// change is scoped to this function's header-read block.
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
	_ = db // reserved for Phase 17-03 request-scoped transaction hookup
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			_, ok := r.Context().Value(UserIDKey).(string)
			if !ok {
				http.Error(w, "Missing user context", http.StatusInternalServerError)
				return
			}

			orgID := r.Header.Get("X-Organization-ID")
			if orgID == "" {
				http.Error(w, "Missing organization context", http.StatusBadRequest)
				return
			}

			ctx := context.WithValue(r.Context(), OrgIDKey, orgID)
			next.ServeHTTP(w, r.WithContext(ctx))
		})
	}
}

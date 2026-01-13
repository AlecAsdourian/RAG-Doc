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

// JWTAuthMiddleware validates JWT and extracts user_id
func JWTAuthMiddleware(validator *JWTValidator) func(http.Handler) http.Handler {
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

// TenantMiddleware extracts organization and sets RLS context
func TenantMiddleware(db *pgxpool.Pool) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			_, ok := r.Context().Value(UserIDKey).(string)
			if !ok {
				http.Error(w, "Missing user context", http.StatusInternalServerError)
				return
			}

			// Get organization from JWT custom claims
			// (Alternative: query organization_memberships table)
			// For now, require it in header (temporary - will be in JWT later)
			// TODO: Implement organization selection for multi-org users
			// TODO: Use userID to query organization_memberships and validate access

			orgID := r.Header.Get("X-Organization-ID") // Temporary: from header
			if orgID == "" {
				http.Error(w, "Missing organization context", http.StatusBadRequest)
				return
			}

			ctx := context.WithValue(r.Context(), OrgIDKey, orgID)

			// Set PostgreSQL RLS context (CRITICAL for tenant isolation)
			conn, err := db.Acquire(ctx)
			if err != nil {
				http.Error(w, "Database error", http.StatusInternalServerError)
				return
			}
			defer conn.Release()

			// SET LOCAL ensures variable is cleared at end of transaction
			_, err = conn.Exec(ctx, "SET LOCAL app.current_tenant = $1", orgID)
			if err != nil {
				http.Error(w, "Failed to set tenant context", http.StatusInternalServerError)
				return
			}

			next.ServeHTTP(w, r.WithContext(ctx))
		})
	}
}

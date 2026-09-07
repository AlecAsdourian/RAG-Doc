package api

import (
	"log/slog"
	"net/http"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
	"github.com/go-chi/httplog/v2"
	"github.com/go-chi/render"
	"github.com/go-playground/validator/v10"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/api/handlers"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/auth"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/client"
)

// Config holds router configuration
type Config struct {
	LogJSON  bool
	LogLevel slog.Level
}

// NewRouter creates a Chi router with middleware chain and route groups.
//
// Sugar over NewRouterWithValidator that builds the production Supabase
// JWKS validator from authConfig. Tests that need to bypass Supabase
// should call NewRouterWithValidator directly with a test validator.
func NewRouter(dbpool *pgxpool.Pool, ragClient *client.RAGClient, authConfig *auth.Config, cfg Config) chi.Router {
	return NewRouterWithValidator(dbpool, ragClient, auth.NewJWTValidator(authConfig), cfg)
}

// NewRouterWithValidator builds the router with a caller-supplied token
// validator. Same middleware chain and routes as NewRouter; only the
// bearer-token verifier is swappable.
func NewRouterWithValidator(dbpool *pgxpool.Pool, ragClient *client.RAGClient, jwtValidator auth.TokenValidator, cfg Config) chi.Router {
	// Initialize structured logger
	logger := httplog.NewLogger("smart-docs-api", httplog.Options{
		JSON:            cfg.LogJSON,
		LogLevel:        cfg.LogLevel,
		Concise:         true,
		RequestHeaders:  true,
		ResponseHeaders: false,
	})

	// Initialize webhook handler
	webhookHandler := auth.NewWebhookHandler(dbpool)

	// Initialize request validator
	validate := validator.New()

	// Initialize search handler
	searchHandler := handlers.NewSearchHandler(ragClient, validate)

	// Initialize chat handler
	chatHandler := handlers.NewChatHandler(ragClient, validate)

	r := chi.NewRouter()

	// Middleware chain - order matters!
	// 1. Request ID (first - generates correlation ID)
	r.Use(middleware.RequestID)
	// 2. Logger (logs start/end with request ID)
	r.Use(httplog.RequestLogger(logger))
	// 3. Recoverer (catches panics, logs them)
	r.Use(middleware.Recoverer)
	// 4. Real IP (handles X-Forwarded-For)
	r.Use(middleware.RealIP)
	// 5. CORS (handle preflight and headers)
	r.Use(corsMiddleware)

	// Note: Do NOT apply middleware.Timeout on main router
	// SSE endpoints need no timeout - they manage their own lifecycle

	// Public routes (no auth required)
	r.Get("/health", healthHandler)
	r.Post("/webhooks/supabase", webhookHandler.HandleSupabaseWebhook())

	// Protected routes (JWT auth + tenant isolation)
	r.Group(func(r chi.Router) {
		// JWT authentication middleware
		r.Use(auth.JWTAuthMiddleware(jwtValidator))
		// Tenant isolation middleware
		r.Use(auth.TenantMiddleware(dbpool))

		// Apply timeout to non-streaming routes only
		r.With(middleware.Timeout(60*time.Second)).Route("/api", func(r chi.Router) {
			// Search endpoint
			r.Post("/search", searchHandler.Search)
		})

		// SSE streaming route - no timeout middleware (streams are long-lived)
		r.Post("/api/chat/stream", chatHandler.StreamChat)
	})

	return r
}

// healthHandler returns a simple health check response
func healthHandler(w http.ResponseWriter, r *http.Request) {
	render.JSON(w, r, map[string]string{
		"status":  "ok",
		"service": "backend-api",
	})
}

// corsMiddleware adds CORS headers to allow frontend access
func corsMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// Set CORS headers
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Webhook-Signature, X-Organization-ID")

		// Handle preflight requests
		if r.Method == "OPTIONS" {
			w.WriteHeader(http.StatusOK)
			return
		}

		next.ServeHTTP(w, r)
	})
}

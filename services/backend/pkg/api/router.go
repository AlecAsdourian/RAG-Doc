package api

import (
	"log/slog"
	"net/http"
	"os"
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

	// Initialize webhook handler. The secret is required at construction
	// time so a deployment missing SUPABASE_WEBHOOK_SECRET panics at
	// startup rather than silently accepting unsigned events.
	webhookSecret := os.Getenv("SUPABASE_WEBHOOK_SECRET")
	if webhookSecret == "" {
		panic("api.NewRouterWithValidator: SUPABASE_WEBHOOK_SECRET must be set")
	}
	// Supabase admin client — used after provisioning to write
	// organization context onto the Supabase user, which is what puts
	// `app_metadata.organization_id` into the JWT that TenantMiddleware
	// reads. Optional at construction: without SUPABASE_URL and
	// SUPABASE_SERVICE_ROLE_KEY we warn loudly and run degraded rather
	// than refusing to start, so tests and offline dev still work.
	// Degraded means provisioned users receive no org claim, and every
	// tenant-scoped request they make is denied.
	var adminClient auth.AdminClient
	supabaseURL := os.Getenv("SUPABASE_URL")
	serviceRoleKey := os.Getenv("SUPABASE_SERVICE_ROLE_KEY")
	if supabaseURL != "" && serviceRoleKey != "" {
		adminClient = auth.NewAdminClient(supabaseURL, serviceRoleKey)
	} else {
		slog.Warn("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY unset; provisioned users " +
			"will NOT receive an organization_id claim and will be denied tenant-scoped routes")
	}

	webhookHandler := auth.NewWebhookHandler(dbpool, webhookSecret, adminClient)

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

	// Supabase webhook — signature-verified, so an attacker without the
	// shared secret cannot deliver ANY event. A per-IP rate limit was
	// tried in the initial PR #13 cut; the reviewer flagged that with
	// middleware.RealIP in the chain, the "IP" is a client-supplied
	// header a bot can spoof or point at a victim. Edge rate limiting
	// belongs at the CDN/WAF (Phase 24), not here.
	// @skip-isolation-test: signature-verified webhook, provisions its own tenant (see 19-02)
	r.Post("/webhooks/supabase", webhookHandler.HandleSupabaseWebhook())

	// OAuth login/callback routes. StateStore is optional at
	// construction time — if Redis is not reachable (typical in tests
	// and offline dev), the routes are simply not mounted rather than
	// panicking the whole router. Real deployments have Redis; the log
	// line surfaces the miss.
	if stateStore, err := auth.NewStateStore(); err == nil {
		oauthConfig := auth.NewOAuthConfig()
		provisioner := auth.NewUserProvisioner(dbpool)
		r.Get("/auth/github/login", auth.HandleGitHubLogin(oauthConfig, stateStore))
		r.Get("/auth/github/callback", auth.HandleGitHubCallback(oauthConfig, provisioner, stateStore))
		r.Get("/auth/gitlab/login", auth.HandleGitLabLogin(oauthConfig, stateStore))
		r.Get("/auth/gitlab/callback", auth.HandleGitLabCallback(oauthConfig, provisioner, stateStore))
	} else {
		slog.Warn("state store unavailable; OAuth routes not mounted",
			slog.String("error", err.Error()))
	}

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
		// X-Organization-ID intentionally absent: Phase 19-03 removed the
		// header path entirely. Tenant identity comes from the JWT's
		// app_metadata claim, which a client cannot forge.
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Webhook-Signature")

		// Handle preflight requests
		if r.Method == "OPTIONS" {
			w.WriteHeader(http.StatusOK)
			return
		}

		next.ServeHTTP(w, r)
	})
}

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
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/db"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/github"
)

// Config holds router configuration
type Config struct {
	LogJSON  bool
	LogLevel slog.Level
}

// NewRouter creates a Chi router with middleware chain and route groups.
//
// Sugar over NewRouterWithValidator that builds the production Supabase
// JWKS validator from authConfig.
//
// Two seams exist below it for tests: NewRouterWithValidator swaps the
// bearer-token verifier, and NewRouterWithValidatorAndAdmin additionally
// swaps the Supabase admin client so a test can observe what gets written
// to a user's app_metadata.
func NewRouter(dbpool *pgxpool.Pool, ragClient *client.RAGClient, authConfig *auth.Config, cfg Config) chi.Router {
	return NewRouterWithValidator(dbpool, ragClient, auth.NewJWTValidator(authConfig), cfg)
}

// NewRouterWithValidator builds the router with a caller-supplied token
// validator, constructing the Supabase admin client from the environment.
// Same middleware chain and routes as NewRouter; only the bearer-token
// verifier is swappable.
func NewRouterWithValidator(dbpool *pgxpool.Pool, ragClient *client.RAGClient, jwtValidator auth.TokenValidator, cfg Config) chi.Router {
	// Supabase admin client — writes organization context onto the Supabase
	// user, which is what puts `app_metadata.organization_id` into the JWT
	// that TenantMiddleware reads. Optional: without SUPABASE_URL and
	// SUPABASE_SERVICE_ROLE_KEY we warn loudly and run degraded rather than
	// refusing to start, so tests and offline dev still work. Degraded means
	// provisioned users receive no org claim, cannot switch organizations,
	// and are denied every tenant-scoped route.
	var adminClient auth.AdminClient
	supabaseURL := os.Getenv("SUPABASE_URL")
	serviceRoleKey := os.Getenv("SUPABASE_SERVICE_ROLE_KEY")
	if supabaseURL != "" && serviceRoleKey != "" {
		adminClient = auth.NewAdminClient(supabaseURL, serviceRoleKey)
	} else {
		slog.Warn("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY unset; provisioned users " +
			"will NOT receive an organization_id claim and will be denied tenant-scoped routes")
	}
	return NewRouterWithValidatorAndAdmin(dbpool, ragClient, jwtValidator, adminClient, cfg)
}

// NewRouterWithValidatorAndAdmin builds the router with both the token
// validator and the Supabase admin client supplied by the caller.
//
// The admin seam exists for tests. Two endpoints write organization context
// to Supabase — the signup webhook and POST /api/user/select-organization —
// and what they write is a security property, not an implementation detail:
// the claim decides which tenant's data the caller reaches on their next
// token. A test that cannot observe that write cannot verify it, which is
// how the role-carryover escalation in 19-04's original plan would have
// shipped unnoticed.
//
// adminClient may be nil; the router runs degraded, and the endpoints that
// need it refuse rather than reporting a success they did not perform.
func NewRouterWithValidatorAndAdmin(
	dbpool *pgxpool.Pool,
	ragClient *client.RAGClient,
	jwtValidator auth.TokenValidator,
	adminClient auth.AdminClient,
	cfg Config,
) chi.Router {
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
		panic("api.NewRouterWithValidatorAndAdmin: SUPABASE_WEBHOOK_SECRET must be set")
	}

	webhookHandler := auth.NewWebhookHandler(dbpool, webhookSecret, adminClient)

	// Initialize request validator
	validate := validator.New()

	// Initialize search handler
	searchHandler := handlers.NewSearchHandler(ragClient, validate)

	// Initialize chat handler
	chatHandler := handlers.NewChatHandler(ragClient, validate)

	// Multi-org endpoints. They share the admin client with the webhook —
	// both write the same organization claim, one at signup and one when
	// the user switches.
	//
	// These take the POOL, not the TenantScoper, and that is correct: they
	// read users / organizations / organization_memberships, none of which
	// have RLS, and they scope by the caller's `sub` rather than by tenant.
	// Routing them through a tenant transaction would require an
	// organization claim that a claim-less user recovering their account
	// does not have. See docs/isolation.md for the rule.
	userOrgsHandler := handlers.NewUserOrgsHandler(dbpool, adminClient, validate)

	// Tenant-scoped database access for Phase 20+ handlers. Handlers that
	// touch a table listed in migration 000008 are constructed with this
	// and NOT with dbpool, so an unscoped query is not something they can
	// express. See 20-01-DESIGN.md.
	tenantScoper := db.NewTenantScoper(dbpool)
	_ = tenantScoper // first consumer lands in 20-03 (repositories CRUD)

	// GitHub App client. Optional at construction, matching the Supabase
	// admin client above: without credentials we warn loudly and run
	// degraded rather than refusing to boot, so tests and offline dev
	// still work. Degraded means repository connection and the webhook
	// receiver cannot function — everything else is unaffected.
	//
	// A malformed key is NOT degraded-and-continue. NewClient fails on it,
	// and a deployment that has credentials but cannot use them should say
	// so at startup rather than at the first repository connect.
	var githubClient *github.Client
	if appID, keyPath := os.Getenv("GITHUB_APP_ID"), os.Getenv("GITHUB_APP_PRIVATE_KEY_PATH"); appID != "" && keyPath != "" {
		gh, err := github.NewClient(appID, keyPath)
		if err != nil {
			panic("api: GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY_PATH are set but unusable: " + err.Error())
		}
		githubClient = gh
	} else {
		slog.Warn("GITHUB_APP_ID or GITHUB_APP_PRIVATE_KEY_PATH unset; " +
			"repository connection and GitHub webhooks are unavailable")
	}
	_ = githubClient // consumers land in 20-03 and 20-04

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

	// Direct OAuth routes are NOT mounted. See ISS-011.
	//
	// These handlers are the Phase-4 reference implementation, written
	// before the project chose Supabase-native OAuth (ISS-005). They were
	// still being mounted whenever Redis happened to be reachable, and in
	// that state they are broken in two independent ways:
	//
	//  1. `HandleGitHubCallback` passes GitHub's NUMERIC user id to
	//     ProvisionOAuthUser, which has parsed its identity argument as a
	//     UUID since 19-02. Every completed callback is a 500 — a live
	//     failure on a mounted route, not dormant code.
	//
	//  2. Even repaired, this is a second provisioning path that never
	//     calls pushOrgContext, so a user created through it would have no
	//     `app_metadata.organization_id` and would be refused by every
	//     tenant-scoped route. Fixing (1) alone would convert a loud 500
	//     into a quiet broken account.
	//
	// Unmounting is the smallest change that stops serving a broken
	// endpoint. The handlers are kept, not deleted: removing them is a
	// planner/user call, and they remain a useful reference if direct
	// OAuth is ever wanted alongside Supabase. Reviving them means giving
	// provisioning a non-UUID identity column and routing them through the
	// same post-provision org-context push the webhook uses.
	//
	// The StateStore probe stays because it still reports a genuine
	// configuration gap, and Phase 20's GitHub App flow will want it.
	if stateStore, err := auth.NewStateStore(); err == nil {
		// Close it. NewStateStore dials Redis and leaves a pooled client
		// with background goroutines behind; this probe only wants the
		// reachability answer, and routers are constructed per test.
		_ = stateStore.Close()
		slog.Info("state store reachable; direct OAuth routes remain unmounted (ISS-011)")
	} else {
		slog.Warn("state store unavailable (OAuth CSRF protection would be unavailable "+
			"if direct OAuth routes were mounted; see ISS-011)",
			slog.String("error", err.Error()))
	}

	// User-scoped routes: authenticated, but deliberately NOT behind
	// TenantMiddleware.
	//
	// These operate on the caller's own memberships, never on tenant-scoped
	// data, and they are the way OUT of having no organization claim. Put
	// them behind TenantMiddleware and a user whose claim is missing —
	// mid-provisioning, or whose org-context push failed — would be 403'd
	// by the very endpoints that exist to fix that. Each handler scopes its
	// own queries to the caller's `sub`; there is no request-controlled
	// input naming a user or an org to read.
	r.Group(func(r chi.Router) {
		r.Use(auth.JWTAuthMiddleware(jwtValidator))

		r.With(middleware.Timeout(60*time.Second)).Route("/api/user", func(r chi.Router) {
			r.Get("/organizations", userOrgsHandler.List)
			// Covered by TestUserOrgsIsolation. Deliberately NOT marked
			// @skip-isolation-test: this is the phase's authorization
			// boundary and must stay gated by the 17-05 CI scanner. An
			// earlier revision carried that marker on the GET above, where
			// the scanner never looks — harmless today only because
			// paren-balancing kept it out of the POST's lookback window,
			// and one reformat away from silently un-gating this route.
			r.Post("/select-organization", userOrgsHandler.Select)
		})
	})

	// Protected routes (JWT auth + tenant isolation)
	r.Group(func(r chi.Router) {
		// JWT authentication middleware
		r.Use(auth.JWTAuthMiddleware(jwtValidator))
		// Tenant isolation middleware
		r.Use(auth.TenantMiddleware())

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

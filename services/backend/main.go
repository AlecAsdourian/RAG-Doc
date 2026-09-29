package main

import (
	"context"
	"log"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/api"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/auth"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/client"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/github"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/internalapi"
)

func main() {
	// Load configuration from environment
	port := os.Getenv("PORT")
	if port == "" {
		port = "8080"
	}

	databaseURL := os.Getenv("DATABASE_URL")
	if databaseURL == "" {
		log.Fatal("DATABASE_URL environment variable is required")
	}

	// Determine log format and level from environment
	logJSON := os.Getenv("LOG_FORMAT") == "json"
	logLevel := slog.LevelInfo
	if os.Getenv("LOG_LEVEL") == "debug" {
		logLevel = slog.LevelDebug
	}

	// One slog logger for everything outside the public router's httplog:
	// this file's warnings and the internal listener, in the same format
	// LOG_FORMAT gives the public side, so a deployment reading JSON does
	// not get one text line per repository token in the middle of it.
	logOpts := &slog.HandlerOptions{Level: logLevel}
	var logHandler slog.Handler
	if logJSON {
		logHandler = slog.NewJSONHandler(os.Stderr, logOpts)
	} else {
		logHandler = slog.NewTextHandler(os.Stderr, logOpts)
	}
	logger := slog.New(logHandler)
	slog.SetDefault(logger)

	// Connect to PostgreSQL
	ctx := context.Background()
	dbpool, err := pgxpool.New(ctx, databaseURL)
	if err != nil {
		log.Fatalf("Unable to connect to database: %v\n", err)
	}
	defer dbpool.Close()

	// Test database connection
	if err := dbpool.Ping(ctx); err != nil {
		log.Fatalf("Unable to ping database: %v\n", err)
	}
	log.Println("Successfully connected to database")

	// Load auth configuration
	authConfig := auth.LoadConfig()

	// Load RAG service URL and create client
	ragServiceURL := os.Getenv("RAG_SERVICE_URL")
	if ragServiceURL == "" {
		ragServiceURL = "http://localhost:8000"
	}
	ragClient := client.NewRAGClient(ragServiceURL)
	log.Printf("RAG client configured for: %s\n", ragServiceURL)

	// The GitHub App client, built ONCE and shared by both listeners (22-04).
	//
	// The public router uses it to connect repositories and to answer the
	// App's webhooks; the internal listener uses it to mint the
	// one-repository, read-only tokens the worker fetches with. The App's
	// private key is loaded here and lives in this process only — the
	// worker never sees it (decision P10 / U4).
	//
	// nil means the App is not configured, which is the degraded shape every
	// test and most dev checkouts have. An error means the credentials are
	// set and unusable, and that stops the process: a deployment that has
	// credentials but cannot use them should say so now rather than at the
	// first repository connect (19-01).
	githubClient, err := github.NewClientFromEnv()
	if err != nil {
		log.Fatalf("GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY_PATH are set but unusable: %v\n", err)
	}
	if githubClient == nil {
		slog.Warn("GITHUB_APP_ID or GITHUB_APP_PRIVATE_KEY_PATH unset; " +
			"repository connection, GitHub webhooks and the internal repository-token " +
			"route are unavailable")
	}

	// Create Chi router with middleware chain
	router := api.NewRouter(dbpool, ragClient, authConfig, api.Config{
		LogJSON:      logJSON,
		LogLevel:     logLevel,
		GitHubClient: githubClient,
	})

	// Create HTTP server
	// Note: WriteTimeout is 0 (disabled) to support SSE streaming
	// Individual routes apply timeouts via middleware.Timeout
	server := &http.Server{
		Addr:         ":" + port,
		Handler:      router,
		ReadTimeout:  15 * time.Second,
		WriteTimeout: 0, // Disabled for SSE support
		IdleTimeout:  120 * time.Second,
	}

	// Start server in a goroutine
	go func() {
		log.Printf("Starting server on port %s\n", port)
		if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("Server failed to start: %v\n", err)
		}
	}()

	// The internal listener (22-04): pkg/internalapi's routes and nothing
	// else, on INTERNAL_ADDR.
	//
	// ⚠ NEVER PUBLISHED. The one route it serves finds a job by id and
	// lease BEFORE it knows the tenant — a queue-wide read over
	// `ingestion_jobs`, which has no row-level security — and the lease it
	// checks is the worker's credential. It binds loopback by default; in
	// compose it is reachable on the compose network only and must never
	// appear in `ports:`; Phase 24 carries "keep the token route internal"
	// as a deployment requirement. See docs/internal-api.md.
	//
	// Without GitHub App credentials there is nothing to mint with, so the
	// listener does not start, and says why at WARN — the same way the
	// public side degrades.
	var internal *http.Server
	if githubClient != nil {
		internalAddr := os.Getenv("INTERNAL_ADDR")
		if internalAddr == "" {
			internalAddr = internalapi.DefaultAddr
		}
		// ⚠ REFUSE TO BIND EVERY INTERFACE unless told to in so many
		// words. `:8081` on a platform that publishes whatever a process
		// listens on is a public token route (PR #52's review, L3). Bind
		// loopback, or the compose service name, or set the override and
		// accept the warning.
		allInterfaces, err := internalapi.CheckListenAddr(internalAddr)
		if err != nil {
			log.Fatalf("INTERNAL_ADDR %q refused: %v\n", internalAddr, err)
		}
		if allInterfaces {
			if os.Getenv(internalapi.AllInterfacesOverrideEnv) != "true" {
				log.Fatalf("INTERNAL_ADDR %q binds every interface, and the token route must "+
					"never be reachable from outside the worker's network; bind loopback or the "+
					"compose service name (INTERNAL_ADDR=backend:8081), or set %s=true and keep "+
					"the address unpublished\n", internalAddr, internalapi.AllInterfacesOverrideEnv)
			}
			slog.Warn("the internal API binds EVERY interface by explicit override; "+
				"this address must never be published, or anyone who can read a lease owner "+
				"can mint repository tokens",
				slog.String("internal_addr", internalAddr),
				slog.String("override", internalapi.AllInterfacesOverrideEnv))
		}
		internal = &http.Server{
			Addr:         internalAddr,
			Handler:      internalapi.NewRouter(dbpool, githubClient, logger),
			ReadTimeout:  15 * time.Second,
			WriteTimeout: 60 * time.Second,
			IdleTimeout:  120 * time.Second,
		}
		go func() {
			log.Printf("Starting internal API on %s (never publish this address)\n", internalAddr)
			if err := internal.ListenAndServe(); err != nil && err != http.ErrServerClosed {
				log.Fatalf("Internal API failed to start: %v\n", err)
			}
		}()
	} else {
		slog.Warn("internal API not started: no GitHub App credentials, so there is " +
			"nothing to mint repository tokens with; the worker cannot fetch repositories")
	}

	// Wait for interrupt signal to gracefully shutdown the server
	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
	<-quit

	log.Println("Shutting down server...")

	// Graceful shutdown with 30 second timeout, both listeners together.
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	if internal != nil {
		if err := internal.Shutdown(shutdownCtx); err != nil {
			log.Printf("Internal API forced to shutdown: %v\n", err)
		}
	}
	if err := server.Shutdown(shutdownCtx); err != nil {
		log.Fatalf("Server forced to shutdown: %v\n", err)
	}

	log.Println("Server stopped")
}

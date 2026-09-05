package isolation

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/testcontainers/testcontainers-go"
	"github.com/testcontainers/testcontainers-go/modules/postgres"
	"github.com/testcontainers/testcontainers-go/wait"
)

const (
	// containerName is the stable name used for testcontainers reuse. A single
	// Postgres container is shared across every test package in the repo.
	containerName = "rag-doc-isolation-tests"

	postgresImage = "postgres:16-alpine"
	postgresDB    = "isolation"
	postgresUser  = "isolation"
	postgresPass  = "isolation"

	// appRole is the non-superuser role every test pool connects as. Postgres
	// superusers bypass RLS unconditionally, so tests would silently pass; a
	// dedicated NOSUPERUSER NOBYPASSRLS role makes RLS actually enforceable.
	appRole = "rag_doc_app"
)

var (
	sharedDSN string
	setupOnce sync.Once
	setupErr  error
)

// SetupTestDB returns a *pgxpool.Pool connected to an ephemeral Postgres with
// all migrations applied. The Postgres container is reused across the test
// package invocation via testcontainers reuse, so the second and subsequent
// SetupTestDB calls in a package are fast.
//
// The returned pool is closed on t.Cleanup. The container itself is not
// stopped between test runs — that would defeat reuse. Docker daemon must be
// running; if it is not, t.Fatal is called with a clear message.
func SetupTestDB(t *testing.T) *pgxpool.Pool {
	t.Helper()

	setupOnce.Do(func() {
		setupErr = setupContainer(context.Background())
	})
	if setupErr != nil {
		t.Fatalf("isolation: failed to set up test container: %v", setupErr)
	}

	cfg, err := pgxpool.ParseConfig(sharedDSN)
	if err != nil {
		t.Fatalf("isolation: parse config: %v", err)
	}
	// Connect as the non-superuser app role so RLS actually applies. Postgres
	// bypasses RLS for superusers even when FORCE ROW LEVEL SECURITY is set,
	// which would make isolation tests pass falsely.
	cfg.AfterConnect = func(ctx context.Context, conn *pgx.Conn) error {
		_, err := conn.Exec(ctx, "SET ROLE "+appRole)
		return err
	}

	pool, err := pgxpool.NewWithConfig(context.Background(), cfg)
	if err != nil {
		t.Fatalf("isolation: open pool: %v", err)
	}
	t.Cleanup(func() { pool.Close() })

	return pool
}

func setupContainer(ctx context.Context) error {
	// Disable the Ryuk reaper so the reusable Postgres container survives
	// between `go test` invocations. WithReuseByName finds an existing
	// container only if it's still running — Ryuk would tear it down at the
	// end of the first session, defeating reuse. This env var is read the
	// first time testcontainers' config initialises, which happens inside
	// postgres.Run below, so setting it here is early enough.
	//
	// Consequence: containers named `rag-doc-isolation-tests` persist on the
	// developer's Docker daemon until manually removed (`docker rm -f
	// rag-doc-isolation-tests`). Data is idempotent (migrations skip
	// already-applied ones) and per-test fixtures clean themselves up.
	if err := os.Setenv("TESTCONTAINERS_RYUK_DISABLED", "true"); err != nil {
		return fmt.Errorf("set ryuk-disabled env: %w", err)
	}

	container, err := postgres.Run(ctx, postgresImage,
		postgres.WithDatabase(postgresDB),
		postgres.WithUsername(postgresUser),
		postgres.WithPassword(postgresPass),
		testcontainers.WithReuseByName(containerName),
		testcontainers.WithWaitStrategy(
			wait.ForLog("database system is ready to accept connections").
				WithOccurrence(2).
				WithStartupTimeout(60*time.Second),
		),
	)
	if err != nil {
		return fmt.Errorf("start postgres container: %w", err)
	}

	dsn, err := container.ConnectionString(ctx, "sslmode=disable")
	if err != nil {
		return fmt.Errorf("get connection string: %w", err)
	}

	if err := applyMigrations(dsn, migrationsDir()); err != nil {
		return fmt.Errorf("apply migrations: %w", err)
	}

	if err := ensureAppRole(ctx, dsn); err != nil {
		return fmt.Errorf("ensure app role: %w", err)
	}

	sharedDSN = dsn
	return nil
}

// ensureAppRole creates a non-superuser role and grants it the privileges tests
// need. It is idempotent so container reuse is safe.
func ensureAppRole(ctx context.Context, dsn string) error {
	conn, err := pgx.Connect(ctx, dsn)
	if err != nil {
		return fmt.Errorf("connect for role setup: %w", err)
	}
	defer conn.Close(ctx)

	stmts := []string{
		// CREATE ROLE is not IF NOT EXISTS, so wrap in DO block.
		`DO $$
		BEGIN
			IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '` + appRole + `') THEN
				CREATE ROLE ` + appRole + ` NOSUPERUSER NOBYPASSRLS INHERIT;
			END IF;
		END $$;`,
		`GRANT USAGE ON SCHEMA public TO ` + appRole + `;`,
		`GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO ` + appRole + `;`,
		`GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ` + appRole + `;`,
		// Session-user (superuser) must be granted the role for SET ROLE to work.
		`GRANT ` + appRole + ` TO ` + postgresUser + `;`,
	}
	for _, s := range stmts {
		if _, err := conn.Exec(ctx, s); err != nil {
			return fmt.Errorf("app role stmt failed: %s: %w", firstLine(s), err)
		}
	}
	return nil
}

func firstLine(s string) string {
	for i, r := range s {
		if r == '\n' {
			return s[:i]
		}
	}
	return s
}

// migrationsDir returns the absolute path to services/backend/migrations relative
// to this source file. Using runtime.Caller keeps the harness portable — it works
// no matter what directory `go test` was invoked from.
func migrationsDir() string {
	_, thisFile, _, _ := runtime.Caller(0)
	// this file: services/backend/pkg/testing/isolation/container.go
	// target:    services/backend/migrations
	return filepath.Join(filepath.Dir(thisFile), "..", "..", "..", "migrations")
}

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

	// The wait-for-log strategy fires as soon as Postgres prints
	// "ready to accept connections", but on cold container start the port
	// mapping and startup handshake occasionally lag a few hundred ms.
	// golang-migrate opens its own connection with no retry, so a first
	// migrate.New hits EOF. Retry a plain ping until the connection is
	// actually usable before handing off to the migrator.
	if err := waitForDB(ctx, dsn, 10*time.Second); err != nil {
		return fmt.Errorf("wait for db: %w", err)
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

// appRoleSetupLockID is the advisory-lock key serializing ensureAppRole
// across processes. The value is arbitrary but must not collide with
// golang-migrate's own advisory lock, which is derived from the database
// name — a fixed constant in a different range cannot.
const appRoleSetupLockID = 0x7261_67646f_63 // "ragdoc"

// ensureAppRole creates a non-superuser role and grants it the privileges
// tests need. It is idempotent, so container reuse is safe.
//
// The whole sequence runs inside one transaction holding an advisory lock,
// because `go test` runs packages in PARALLEL against the SAME reused
// container and every one of these statements races:
//
//   - The DO block is check-then-act. Two processes can both observe the
//     role missing and both attempt CREATE ROLE; the loser gets SQLSTATE
//     42710 (role already exists).
//   - The GRANTs update shared catalog tuples (pg_namespace, pg_class,
//     pg_authid). Concurrent updates to the same tuple fail with SQLSTATE
//     XX000 `tuple concurrently updated`, which is not retried by anything
//     and surfaces as a hard test failure.
//
// This was observed in ~40% of COLD-container runs and never on a warm
// one, which is the worst possible flake profile: CI is always cold, so it
// fails there and passes locally. Tracked as ISS-010.
//
// Two details matter:
//
//   - `pg_advisory_xact_lock`, not the session-level `pg_advisory_lock`.
//     CREATE ROLE and GRANT are both transactional in Postgres, so the
//     whole thing commits or rolls back together, and a transaction-scoped
//     lock is released by the server however the process dies. A session
//     lock leaks if a test binary panics between acquire and release.
//   - The lock wraps ALL the statements, not just the GRANTs. Locking only
//     the part that failed most visibly would leave the CREATE ROLE race
//     open to surface later under a tighter interleaving.
//
// This is the same mechanism golang-migrate already uses around migrations
// in applyMigrations above — which is precisely why migrations survived
// the concurrency that broke this function.
func ensureAppRole(ctx context.Context, dsn string) error {
	conn, err := pgx.Connect(ctx, dsn)
	if err != nil {
		return fmt.Errorf("connect for role setup: %w", err)
	}
	defer conn.Close(ctx)

	tx, err := conn.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin role setup tx: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()

	// Blocks until any concurrent ensureAppRole commits. Released
	// automatically at commit or rollback.
	if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock($1)`, int64(appRoleSetupLockID)); err != nil {
		return fmt.Errorf("acquire role setup lock: %w", err)
	}

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
		if _, err := tx.Exec(ctx, s); err != nil {
			return fmt.Errorf("app role stmt failed: %s: %w", firstLine(s), err)
		}
	}

	if err := tx.Commit(ctx); err != nil {
		return fmt.Errorf("commit role setup: %w", err)
	}
	return nil
}

// waitForDB polls the given DSN with a short-timeout ping every 200ms
// until either a ping succeeds or the overall deadline elapses. Returns
// the last ping error if the deadline is hit.
func waitForDB(ctx context.Context, dsn string, deadline time.Duration) error {
	waitCtx, cancel := context.WithTimeout(ctx, deadline)
	defer cancel()

	var lastErr error
	for {
		pingCtx, pingCancel := context.WithTimeout(waitCtx, 2*time.Second)
		conn, err := pgx.Connect(pingCtx, dsn)
		if err == nil {
			err = conn.Ping(pingCtx)
			_ = conn.Close(pingCtx)
			if err == nil {
				pingCancel()
				return nil
			}
		}
		pingCancel()
		lastErr = err

		select {
		case <-waitCtx.Done():
			return fmt.Errorf("db not ready after %s: %w", deadline, lastErr)
		case <-time.After(200 * time.Millisecond):
		}
	}
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

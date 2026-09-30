package isolation

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
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
	// containerNamePrefix begins the name of the Postgres container the
	// harness reuses. The whole name is per checkout: see
	// resolveContainerName. Every test package in one checkout derives the
	// same name, so they share one container, as before.
	//
	// ⚠ CHANGE THE PREFIX WHENEVER postgresImage CHANGES. testcontainers-go
	// v0.44.0's ReuseOrCreateContainer finds a container by NAME and uses it
	// whatever its image (docker.go:1424-1441): there is no image
	// comparison. Measured in 22-RESEARCH.md Q2: a leftover
	// `postgres:16-alpine` container under the old name was reused by a
	// harness asking for pgvector, and the first migration needing the
	// extension failed with `extension "vector" is not available`. A new
	// prefix means a stale container is simply not found. Two old
	// containers, `rag-doc-isolation-tests` (before 22-01) and
	// `rag-doc-isolation-tests-pgv16` (the one shared container before
	// ISS-037), are left on developer machines; docs/local-development.md
	// says when they can be removed.
	containerNamePrefix = "rag-doc-isolation-tests-pgv16"

	// containerNameEnv overrides the derived name with a name of the
	// developer's choosing, for example to share one container between
	// checkouts known to be at the same migration version. An empty value
	// counts as unset.
	containerNameEnv = "ISOLATION_CONTAINER_NAME"

	// containerNameHashLen is how many hex digits of the checkout's hash the
	// name carries: 48 bits, the length of a short Docker id.
	containerNameHashLen = 12

	// checkoutLabel is the Docker label recording which checkout created a
	// harness container, so a listing can tell a live worktree's container
	// from one whose worktree is gone. Reuse ignores it: it matches the
	// name alone.
	checkoutLabel = "rag-doc.isolation.checkout"

	// postgresImage is pinned by digest, and the same reference appears in
	// the Python conftest, backend-ci.yml and docker-compose.yml. PostgreSQL
	// 16.15 with pgvector 0.8.6. Verified with
	// `docker buildx imagetools inspect pgvector/pgvector:pg16` (22-01).
	postgresImage = "pgvector/pgvector:pg16@sha256:ccc6e83d6e35e931dc7c5def2022729d5a6c370318d099181995567ff1fb4d6b"
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

// validContainerName is what an ISOLATION_CONTAINER_NAME value must match:
// Docker's own rule for a container name, [a-zA-Z0-9][a-zA-Z0-9_.-]+
// (daemon/names in docker v28.3.3), narrowed to lowercase. A derived name
// matches it by construction: the prefix, a dash and hex digits.
var validContainerName = regexp.MustCompile(`^[a-z0-9][a-z0-9_.-]+$`)

// resolveContainerName returns the name of the container this checkout's
// harness creates or reuses: ISOLATION_CONTAINER_NAME when it is set,
// otherwise containerNamePrefix, a dash, and a short hash of the checkout's
// root directory.
//
// ONE CONTAINER PER CHECKOUT, NOT ONE PER MACHINE (ISS-037). The harness
// applies its own tree's migrations to whatever database it finds, and
// golang-migrate refuses a database recorded at a version the tree does not
// have. With a single name, every worktree on a machine shared one
// container: 22-02's run migrated it to 17 while 22-04's tree was at 16,
// and every 22-04 run then failed with "no migration found for version 17".
// A name derived from the checkout gives each worktree its own container,
// and the same one on every run; CI has one checkout, so one container, as
// before. The cost is one Postgres per worktree, which the harness never
// removes.
//
// The derived name is the default, and the variable only an override,
// because a safety that has to be remembered brings the collision back the
// first time someone forgets it.
func resolveContainerName() (string, error) {
	return containerNameFor(os.Getenv(containerNameEnv), checkoutRoot())
}

// containerNameFor is resolveContainerName with its two inputs passed in,
// so a test can give it any checkout root.
func containerNameFor(override, root string) (string, error) {
	if override != "" {
		if !validContainerName.MatchString(override) {
			return "", fmt.Errorf("%s=%q is not a usable container name: "+
				"use lowercase letters, digits, underscores, dots and dashes, "+
				"starting with a letter or digit, at least two characters",
				containerNameEnv, override)
		}
		return override, nil
	}
	return containerNamePrefix + "-" + shortHash(root), nil
}

// shortHash is the first containerNameHashLen hex digits of the SHA-256 of
// s: lowercase hex, so always valid in a container name.
func shortHash(s string) string {
	sum := sha256.Sum256([]byte(s))
	return hex.EncodeToString(sum[:])[:containerNameHashLen]
}

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
	// between `go test` invocations. Ryuk would tear it down at the end of
	// the first session, leaving nothing to reuse. WithReuseByName finds a
	// stopped container as well as a running one and starts it
	// (docker.go:1383-1385 and 1496-1507), so a Docker restart costs a
	// start, not a new database. This env var is read the first time
	// testcontainers' config initialises, which happens inside postgres.Run
	// below, so setting it here is early enough.
	//
	// Consequence: this checkout's container (resolveContainerName) persists
	// on the developer's Docker daemon until removed by hand, one per
	// checkout; docs/local-development.md lists them and says how to remove
	// them. Data is idempotent (migrations skip already-applied ones) and
	// per-test fixtures clean themselves up.
	//
	// ⚠ golang-migrate never re-applies a version it has recorded, so after
	// EDITING a migration that this container has already applied, remove
	// the container: reuse would keep the old schema.
	if err := os.Setenv("TESTCONTAINERS_RYUK_DISABLED", "true"); err != nil {
		return fmt.Errorf("set ryuk-disabled env: %w", err)
	}

	name, err := resolveContainerName()
	if err != nil {
		return err
	}

	container, err := postgres.Run(ctx, postgresImage,
		postgres.WithDatabase(postgresDB),
		postgres.WithUsername(postgresUser),
		postgres.WithPassword(postgresPass),
		testcontainers.WithReuseByName(name),
		testcontainers.WithLabels(map[string]string{checkoutLabel: checkoutRoot()}),
		testcontainers.WithWaitStrategy(
			wait.ForLog("database system is ready to accept connections").
				WithOccurrence(2).
				WithStartupTimeout(60*time.Second),
		),
	)
	if err != nil {
		return fmt.Errorf("start postgres container %s: %w", name, err)
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

// appRoleGrants are the privileges the harness gives appRole in a database.
// Grants are per-database and cover only the tables that exist when they
// run, so a database migrated further afterwards needs them again: the
// seeded-migration gate re-runs them at version 12. The Python conftest's
// `_ensure_app_role` carries the same three.
//
// ⚠ NO TRUNCATE, deliberately. Row-level security does not govern it
// (22-CONTEXT P2's addendum).
var appRoleGrants = []string{
	`GRANT USAGE ON SCHEMA public TO ` + appRole + `;`,
	`GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO ` + appRole + `;`,
	`GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ` + appRole + `;`,
}

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
	}
	stmts = append(stmts, appRoleGrants...)
	// Session-user (superuser) must be granted the role for SET ROLE to work.
	stmts = append(stmts, `GRANT `+appRole+` TO `+postgresUser+`;`)
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

// migrationsDir returns the absolute path to services/backend/migrations in
// the checkout this source file belongs to. Deriving it from checkoutRoot
// keeps one anchor for both, so the migrations applied and the container
// they are applied to always name the same checkout.
func migrationsDir() string {
	return filepath.Join(checkoutRoot(), "services", "backend", "migrations")
}

// checkoutRoot returns the absolute path of the repository checkout (a
// worktree's own root, in a worktree) that this source file was compiled
// from. It reads the path the compiler recorded, via runtime.Caller, so it
// needs no git and works whatever directory `go test` runs in.
func checkoutRoot() string {
	_, thisFile, _, _ := runtime.Caller(0)
	// this file: services/backend/pkg/testing/isolation/container.go
	// target:    the checkout root, five directories up from its directory
	return canonicalPath(filepath.Join(filepath.Dir(thisFile), "..", "..", "..", "..", ".."))
}

// canonicalPath resolves symlinks and, on Windows, letter case, so one
// checkout reached by two spellings derives one container name (measured on
// go1.25, windows: a lower-cased checkout path comes back in its on-disk
// case). A path that cannot be resolved is returned cleaned; the harness
// then fails on the migrations under it, which says more than a name would.
func canonicalPath(p string) string {
	if resolved, err := filepath.EvalSymlinks(p); err == nil {
		return resolved
	}
	return filepath.Clean(p)
}

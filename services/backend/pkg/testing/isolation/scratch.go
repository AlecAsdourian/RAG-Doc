package isolation

import (
	"context"
	"fmt"
	"net"
	"net/url"
	"strconv"
	"strings"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

const (
	// SuperuserRole is the harness container's superuser. A scratch database
	// it owns is migrated the way the harnesses and CI migrate: as a role
	// that bypasses row-level security.
	SuperuserRole = postgresUser

	// DeploymentOwnerRole is the DEPLOYMENT SHAPE (21-01): a NOSUPERUSER
	// NOBYPASSRLS role that owns the tables and runs the migrations, so
	// FORCE ROW LEVEL SECURITY applies to it. ISS-031 is invisible to
	// SuperuserRole and live for this one.
	DeploymentOwnerRole = "rag_doc_owner"

	// deploymentOwnerPassword is a test credential for a role that exists
	// only inside the throwaway harness container. The container's
	// pg_hba.conf requires a password for TCP connections.
	deploymentOwnerPassword = "rag_doc_owner"
)

// ScratchDB is an empty database created inside the shared harness
// container, dropped when the test ends.
type ScratchDB struct {
	Name string

	// OwnerDSN connects as the database's owner, the role migrations run
	// as. For a SuperuserRole-owned database it equals SuperuserDSN.
	OwnerDSN string

	// SuperuserDSN connects as the container superuser. Read through it to
	// see every tenant's rows: row-level security cannot hide a gap from it.
	SuperuserDSN string
}

// ScratchDatabase creates an empty database owned by owner inside the
// harness container, and drops it in t.Cleanup.
//
// The harness container, not a second one: a database at a different
// migration version costs a `CREATE DATABASE`, not a container start, and a
// database dropped in t.Cleanup needs no Ryuk reaper. Nothing in the shared
// `isolation` database is read or written.
//
// THE OWNER IS A PARAMETER, AND IT CHANGES WHAT A TEST PROVES. owner is
// either SuperuserRole, which bypasses row-level security as the harnesses
// and CI do, or DeploymentOwnerRole, which does not. A helper that chose the
// owner silently would change what an existing test proves, so each caller
// names it. pkg/jobs/backfill_migration_test.go passes SuperuserRole, by
// design (its header says why); the seeded-migration gate passes
// DeploymentOwnerRole.
func ScratchDatabase(t *testing.T, pool *pgxpool.Pool, owner string) ScratchDB {
	t.Helper()
	ctx := context.Background()

	cfg := pool.Config().ConnConfig
	var ownerPassword string
	switch owner {
	case SuperuserRole:
		ownerPassword = cfg.Password
	case DeploymentOwnerRole:
		ensureDeploymentOwner(t, pool)
		ownerPassword = deploymentOwnerPassword
	default:
		t.Fatalf("isolation: ScratchDatabase: owner must be SuperuserRole or DeploymentOwnerRole, got %q", owner)
	}

	name := "scratch_" + strings.ReplaceAll(uuid.NewString(), "-", "")[:16]

	// CREATE DATABASE cannot run inside a transaction, so this is a plain
	// Exec on a hijacked superuser connection. Both identifiers are
	// generated or constant above, never caller-supplied free text.
	WithSuperuserConn(t, pool, func(conn *pgx.Conn) {
		t.Helper()
		if _, err := conn.Exec(ctx,
			"CREATE DATABASE "+pgx.Identifier{name}.Sanitize()+
				" OWNER "+pgx.Identifier{owner}.Sanitize()); err != nil {
			t.Fatalf("isolation: create scratch database: %v", err)
		}
	})
	t.Cleanup(func() {
		WithSuperuserConn(t, pool, func(conn *pgx.Conn) {
			// Anything still connected would make the drop fail, and a
			// leaked database is a slow leak on a container that is reused
			// between runs.
			_, _ = conn.Exec(context.Background(),
				`SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1`,
				name)
			if _, err := conn.Exec(context.Background(),
				"DROP DATABASE IF EXISTS "+pgx.Identifier{name}.Sanitize()); err != nil {
				t.Logf("isolation: could not drop scratch database %s: %v", name, err)
			}
		})
	})

	return ScratchDB{
		Name:         name,
		OwnerDSN:     scratchDSN(cfg.Host, cfg.Port, owner, ownerPassword, name),
		SuperuserDSN: scratchDSN(cfg.Host, cfg.Port, cfg.User, cfg.Password, name),
	}
}

func scratchDSN(host string, port uint16, user, password, database string) string {
	return (&url.URL{
		Scheme:   "postgres",
		User:     url.UserPassword(user, password),
		Host:     net.JoinHostPort(host, strconv.Itoa(int(port))),
		Path:     "/" + database,
		RawQuery: "sslmode=disable",
	}).String()
}

// ensureDeploymentOwner creates DeploymentOwnerRole if it is missing, then
// refuses to continue unless it really is the deployment shape.
//
// ROLES ARE CLUSTER-WIDE, so every test process sharing the container races
// to create this one. It is serialized exactly as ensureAppRole is, under
// the same transaction-scoped advisory lock (ISS-010): CREATE ROLE is
// check-then-act, and concurrent role DDL fails with `tuple concurrently
// updated`.
func ensureDeploymentOwner(t *testing.T, pool *pgxpool.Pool) {
	t.Helper()
	ctx := context.Background()

	WithSuperuserConn(t, pool, func(conn *pgx.Conn) {
		t.Helper()
		tx, err := conn.Begin(ctx)
		if err != nil {
			t.Fatalf("isolation: begin owner-role setup: %v", err)
		}
		defer func() { _ = tx.Rollback(ctx) }()

		if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock($1)`, int64(appRoleSetupLockID)); err != nil {
			t.Fatalf("isolation: acquire role setup lock: %v", err)
		}
		if _, err := tx.Exec(ctx, fmt.Sprintf(`DO $$
		BEGIN
			IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '%s') THEN
				CREATE ROLE %s NOSUPERUSER NOBYPASSRLS LOGIN PASSWORD '%s';
			END IF;
		END $$;`, DeploymentOwnerRole, DeploymentOwnerRole, deploymentOwnerPassword)); err != nil {
			t.Fatalf("isolation: create %s: %v", DeploymentOwnerRole, err)
		}
		if err := tx.Commit(ctx); err != nil {
			t.Fatalf("isolation: commit owner-role setup: %v", err)
		}

		// ⚠ THE PREMISE OF EVERY TEST THAT USES THIS ROLE. An owner that is
		// a superuser, or has BYPASSRLS, is not subject to FORCE ROW LEVEL
		// SECURITY, and a test migrating as it proves nothing about the
		// deployment shape while still passing. Checked on every call,
		// because the role outlives any one run.
		var super, bypass, login bool
		if err := conn.QueryRow(ctx,
			`SELECT rolsuper, rolbypassrls, rolcanlogin FROM pg_roles WHERE rolname = $1`,
			DeploymentOwnerRole,
		).Scan(&super, &bypass, &login); err != nil {
			t.Fatalf("isolation: read %s: %v", DeploymentOwnerRole, err)
		}
		if super || bypass || !login {
			t.Fatalf("isolation: %s must be NOSUPERUSER NOBYPASSRLS LOGIN; it is super=%v bypassrls=%v login=%v",
				DeploymentOwnerRole, super, bypass, login)
		}
	})
}

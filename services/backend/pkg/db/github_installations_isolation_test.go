package db_test

// Isolation tests for github_installations, the tenant-scoped table
// migration 000010 adds.
//
// docs/isolation.md requires these for every tenant-scoped table. The CI
// scanner does NOT — it inspects route declarations, not schema — so a
// new table shipped without RLS would pass CI. That gap is exactly why
// this file exists alongside the migration rather than waiting for 20-04
// to add the endpoints that use it.

import (
	"context"
	"testing"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/stretchr/testify/require"

	"github.com/yourusername/smart-docs-platform/services/backend/pkg/db"
	"github.com/yourusername/smart-docs-platform/services/backend/pkg/testing/isolation"
)

// insertInstallation adds a github_installations row under org's scope.
func insertInstallation(t *testing.T, scoper *db.TenantScoper, orgID string, ghID int64) {
	t.Helper()
	require.NoError(t, scoper.InTenantTx(ctxForTenant(orgID), func(tx pgx.Tx) error {
		_, err := tx.Exec(context.Background(), `
			INSERT INTO github_installations
			  (organization_id, github_installation_id, account_login,
			   account_type, repository_selection)
			VALUES ($1, $2, 'someone', 'User', 'selected')
		`, orgID, ghID)
		return err
	}))
}

func TestGitHubInstallations_AreTenantIsolated(t *testing.T) {
	pool := isolation.SetupTestDB(t)

	isolation.WithTwoOrgs(t, pool, func(orgA, orgB *isolation.TestOrg) {
		scoper := db.NewTenantScoper(pool)

		insertInstallation(t, scoper, orgA.ID, 111111)
		insertInstallation(t, scoper, orgB.ID, 222222)

		t.Run("each org sees only its own", func(t *testing.T) {
			var seen []int64
			require.NoError(t, scoper.InTenantTx(ctxForTenant(orgA.ID), func(tx pgx.Tx) error {
				rows, err := tx.Query(context.Background(),
					`SELECT github_installation_id FROM github_installations`)
				if err != nil {
					return err
				}
				defer rows.Close()
				for rows.Next() {
					var id int64
					if err := rows.Scan(&id); err != nil {
						return err
					}
					seen = append(seen, id)
				}
				return rows.Err()
			}))
			require.Equal(t, []int64{111111}, seen,
				"cross-tenant leak: orgA saw an installation that is not theirs")
		})

		t.Run("one org cannot delete another's", func(t *testing.T) {
			require.NoError(t, scoper.InTenantTx(ctxForTenant(orgA.ID), func(tx pgx.Tx) error {
				_, err := tx.Exec(context.Background(),
					`DELETE FROM github_installations WHERE github_installation_id = $1`, 222222)
				return err
			}))

			// Assert on the ROW, not the statement's error. RLS makes a
			// cross-tenant DELETE match nothing rather than fail, so a
			// no-error result proves nothing on its own.
			var stillThere int
			require.NoError(t, scoper.InTenantTx(ctxForTenant(orgB.ID), func(tx pgx.Tx) error {
				return tx.QueryRow(context.Background(),
					`SELECT count(*) FROM github_installations WHERE github_installation_id = $1`,
					222222).Scan(&stillThere)
			}))
			require.Equal(t, 1, stillThere, "orgA deleted orgB's installation")
		})

		t.Run("one org cannot insert into another", func(t *testing.T) {
			err := scoper.InTenantTx(ctxForTenant(orgA.ID), func(tx pgx.Tx) error {
				_, e := tx.Exec(context.Background(), `
					INSERT INTO github_installations
					  (organization_id, github_installation_id, account_login,
					   account_type, repository_selection)
					VALUES ($1, $2, 'attacker', 'User', 'all')
				`, orgB.ID, 333333)
				return e
			})
			require.Error(t, err, "RLS WITH CHECK must refuse a write naming another tenant")
		})
	})
}

// TestGitHubInstallations_OneInstallationOneOrganization pins the UNIQUE
// constraint that IS the tenancy boundary.
//
// Without it two organizations could both claim the same GitHub
// installation, and a repository reached through it would have an
// ambiguous owner. It reads like a dedup convenience; it is not.
func TestGitHubInstallations_OneInstallationOneOrganization(t *testing.T) {
	pool := isolation.SetupTestDB(t)

	isolation.WithTwoOrgs(t, pool, func(orgA, orgB *isolation.TestOrg) {
		scoper := db.NewTenantScoper(pool)
		const shared = int64(987654)

		insertInstallation(t, scoper, orgA.ID, shared)

		err := scoper.InTenantTx(ctxForTenant(orgB.ID), func(tx pgx.Tx) error {
			_, e := tx.Exec(context.Background(), `
				INSERT INTO github_installations
				  (organization_id, github_installation_id, account_login,
				   account_type, repository_selection)
				VALUES ($1, $2, 'someone', 'User', 'selected')
			`, orgB.ID, shared)
			return e
		})

		require.Error(t, err,
			"a second organization claimed the same GitHub installation; "+
				"a repository reached through it now has two possible owners")

		var pgErr *pgconn.PgError
		require.ErrorAs(t, err, &pgErr)
		require.Equal(t, "23505", pgErr.Code, "expected a unique violation")
	})
}

// TestGitHubInstallations_UnscopedWriteIsRefused confirms the 000009
// trigger is attached to the new table.
//
// RLS alone would make an unscoped write a silent no-op. The trigger is
// what turns it into a visible failure, and attaching it is a separate
// line in the migration from enabling RLS — so it is separately
// forgettable.
func TestGitHubInstallations_UnscopedWriteIsRefused(t *testing.T) {
	pool := isolation.SetupTestDB(t)

	isolation.WithTwoOrgs(t, pool, func(orgA, _ *isolation.TestOrg) {
		_, err := pool.Exec(context.Background(), `
			INSERT INTO github_installations
			  (organization_id, github_installation_id, account_login,
			   account_type, repository_selection)
			VALUES ($1, $2, 'someone', 'User', 'selected')
		`, orgA.ID, 555555)

		require.Error(t, err, "an unscoped write to a tenant-scoped table must be refused")

		var pgErr *pgconn.PgError
		require.ErrorAs(t, err, &pgErr)
		require.Containsf(t, []string{"42501", "22P02"}, pgErr.Code,
			"expected the 000009 trigger (42501) or the RLS uuid cast (22P02), got %s", pgErr.Code)
	})
}

// TestEveryOrganizationHasExactlyOneDefaultProject pins the invariant
// 20-03 depends on when it resolves which project a new repository
// belongs to.
//
// `repositories.project_id` is NOT NULL, and before migration 000010
// nothing in production had ever created a project.
func TestEveryOrganizationHasExactlyOneDefaultProject(t *testing.T) {
	pool := isolation.SetupTestDB(t)

	isolation.WithTwoOrgs(t, pool, func(orgA, orgB *isolation.TestOrg) {
		ctx := context.Background()

		// projects has no RLS, so read it straight off the pool.
		for _, orgID := range []string{orgA.ID, orgB.ID} {
			var defaults int
			require.NoError(t, pool.QueryRow(ctx,
				`SELECT count(*) FROM projects WHERE organization_id = $1 AND is_default`,
				orgID).Scan(&defaults))
			require.Equalf(t, 1, defaults,
				"organization %s must have exactly one default project; 20-03 resolves "+
					"a repository's project through it", orgID)
		}
	})
}

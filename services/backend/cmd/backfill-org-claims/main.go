// Command backfill-org-claims pushes organization context onto Supabase
// users who are missing it.
//
// # Why this exists
//
// A user's organization claim reaches their JWT exactly one way: the
// signup webhook calls the Supabase admin API and writes
// `raw_app_meta_data`. That happens once. Supabase database webhooks fire
// once and never retry, and the trigger behind ours is AFTER INSERT on
// auth.users — so there is one delivery per user, for all time.
//
// If that single push fails (a transient 5xx from the admin API, a network
// blip, a deploy running without SUPABASE_SERVICE_ROLE_KEY), the user has
// no `app_metadata.organization_id` and TenantMiddleware returns 403 for
// every tenant-scoped route. Refreshing their token does not help:
// Supabase re-reads the same never-written column at every mint. Nothing
// in the request path repairs it.
//
// This command is that repair. It reads org ownership from OUR database —
// the source of truth — and re-pushes the claim for every owner, or for
// one named user.
//
// # Usage
//
//	backfill-org-claims [-dry-run] [-user <supabase-user-id>]
//
// Environment:
//
//	DATABASE_URL                 required — the application database
//	SUPABASE_URL                 required — e.g. https://abc.supabase.co
//	SUPABASE_SERVICE_ROLE_KEY    required — admin credentials
//
// The push is a merge, so running this is idempotent and safe at any time,
// including while the webhook is live. Re-running it against a healthy
// system is a no-op that rewrites identical values.
//
// Run it after any incident that touched Supabase or the webhook path, and
// consider scheduling it — a periodic run is what turns "one shot per
// user" into an eventually-consistent system.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log"
	"os"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/yourusername/smart-docs-platform/services/backend/pkg/auth"
)

func main() {
	var (
		dryRun = flag.Bool("dry-run", false,
			"report what would be pushed without calling Supabase")
		onlyUser = flag.String("user", "",
			"repair a single Supabase user id instead of every owner")
		timeout = flag.Duration("timeout", 5*time.Minute,
			"overall deadline for the run")
	)
	flag.Parse()

	if err := run(*dryRun, *onlyUser, *timeout); err != nil {
		log.Fatalf("backfill-org-claims: %v", err)
	}
}

func run(dryRun bool, onlyUser string, timeout time.Duration) error {
	databaseURL := os.Getenv("DATABASE_URL")
	if databaseURL == "" {
		return errors.New("DATABASE_URL is required")
	}

	// Resolve the admin credentials before touching the database, so a
	// misconfigured run fails in under a second instead of after a full
	// table scan. Skipped under -dry-run so the command stays useful as a
	// read-only audit on a machine without production credentials.
	var admin auth.AdminClient
	if !dryRun {
		supabaseURL := os.Getenv("SUPABASE_URL")
		serviceRoleKey := os.Getenv("SUPABASE_SERVICE_ROLE_KEY")
		if supabaseURL == "" || serviceRoleKey == "" {
			return errors.New("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required " +
				"(use -dry-run to audit without them)")
		}
		admin = auth.NewAdminClient(supabaseURL, serviceRoleKey)
	}

	var filter uuid.UUID
	if onlyUser != "" {
		parsed, err := uuid.Parse(onlyUser)
		if err != nil {
			return fmt.Errorf("-user %q is not a valid UUID: %w", onlyUser, err)
		}
		filter = parsed
	}

	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()

	pool, err := pgxpool.New(ctx, databaseURL)
	if err != nil {
		return fmt.Errorf("connect to database: %w", err)
	}
	defer pool.Close()
	if err := pool.Ping(ctx); err != nil {
		return fmt.Errorf("ping database: %w", err)
	}

	assignments, err := auth.NewUserProvisioner(pool).ListOwnerOrgAssignments(ctx)
	if err != nil {
		return err
	}

	var pushed, skipped, failed int
	for _, a := range assignments {
		if filter != uuid.Nil && a.SupabaseUserID != filter {
			skipped++
			continue
		}

		if dryRun {
			log.Printf("would push org=%s to supabase_user_id=%s (%s)",
				a.OrganizationID, a.SupabaseUserID, a.Email)
			pushed++
			continue
		}

		err := admin.UpdateUserAppMetadata(ctx, a.SupabaseUserID.String(), map[string]any{
			"organization_id":   a.OrganizationID.String(),
			"organization_role": "owner",
		})
		if err != nil {
			// Keep going. One user's failure — a deleted Supabase account,
			// say — must not strand everyone after them in the list.
			log.Printf("FAILED supabase_user_id=%s (%s): %v", a.SupabaseUserID, a.Email, err)
			failed++
			continue
		}
		log.Printf("pushed org=%s to supabase_user_id=%s (%s)",
			a.OrganizationID, a.SupabaseUserID, a.Email)
		pushed++
	}

	if filter != uuid.Nil && pushed == 0 && failed == 0 {
		return fmt.Errorf("no owner organization found for supabase user %s — "+
			"either the id is wrong or the user was never provisioned "+
			"(this command repairs missing claims, not missing organizations)", filter)
	}

	verb := "pushed"
	if dryRun {
		verb = "would push"
	}
	log.Printf("done: %s %d, skipped %d, failed %d", verb, pushed, skipped, failed)

	// Exit non-zero on any failure so a scheduled run surfaces in whatever
	// is watching it, rather than reporting success with errors in the log.
	if failed > 0 {
		return fmt.Errorf("%d user(s) could not be repaired", failed)
	}
	return nil
}

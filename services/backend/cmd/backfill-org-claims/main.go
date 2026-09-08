// Command backfill-org-claims pushes organization context onto Supabase
// users who are missing it.
//
// # Why this exists
//
// A user's organization claim reaches their JWT exactly one way: the
// signup webhook calls the Supabase admin API and writes
// `raw_app_meta_data`. That happens once.
//
// # On webhook retries — the evidence, stated honestly
//
// This command exists because a failed push has no automatic second
// chance. Two facts underpin that, and they carry different weight:
//
//  1. VERIFIED, in this repo: the trigger is `AFTER INSERT ON auth.users`
//     (scripts/supabase_user_trigger.sql), so exactly one
//     `auth_user_events` row is written per user, ever. One event, one
//     delivery attempt.
//
//  2. NOT INDEPENDENTLY VERIFIED: that Supabase does not retry a failed
//     delivery. Phase 4 recorded "Supabase webhooks fire once (no
//     automatic retry)" in 04-06-SUMMARY.md — but that same document also
//     said to "return 500 for provisioning failures (trigger Supabase
//     retry)", so it contradicts itself. The 500 line has been struck as
//     the wrong half. Supabase Database Webhooks are built on pg_net,
//     which is fire-and-forget, which is consistent with (1)'s
//     conclusion — but that is inference, not a measurement against this
//     project.
//
// The design deliberately does not depend on resolving (2). Even if
// Supabase did retry, retries are finite and a user who exhausts them is
// stranded identically, and users provisioned before the claim existed
// need this command regardless. **Do not rely on webhook retry** is the
// operative rule; whether the retry exists at all is a question worth
// answering, tracked in 19-03-SUMMARY.md, not a load-bearing assumption.
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

const (
	// setupTimeout bounds connecting and listing — one indexed query.
	setupTimeout = 30 * time.Second
	// minRunTimeout is the floor of the derived push budget, so a tiny
	// database still tolerates one slow Supabase response.
	minRunTimeout = 2 * time.Minute
	// perUserBudget is deliberately ~20x a healthy admin PUT (~100ms). It
	// is a ceiling that catches a genuinely wedged run, not a performance
	// target.
	perUserBudget = 2 * time.Second
)

func main() {
	var (
		dryRun = flag.Bool("dry-run", false,
			"report what would be pushed without calling Supabase")
		onlyUser = flag.String("user", "",
			"repair a single Supabase user id instead of every owner")
		timeout = flag.Duration("timeout", 0,
			"overall deadline; 0 (default) derives one from the number of users to repair")
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

	// Connecting and listing is bounded separately and tightly — it is one
	// indexed query, and if the database is unreachable we want to know in
	// seconds, not after the whole push budget drains.
	setupCtx, cancelSetup := context.WithTimeout(context.Background(), setupTimeout)
	defer cancelSetup()

	pool, err := pgxpool.New(setupCtx, databaseURL)
	if err != nil {
		return fmt.Errorf("connect to database: %w", err)
	}
	defer pool.Close()
	if err := pool.Ping(setupCtx); err != nil {
		return fmt.Errorf("ping database: %w", err)
	}

	assignments, err := auth.NewUserProvisioner(pool).ListOwnerOrgAssignments(setupCtx)
	if err != nil {
		return err
	}

	// Derive the push deadline from the work actually found, unless the
	// operator pinned one.
	//
	// A fixed default is the wrong shape here: this command's runtime is
	// linear in user count, so any constant is simultaneously too long for
	// a 5-user dev database and too short for a real one — and "too short"
	// fails silently in the worst way, stopping partway through a repair
	// with no signal that the remaining users were never attempted. The
	// budget below is a generous ceiling, not a target; a healthy run
	// finishes in a fraction of it.
	if timeout <= 0 {
		timeout = minRunTimeout + time.Duration(len(assignments))*perUserBudget
	}
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()

	var pushed, skipped, failed int
	for i, a := range assignments {
		// Stop at the deadline instead of grinding through the remainder.
		//
		// Every call after the context expires fails instantly with
		// "context deadline exceeded", so without this the run logs a
		// FAILED line per remaining user and reports, say, 9,600 failures
		// when the truth is "we ran out of time after 400". That number is
		// the one an operator acts on, and it must not lie about how much
		// is actually broken.
		if err := ctx.Err(); err != nil {
			return fmt.Errorf("deadline reached after %d of %d users "+
				"(%d pushed, %d failed) — re-run with a longer -timeout: %w",
				i, len(assignments), pushed, failed, err)
		}

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

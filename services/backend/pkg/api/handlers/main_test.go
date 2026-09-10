package handlers_test

// TestMain seeds env vars every isolation test in this package needs.
//
// 19-01 hardened `api.NewRouterWithValidator` to panic when
// SUPABASE_WEBHOOK_SECRET is unset — a production fail-loud that
// prevents deploying a webhook receiver with the signature bypass the
// prior code had. In tests we're not shipping anything, we just need the
// constructor to return a router; a stable non-empty value is enough.
//
// If a future plan adds more required env vars at router construction
// time (JWKS URL, service role key, etc.), extend this file rather than
// scattering `os.Setenv` calls across individual tests.

import (
	"os"
	"testing"
)

// TestGitHubWebhookSecret is the secret this package's tests sign with.
// Exported within the package so the webhook tests can produce signatures
// the router will accept, rather than reaching for the env var.
const TestGitHubWebhookSecret = "isolation-tests-github-webhook-secret-not-for-production"

func TestMain(m *testing.M) {
	if os.Getenv("SUPABASE_WEBHOOK_SECRET") == "" {
		os.Setenv("SUPABASE_WEBHOOK_SECRET", "isolation-tests-webhook-secret-not-for-production")
	}
	// 20-05 added the same fail-loud for the GitHub receiver: its HMAC
	// signature is the only authentication it has, so an empty secret
	// panics at construction rather than accepting unsigned deliveries.
	if os.Getenv("GITHUB_WEBHOOK_SECRET") == "" {
		os.Setenv("GITHUB_WEBHOOK_SECRET", TestGitHubWebhookSecret)
	}
	os.Exit(m.Run())
}

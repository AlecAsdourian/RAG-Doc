// Package auth's abuse.go carries the minimum-viable abuse protection
// for the public signup surface: a per-IP rate limit on the Supabase
// webhook, and a small disposable-email-domain blocklist consulted
// inside the webhook handler after signature verification succeeds.
//
// This is deliberately not a full anti-abuse system. Phase 24 adds
// captcha, richer rate-limit policies, and per-org billing controls;
// this file exists to keep the current signup path from being trivially
// spammable by a bot with valid Supabase credentials.
package auth

import (
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/go-chi/httprate"
)

// WebhookRateLimitPerMin caps how many webhook POSTs the same IP may
// deliver in a rolling minute. 10 is well above real Supabase retry
// traffic (which retries with exponential backoff on 5xx) and low
// enough to make a spam loop obvious. Overridable via env for tests
// and dev.
const defaultWebhookRateLimitPerMin = 10

// disposableEmailDomains is a hand-curated blocklist of well-known
// throwaway-email providers. Kept small and inline deliberately —
// pulling in a full domain-reputation service is a Phase 24 concern
// and would add a runtime dependency.
//
// If a legitimate user is caught by this list, they can re-sign up
// with a real email. If a domain not on the list starts flooding the
// webhook, add it here in the follow-up PR. The list is intentionally
// short so a future domain-reputation service replaces it cleanly.
//
// Sources checked (2026-09-07):
//   - github.com/disposable-email-domains/disposable-email-domains (MIT)
//   - github.com/wesbos/burner-email-providers (MIT)
var disposableEmailDomains = map[string]struct{}{
	"10minutemail.com":     {},
	"10minutemail.net":     {},
	"20minutemail.com":     {},
	"33mail.com":           {},
	"burnermail.io":        {},
	"disposable.email":     {},
	"fakeinbox.com":        {},
	"guerrillamail.com":    {},
	"guerrillamail.net":    {},
	"mailinator.com":       {},
	"mailinator.net":       {},
	"maildrop.cc":          {},
	"mintemail.com":        {},
	"mytemp.email":         {},
	"nada.email":           {},
	"sharklasers.com":      {},
	"tempmail.com":         {},
	"tempmail.io":          {},
	"tempmailer.com":       {},
	"throwawaymail.com":    {},
	"trashmail.com":        {},
	"yopmail.com":          {},
}

// RateLimitWebhook returns a middleware that limits webhook POSTs per
// source IP. Reads the limit from WEBHOOK_RATE_LIMIT_PER_MIN if set,
// otherwise uses the default. httprate honors the RealIP middleware
// higher in the chain (see router's `middleware.RealIP`), so
// X-Forwarded-For does the right thing behind a proxy.
func RateLimitWebhook() func(http.Handler) http.Handler {
	limit := defaultWebhookRateLimitPerMin
	if v := os.Getenv("WEBHOOK_RATE_LIMIT_PER_MIN"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			limit = n
		}
	}
	// httprate.LimitByIP sets Retry-After on 429 responses automatically.
	return httprate.LimitByIP(limit, time.Minute)
}

// IsDisposableEmail returns true when the domain part of `addr` is a
// known throwaway-email provider. Case-insensitive. Malformed input
// (missing `@`, empty) returns false — validation of the address shape
// itself is the caller's responsibility.
func IsDisposableEmail(addr string) bool {
	at := strings.LastIndex(addr, "@")
	if at < 0 || at == len(addr)-1 {
		return false
	}
	domain := strings.ToLower(addr[at+1:])
	_, ok := disposableEmailDomains[domain]
	return ok
}

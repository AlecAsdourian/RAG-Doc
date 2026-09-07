// Package auth's abuse.go carries a disposable-email-domain blocklist
// consulted inside the webhook handler after signature verification
// succeeds. That's the whole abuse guard — deliberately.
//
// The initial cut of this file also shipped a per-IP rate limit on the
// webhook route. The reviewer on PR #13 pointed out (correctly) that it
// was security theater: chi's `middleware.RealIP` rewrites `RemoteAddr`
// from client-supplied headers, so a bot spoofing X-Forwarded-For gets
// unbounded buckets — or can pin the header to a victim's IP to DoS
// them out of signing up. On top of that, the webhook's real defense is
// the HMAC signature check: an attacker without the shared secret
// cannot deliver ANY event, so rate-limiting the endpoint against
// signature-forging attackers is defense against nothing. Real edge
// rate limiting is a Phase 24 concern (CDN/WAF at the ingress).
//
// The disposable-email check IS worth keeping: Supabase itself
// throttles bot signups upstream, but if a signup does succeed with a
// throwaway address, we refuse to auto-provision an organization for
// it. That's a policy check, not a rate limit, and it fires only after
// the signature check has validated the caller.
package auth

import "strings"

// disposableEmailDomains is a hand-curated blocklist of well-known
// throwaway-email providers. Matches on exact-domain OR any subdomain
// (see IsDisposableEmail below) — Mailinator and several other
// providers deliver arbitrary subdomains to the same inbox pool, so
// exact-match would be a two-character bypass.
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
	"10minutemail.com":  {},
	"10minutemail.net":  {},
	"20minutemail.com":  {},
	"33mail.com":        {},
	"burnermail.io":     {},
	"disposable.email":  {},
	"fakeinbox.com":     {},
	"grr.la":            {},
	"guerrillamail.biz": {},
	"guerrillamail.com": {},
	"guerrillamail.info": {},
	"guerrillamail.net": {},
	"guerrillamail.org": {},
	"mailinator.com":    {},
	"mailinator.net":    {},
	"maildrop.cc":       {},
	"mintemail.com":     {},
	"mytemp.email":      {},
	"nada.email":        {},
	"pokemail.net":      {},
	"sharklasers.com":   {},
	"spam4.me":          {},
	"temp-mail.org":     {},
	"tempmail.com":      {},
	"tempmail.io":       {},
	"tempmailer.com":    {},
	"tmpmail.org":       {},
	"throwawaymail.com": {},
	"trashmail.com":     {},
	"yopmail.com":       {},
}

// IsDisposableEmail returns true when the domain part of `addr` matches
// a known throwaway-email provider — either exactly, or as a subdomain
// of one. Case-insensitive. Malformed input (missing `@`, empty) returns
// false; validation of the address shape itself is the caller's job.
//
// The subdomain rule uses a dot boundary (`.`+entry) so an adversary
// cannot craft `evil.example.commailinator.com` to match `mailinator.com`.
func IsDisposableEmail(addr string) bool {
	at := strings.LastIndex(addr, "@")
	if at < 0 || at == len(addr)-1 {
		return false
	}
	domain := strings.ToLower(addr[at+1:])
	if _, ok := disposableEmailDomains[domain]; ok {
		return true
	}
	for entry := range disposableEmailDomains {
		if strings.HasSuffix(domain, "."+entry) {
			return true
		}
	}
	return false
}

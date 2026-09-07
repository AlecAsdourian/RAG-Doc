package auth

import (
	"testing"

	"github.com/stretchr/testify/assert"
)

func TestIsDisposableEmail(t *testing.T) {
	cases := []struct {
		addr string
		want bool
	}{
		// Exact-match blocklisted domains.
		{"alice@mailinator.com", true},
		{"Alice@Mailinator.COM", true}, // case-insensitive
		{"bob@10minutemail.com", true},
		{"carol@guerrillamail.net", true},
		{"dave@yopmail.com", true},
		{"eve@grr.la", true},

		// Subdomain of a blocklisted provider — Mailinator delivers every
		// subdomain to the same inbox pool. Exact-match was a two-character
		// bypass; suffix-match closes it.
		{"alice@sub.mailinator.com", true},
		{"bot@deep.subdomain.mailinator.com", true},

		// Legit domains.
		{"alice@example.com", false},
		{"bob@company.io", false},
		{"admin@test.org", false},

		// Malformed input — caller validates shape; we return false.
		{"no-at-sign", false},
		{"alice@", false},
		{"", false},
	}

	for _, c := range cases {
		t.Run(c.addr, func(t *testing.T) {
			assert.Equal(t, c.want, IsDisposableEmail(c.addr))
		})
	}
}

// TestIsDisposableEmail_BoundaryGuard specifically pins the dot-boundary
// rule so a future refactor to `strings.HasSuffix(domain, entry)` (without
// the leading dot) fails loudly rather than silently letting through
// crafted domains that end in the target string but aren't subdomains.
func TestIsDisposableEmail_BoundaryGuard(t *testing.T) {
	// evilmailinator.com is NOT a subdomain of mailinator.com — it just
	// ends with the same letters. Must return false.
	assert.False(t, IsDisposableEmail("attacker@evilmailinator.com"))
	// sub.mailinator.com IS a subdomain — must return true.
	assert.True(t, IsDisposableEmail("attacker@sub.mailinator.com"))
}

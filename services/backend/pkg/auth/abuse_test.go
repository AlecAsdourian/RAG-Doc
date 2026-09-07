package auth

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/stretchr/testify/assert"
)

func TestIsDisposableEmail(t *testing.T) {
	cases := []struct {
		addr string
		want bool
	}{
		// Blocklisted domains.
		{"alice@mailinator.com", true},
		{"Alice@Mailinator.COM", true}, // case-insensitive
		{"bob@10minutemail.com", true},
		{"carol@guerrillamail.net", true},
		{"dave@yopmail.com", true},

		// Legit domains.
		{"alice@example.com", false},
		{"bob@company.io", false},
		{"admin@test.org", false},

		// Malformed input — caller validates shape; we return false.
		{"no-at-sign", false},
		{"alice@", false}, // empty domain
		{"", false},

		// Subdomain of a blocklisted domain is NOT blocked (deliberate — we
		// check exact-match, not suffix). Documents current behavior.
		{"alice@sub.mailinator.com", false},
	}

	for _, c := range cases {
		t.Run(c.addr, func(t *testing.T) {
			assert.Equal(t, c.want, IsDisposableEmail(c.addr))
		})
	}
}

func TestRateLimitWebhook_ReturnsHandler(t *testing.T) {
	// Smoke test: middleware constructs without panicking and returns a
	// handler that passes requests through when under the limit.
	mw := RateLimitWebhook()
	wrapped := mw(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusAccepted)
	}))

	req := httptest.NewRequest(http.MethodPost, "/webhooks/supabase", nil)
	req.RemoteAddr = "192.0.2.1:1234"
	rr := httptest.NewRecorder()
	wrapped.ServeHTTP(rr, req)
	assert.Equal(t, http.StatusAccepted, rr.Code)
}

func TestRateLimitWebhook_LimitsBurst(t *testing.T) {
	// Fire >10 requests from the same IP in the same minute. The middleware
	// should let the first ten through and 429 the eleventh.
	mw := RateLimitWebhook()
	wrapped := mw(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusAccepted)
	}))

	statuses := make([]int, 0, 15)
	for i := 0; i < 15; i++ {
		req := httptest.NewRequest(http.MethodPost, "/webhooks/supabase", nil)
		req.RemoteAddr = "192.0.2.42:1234"
		rr := httptest.NewRecorder()
		wrapped.ServeHTTP(rr, req)
		statuses = append(statuses, rr.Code)
	}

	// First 10 should be 202, the remainder 429. Exact counts depend on
	// httprate's window arithmetic; assert the shape rather than exact
	// indices.
	var passed, rejected int
	for _, s := range statuses {
		switch s {
		case http.StatusAccepted:
			passed++
		case http.StatusTooManyRequests:
			rejected++
		}
	}
	assert.LessOrEqual(t, passed, 10, "no more than 10 requests should pass in a burst")
	assert.GreaterOrEqual(t, rejected, 1, "at least one request should be rate-limited")
}

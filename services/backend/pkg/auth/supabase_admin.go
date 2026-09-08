package auth

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// AdminClient writes server-controlled metadata onto a Supabase user.
//
// This is the carrier that gets organization context into the JWT.
// Supabase surfaces `raw_app_meta_data` as the `app_metadata` claim on
// every access token it issues, so writing here is what makes
// `app_metadata.organization_id` appear for TenantMiddleware to read.
//
// Verified against the live project 2026-09-08 (19-03 plan, Sub-step D):
// Supabase MERGES the supplied app_metadata keys rather than replacing
// the object, so callers send only the keys they own and Supabase's own
// `provider` / `providers` survive untouched. No read-modify-write is
// required — which matters, because this project's admin READ endpoints
// currently return 500 while writes work fine.
type AdminClient interface {
	UpdateUserAppMetadata(ctx context.Context, supabaseUserID string, meta map[string]any) error
}

// httpAdminClient is the real implementation, talking to Supabase's
// GoTrue admin API with the service-role key.
type httpAdminClient struct {
	baseURL        string
	serviceRoleKey string
	httpClient     *http.Client
}

// NewAdminClient builds an AdminClient for the given Supabase project.
//
// Panics if either argument is empty, matching the fail-closed pattern
// established for the webhook secret in 19-01: a deployment missing its
// credentials should fail at startup rather than silently degrade to a
// state where org context never reaches any JWT.
func NewAdminClient(baseURL, serviceRoleKey string) AdminClient {
	if strings.TrimSpace(baseURL) == "" {
		panic("auth.NewAdminClient: baseURL is empty; set SUPABASE_URL")
	}
	if strings.TrimSpace(serviceRoleKey) == "" {
		panic("auth.NewAdminClient: serviceRoleKey is empty; set SUPABASE_SERVICE_ROLE_KEY")
	}
	return &httpAdminClient{
		baseURL:        strings.TrimRight(baseURL, "/"),
		serviceRoleKey: serviceRoleKey,
		httpClient: &http.Client{
			Timeout: 10 * time.Second,
			// Never follow a redirect. Go strips `Authorization` when a
			// redirect crosses to a different host, but its sensitive-header
			// list does not include GoTrue's custom `apikey` header — which
			// carries the same service-role secret. A redirect to an
			// attacker-controlled host would hand them the key verbatim.
			//
			// The admin API has no legitimate reason to redirect, so
			// refusing to follow costs nothing: the 3xx surfaces as a
			// non-2xx error below.
			CheckRedirect: func(*http.Request, []*http.Request) error {
				return http.ErrUseLastResponse
			},
		},
	}
}

// UpdateUserAppMetadata merges meta into the user's app_metadata.
//
// Errors carry the status and a truncated response body. The response body
// is scrubbed of the service-role key before it goes anywhere — see
// redactSecret.
func (c *httpAdminClient) UpdateUserAppMetadata(
	ctx context.Context,
	supabaseUserID string,
	meta map[string]any,
) error {
	if supabaseUserID == "" {
		return fmt.Errorf("supabase user id is empty")
	}
	if len(meta) == 0 {
		return fmt.Errorf("no metadata supplied")
	}

	body, err := json.Marshal(map[string]any{"app_metadata": meta})
	if err != nil {
		return fmt.Errorf("marshal app_metadata: %w", err)
	}

	endpoint := c.baseURL + "/auth/v1/admin/users/" + url.PathEscape(supabaseUserID)
	req, err := http.NewRequestWithContext(ctx, http.MethodPut, endpoint, bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("build admin request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	// GoTrue requires both. `apikey` identifies the project; the bearer
	// token authorizes the admin operation.
	req.Header.Set("apikey", c.serviceRoleKey)
	req.Header.Set("Authorization", "Bearer "+c.serviceRoleKey)

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("admin request failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		snippet, _ := io.ReadAll(io.LimitReader(resp.Body, 300))
		return fmt.Errorf(
			"supabase admin returned %d updating user %s: %s",
			resp.StatusCode, supabaseUserID,
			c.redactSecret(strings.TrimSpace(string(snippet))),
		)
	}

	return nil
}

// redactSecret removes the service-role key from text that is about to be
// embedded in an error — and therefore, at the only call site, written to
// the log.
//
// This is not paranoia about our own format string. The response body is
// written by whatever answered the request, which on a bad day is a proxy,
// WAF, or CDN error page rather than GoTrue. Several of those echo the
// request headers back in the body for debugging, and this client sends the
// key in `apikey` and `Authorization`. Without this, one 502 from a
// header-echoing intermediary puts the service-role key in the application
// log in plaintext.
func (c *httpAdminClient) redactSecret(s string) string {
	if c.serviceRoleKey == "" {
		return s
	}
	return strings.ReplaceAll(s, c.serviceRoleKey, "[REDACTED]")
}

package auth

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

const testServiceKey = "test-service-role-key-not-real"

func TestNewAdminClient_PanicsOnMissingCredentials(t *testing.T) {
	assert.Panics(t, func() { NewAdminClient("", testServiceKey) },
		"empty baseURL must panic")
	assert.Panics(t, func() { NewAdminClient("https://x.supabase.co", "") },
		"empty serviceRoleKey must panic — a missing key would silently stop org context reaching any JWT")
	assert.Panics(t, func() { NewAdminClient("https://x.supabase.co", "   ") },
		"whitespace-only serviceRoleKey must panic too")
}

func TestUpdateUserAppMetadata_RequestShape(t *testing.T) {
	type captured struct {
		method string
		path   string
		apikey string
		auth   string
		ctype  string
		body   map[string]any
	}
	var got captured

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got.method = r.Method
		got.path = r.URL.Path
		got.apikey = r.Header.Get("apikey")
		got.auth = r.Header.Get("Authorization")
		got.ctype = r.Header.Get("Content-Type")
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &got.body)
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"id":"u-1"}`))
	}))
	t.Cleanup(srv.Close)

	c := NewAdminClient(srv.URL, testServiceKey)
	err := c.UpdateUserAppMetadata(context.Background(), "u-1", map[string]any{
		"organization_id":   "org-123",
		"organization_role": "owner",
	})
	require.NoError(t, err)

	assert.Equal(t, http.MethodPut, got.method)
	assert.Equal(t, "/auth/v1/admin/users/u-1", got.path)
	assert.Equal(t, testServiceKey, got.apikey, "GoTrue requires the apikey header")
	assert.Equal(t, "Bearer "+testServiceKey, got.auth)
	assert.Equal(t, "application/json", got.ctype)

	// Body must nest under app_metadata, carrying only our keys. Supabase
	// merges server-side (verified 2026-09-08), so sending a partial
	// object is correct — sending provider/providers back would risk
	// clobbering Supabase's own values.
	am, ok := got.body["app_metadata"].(map[string]any)
	require.True(t, ok, "payload must nest under app_metadata, got: %+v", got.body)
	assert.Equal(t, "org-123", am["organization_id"])
	assert.Equal(t, "owner", am["organization_role"])
	assert.NotContains(t, am, "provider", "must not send Supabase's own keys back")
}

func TestUpdateUserAppMetadata_EscapesUserIDInPath(t *testing.T) {
	var gotPath string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.EscapedPath()
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(srv.Close)

	c := NewAdminClient(srv.URL, testServiceKey)
	err := c.UpdateUserAppMetadata(context.Background(), "weird/../id", map[string]any{"k": "v"})
	require.NoError(t, err)

	// What matters is that the SLASHES are escaped, so the id cannot
	// escape its path segment. A literal ".." with no slashes around it
	// is inert — asserting on its absence would be testing the wrong
	// property.
	assert.Equal(t, "/auth/v1/admin/users/weird%2F..%2Fid", gotPath,
		"slashes in the user id must be percent-encoded so it stays one path segment")
}

func TestUpdateUserAppMetadata_NonSuccessReturnsError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte(`{"code":500,"msg":"Database error loading user"}`))
	}))
	t.Cleanup(srv.Close)

	c := NewAdminClient(srv.URL, testServiceKey)
	err := c.UpdateUserAppMetadata(context.Background(), "u-1", map[string]any{"k": "v"})

	require.Error(t, err)
	assert.Contains(t, err.Error(), "500", "error should name the status")
	assert.Contains(t, err.Error(), "Database error loading user", "error should include the response body")
}

// TestUpdateUserAppMetadata_NeverLeaksServiceKey is the one that matters
// for incident hygiene: the service-role key is a full-project credential
// and must never reach a log line or an error surfaced to a caller.
//
// The failing server here echoes the key into its response body, which is
// not a contrived scenario — proxies, WAFs and CDN error pages routinely
// dump request headers into their bodies, and this client sends the key in
// both `apikey` and `Authorization`.
//
// This assertion deliberately covers the WHOLE error string. An earlier
// version split on the first colon and inspected only the prefix, which
// tested the format string and nothing else: the response body — the only
// part that could ever carry the key — was the half being thrown away, and
// the test passed while the key flowed straight into the error.
func TestUpdateUserAppMetadata_NeverLeaksServiceKey(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadGateway)
		_, _ = w.Write([]byte(
			`<html>502 Bad Gateway. Request headers: apikey=` + testServiceKey +
				` authorization=Bearer ` + testServiceKey + `</html>`))
	}))
	t.Cleanup(srv.Close)

	c := NewAdminClient(srv.URL, testServiceKey)
	err := c.UpdateUserAppMetadata(context.Background(), "u-1", map[string]any{"k": "v"})
	require.Error(t, err)

	assert.NotContains(t, err.Error(), testServiceKey,
		"the service-role key must not appear anywhere in the error, including the echoed body")
	assert.Contains(t, err.Error(), "[REDACTED]",
		"the key should be replaced in place, so the rest of the body stays debuggable")
	assert.Contains(t, err.Error(), "502",
		"redaction must not cost us the status code")
}

// TestUpdateUserAppMetadata_DoesNotFollowRedirects guards the second way
// the key can walk out the door.
//
// Go's http.Client strips `Authorization` when a redirect crosses to a
// different host — but its sensitive-header list knows nothing about
// GoTrue's custom `apikey` header, which carries the very same secret. A
// redirect to an attacker-controlled host would hand it over verbatim.
//
// The admin API has no legitimate reason to redirect, so the client
// refuses to follow one at all and surfaces the 3xx as an error.
func TestUpdateUserAppMetadata_DoesNotFollowRedirects(t *testing.T) {
	var redirectTargetSawKey bool
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("apikey") == testServiceKey {
			redirectTargetSawKey = true
		}
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(target.Close)

	redirector := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, target.URL+"/auth/v1/admin/users/u-1", http.StatusTemporaryRedirect)
	}))
	t.Cleanup(redirector.Close)

	c := NewAdminClient(redirector.URL, testServiceKey)
	err := c.UpdateUserAppMetadata(context.Background(), "u-1", map[string]any{"k": "v"})

	require.Error(t, err, "a redirect must surface as an error, not be followed silently")
	assert.False(t, redirectTargetSawKey,
		"the service-role key must never be sent to a redirect target")
}

// fakeAdmin records calls and can be told to fail.
type fakeAdmin struct {
	calls    []fakeAdminCall
	failWith error
}

type fakeAdminCall struct {
	userID string
	meta   map[string]any
}

func (f *fakeAdmin) UpdateUserAppMetadata(_ context.Context, userID string, meta map[string]any) error {
	f.calls = append(f.calls, fakeAdminCall{userID: userID, meta: meta})
	return f.failWith
}

// TestWebhook_PushesOrgContextAfterProvisioning verifies the 19-03 wiring:
// a successful provision results in organization context being written to
// Supabase, so the next token carries app_metadata.organization_id.
func TestWebhook_PushesOrgContextAfterProvisioning(t *testing.T) {
	db := SetupTestDB(t)
	defer CleanupTestDB(t, db)

	admin := &fakeAdmin{}
	handler := NewWebhookHandler(db, "test-secret-key", admin)

	supabaseID := "770e8400-e29b-41d4-a716-44665544aaaa"
	rr := deliverUserCreated(t, handler, supabaseID, "pusher@example.com")
	require.Equal(t, http.StatusAccepted, rr.Code)

	require.Len(t, admin.calls, 1, "org context must be pushed exactly once")
	call := admin.calls[0]
	assert.Equal(t, supabaseID, call.userID)
	assert.NotEmpty(t, call.meta["organization_id"], "must carry the provisioned org id")
	assert.Equal(t, "owner", call.meta["organization_role"])
	assert.NotContains(t, call.meta, "provider",
		"must send only our keys — Supabase merges and owns provider/providers")
}

// TestWebhook_AdminPushFailureIsNotFatal pins the deliberate choice that
// our database is the source of truth and the Supabase write is a
// projection. A failed push must not turn a successful provision into a
// 500, and must not roll back the user or organization.
func TestWebhook_AdminPushFailureIsNotFatal(t *testing.T) {
	db := SetupTestDB(t)
	defer CleanupTestDB(t, db)

	admin := &fakeAdmin{failWith: assertAnError{}}
	handler := NewWebhookHandler(db, "test-secret-key", admin)

	rr := deliverUserCreated(t, handler, "770e8400-e29b-41d4-a716-44665544bbbb", "resilient@example.com")

	assert.Equal(t, http.StatusAccepted, rr.Code,
		"a failed Supabase push must not fail the webhook")

	var users int
	require.NoError(t, db.QueryRow(context.Background(),
		"SELECT COUNT(*) FROM users WHERE email = $1", "resilient@example.com").Scan(&users))
	assert.Equal(t, 1, users, "user must still be provisioned")

	var orgs int
	require.NoError(t, db.QueryRow(context.Background(),
		`SELECT COUNT(*) FROM organization_memberships om
		 JOIN users u ON u.id = om.user_id
		 WHERE u.email = $1 AND om.role = 'owner'`, "resilient@example.com").Scan(&orgs))
	assert.Equal(t, 1, orgs, "organization must still be created")
}

// TestWebhook_NilAdminClientDegradesGracefully — offline dev and tests
// construct the handler without Supabase; that must not panic.
func TestWebhook_NilAdminClientDegradesGracefully(t *testing.T) {
	db := SetupTestDB(t)
	defer CleanupTestDB(t, db)

	handler := NewWebhookHandler(db, "test-secret-key", nil)
	rr := deliverUserCreated(t, handler, "770e8400-e29b-41d4-a716-44665544cccc", "nil-admin@example.com")
	assert.Equal(t, http.StatusAccepted, rr.Code)
}

type assertAnError struct{}

func (assertAnError) Error() string { return "simulated supabase admin failure" }

// deliverUserCreated posts a correctly-signed user.created event.
func deliverUserCreated(t *testing.T, h *WebhookHandler, supabaseID, email string) *httptest.ResponseRecorder {
	t.Helper()

	payload := SupabaseWebhookEvent{
		Type:   "INSERT",
		Table:  "auth_user_events",
		Schema: "public",
		Record: json.RawMessage(`{
			"id": "` + supabaseID + `",
			"supabase_user_id": "` + supabaseID + `",
			"email": "` + email + `",
			"event_type": "INSERT",
			"raw_user_meta_data": {"full_name": "Test Person", "provider": "github"},
			"created_at": "2026-09-08T12:00:00Z"
		}`),
	}
	body, err := json.Marshal(payload)
	require.NoError(t, err)

	mac := hmac.New(sha256.New, []byte("test-secret-key"))
	mac.Write(body)
	sig := hex.EncodeToString(mac.Sum(nil))

	req := httptest.NewRequest(http.MethodPost, "/webhooks/supabase", bytes.NewReader(body))
	req.Header.Set("X-Webhook-Signature", sig)
	rr := httptest.NewRecorder()
	h.HandleSupabaseWebhook()(rr, req)
	return rr
}

func TestUpdateUserAppMetadata_RejectsEmptyInput(t *testing.T) {
	c := NewAdminClient("https://x.supabase.co", testServiceKey)

	err := c.UpdateUserAppMetadata(context.Background(), "", map[string]any{"k": "v"})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "user id")

	err = c.UpdateUserAppMetadata(context.Background(), "u-1", nil)
	require.Error(t, err)
	assert.Contains(t, err.Error(), "metadata")
}

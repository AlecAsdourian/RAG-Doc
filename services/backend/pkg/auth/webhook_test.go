package auth

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestWebhookHandler_SignatureVerification verifies HMAC signature validation
func TestWebhookHandler_SignatureVerification(t *testing.T) {
	db := SetupTestDB(t)
	defer CleanupTestDB(t, db)

	// Set webhook secret
	os.Setenv("SUPABASE_WEBHOOK_SECRET", "test-secret-key")
	defer os.Unsetenv("SUPABASE_WEBHOOK_SECRET")

	handler := NewWebhookHandler(db)

	payload := []byte(`{"type":"INSERT","table":"users","schema":"auth","record":{}}`)

	// Compute valid signature
	mac := hmac.New(sha256.New, []byte("test-secret-key"))
	mac.Write(payload)
	validSignature := hex.EncodeToString(mac.Sum(nil))

	// Test valid signature
	assert.True(t, handler.verifySignature(payload, validSignature), "Valid signature should pass")

	// Test invalid signature
	assert.False(t, handler.verifySignature(payload, "invalid-signature"), "Invalid signature should fail")

	// Test missing signature
	assert.False(t, handler.verifySignature(payload, ""), "Missing signature should fail")
}

// TestWebhookHandler_UserCreatedEvent verifies user provisioning from webhook
func TestWebhookHandler_UserCreatedEvent(t *testing.T) {
	db := SetupTestDB(t)
	defer CleanupTestDB(t, db)

	// Set webhook secret
	os.Setenv("SUPABASE_WEBHOOK_SECRET", "test-secret-key")
	defer os.Unsetenv("SUPABASE_WEBHOOK_SECRET")

	handler := NewWebhookHandler(db)

	// Create webhook payload for user creation
	webhookPayload := SupabaseWebhookEvent{
		Type:   "INSERT",
		Table:  "users",
		Schema: "auth",
		Record: json.RawMessage(`{
			"id": "550e8400-e29b-41d4-a716-446655440000",
			"email": "test@example.com",
			"user_metadata": {
				"full_name": "Test User"
			},
			"provider": "github",
			"created_at": "2024-01-13T12:00:00Z"
		}`),
	}

	payloadBytes, err := json.Marshal(webhookPayload)
	require.NoError(t, err)

	// Compute signature
	mac := hmac.New(sha256.New, []byte("test-secret-key"))
	mac.Write(payloadBytes)
	signature := hex.EncodeToString(mac.Sum(nil))

	// Create HTTP request
	req := httptest.NewRequest(http.MethodPost, "/webhooks/supabase", bytes.NewReader(payloadBytes))
	req.Header.Set("X-Webhook-Signature", signature)
	req.Header.Set("Content-Type", "application/json")

	// Create response recorder
	rr := httptest.NewRecorder()

	// Handle webhook
	handler.HandleSupabaseWebhook()(rr, req)

	// Verify response
	assert.Equal(t, http.StatusOK, rr.Code, "Webhook should succeed")

	// Verify user was created in database
	var count int
	err = db.QueryRow(req.Context(),
		"SELECT COUNT(*) FROM users WHERE email = $1", "test@example.com").Scan(&count)
	require.NoError(t, err)
	assert.Equal(t, 1, count, "User should be created in database")

	// Verify organization was created
	var orgCount int
	err = db.QueryRow(req.Context(),
		`SELECT COUNT(*) FROM organizations WHERE name LIKE 'Test%'`).Scan(&orgCount)
	require.NoError(t, err)
	assert.Equal(t, 1, orgCount, "Organization should be created for new user")

	// Verify user is owner of organization
	var membershipCount int
	err = db.QueryRow(req.Context(),
		`SELECT COUNT(*) FROM organization_memberships om
		 JOIN users u ON om.user_id = u.id
		 WHERE u.email = $1 AND om.role = 'owner'`, "test@example.com").Scan(&membershipCount)
	require.NoError(t, err)
	assert.Equal(t, 1, membershipCount, "User should be owner of their organization")
}

// TestWebhookHandler_InvalidSignature verifies webhook rejects invalid signatures
func TestWebhookHandler_InvalidSignature(t *testing.T) {
	db := SetupTestDB(t)
	defer CleanupTestDB(t, db)

	// Set webhook secret
	os.Setenv("SUPABASE_WEBHOOK_SECRET", "test-secret-key")
	defer os.Unsetenv("SUPABASE_WEBHOOK_SECRET")

	handler := NewWebhookHandler(db)

	payload := []byte(`{"type":"INSERT","table":"users","schema":"auth","record":{}}`)

	// Create request with invalid signature
	req := httptest.NewRequest(http.MethodPost, "/webhooks/supabase", bytes.NewReader(payload))
	req.Header.Set("X-Webhook-Signature", "invalid-signature")

	rr := httptest.NewRecorder()
	handler.HandleSupabaseWebhook()(rr, req)

	assert.Equal(t, http.StatusUnauthorized, rr.Code, "Invalid signature should return 401")
}

// TestWebhookHandler_InvalidMethod verifies webhook only accepts POST
func TestWebhookHandler_InvalidMethod(t *testing.T) {
	db := SetupTestDB(t)
	defer CleanupTestDB(t, db)

	handler := NewWebhookHandler(db)

	// Try GET request
	req := httptest.NewRequest(http.MethodGet, "/webhooks/supabase", nil)
	rr := httptest.NewRecorder()

	handler.HandleSupabaseWebhook()(rr, req)

	assert.Equal(t, http.StatusMethodNotAllowed, rr.Code, "GET request should return 405")
}

// TestWebhookHandler_ExistingUser verifies webhook doesn't duplicate existing users
func TestWebhookHandler_ExistingUser(t *testing.T) {
	db := SetupTestDB(t)
	defer CleanupTestDB(t, db)

	// Set webhook secret
	os.Setenv("SUPABASE_WEBHOOK_SECRET", "test-secret-key")
	defer os.Unsetenv("SUPABASE_WEBHOOK_SECRET")

	handler := NewWebhookHandler(db)

	// Create user first
	existingUser := CreateTestUser(t, db, "existing@example.com", "Existing User")
	existingOrg := CreateTestOrg(t, db, "Existing Org", "existing-org")
	AddUserToOrg(t, db, existingUser, existingOrg, "owner")

	// Webhook payload for same user
	webhookPayload := SupabaseWebhookEvent{
		Type:   "INSERT",
		Table:  "users",
		Schema: "auth",
		Record: json.RawMessage(`{
			"id": "550e8400-e29b-41d4-a716-446655440001",
			"email": "existing@example.com",
			"user_metadata": {
				"full_name": "Existing User"
			},
			"provider": "github",
			"created_at": "2024-01-13T12:00:00Z"
		}`),
	}

	payloadBytes, err := json.Marshal(webhookPayload)
	require.NoError(t, err)

	// Compute signature
	mac := hmac.New(sha256.New, []byte("test-secret-key"))
	mac.Write(payloadBytes)
	signature := hex.EncodeToString(mac.Sum(nil))

	req := httptest.NewRequest(http.MethodPost, "/webhooks/supabase", bytes.NewReader(payloadBytes))
	req.Header.Set("X-Webhook-Signature", signature)

	rr := httptest.NewRecorder()
	handler.HandleSupabaseWebhook()(rr, req)

	assert.Equal(t, http.StatusOK, rr.Code, "Webhook should succeed for existing user")

	// Verify only one user record exists
	var count int
	err = db.QueryRow(req.Context(),
		"SELECT COUNT(*) FROM users WHERE email = $1", "existing@example.com").Scan(&count)
	require.NoError(t, err)
	assert.Equal(t, 1, count, "Should not duplicate user")

	// Verify only one organization exists
	var orgCount int
	err = db.QueryRow(req.Context(),
		"SELECT COUNT(*) FROM organizations WHERE slug = 'existing-org'").Scan(&orgCount)
	require.NoError(t, err)
	assert.Equal(t, 1, orgCount, "Should not create duplicate organization")
}

// TestGenerateOrgNameFromEmail verifies organization name generation
func TestGenerateOrgNameFromEmail(t *testing.T) {
	tests := []struct {
		email    string
		expected string
	}{
		{"alice@example.com", "Alice's Organization"},
		{"bob.smith@company.io", "Bob's Organization"},
		{"admin@test.org", "Admin's Organization"},
		{"", "My Organization"},
	}

	for _, tt := range tests {
		t.Run(tt.email, func(t *testing.T) {
			result := generateOrgNameFromEmail(tt.email)
			assert.Equal(t, tt.expected, result)
		})
	}
}

// TestGenerateOrgSlugFromEmail verifies organization slug generation
func TestGenerateOrgSlugFromEmail(t *testing.T) {
	tests := []struct {
		email    string
		expected string
	}{
		{"alice@example.com", "alice-org"},
		{"bob.smith@company.io", "bob-smith-org"},
		{"admin_user@test.org", "admin-user-org"},
		{"", "my-org"},
	}

	for _, tt := range tests {
		t.Run(tt.email, func(t *testing.T) {
			result := generateOrgSlugFromEmail(tt.email)
			assert.Equal(t, tt.expected, result)
		})
	}
}

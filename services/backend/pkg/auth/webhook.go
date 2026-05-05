package auth

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strings"

	"github.com/jackc/pgx/v5/pgxpool"
)

// SupabaseWebhookEvent represents the structure of Supabase database webhook events
type SupabaseWebhookEvent struct {
	Type      string          `json:"type"`       // e.g., "INSERT", "UPDATE", "DELETE"
	Table     string          `json:"table"`      // e.g., "auth_user_events"
	Schema    string          `json:"schema"`     // e.g., "public"
	Record    json.RawMessage `json:"record"`     // The actual data
	OldRecord json.RawMessage `json:"old_record,omitempty"`
}

// AuthUserEvent represents a record from our auth_user_events table
// This is populated by a database trigger on auth.users
type AuthUserEvent struct {
	ID              string                 `json:"id"`                // Event UUID
	SupabaseUserID  string                 `json:"supabase_user_id"`  // User's Supabase Auth UUID
	Email           string                 `json:"email"`
	RawUserMetaData map[string]interface{} `json:"raw_user_meta_data"` // Contains provider info, name, etc.
	EventType       string                 `json:"event_type"`         // INSERT, UPDATE, DELETE
	CreatedAt       string                 `json:"created_at"`
}

// SupabaseAuthUser represents a user record from Supabase Auth (legacy, kept for reference)
type SupabaseAuthUser struct {
	ID           string                 `json:"id"`
	Email        string                 `json:"email"`
	UserMetadata map[string]interface{} `json:"user_metadata"`
	AppMetadata  map[string]interface{} `json:"app_metadata"`
	CreatedAt    string                 `json:"created_at"`
	Provider     string                 `json:"provider"`
	ProviderID   string                 `json:"provider_id"`
}

// WebhookHandler handles Supabase webhook events
type WebhookHandler struct {
	provisioner   *UserProvisioner
	webhookSecret string
}

// NewWebhookHandler creates a new webhook handler
func NewWebhookHandler(db *pgxpool.Pool) *WebhookHandler {
	return &WebhookHandler{
		provisioner:   NewUserProvisioner(db),
		webhookSecret: os.Getenv("SUPABASE_WEBHOOK_SECRET"),
	}
}

// HandleSupabaseWebhook processes incoming Supabase webhook events
func (h *WebhookHandler) HandleSupabaseWebhook() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		// Only accept POST requests
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}

		// Read request body
		body, err := io.ReadAll(r.Body)
		if err != nil {
			http.Error(w, "Failed to read request body", http.StatusBadRequest)
			return
		}
		defer r.Body.Close()

		// Verify webhook signature
		signature := r.Header.Get("X-Webhook-Signature")
		if !h.verifySignature(body, signature) {
			http.Error(w, "Invalid webhook signature", http.StatusUnauthorized)
			return
		}

		// Parse webhook event
		var event SupabaseWebhookEvent
		if err := json.Unmarshal(body, &event); err != nil {
			log.Printf("[Webhook] Failed to parse payload: %v", err)
			http.Error(w, "Invalid webhook payload", http.StatusBadRequest)
			return
		}

		log.Printf("[Webhook] Received event: schema=%s table=%s type=%s", event.Schema, event.Table, event.Type)

		// Handle user creation events from our auth_user_events table
		// This table is populated by a database trigger on auth.users
		if event.Schema == "public" && event.Table == "auth_user_events" && event.Type == "INSERT" {
			if err := h.handleAuthUserEvent(r, event.Record); err != nil {
				http.Error(w, fmt.Sprintf("Failed to process user creation: %v", err), http.StatusInternalServerError)
				return
			}

			w.WriteHeader(http.StatusOK)
			json.NewEncoder(w).Encode(map[string]string{
				"status": "success",
				"message": "User provisioned successfully",
			})
			return
		}

		// Ignore other events
		w.WriteHeader(http.StatusOK)
		json.NewEncoder(w).Encode(map[string]string{
			"status": "ignored",
			"message": "Event type not handled",
		})
	}
}

// verifySignature verifies the HMAC-SHA256 signature of the webhook payload
func (h *WebhookHandler) verifySignature(payload []byte, signature string) bool {
	if h.webhookSecret == "" {
		// No secret configured - skip verification in development
		// WARNING: This should never happen in production
		return true
	}

	if signature == "" {
		return false
	}

	// Compute expected signature
	mac := hmac.New(sha256.New, []byte(h.webhookSecret))
	mac.Write(payload)
	expectedSignature := hex.EncodeToString(mac.Sum(nil))

	// Compare signatures (constant-time comparison to prevent timing attacks)
	return hmac.Equal([]byte(signature), []byte(expectedSignature))
}

// handleAuthUserEvent processes a user event from our auth_user_events table
// This is triggered by our database trigger on auth.users
func (h *WebhookHandler) handleAuthUserEvent(r *http.Request, recordData json.RawMessage) error {
	var event AuthUserEvent
	if err := json.Unmarshal(recordData, &event); err != nil {
		return fmt.Errorf("failed to parse auth user event: %w", err)
	}

	// Only process INSERT events (new user signups)
	if event.EventType != "INSERT" {
		return nil // Silently ignore UPDATE/DELETE events for now
	}

	// Extract full name from raw_user_meta_data
	// GitHub/GitLab OAuth populates this with user profile info
	fullName := ""
	if event.RawUserMetaData != nil {
		if name, ok := event.RawUserMetaData["full_name"].(string); ok {
			fullName = name
		} else if name, ok := event.RawUserMetaData["name"].(string); ok {
			fullName = name
		}
	}

	// Determine provider from raw_user_meta_data
	// Supabase stores provider info in the metadata
	provider := "email"
	if event.RawUserMetaData != nil {
		if p, ok := event.RawUserMetaData["provider"].(string); ok {
			provider = p
		}
	}

	// Provision user in our database
	provisionedUser, isNewUser, err := h.provisioner.ProvisionOAuthUser(
		r.Context(),
		provider,
		event.Email,
		fullName,
		event.SupabaseUserID,
	)
	if err != nil {
		return fmt.Errorf("failed to provision user: %w", err)
	}

	// For new users, create a default organization
	if isNewUser {
		orgName := generateOrgNameFromEmail(event.Email)
		orgSlug := generateOrgSlugFromEmail(event.Email)

		_, err := h.provisioner.CreateOrganizationForUser(
			r.Context(),
			provisionedUser.ID,
			orgName,
			orgSlug,
		)
		if err != nil {
			return fmt.Errorf("failed to create organization: %w", err)
		}
	}

	return nil
}

// generateOrgNameFromEmail generates a default organization name from email
// e.g., "alice@example.com" → "Alice's Organization"
func generateOrgNameFromEmail(email string) string {
	parts := strings.Split(email, "@")
	if len(parts) == 0 {
		return "My Organization"
	}

	username := parts[0]
	// Capitalize first letter
	if len(username) > 0 {
		username = strings.ToUpper(username[:1]) + username[1:]
	}

	return username + "'s Organization"
}

// generateOrgSlugFromEmail generates a URL-safe slug from email
// e.g., "alice@example.com" → "alice-org"
func generateOrgSlugFromEmail(email string) string {
	parts := strings.Split(email, "@")
	if len(parts) == 0 {
		return "my-org"
	}

	username := parts[0]
	// Replace non-alphanumeric characters with hyphens
	slug := strings.ToLower(username)
	slug = strings.ReplaceAll(slug, ".", "-")
	slug = strings.ReplaceAll(slug, "_", "-")

	return slug + "-org"
}

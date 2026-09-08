package auth

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"strings"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
)

// disposableEmailError is returned by handleAuthUserEvent when the email
// domain is on the disposable-email blocklist. The webhook wrapper
// converts this to a 422 response.
type disposableEmailError struct {
	email string
}

func (e *disposableEmailError) Error() string {
	return fmt.Sprintf("disposable email domain refused: %s", e.email)
}


// MaxWebhookBodyBytes bounds the request body the webhook is willing to
// read. 64KB is comfortably larger than a real Supabase user event
// (~1-2KB) and small enough to hold the whole payload in memory without
// concern. `http.MaxBytesReader` returns a 413 when exceeded.
const MaxWebhookBodyBytes = 64 << 10

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

// WebhookHandler handles Supabase webhook events.
//
// The webhook secret is required; a zero-value secret would fail-open on
// every signature check and let unauthenticated callers trigger user
// provisioning. Constructors must supply it explicitly — the caller
// reads it from `SUPABASE_WEBHOOK_SECRET` and panics on empty.
type WebhookHandler struct {
	provisioner   *UserProvisioner
	webhookSecret string
	admin         AdminClient
}

// NewWebhookHandler creates a webhook handler with the given secret and
// Supabase admin client.
//
// Panics if webhookSecret is empty. Callers (currently pkg/api/router.go)
// read the secret from env at startup and fail loudly rather than let a
// misconfigured deployment silently accept unsigned payloads.
//
// `admin` may be nil, in which case the org-context push to Supabase is
// skipped with a log line. That is a degraded mode, not a supported one:
// without the push, provisioned users never receive an
// `app_metadata.organization_id` claim and TenantMiddleware will 403
// every request they make. It exists so tests and offline dev can
// construct a handler without a live Supabase.
func NewWebhookHandler(db *pgxpool.Pool, webhookSecret string, admin AdminClient) *WebhookHandler {
	if webhookSecret == "" {
		panic("auth.NewWebhookHandler: webhookSecret is empty; set SUPABASE_WEBHOOK_SECRET before constructing the router")
	}
	return &WebhookHandler{
		provisioner:   NewUserProvisioner(db),
		webhookSecret: webhookSecret,
		admin:         admin,
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

		// Bound the body before reading. A malicious caller cannot exhaust
		// server memory by streaming an arbitrarily large payload; a
		// legitimate Supabase event is ~1-2KB.
		r.Body = http.MaxBytesReader(w, r.Body, MaxWebhookBodyBytes)
		defer r.Body.Close()

		body, err := io.ReadAll(r.Body)
		if err != nil {
			// MaxBytesReader returns *http.MaxBytesError since Go 1.19;
			// prefer errors.As over a string comparison so a stdlib
			// message rewording doesn't silently degrade 413 to 400.
			var maxErr *http.MaxBytesError
			if errors.As(err, &maxErr) {
				http.Error(w, "Request body too large", http.StatusRequestEntityTooLarge)
				return
			}
			http.Error(w, "Failed to read request body", http.StatusBadRequest)
			return
		}

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
				var disposable *disposableEmailError
				if errors.As(err, &disposable) {
					http.Error(w, "email domain not allowed", http.StatusUnprocessableEntity)
					return
				}
				// A different Supabase identity already owns this address.
				// 409 (not 5xx) so Supabase stops retrying — retrying cannot
				// resolve it, and the operator needs to reconcile the two
				// accounts by hand.
				if errors.Is(err, ErrEmailOwnedByAnotherIdentity) {
					log.Printf("[Webhook] identity conflict: %v", err)
					http.Error(w, "email registered to a different identity", http.StatusConflict)
					return
				}
				http.Error(w, fmt.Sprintf("Failed to process user creation: %v", err), http.StatusInternalServerError)
				return
			}

			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusAccepted)
			json.NewEncoder(w).Encode(map[string]string{
				"status":  "success",
				"message": "User provisioned successfully",
			})
			return
		}

		// Ignore other events
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusAccepted)
		json.NewEncoder(w).Encode(map[string]string{
			"status":  "ignored",
			"message": "Event type not handled",
		})
	}
}

// verifySignature verifies the HMAC-SHA256 signature of the webhook
// payload against the shared secret.
//
// Fail-closed. Prior versions of this method had a
// `if h.webhookSecret == "" { return true }` branch for "development
// convenience" that turned into a production vulnerability the moment
// SUPABASE_WEBHOOK_SECRET was ever unset. The constructor now panics on
// empty secret, and this function has no bypass — every request must
// present a valid signature.
func (h *WebhookHandler) verifySignature(payload []byte, signature string) bool {
	if signature == "" {
		return false
	}
	mac := hmac.New(sha256.New, []byte(h.webhookSecret))
	mac.Write(payload)
	expectedSignature := hex.EncodeToString(mac.Sum(nil))
	// hmac.Equal is constant-time; a naive `==` would leak signature
	// prefix bytes via a timing side-channel.
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

	// Refuse to provision throwaway-email signups. This runs AFTER signature
	// verification so we're not leaking the blocklist to random callers —
	// only Supabase itself can trigger this branch. Real anti-abuse ships
	// in Phase 24; this list is the minimum viable stopgap.
	if IsDisposableEmail(event.Email) {
		return &disposableEmailError{email: event.Email}
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

	// Create the starter organization if the user doesn't already own one.
	//
	// Deliberately NOT gated on isNewUser. If org creation fails after the
	// user row commits, we return 500, Supabase retries, and the retry now
	// sees an existing user (isNewUser=false). Gating on that flag meant
	// the retry skipped org creation entirely and returned 202 — leaving
	// the user permanently organization-less with no further retries and
	// no error anywhere. Checking actual ownership makes the whole handler
	// convergent: however many times it runs, the end state is one user
	// with one owner org.
	orgID, hasOrg, err := h.provisioner.UserOwnerOrgID(r.Context(), provisionedUser.ID)
	if err != nil {
		return fmt.Errorf("failed to check existing organization: %w", err)
	}
	if !hasOrg {
		orgName := generateOrgNameFromEmail(event.Email)
		orgSlug := generateOrgSlugFromEmail(event.Email)

		orgID, err = h.provisioner.CreateOrganizationForUser(
			r.Context(),
			provisionedUser.ID,
			orgName,
			orgSlug,
		)
		if err != nil {
			return fmt.Errorf("failed to create organization: %w", err)
		}
	}

	// Project the org onto the Supabase user, so the next access token
	// Supabase issues carries `app_metadata.organization_id` for
	// TenantMiddleware to read. Runs on every delivery, not just fresh
	// provisions — an earlier delivery may have created the org and then
	// failed this push, and only re-running it converges.
	//
	// Deliberately non-fatal. Our database is the source of truth; this
	// is a projection into a system we don't control. Failing the webhook
	// here would make Supabase retry, which is fine, but it would also
	// mask a successful provision behind a 500. A logged failure plus the
	// next delivery (or 19-04's select-organization) repairs it.
	h.pushOrgContext(r, event.SupabaseUserID, orgID)

	_ = isNewUser // retained for readability at the call site above
	return nil
}

// pushOrgContext writes organization context onto the Supabase user.
// Never returns an error — see the call site for why failures here are
// logged rather than propagated.
func (h *WebhookHandler) pushOrgContext(r *http.Request, supabaseUserID string, orgID uuid.UUID) {
	if h.admin == nil {
		log.Printf("[Webhook] no Supabase admin client configured; skipping org-context push for user %s "+
			"(this user will have no organization_id claim until it is pushed)", supabaseUserID)
		return
	}
	if orgID == uuid.Nil {
		log.Printf("[Webhook] no organization resolved for user %s; skipping org-context push", supabaseUserID)
		return
	}
	if err := h.admin.UpdateUserAppMetadata(r.Context(), supabaseUserID, map[string]any{
		"organization_id":   orgID.String(),
		"organization_role": "owner",
	}); err != nil {
		log.Printf("[Webhook] failed to push org context to Supabase for user %s: %v", supabaseUserID, err)
	}
}

// emailLocalPart returns the portion of an address before the `@`, and
// whether the address had a usable local part at all.
//
// The `ok` return exists because `strings.Split("", "@")` returns a
// one-element slice containing the empty string — NOT an empty slice.
// The prior implementations guarded with `if len(parts) == 0`, which is
// unreachable for every input, so `""` fell through and produced
// `"-org"` / `"'s Organization"` instead of the intended fallbacks.
// That was the long-standing TestGenerateOrgSlugFromEmail failure.
func emailLocalPart(email string) (string, bool) {
	at := strings.Index(email, "@")
	if at <= 0 {
		// No `@` at all, or the address starts with `@` (empty local part).
		return "", false
	}
	return email[:at], true
}

func isASCIIAlnum(b byte) bool {
	return (b >= 'a' && b <= 'z') || (b >= 'A' && b <= 'Z') || (b >= '0' && b <= '9')
}

// firstNameFragment returns the first run of alphanumeric characters in
// local, skipping any leading punctuation. "bob.smith" → "bob",
// "_leading_underscore" → "leading", "___" → "".
func firstNameFragment(local string) string {
	start := 0
	for start < len(local) && !isASCIIAlnum(local[start]) {
		start++
	}
	end := start
	for end < len(local) && isASCIIAlnum(local[end]) {
		end++
	}
	return local[start:end]
}

// generateOrgNameFromEmail generates a default organization name from email.
//
//	"alice@example.com"     → "Alice's Organization"
//	"bob.smith@company.io"  → "Bob's Organization"   (first fragment only)
//	""                      → "My Organization"
func generateOrgNameFromEmail(email string) string {
	local, ok := emailLocalPart(email)
	if !ok {
		return "My Organization"
	}
	first := firstNameFragment(local)
	if first == "" {
		return "My Organization"
	}
	return strings.ToUpper(first[:1]) + strings.ToLower(first[1:]) + "'s Organization"
}

// maxSlugFragmentLen caps the sanitized email fragment so a pathological
// 200-character local part doesn't produce an unwieldy slug. The
// organizations.slug column is VARCHAR-free (TEXT) but the value ends up
// in URLs.
const maxSlugFragmentLen = 30

// sanitizeSlugFragment lowercases local and collapses every run of
// non-`[a-z0-9]` characters into a single dash, then trims dashes from
// both ends. Satisfies the `organizations.slug` CHECK (`^[a-z0-9-]+$`)
// from migration 000001 and additionally avoids leading, trailing, and
// doubled dashes.
func sanitizeSlugFragment(local string) string {
	var b strings.Builder
	lastWasDash := false
	for i := 0; i < len(local); i++ {
		c := local[i]
		switch {
		case c >= 'A' && c <= 'Z':
			b.WriteByte(c - 'A' + 'a')
			lastWasDash = false
		case (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9'):
			b.WriteByte(c)
			lastWasDash = false
		default:
			if !lastWasDash {
				b.WriteByte('-')
				lastWasDash = true
			}
		}
	}
	s := strings.Trim(b.String(), "-")
	if len(s) > maxSlugFragmentLen {
		// Trim again after truncation — the cut can land on a dash.
		s = strings.TrimRight(s[:maxSlugFragmentLen], "-")
	}
	return s
}

// generateOrgSlugFromEmail generates a URL-safe slug from email.
//
//	"alice@example.com"      → "alice-org"
//	"bob.smith@company.io"   → "bob-smith-org"
//	"admin_user@test.org"    → "admin-user-org"
//	""                       → "my-org"
//
// The result is a deterministic BASE. Two users sharing an email local
// part produce the same slug, so CreateOrganizationForUser appends a
// random suffix on conflict rather than failing.
func generateOrgSlugFromEmail(email string) string {
	local, ok := emailLocalPart(email)
	if !ok {
		return "my-org"
	}
	fragment := sanitizeSlugFragment(local)
	if fragment == "" {
		return "my-org"
	}
	return fragment + "-org"
}

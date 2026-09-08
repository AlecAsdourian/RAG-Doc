// Package testjwt signs HS256 JWTs for isolation tests.
//
// IMPORTANT: This package is intended for use only from _test.go files. It
// carries a hardcoded HS256 signing secret used to mint tokens that a
// test-only middleware can accept. Production JWT validation goes through
// pkg/auth against Supabase JWKS and rejects anything signed here.
//
// Split out of pkg/testing/isolation so a production import of the
// isolation harness does not pull the signing secret into the shipped
// binary as a linker symbol. Import this package only from test code.
package testjwt

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"time"
)

// SigningSecret is the HS256 signing key. It is a constant so tests
// deterministically share it; it must never be used by production code.
const SigningSecret = "isolation-test-jwt-secret-not-for-production"

// Sign returns an HS256-signed JWT carrying the claims a production
// middleware reader expects:
//
//   - sub: user id
//   - app_metadata.organization_id: read by pkg/auth.ExtractOrganizationID
//   - app_metadata.organization_role: read by pkg/auth.ExtractOrganizationRole
//   - app_metadata.provider / providers: Supabase always sets these
//   - iat / exp: standard timestamps, 1h validity
//
// The organization claims are NESTED under app_metadata, not top-level.
// TestSign_ClaimShapeMatchesSupabase pins that shape, and
// TestSign_DoesNotEmitFlatClaims guards the reverse — a "simplification"
// back to top-level claims would make every downstream isolation test
// pass against tokens production can never receive.
func Sign(userID, orgID, role string) string {
	header := map[string]string{"alg": "HS256", "typ": "JWT"}
	now := time.Now().Unix()
	payload := map[string]any{
		"sub": userID,
		// NESTED under app_metadata, matching what Supabase actually
		// issues. Verified against the live project 2026-09-08: writing
		// organization_id through the admin API surfaces it at
		// app_metadata.organization_id, and no top-level organization_id
		// claim exists. Emitting the flat shape here would let every
		// isolation test pass against a middleware that could never read
		// a real Supabase token.
		//
		// provider/providers are included because Supabase always sets
		// them, so test tokens exercise the same "our keys sit alongside
		// Supabase's" shape that production sees.
		"app_metadata": map[string]any{
			"organization_id":   orgID,
			"organization_role": role,
			"provider":          "email",
			"providers":         []string{"email"},
		},
		"iat": now,
		"exp": now + 3600,
	}
	return sign(header, payload)
}

// SignWithoutOrg returns a validly-signed token carrying an app_metadata
// object that has Supabase's own keys but NOT ours.
//
// This is the real shape of an un-provisioned user, and getting it right
// matters. Supabase ALWAYS populates provider/providers on every account
// it creates — verified against the live project 2026-09-08 — so a token
// with no app_metadata claim whatsoever is not a state any real user is
// ever in. An earlier version of this helper omitted the object entirely
// and its doc comment claimed to reproduce the failed-push state; it
// exercised a code path (claim absent) that is different from the one
// production actually hits (claim present, our key missing).
//
// Both paths must 403, and both are now covered — this helper for the
// realistic one, SignWithNoAppMetadata for the defensive one.
//
// This is the shape a real user has between "Supabase created the
// account" and "our webhook pushed organization context back" — and the
// shape they keep permanently if that push failed, since webhooks never
// retry. Tests use it to assert that TenantMiddleware refuses such a
// caller rather than defaulting them into somebody's organization.
func SignWithoutOrg(userID string) string {
	header := map[string]string{"alg": "HS256", "typ": "JWT"}
	now := time.Now().Unix()
	payload := map[string]any{
		"sub": userID,
		"app_metadata": map[string]any{
			"provider":  "email",
			"providers": []string{"email"},
		},
		"iat": now,
		"exp": now + 3600,
	}
	return sign(header, payload)
}

// SignWithNoAppMetadata returns a validly-signed token with no
// app_metadata claim at all.
//
// Supabase does not issue this shape, so it is a defensive case rather
// than a realistic one — but the middleware must still refuse it, and
// "the identity provider would never do that" is exactly the assumption
// that makes a security hole survive review.
func SignWithNoAppMetadata(userID string) string {
	header := map[string]string{"alg": "HS256", "typ": "JWT"}
	now := time.Now().Unix()
	payload := map[string]any{
		"sub": userID,
		"iat": now,
		"exp": now + 3600,
	}
	return sign(header, payload)
}

func sign(header map[string]string, payload map[string]any) string {
	encode := func(v any) string {
		b, _ := json.Marshal(v)
		return base64.RawURLEncoding.EncodeToString(b)
	}
	signingInput := encode(header) + "." + encode(payload)
	mac := hmac.New(sha256.New, []byte(SigningSecret))
	mac.Write([]byte(signingInput))
	sig := base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
	return signingInput + "." + sig
}

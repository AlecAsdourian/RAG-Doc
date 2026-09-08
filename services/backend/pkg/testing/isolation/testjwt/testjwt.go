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
//   - organization_id: matches pkg/auth/jwt.go ExtractOrganizationID
//   - organization_role: mirrors the naming for Phase 19's role reader
//   - iat / exp: standard timestamps, 1h validity
//
// A drift-guard test (TestSign_ClaimNamesMatchProduction) pins the claim
// names so a future rename cannot silently break every downstream isolation
// test signed with Sign.
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

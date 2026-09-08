package testjwt

import (
	"encoding/base64"
	"encoding/json"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// decodeClaims pulls the payload out of a signed token.
func decodeClaims(t *testing.T, token string) map[string]any {
	t.Helper()
	parts := strings.Split(token, ".")
	require.Len(t, parts, 3, "JWT must have 3 dot-separated parts")

	payload, err := base64.RawURLEncoding.DecodeString(parts[1])
	require.NoError(t, err)

	var claims map[string]any
	require.NoError(t, json.Unmarshal(payload, &claims))
	return claims
}

// TestSign_ClaimShapeMatchesSupabase pins the claim contract between Sign
// and the production reader in pkg/auth/jwt.go.
//
// The shape is NESTED — `app_metadata.organization_id` — because that is
// what Supabase actually issues. Confirmed against the live project on
// 2026-09-08 by writing organization_id via the admin API, signing in, and
// decoding the resulting access token: `app_metadata` was present as a
// top-level claim carrying our keys, and there was no top-level
// `organization_id`.
//
// This matters more than it looks. Before Phase 19-03, Sign emitted the
// flat shape. Every isolation test passed against it — while a real
// Supabase token would have been rejected by the same middleware, because
// the claim it reads simply is not there. A test harness that disagrees
// with the identity provider proves nothing.
func TestSign_ClaimShapeMatchesSupabase(t *testing.T) {
	claims := decodeClaims(t, Sign("user-1", "org-1", "owner"))

	assert.Equal(t, "user-1", claims["sub"])

	am, ok := claims["app_metadata"].(map[string]any)
	require.True(t, ok, "Sign must emit an app_metadata object; got %T", claims["app_metadata"])

	assert.Equal(t, "org-1", am["organization_id"],
		"pkg/auth.ExtractOrganizationID reads app_metadata.organization_id")
	assert.Equal(t, "owner", am["organization_role"],
		"pkg/auth.ExtractOrganizationRole reads app_metadata.organization_role")
}

// TestSign_DoesNotEmitFlatClaims is the drift guard in the other
// direction. If someone "simplifies" Sign back to top-level claims, the
// nested assertions above would still need updating — but this test fails
// immediately and says why.
func TestSign_DoesNotEmitFlatClaims(t *testing.T) {
	claims := decodeClaims(t, Sign("user-1", "org-1", "owner"))

	for _, k := range []string{
		"organization_id",   // the pre-19-03 shape
		"organization_role", // the pre-19-03 shape
		"org_id",            // the pre-17-01 legacy shape
		"org_role",          // the pre-17-01 legacy shape
	} {
		_, present := claims[k]
		assert.False(t, present,
			"%q must NOT be a top-level claim — Supabase nests tenant context under app_metadata, "+
				"and a flat claim here would make tests pass against tokens production can never receive", k)
	}
}

// TestSign_IncludesSupabaseOwnedKeys keeps test tokens shaped like real
// ones: Supabase always populates provider/providers alongside whatever we
// write, so exercising that co-existence guards against a reader that
// assumes app_metadata contains only our keys.
func TestSign_IncludesSupabaseOwnedKeys(t *testing.T) {
	claims := decodeClaims(t, Sign("user-1", "org-1", "owner"))
	am := claims["app_metadata"].(map[string]any)

	assert.Equal(t, "email", am["provider"],
		"real Supabase tokens carry provider alongside our keys")
	assert.NotEmpty(t, am["providers"],
		"real Supabase tokens carry providers alongside our keys")
}

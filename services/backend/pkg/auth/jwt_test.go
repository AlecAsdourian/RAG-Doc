package auth

import (
	"strings"
	"testing"

	"github.com/lestrrat-go/jwx/v3/jwt"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// tokenWithAppMetadata builds an unsigned token carrying whatever shape is
// handed to it. Signature verification is JWTAuthMiddleware's job and has
// already happened by the time these extractors run, so these tests are
// deliberately about claim SHAPE only.
func tokenWithAppMetadata(t *testing.T, value any) jwt.Token {
	t.Helper()
	tok, err := jwt.NewBuilder().
		Subject("11111111-1111-1111-1111-111111111111").
		Claim(AppMetadataClaim, value).
		Build()
	require.NoError(t, err)
	return tok
}

// TestExtractOrganizationID_RejectsMalformedClaimShapes is the type-confusion
// guard on the tenant trust boundary.
//
// Every one of these shapes must produce an ERROR, never a wrong-but-silent
// value. A shape that slipped through as a non-empty string would become
// the tenant id for the whole request — the exact thing this phase exists
// to make unforgeable.
//
// The shapes are not hypothetical: `app_metadata` is a free-form JSONB
// column in Supabase, so anything that can be written there can arrive
// here.
func TestExtractOrganizationID_RejectsMalformedClaimShapes(t *testing.T) {
	orgID := "22222222-2222-2222-2222-222222222222"

	cases := []struct {
		name string
		meta any
	}{
		{"app_metadata is a string", "not-an-object"},
		{"app_metadata is a number", 42},
		{"app_metadata is an array", []any{orgID}},
		{"app_metadata is a bool", true},
		{"app_metadata is an empty object", map[string]any{}},
		{"organization_id is a number", map[string]any{orgIDKey: 42}},
		{"organization_id is a bool", map[string]any{orgIDKey: true}},
		{"organization_id is null", map[string]any{orgIDKey: nil}},
		{"organization_id is an empty string", map[string]any{orgIDKey: ""}},
		{"organization_id is a nested object", map[string]any{orgIDKey: map[string]any{"id": orgID}}},
		{"organization_id is an array", map[string]any{orgIDKey: []any{orgID}}},
		{"only Supabase's own keys present", map[string]any{
			"provider": "email", "providers": []any{"email"},
		}},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := ExtractOrganizationID(tokenWithAppMetadata(t, tc.meta))
			require.Error(t, err, "malformed claim must not yield a tenant id")
			assert.Empty(t, got, "no tenant id may escape alongside an error")
		})
	}
}

// TestExtractOrganizationID_RequiresUUID pins the check that stops a
// non-UUID claim from reaching a tenant scope.
//
// This is not cosmetic validation. Postgres cannot bind a parameter into
// `SET LOCAL app.current_tenant`, so the established pattern for applying
// a tenant id is string interpolation (pkg/testing/isolation/tenants.go).
// Today's consumers parse the UUID themselves first, so an unvalidated
// value is a 500 rather than injection — but ISS-008 moves that
// interpolation into the middleware, and this test is what keeps the
// window closed when it does.
func TestExtractOrganizationID_RequiresUUID(t *testing.T) {
	for _, raw := range []string{
		"not-a-uuid-at-all",
		"x'; SET ROLE postgres; --",
		"22222222-2222-2222-2222-22222222222",  // one char short
		"22222222222222222222222222222222222",  // no dashes, wrong length
		" 22222222-2222-2222-2222-222222222222", // leading space
		"22222222-2222-2222-2222-222222222222\n",
	} {
		t.Run(raw, func(t *testing.T) {
			meta := map[string]any{orgIDKey: raw}
			got, err := ExtractOrganizationID(tokenWithAppMetadata(t, meta))
			require.Error(t, err, "a non-UUID tenant id must be refused at the boundary")
			assert.Empty(t, got)
		})
	}
}

// TestExtractOrganizationID_AcceptsRealSupabaseShape is the positive case,
// shaped exactly like a live token: our two keys sitting alongside the
// provider keys Supabase always writes.
func TestExtractOrganizationID_AcceptsRealSupabaseShape(t *testing.T) {
	orgID := "22222222-2222-2222-2222-222222222222"
	tok := tokenWithAppMetadata(t, map[string]any{
		"organization_id":   orgID,
		"organization_role": "owner",
		"provider":          "email",
		"providers":         []any{"email"},
	})

	gotID, err := ExtractOrganizationID(tok)
	require.NoError(t, err)
	assert.Equal(t, orgID, gotID)

	gotRole, err := ExtractOrganizationRole(tok)
	require.NoError(t, err)
	assert.Equal(t, "owner", gotRole)
}

// TestExtractOrganizationID_ErrorsDistinguishRemediations checks the error
// TEXT, which is unusual for a test and deliberate here.
//
// These three failures look identical to a caller — all of them are a 403 —
// but they need three different responses from whoever reads the log:
//
//   - no app_metadata at all  → the user was never provisioned
//   - present, missing our key → provisioning worked, the Supabase push did
//     not; run backfill-org-claims
//   - present but not an object → not a Supabase-shaped token; provisioning
//     is not the problem
//
// Before this was pinned, the wrong-type case reported "the user was never
// provisioned", sending an operator to re-run provisioning that had
// already succeeded.
func TestExtractOrganizationID_ErrorsDistinguishRemediations(t *testing.T) {
	t.Run("claim absent", func(t *testing.T) {
		tok, err := jwt.NewBuilder().Subject("u1").Build()
		require.NoError(t, err)

		_, err = ExtractOrganizationID(tok)
		require.Error(t, err)
		assert.Contains(t, strings.ToLower(err.Error()), "never provisioned")
	})

	t.Run("claim present but missing our key", func(t *testing.T) {
		_, err := ExtractOrganizationID(tokenWithAppMetadata(t, map[string]any{
			"provider": "email",
		}))
		require.Error(t, err)
		assert.Contains(t, err.Error(), "backfill-org-claims",
			"the error must name the repair command — nothing else repairs this")
		assert.NotContains(t, strings.ToLower(err.Error()), "never provisioned",
			"provisioning succeeded here; saying otherwise misdirects the operator")
	})

	t.Run("claim present but not an object", func(t *testing.T) {
		_, err := ExtractOrganizationID(tokenWithAppMetadata(t, "not-an-object"))
		require.Error(t, err)
		assert.Contains(t, strings.ToLower(err.Error()), "not an object")
		assert.NotContains(t, strings.ToLower(err.Error()), "never provisioned",
			"a wrong-typed claim is not a provisioning failure")
	})
}

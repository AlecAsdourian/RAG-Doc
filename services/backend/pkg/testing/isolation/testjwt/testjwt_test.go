package testjwt

import (
	"encoding/base64"
	"encoding/json"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestSign_ClaimNamesMatchProduction pins the claim-name contract between
// Sign and the production reader at pkg/auth/jwt.go:57. If Sign drifts from
// "organization_id" every Phase 19+ endpoint test signed with Sign will
// silently fail middleware — this test is the early warning.
func TestSign_ClaimNamesMatchProduction(t *testing.T) {
	token := Sign("user-1", "org-1", "owner")
	parts := strings.Split(token, ".")
	require.Len(t, parts, 3, "JWT must have 3 dot-separated parts")

	payload, err := base64.RawURLEncoding.DecodeString(parts[1])
	require.NoError(t, err)

	var claims map[string]any
	require.NoError(t, json.Unmarshal(payload, &claims))

	assert.Equal(t, "org-1", claims["organization_id"],
		"Sign must emit organization_id — pkg/auth/jwt.go:57 reads this key")
	assert.Equal(t, "owner", claims["organization_role"],
		"Sign must emit organization_role for Phase 19 role-based routing")
	assert.Equal(t, "user-1", claims["sub"])

	_, hasOrgID := claims["org_id"]
	assert.False(t, hasOrgID, "the legacy org_id key must not be present")
	_, hasOrgRole := claims["org_role"]
	assert.False(t, hasOrgRole, "the legacy org_role key must not be present")
}

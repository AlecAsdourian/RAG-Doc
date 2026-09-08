package auth

import (
	"context"
	"fmt"

	"github.com/lestrrat-go/jwx/v3/jwk"
	"github.com/lestrrat-go/jwx/v3/jwt"
)

// TokenValidator is the seam JWTAuthMiddleware uses to parse and verify
// bearer tokens. The production implementation is *JWTValidator, which
// pulls Supabase's JWKS at request time; test code substitutes an HS256
// validator (see pkg/testing/isolation/testjwt) so router integration
// tests can mint tokens without a live Supabase.
type TokenValidator interface {
	ValidateToken(ctx context.Context, tokenString string) (jwt.Token, error)
}

type JWTValidator struct {
	jwksURL string
	issuer  string
}

func NewJWTValidator(config *Config) *JWTValidator {
	return &JWTValidator{
		jwksURL: config.JWKSUrl,
		issuer:  config.SupabaseURL,
	}
}

// ValidateToken validates JWT against Supabase JWKS
func (v *JWTValidator) ValidateToken(ctx context.Context, tokenString string) (jwt.Token, error) {
	// Fetch JWKs from Supabase (cached, auto-refreshed by library)
	set, err := jwk.Fetch(ctx, v.jwksURL)
	if err != nil {
		return nil, fmt.Errorf("failed to fetch JWKs: %w", err)
	}

	// Parse and validate token
	token, err := jwt.Parse(
		[]byte(tokenString),
		jwt.WithKeySet(set),        // Use JWKs for verification
		jwt.WithValidate(true),     // Validates exp, nbf, iat
		jwt.WithIssuer(v.issuer),   // Validate issuer claim
	)
	if err != nil {
		return nil, fmt.Errorf("invalid token: %w", err)
	}

	return token, nil
}

// ExtractUserID gets user_id from validated token
func ExtractUserID(token jwt.Token) (string, error) {
	userID, ok := token.Subject()
	if !ok || userID == "" {
		return "", fmt.Errorf("missing subject claim")
	}
	return userID, nil
}

// AppMetadataClaim is the JWT claim Supabase populates from the user's
// `raw_app_meta_data` column. It is server-controlled — unlike
// `user_metadata`, which the client can write — so it is the only safe
// place to carry tenant identity.
const AppMetadataClaim = "app_metadata"

const (
	orgIDKey   = "organization_id"
	orgRoleKey = "organization_role"
)

// extractAppMetadata pulls the `app_metadata` object off the token.
//
// The claim is NESTED, not top-level. Verified against the live Supabase
// project on 2026-09-08: writing organization_id via the admin API
// surfaces it at `app_metadata.organization_id`, and there is no
// top-level `organization_id` claim. See the 19-03 plan's Sub-step D.
func extractAppMetadata(token jwt.Token) (map[string]any, error) {
	var meta map[string]any
	if err := token.Get(AppMetadataClaim, &meta); err != nil {
		return nil, fmt.Errorf(
			"token has no %s claim — the user was never provisioned, or is authenticating "+
				"against a project where provisioning never ran: %w", AppMetadataClaim, err)
	}
	if meta == nil {
		return nil, fmt.Errorf("%s claim is null", AppMetadataClaim)
	}
	return meta, nil
}

// extractAppMetadataString reads one string key out of app_metadata.
//
// The two failure modes are deliberately distinguished in the error text
// because they call for different fixes: a missing `app_metadata` claim
// means the user was never provisioned at all, while a present claim
// missing our key means provisioning ran but the Supabase push failed
// (see WebhookHandler.pushOrgContext) and will self-heal on the next
// webhook delivery or an org switch.
func extractAppMetadataString(token jwt.Token, key string) (string, error) {
	meta, err := extractAppMetadata(token)
	if err != nil {
		return "", err
	}
	raw, ok := meta[key]
	if !ok {
		return "", fmt.Errorf(
			"%s present but missing %q — provisioning likely succeeded while the Supabase "+
				"metadata push failed; it should self-heal on the next webhook delivery",
			AppMetadataClaim, key)
	}
	s, ok := raw.(string)
	if !ok {
		return "", fmt.Errorf("%s.%s is %T, expected string", AppMetadataClaim, key, raw)
	}
	if s == "" {
		return "", fmt.Errorf("%s.%s is empty", AppMetadataClaim, key)
	}
	return s, nil
}

// ExtractOrganizationID gets the caller's organization from
// `app_metadata.organization_id`.
func ExtractOrganizationID(token jwt.Token) (string, error) {
	return extractAppMetadataString(token, orgIDKey)
}

// ExtractOrganizationRole gets the caller's role within their active
// organization from `app_metadata.organization_role`.
func ExtractOrganizationRole(token jwt.Token) (string, error) {
	return extractAppMetadataString(token, orgRoleKey)
}

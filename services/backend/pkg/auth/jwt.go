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

// ExtractOrganizationID gets organization_id from custom claims
func ExtractOrganizationID(token jwt.Token) (string, error) {
	var orgID string
	if err := token.Get("organization_id", &orgID); err != nil {
		return "", fmt.Errorf("missing organization_id claim: %w", err)
	}
	if orgID == "" {
		return "", fmt.Errorf("empty organization_id claim")
	}
	return orgID, nil
}

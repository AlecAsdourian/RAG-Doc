package testjwt

import (
	"context"
	"fmt"

	"github.com/lestrrat-go/jwx/v3/jwa"
	"github.com/lestrrat-go/jwx/v3/jwt"
)

// Validator parses HS256 tokens signed with SigningSecret. It structurally
// implements pkg/auth.TokenValidator so the production router can be
// constructed via api.NewRouterWithValidator in tests without any Supabase
// JWKS dependency.
//
// This validator MUST NOT be reachable from production code paths: it
// accepts tokens signed with a hardcoded HMAC secret and would collapse
// tenant isolation if wired into a live server. Import from _test.go only.
type Validator struct{}

// NewValidator returns a HS256 validator keyed on SigningSecret.
func NewValidator() *Validator {
	return &Validator{}
}

// ValidateToken parses tokenString as an HS256 JWT and verifies it against
// SigningSecret. Standard exp/nbf/iat validation runs; issuer is not
// checked because Sign does not stamp an iss claim.
func (v *Validator) ValidateToken(ctx context.Context, tokenString string) (jwt.Token, error) {
	token, err := jwt.Parse(
		[]byte(tokenString),
		jwt.WithKey(jwa.HS256(), []byte(SigningSecret)),
		jwt.WithValidate(true),
	)
	if err != nil {
		return nil, fmt.Errorf("testjwt: %w", err)
	}
	return token, nil
}

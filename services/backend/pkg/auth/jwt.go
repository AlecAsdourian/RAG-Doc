package auth

import (
	"context"
	"fmt"

	"github.com/google/uuid"
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
		jwt.WithKeySet(set),      // Use JWKs for verification
		jwt.WithValidate(true),   // Validates exp, nbf, iat
		jwt.WithIssuer(v.issuer), // Validate issuer claim
	)
	if err != nil {
		return nil, fmt.Errorf("invalid token: %w", err)
	}

	return token, nil
}

// ExtractUserID gets the caller's Supabase user id from the `sub` claim.
//
// The value must parse as a UUID, for the same reason ExtractOrganizationID
// requires one: `sub` is an identifier that flows into queries comparing
// against `users.supabase_user_id`, a uuid column. An unvalidated value
// reaches the driver and comes back as a 500 — reported to the caller as
// our bug rather than their bad token — and it discards the only
// structural guarantee we have about a value the rest of the request
// trusts completely.
//
// Supabase always issues a UUID here (it is `auth.users.id`), so this
// rejects nothing a real token carries. "The identity provider would never
// do that" is not a reason to skip the check; it is the assumption that
// keeps holes alive through review.
//
// The CANONICAL form is returned, not the input. uuid.Parse is more
// permissive than Postgres: it accepts `urn:uuid:...`, which Postgres's
// uuid type rejects outright, plus braced and unhyphenated forms that
// Postgres accepts but that make four textually different `sub` values
// resolve to one identity. Validating without canonicalizing narrowed the
// 500 rather than closing it — a URN-form subject still reached the driver.
// Returning u.String() closes it and makes the value downstream code
// compares byte-for-byte actually stable.
//
// Forward-looking caveat: if this project ever enables Supabase
// Third-Party Auth (Clerk, Firebase, Auth0), `sub` stops being a UUID —
// Firebase issues 28-char alphanumerics, Auth0 `auth0|...`, Clerk
// `user_...` — and this check would 401 every user. Unreachable today
// (the validator pins the Supabase issuer and JWKS), and the underlying
// constraint is real regardless: `users.supabase_user_id` is a uuid
// column, so such a migration needs a schema change, not just a looser
// check here.
func ExtractUserID(token jwt.Token) (string, error) {
	userID, ok := token.Subject()
	if !ok || userID == "" {
		return "", fmt.Errorf("missing subject claim")
	}
	parsed, err := uuid.Parse(userID)
	if err != nil {
		return "", fmt.Errorf("subject claim is not a valid UUID: %w", err)
	}
	return parsed.String(), nil
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
	// token.Get fails for three different situations that need three
	// different responses from whoever reads the log: the claim is absent,
	// the claim is JSON null, or the claim is present but not an object.
	// Distinguish them explicitly — a wrong-type claim reported as "never
	// provisioned" sends the reader to re-run provisioning that already
	// worked.
	if err := token.Get(AppMetadataClaim, new(any)); err != nil {
		return nil, fmt.Errorf(
			"token has no usable %s claim — the user was never provisioned, or is "+
				"authenticating against a project where provisioning never ran: %w",
			AppMetadataClaim, err)
	}

	var meta map[string]any
	if err := token.Get(AppMetadataClaim, &meta); err != nil {
		return nil, fmt.Errorf(
			"%s claim is present but is not an object (%w) — this is not a Supabase-shaped "+
				"token; provisioning is not the problem", AppMetadataClaim, err)
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
// missing our key means provisioning ran and the Supabase metadata push
// failed (see WebhookHandler.pushOrgContext).
//
// The second case does NOT repair itself. Supabase database webhooks fire
// once with no retry, and the trigger behind them is AFTER INSERT on
// auth.users, so there is exactly one delivery per user for all time.
// Recovery requires running `cmd/backfill-org-claims`. Earlier revisions
// of this comment promised self-healing "on the next webhook delivery";
// there is no next delivery, and saying so sent operators looking for a
// retry that never comes.
func extractAppMetadataString(token jwt.Token, key string) (string, error) {
	meta, err := extractAppMetadata(token)
	if err != nil {
		return "", err
	}
	raw, ok := meta[key]
	if !ok {
		return "", fmt.Errorf(
			"%s present but missing %q — provisioning succeeded and the Supabase metadata "+
				"push did not; repair with cmd/backfill-org-claims (no webhook retry will fix it)",
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
//
// The value must parse as a UUID. This is a trust-boundary check, not a
// formatting nicety: the returned string is the tenant identifier that
// flows into every downstream tenant scope, and Postgres cannot bind a
// parameter into `SET LOCAL app.current_tenant`, so the established
// in-repo pattern for applying it is string interpolation
// (`pkg/testing/isolation/tenants.go`). Today the only consumers parse the
// UUID themselves before interpolating, so an unvalidated value produces a
// 500 rather than injection — but ISS-008 will move exactly that
// interpolation into the middleware, and at that point an unchecked claim
// becomes SQL injection at the front door. Validating here closes it
// before that lands.
//
// Rejecting is safe: our own writer only ever pushes a uuid.UUID string
// (see WebhookHandler.pushOrgContext), so a non-UUID claim cannot come
// from a correctly-provisioned user.
// Like ExtractUserID, this returns the CANONICAL form. That matters more
// here than it looks: ISS-008 will string-interpolate this value into
// `SET LOCAL app.current_tenant` (Postgres cannot bind a parameter into a
// SET), and `urn:uuid:...` passes uuid.Parse while being rejected by
// Postgres — validated, but still wrong at exactly the point the
// validation exists to protect.
func ExtractOrganizationID(token jwt.Token) (string, error) {
	raw, err := extractAppMetadataString(token, orgIDKey)
	if err != nil {
		return "", err
	}
	parsed, err := uuid.Parse(raw)
	if err != nil {
		return "", fmt.Errorf("%s.%s is not a valid UUID: %w", AppMetadataClaim, orgIDKey, err)
	}
	return parsed.String(), nil
}

// ExtractOrganizationRole gets the caller's role within their active
// organization from `app_metadata.organization_role`.
func ExtractOrganizationRole(token jwt.Token) (string, error) {
	return extractAppMetadataString(token, orgRoleKey)
}

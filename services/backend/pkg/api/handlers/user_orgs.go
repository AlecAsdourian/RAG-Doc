package handlers

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"

	"github.com/go-chi/render"
	"github.com/go-playground/validator/v10"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/lestrrat-go/jwx/v3/jwt"

	"github.com/yourusername/smart-docs-platform/services/backend/pkg/auth"
)

// UserOrgsHandler serves the two endpoints a multi-org user needs: seeing
// which organizations they belong to, and choosing which one is active.
//
// Both are USER-scoped, not tenant-scoped. They read `organization_memberships`
// for the caller and never touch a tenant-scoped table, so they are mounted
// under JWT authentication but outside TenantMiddleware. That is not a
// shortcut — a user whose organization claim is missing must still be able
// to list their organizations and pick one, and TenantMiddleware would 403
// them before the handler ran. These endpoints are the way out of that
// state, so they cannot be behind the gate that state trips.
type UserOrgsHandler struct {
	db       *pgxpool.Pool
	admin    auth.AdminClient
	validate *validator.Validate
}

// NewUserOrgsHandler builds the handler.
//
// admin may be nil (the router runs degraded without Supabase credentials,
// matching 19-03's wiring). List still works; Select refuses with 503
// rather than reporting a success it did not perform.
func NewUserOrgsHandler(db *pgxpool.Pool, admin auth.AdminClient, validate *validator.Validate) *UserOrgsHandler {
	return &UserOrgsHandler{db: db, admin: admin, validate: validate}
}

// OrgMembership is one row of GET /api/user/organizations.
type OrgMembership struct {
	ID       string `json:"id"`
	Name     string `json:"name"`
	Slug     string `json:"slug"`
	Role     string `json:"role"`
	IsActive bool   `json:"is_active"`
}

// OrgListResponse is the response envelope. It is an object rather than a
// bare array so fields can be added later without breaking clients, and so
// `active_organization_id` can be reported explicitly — a caller whose
// token carries no organization claim gets `null` there and an empty
// `organizations` list, which is a meaningfully different state from
// "belongs to organizations, none active".
type OrgListResponse struct {
	Organizations        []OrgMembership `json:"organizations"`
	ActiveOrganizationID *string         `json:"active_organization_id"`
}

func (o *OrgListResponse) Render(w http.ResponseWriter, r *http.Request) error { return nil }

// MaxSelectOrgBodyBytes bounds the select-organization request body. A
// valid body is a single UUID in a JSON object — about 55 bytes — so 4KB
// is generous by two orders of magnitude and still refuses to allocate on
// behalf of a caller sending megabytes.
const MaxSelectOrgBodyBytes = 4 << 10

// SelectOrgRequest is the body of POST /api/user/select-organization.
type SelectOrgRequest struct {
	OrganizationID string `json:"organization_id" validate:"required,uuid"`
}

// SelectOrgResponse tells the client what it must do next. The backend
// never mints or returns a Supabase-signed token; the caller's existing
// access token still carries the OLD organization until they refresh.
type SelectOrgResponse struct {
	Status           string `json:"status"`
	OrganizationID   string `json:"organization_id"`
	OrganizationRole string `json:"organization_role"`
	NextAction       string `json:"next_action"`
}

func (s *SelectOrgResponse) Render(w http.ResponseWriter, r *http.Request) error {
	render.Status(r, http.StatusAccepted)
	return nil
}

// callerSupabaseID pulls the Supabase user id out of the request context.
//
// This is the JWT `sub` claim, which for a Supabase-issued token is the
// `auth.users.id` — the same value `users.supabase_user_id` stores. Every
// query below joins on it, so getting this wrong means silently finding
// no memberships rather than erroring.
func callerSupabaseID(r *http.Request) (string, bool) {
	id, ok := r.Context().Value(auth.UserIDKey).(string)
	return id, ok && id != ""
}

// activeOrgFromToken reads the caller's current organization claim.
//
// Read from the token rather than from auth.OrgIDKey because these routes
// deliberately sit outside TenantMiddleware, which is what sets that key.
// Absence is normal and not an error: a user mid-provisioning, or one
// whose org-context push failed, has no claim and still needs this
// endpoint to work.
func activeOrgFromToken(r *http.Request) *string {
	token, ok := r.Context().Value(auth.TokenKey).(jwt.Token)
	if !ok {
		return nil
	}
	orgID, err := auth.ExtractOrganizationID(token)
	if err != nil {
		return nil
	}
	return &orgID
}

// List handles GET /api/user/organizations.
func (h *UserOrgsHandler) List(w http.ResponseWriter, r *http.Request) {
	supabaseUserID, ok := callerSupabaseID(r)
	if !ok {
		render.Render(w, r, ErrInternal(
			errors.New("user context missing from request; middleware chain misconfigured")))
		return
	}

	ctx := r.Context()
	activeOrgID := activeOrgFromToken(r)

	// Scoped to the caller by `u.supabase_user_id = $1` and nothing else.
	// There is no request-controlled input in this query at all — the
	// caller cannot ask about another user, because there is no parameter
	// with which to name one.
	rows, err := h.db.Query(ctx, `
		SELECT o.id, o.name, o.slug, om.role
		FROM organization_memberships om
		JOIN users u         ON u.id = om.user_id
		JOIN organizations o ON o.id = om.organization_id
		WHERE u.supabase_user_id = $1
		ORDER BY o.name ASC, o.id ASC
	`, supabaseUserID)
	if err != nil {
		render.Render(w, r, ErrInternal(fmt.Errorf("list organizations: %w", err)))
		return
	}
	defer rows.Close()

	// Non-nil so an empty result marshals as [] rather than null. A client
	// iterating the response should not have to special-case a user who
	// belongs to nothing.
	orgs := make([]OrgMembership, 0, 4)
	for rows.Next() {
		var m OrgMembership
		if err := rows.Scan(&m.ID, &m.Name, &m.Slug, &m.Role); err != nil {
			render.Render(w, r, ErrInternal(fmt.Errorf("scan organization row: %w", err)))
			return
		}
		// EqualFold rather than ==. Postgres emits lowercase UUIDs, so the
		// two sides match today — but the claim side is whatever was
		// written to Supabase, and an uppercase value there would silently
		// flag every row inactive rather than erroring. A UUID's case
		// carries no meaning; comparing as if it did is a bug waiting for
		// one careless writer.
		m.IsActive = activeOrgID != nil && strings.EqualFold(m.ID, *activeOrgID)
		orgs = append(orgs, m)
	}
	if err := rows.Err(); err != nil {
		render.Render(w, r, ErrInternal(fmt.Errorf("iterate organizations: %w", err)))
		return
	}

	render.Render(w, r, &OrgListResponse{
		Organizations:        orgs,
		ActiveOrganizationID: activeOrgID,
	})
}

// Select handles POST /api/user/select-organization.
//
// It validates that the caller actually belongs to the target organization,
// then writes BOTH the organization id and the caller's role in that
// organization onto their Supabase user, so the next token they mint
// carries the correct claims.
//
// Two things about this are load-bearing and easy to get wrong:
//
//  1. The membership check is the ONLY authorization on this path. The
//     original plan called it "defense-in-depth in case the hook is
//     misconfigured" — but 19-03 established there is no Auth Hook, and
//     nothing else validates the target. Without this check any
//     authenticated user could join any organization by POSTing its id,
//     which is the vulnerability 19-03 closed, reopened through a
//     different door.
//
//  2. The role must be written, not just the organization. Supabase MERGES
//     app_metadata, so omitting `organization_role` leaves the previous
//     value in place — verified against the live project on 2026-09-08. A
//     user who owns orgA and is a plain member of orgB would switch to
//     orgB still claiming `organization_role: "owner"`. Nothing reads the
//     role yet, which is exactly why that would have shipped unnoticed.
func (h *UserOrgsHandler) Select(w http.ResponseWriter, r *http.Request) {
	supabaseUserID, ok := callerSupabaseID(r)
	if !ok {
		render.Render(w, r, ErrInternal(
			errors.New("user context missing from request; middleware chain misconfigured")))
		return
	}

	// Bound the body before decoding it. A valid request here is about 55
	// bytes; without a limit the decoder materializes whatever an
	// authenticated caller sends into a string before the validator gets
	// to reject it, at roughly 7x the wire size in allocations. Same guard
	// pkg/auth/webhook.go applies, for the same reason.
	r.Body = http.MaxBytesReader(w, r.Body, MaxSelectOrgBodyBytes)

	var req SelectOrgRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		render.Render(w, r, ErrInvalidRequest(err))
		return
	}
	if err := h.validate.Struct(req); err != nil {
		// Deliberately not the raw validator error. Its text names Go
		// struct fields and tags — "Key: 'SelectOrgRequest.OrganizationID'
		// Error:Field validation for 'OrganizationID' failed on the 'uuid'
		// tag" — which tells a caller nothing useful and tells everyone
		// else about our internals. (The other handlers still echo it;
		// that's pre-existing and worth a sweep, not a change to shared
		// error helpers inside this PR.)
		render.Render(w, r, ErrInvalidRequest(
			errors.New("organization_id must be a valid UUID")))
		return
	}

	ctx := r.Context()

	// Authorization. Fetching the role doubles as the membership check:
	// no row means no membership, and the role is what we must write.
	role, err := h.callerRoleIn(ctx, supabaseUserID, req.OrganizationID)
	if errors.Is(err, errNoMembership) {
		// Deliberately indistinguishable from "that organization does not
		// exist". Telling a caller which org ids are real turns this
		// endpoint into an enumeration oracle for other tenants.
		render.Render(w, r, ErrForbidden())
		return
	}
	if err != nil {
		render.Render(w, r, ErrInternal(fmt.Errorf("verify membership: %w", err)))
		return
	}

	if h.admin == nil {
		// Degraded router (no Supabase credentials). Refuse loudly rather
		// than returning 202 for a switch that cannot have happened — the
		// client would call refreshSession() and silently keep its old
		// organization.
		render.Render(w, r, ErrServiceUnavailable(
			errors.New("supabase admin client not configured; cannot update organization claim")))
		return
	}

	if err := h.admin.UpdateUserAppMetadata(ctx, supabaseUserID, map[string]any{
		"organization_id":   req.OrganizationID,
		"organization_role": role,
	}); err != nil {
		render.Render(w, r, ErrInternal(fmt.Errorf("update organization claim: %w", err)))
		return
	}

	render.Render(w, r, &SelectOrgResponse{
		Status:           "org_updated",
		OrganizationID:   req.OrganizationID,
		OrganizationRole: role,
		NextAction: "call supabase.auth.refreshSession() to receive a new access token " +
			"carrying the updated organization claim; the current token still has the old one",
	})
}

// errNoMembership signals "the caller does not belong to that organization",
// which the handler maps to 403. It is a distinct error rather than a bare
// bool so a genuine query failure can never be mistaken for a denial —
// failing open here would be an authorization bypass.
var errNoMembership = errors.New("caller is not a member of the target organization")

// callerRoleIn returns the caller's role in orgID, or errNoMembership.
func (h *UserOrgsHandler) callerRoleIn(ctx context.Context, supabaseUserID, orgID string) (string, error) {
	// orgID is already validator-checked (`validate:"uuid"`); this is the
	// belt to that suspenders, so removing the struct tag degrades to a
	// clean denial rather than a driver error rendered as 500.
	//
	// `supabaseUserID` is NOT checked here — it is validated at the token
	// boundary by auth.ExtractUserID, which is the right place: every route
	// benefits, not just this one.
	if _, err := uuid.Parse(orgID); err != nil {
		return "", errNoMembership
	}

	// No ORDER BY / LIMIT. `organization_memberships` has
	// UNIQUE(user_id, organization_id), so at most one row can match and a
	// tiebreak is unreachable code pretending to be a decision.
	//
	// If that constraint is ever relaxed, this must fail rather than pick.
	// The tempting fixes are both wrong: "oldest wins" is arbitrary, and
	// "highest privilege wins" is backwards — for an authorization
	// decision the fail-safe direction is least privilege. Ambiguous
	// membership is a data-integrity bug, and quietly resolving it would
	// write a role into the caller's token based on a guess.
	rows, err := h.db.Query(ctx, `
		SELECT om.role
		FROM organization_memberships om
		JOIN users u ON u.id = om.user_id
		WHERE u.supabase_user_id = $1 AND om.organization_id = $2
	`, supabaseUserID, orgID)
	if err != nil {
		return "", err
	}
	roles, err := pgx.CollectRows(rows, pgx.RowTo[string])
	if err != nil {
		return "", err
	}

	switch len(roles) {
	case 0:
		return "", errNoMembership
	case 1:
		return roles[0], nil
	default:
		return "", fmt.Errorf(
			"data integrity: user %s has %d membership rows in organization %s; "+
				"refusing to guess which role to write into their token",
			supabaseUserID, len(roles), orgID)
	}
}

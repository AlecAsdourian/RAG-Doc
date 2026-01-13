# Phase 4: Authentication System - Context

**Gathered:** 2026-01-12
**Status:** Ready for research

<vision>
## How This Should Work

**Organization-first multi-tenant model.** Organizations are the primary entity - everything flows from the organization. Users belong to orgs, all data access is org-scoped. Think Slack or GitHub's team model.

**Hybrid signup flow:** First user signs up and creates their organization. Then they invite team members. But also support direct invites - send someone an invitation email, they can join that org even without an existing account.

**Leveraging proven auth infrastructure:** Use a managed auth service (Auth0 or similar) to handle the security primitives - token generation, session management, OAuth flows, password hashing. Don't reinvent those wheels.

**Focus on authorization, not authentication:** The managed service handles authentication. This phase focuses on building airtight multi-tenant authorization - ensuring User A can never access Org B's data, even if there's a bug in the application code.

**Configurable sessions:** Different session lengths for different contexts. Maybe longer sessions on trusted devices, shorter on shared machines. Flexible based on security vs convenience tradeoffs.

</vision>

<essential>
## What Must Be Nailed

- **Multi-tenant isolation with defense in depth**
  - Application middleware checks org membership on every request
  - Database row-level security (RLS) as a safety net
  - Even if app code has bugs, database prevents cross-org access

- **Comprehensive testing for authorization**
  - Unit tests for authorization middleware and logic
  - Integration tests verifying User A cannot access Org B's repositories/data
  - Real database queries, realistic multi-tenant scenarios
  - Catch authorization bugs before they reach production

- **Minimal but extensible role model**
  - Two roles for now: owner/admin vs member
  - Just enough structure to avoid major rework when more roles are needed
  - Simple permissions: admins can invite users and manage org, members can access data

</essential>

<boundaries>
## What's Out of Scope

- **Billing and subscriptions** - Not handling paid plans, usage limits, or subscription management. That's Phase 16 (Multi-tenant & Deployment).

- **Additional social providers** - No Google, Microsoft, Apple sign-in. Just GitHub/GitLab OAuth (since users will connect repos anyway) or basic email/password.

- **Email verification and 2FA** - No email confirmation flow, no two-factor authentication. Keep it simple for now - those are post-MVP enhancements.

- **Complex RBAC** - Not building fine-grained permissions like "can edit X but not Y". Just org membership with two roles (admin/member). More granular permissions are future work.

</boundaries>

<specifics>
## Specific Ideas

**Auth provider preference:** Auth0 or similar managed service
- Handles security primitives (tokens, sessions, OAuth, password hashing)
- Built-in security best practices
- Less to build, more to configure
- Frees up implementation effort for the multi-tenant authorization layer

**Defense in depth for multi-tenancy:**
- Application layer: Middleware extracts user's org from token, checks authorization before data access
- Database layer: Postgres row-level security policies enforce org isolation
- Testing layer: Both unit and integration tests verify isolation

**GitHub/GitLab OAuth:** Makes sense since users will authenticate with these services anyway to connect repositories. Smooth flow: sign in with GitHub → authorize the app → automatically connected for repo access later.

**Session handling:** Configurable based on context (trusted device vs shared machine). Let the auth provider handle the mechanics, just configure the policies.

</specifics>

<notes>
## Additional Context

**Key insight:** Use managed auth service for security fundamentals, focus engineering effort on the multi-tenant authorization logic that's specific to this SaaS platform.

**Testing emphasis:** User specifically called out "backed by tests" for multi-tenant isolation. This is critical for SaaS - one bug in authorization could expose Org A's data to Org B. Comprehensive testing is non-negotiable.

**Minimal roles now, extensibility later:** Two-role model (admin/member) is enough for Phase 4-5, but designed to extend when more granular permissions are needed. Avoid both over-engineering now AND painting ourselves into a corner for later.

**Database-level security:** Row-level security in Postgres as a second line of defense. Application middleware should catch everything, but RLS ensures database won't leak data even if application has bugs. Standard pattern for secure multi-tenancy.

</notes>

---

*Phase: 04-authentication-system*
*Context gathered: 2026-01-12*

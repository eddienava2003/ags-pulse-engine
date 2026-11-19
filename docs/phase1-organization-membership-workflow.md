# Phase 1 — Organization Membership Workflow

Status: implementation design on an isolated branch; not deployed.

## Verified production baseline

- API version on `main`: 1.7.8.
- The API verifies Supabase access tokens with `supabase.auth.get_user(token)`.
- Organization selection is resolved against `organization_memberships`.
- Authenticated users have SELECT-only access to `organization_memberships`; writes are not available directly to clients.
- The membership table primary key is `(organization_id, user_id)`.
- The API's Supabase client is initialized from `SUPABASE_SECRET_KEY` or legacy `SUPABASE_KEY`.
- No invitation, membership role-change, or member-removal API route exists in `main.py`.

## Required behavior

1. An owner or admin can create an invitation only for an organization they belong to.
2. Only owner/admin can invite users or change roles. Removing or demoting an owner requires owner authority.
3. Invitation email is normalized and role is validated against the existing role vocabulary.
4. The invitation has a cryptographically random, single-use token. Store only a keyed digest, never the raw token.
5. Invitation expires (recommended: 7 days), can be revoked, and cannot be accepted twice.
6. Acceptance requires a valid Supabase bearer token and a verified Auth email matching the invitation email. The organization/user pair is inserted idempotently.
7. A user cannot accept an invitation for a different email, change its organization, choose a different role, or self-assign a membership.
8. Prevent removing/demoting the last active owner in a transaction.
9. Return generic responses for unknown email addresses to avoid account enumeration.
10. Record inviter, invitee, organization, role, timestamps, and actor for auditability. Do not log invitation tokens.

## Implementation architecture

- Add a private invitation table with RLS enabled and no client-facing policies or grants.
- Add narrowly scoped database RPCs for atomic invitation acceptance and last-owner-safe role/removal operations. Revoke PUBLIC/anon execution; grant execution only to service_role.
- Add API routes:
  - `POST /organizations/{organization_id}/invitations`
  - `GET /organizations/{organization_id}/members`
  - `POST /organization-invitations/accept`
  - `PATCH /organizations/{organization_id}/members/{user_id}`
  - `DELETE /organizations/{organization_id}/members/{user_id}`
- Invitation creation uses Supabase Auth Admin invite delivery via the server-side service credential. Never expose the service credential to clients.
- Keep organization authorization checks server-side and derive the actor from the verified bearer token. Never trust an actor/user ID supplied in JSON.
- Ensure the API distinguishes service-role administrative operations from user-scoped reads and validates every requested organization against the verified actor's membership and role.

## Required tests before merge/deploy

- Unauthenticated invite/list/accept/role-change/remove requests are rejected.
- A member/viewer cannot invite, change roles, or remove members.
- An admin cannot grant owner or remove/demote an owner unless explicitly permitted by the final role policy.
- An owner can invite and administer members in their own organization only.
- A user from organization A cannot list, invite, change, or remove members in organization B.
- Invite email mismatch, expired token, revoked token, replayed token, and unverified email are rejected.
- Acceptance creates exactly one membership for the invited organization and authenticated user.
- Concurrent owner-removal attempts cannot leave an organization without an owner.
- Existing shipment, event, route, checkpoint, and dashboard tenant-isolation tests remain passing.

## Rollout gates

- Apply migration to a non-production Supabase project first.
- Run unit/integration tests and a real two-user/two-organization test.
- Review RLS and Supabase security advisors after migration.
- Deploy only after explicit review; do not merge this branch automatically.
- Phase 1 item 5 remains incomplete until invitation, acceptance, role administration, and sign-in flows pass.
- Phase 1 item 6 remains incomplete until cross-tenant read and write denial is verified with two real accounts.

## Known dependency

The current project has one known Auth user/account and one organization. A second independent Auth identity is still required for the final end-to-end tenant-isolation test. Implementation and static authorization tests can proceed without it, but this does not replace that production verification.

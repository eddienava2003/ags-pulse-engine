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

- Add `public.organization_invitations` with RLS enabled and no anon/authenticated grants or policies; only the server-side service role can access it.
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

## Invitation email configuration required

The backend places a one-time invitation token in Supabase Auth invite metadata as `ags_invitation_token`. The project's Auth **Invite user** email template must render this value and direct the user to the application's acceptance screen. For example:

```html
<p>You have been invited to join an AGS-Pulse organization.</p>
<p>Invitation token: {{ .Data.ags_invitation_token }}</p>
<p>After signing in and verifying this email, paste the token into the invitation acceptance screen.</p>
```

Configure `SUPABASE_INVITE_REDIRECT_URL` in the backend environment to the approved app acceptance URL. Do not deploy until the template and redirect URL are configured and tested. The token is single-use, stored as a SHA-256 digest of a high-entropy random token in the database, and must never be logged.

The current implementation includes role changes and member removal through transactional last-owner-safe RPCs, but these operations are untested and must pass the explicit tests above before item 5 can be marked complete. Invitation revocation is not yet implemented.

## Implementation notes and constraints

- Supabase Auth invitation links default to a one-hour expiry in this project configuration; the database invitation expiry is aligned to one hour. The email OTP expiry setting must be checked before deployment. If it differs, align the database default and API response.
- Supabase Auth returns an error when `invite_user_by_email()` is used for an email that already belongs to a confirmed Auth user. This implementation cleans up the pending invitation record on delivery failure and grants no membership. Supporting already-registered users requires a separate verified email-delivery flow (for example, a custom invitation email provider); it is not silently treated as supported.
- Only an owner can invite an admin, promote/demote owner/admin roles, or remove an owner/admin. Admins can administer dispatcher/member/viewer roles. The final owner cannot be demoted or removed.
- The API now enables CORS for PATCH and DELETE because member role updates and removal use those methods.
- The current endpoint implementation is still unmerged and untested against a disposable Supabase project. Do not apply either SQL file to production.

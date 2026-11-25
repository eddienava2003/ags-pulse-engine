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
4. The invitation has a cryptographically random, single-use token. Store only a SHA-256 digest of the high-entropy token, never the raw token.
5. Invitation expires after one hour (must be aligned with Supabase Auth invite-link expiry) and cannot be accepted twice. Invitation revocation is not implemented yet.
6. Acceptance requires a valid Supabase bearer token and a verified Auth email matching the invitation email. It creates a membership once; an account already in the organization is rejected without consuming the invitation.
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
- Invitation creation uses Supabase passwordless email sign-in (`sign_in_with_otp`) with the existing Auth email delivery configuration. `should_create_user=true` supports new and existing Auth identities; no new credential or email-provider key is introduced.
- Keep organization authorization checks server-side and derive the actor from the verified bearer token. Never trust an actor/user ID supplied in JSON.
- Ensure the API distinguishes service-role administrative operations from user-scoped reads and validates every requested organization against the verified actor's membership and role.

## Required tests before merge/deploy

- Unauthenticated invite/list/accept/role-change/remove requests are rejected.
- A member/viewer cannot invite, change roles, or remove members.
- An admin cannot grant owner or remove/demote an owner unless explicitly permitted by the final role policy.
- An owner can invite and administer members in their own organization only.
- A user from organization A cannot list, invite, change, or remove members in organization B.
- Invite email mismatch, expired token, replayed token, and unverified email are rejected. Revocation tests remain blocked until a revocation endpoint is implemented.
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

## Invitation email delivery and acceptance

- Invitation delivery uses Supabase Auth passwordless email sign-in (Magic Link) through the project's existing Auth email provider. The backend calls `sign_in_with_otp` with `should_create_user=true`, so both a new invitee and an already-registered/confirmed Auth user can receive a sign-in link.
- No custom invitation template variable is required. The existing Supabase Magic Link template must retain its standard confirmation link (normally `{{ .ConfirmationURL }}`). No SMTP/API key, Supabase key, or other secret is changed by this code.
- Configure the non-secret `SUPABASE_INVITE_REDIRECT_URL` to the exact callback URL `https://<phase1-test-host>/organization-invitations/callback` and ensure that exact URL is included in the Supabase Auth redirect allow list. This is a URL setting, not a key or secret.
- After the recipient clicks the email link, Supabase returns the authenticated access token in the browser URL fragment. The callback page reads the session token and one-time invitation token, immediately removes both from the visible URL, then calls the authenticated acceptance endpoint. Membership is granted only after the API validates the bearer token, verified email, invitation digest, expiry, and single-use state.
- The invitation token remains high-entropy, single-use, and stored only as a SHA-256 digest. It is not written to logs or returned by the API. The callback sets no-store and no-referrer response headers.
- Supabase Auth redirect allow-list and email rate/expiry settings remain external prerequisites. The existing Auth email delivery configuration must successfully deliver a Magic Link; this code does not modify provider settings or credentials.
- The test deployment must verify delivery to both a new test identity and an existing confirmed identity. The one-hour database invitation expiry must not exceed the configured Magic Link validity.
- Do not merge or deploy to production until email delivery, callback acceptance, replay rejection, expiry, and two-organization isolation have passed in the isolated Phase 1 environment.

## Expiry handling correction

- Expired invitations are marked `expired` and returned as a normal RPC result, not raised as an exception, so PostgreSQL commits the status transition.
- The API returns HTTP 410 for an expired token.
- Invitation creation marks any expired pending invitation for the same organization/email as expired before checking for an existing pending invitation. This releases the partial unique index and permits a fresh invitation.
- These changes are code-reviewed only; they still require migration and API integration tests in a disposable non-production Supabase environment.

## Invitation authorization concurrency hardening

- Invitation creation now runs through `create_organization_invitation()`, a service-role-only database RPC.
- The RPC locks the organization row, rechecks the actor's current owner/admin role, enforces owner-only administrator invitations, expires stale invitations, checks for an existing pending invitation, and inserts the new invitation in one transaction.
- The API retains its early role check for fast feedback, but the database transaction is the authoritative authorization gate. This closes the stale-role time-of-check/time-of-use window.
- This additional RPC is part of `database/phase1_organization_invitations.sql` and must be applied to the disposable Phase 1 test project before invitation creation can be integration-tested.

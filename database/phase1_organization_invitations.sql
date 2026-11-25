-- AGS-Pulse Phase 1 organization invitations
-- Apply only to a development Supabase branch until reviewed and tested.

create table if not exists public.organization_invitations (
    id uuid primary key default gen_random_uuid(),
    organization_id uuid not null references public.organizations(id) on delete cascade,
    email text not null,
    member_role text not null check (member_role in ('admin','dispatcher','member','viewer')),
    token_hash text not null unique,
    invited_by uuid not null references auth.users(id),
    status text not null default 'pending' check (status in ('pending','accepted','revoked','expired')),
    expires_at timestamptz not null default (now() + interval '1 hour'),
    accepted_by uuid references auth.users(id),
    accepted_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create unique index if not exists organization_invitations_one_pending_email
    on public.organization_invitations (organization_id, lower(email))
    where status = 'pending';

alter table public.organization_invitations enable row level security;
revoke all on public.organization_invitations from public, anon, authenticated;
grant select, insert, update, delete on public.organization_invitations to service_role;

create or replace function public.create_organization_invitation(
    p_organization_id uuid,
    p_actor_user_id uuid,
    p_email text,
    p_member_role text,
    p_token_hash text
) returns jsonb
language plpgsql
set search_path = pg_catalog, public
as $
declare
    actor_role text;
    invitation_id uuid;
begin
    if p_organization_id is null or p_actor_user_id is null
       or p_email is null or length(trim(p_email)) = 0
       or p_member_role is null
       or p_member_role not in ('admin','dispatcher','member','viewer')
       or p_token_hash is null or length(p_token_hash) <> 64 then
        raise exception 'Invalid organization invitation request' using errcode = '22023';
    end if;

    -- Serialize invitation creation with membership administration for this org.
    perform 1 from public.organizations
    where id = p_organization_id
    for update;
    if not found then
        raise exception 'Organization not found' using errcode = 'P0002';
    end if;

    select member_role into actor_role
    from public.organization_memberships
    where organization_id = p_organization_id
      and user_id = p_actor_user_id;

    if coalesce(actor_role, '') not in ('owner','admin') then
        raise exception 'Insufficient organization role' using errcode = '42501';
    end if;

    if p_member_role = 'admin' and actor_role <> 'owner' then
        raise exception 'Only an owner may invite an administrator' using errcode = '42501';
    end if;

    update public.organization_invitations
    set status = 'expired', updated_at = now()
    where organization_id = p_organization_id
      and lower(email) = lower(trim(p_email))
      and status = 'pending'
      and expires_at <= now();

    if exists (
        select 1 from public.organization_invitations
        where organization_id = p_organization_id
          and lower(email) = lower(trim(p_email))
          and status = 'pending'
    ) then
        raise exception 'A pending invitation already exists for this email' using errcode = '23505';
    end if;

    insert into public.organization_invitations (
        organization_id, email, member_role, token_hash, invited_by, status
    ) values (
        p_organization_id, lower(trim(p_email)), p_member_role, p_token_hash, p_actor_user_id, 'pending'
    ) returning id into invitation_id;

    return jsonb_build_object('success', true, 'invitation_id', invitation_id);
end;
$;

revoke all on function public.create_organization_invitation(uuid, uuid, text, text, text) from public, anon, authenticated;
grant execute on function public.create_organization_invitation(uuid, uuid, text, text, text) to service_role;

create or replace function public.accept_organization_invitation(
    p_token_hash text,
    p_user_id uuid,
    p_verified_email text
) returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
    invitation public.organization_invitations%rowtype;
begin
    if p_token_hash is null or length(p_token_hash) <> 64
       or p_user_id is null or p_verified_email is null then
        raise exception 'Invalid invitation acceptance request' using errcode = '22023';
    end if;

    select * into invitation
    from public.organization_invitations
    where token_hash = p_token_hash
    for update;

    if not found then
        raise exception 'Invitation is invalid or unavailable' using errcode = 'P0002';
    end if;

    if invitation.status = 'pending' and invitation.expires_at <= now() then
        -- Return normally so this status change commits instead of being rolled back
        -- by an exception in the same transaction.
        update public.organization_invitations
        set status = 'expired', updated_at = now()
        where id = invitation.id;

        return jsonb_build_object('success', false, 'error', 'invitation_expired');
    end if;

    if invitation.status <> 'pending' then
        raise exception 'Invitation is invalid or unavailable' using errcode = 'P0002';
    end if;

    if lower(invitation.email) <> lower(trim(p_verified_email)) then
        raise exception 'Invitation email does not match authenticated account' using errcode = '42501';
    end if;

    -- Do not consume an invitation or report a role that was not granted
    -- when the authenticated account is already a member of this organization.
    if exists (
        select 1
        from public.organization_memberships
        where organization_id = invitation.organization_id
          and user_id = p_user_id
    ) then
        raise exception 'User is already a member of this organization' using errcode = '23505';
    end if;

    insert into public.organization_memberships (organization_id, user_id, member_role)
    values (invitation.organization_id, p_user_id, invitation.member_role);

    update public.organization_invitations
    set status = 'accepted',
        accepted_by = p_user_id,
        accepted_at = now(),
        updated_at = now()
    where id = invitation.id;

    return jsonb_build_object(
        'success', true,
        'organization_id', invitation.organization_id,
        'member_role', invitation.member_role
    );
end;
$$;

revoke all on function public.accept_organization_invitation(text, uuid, text) from public, anon, authenticated;
grant execute on function public.accept_organization_invitation(text, uuid, text) to service_role;


-- Passwordless email-link acceptance uses a non-secret invitation UUID in the
-- callback URL. Authenticated, verified email matching remains mandatory.
create or replace function public.accept_organization_invitation_by_id(
    p_invitation_id uuid,
    p_user_id uuid,
    p_verified_email text
) returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
    invitation public.organization_invitations%rowtype;
begin
    if p_invitation_id is null or p_user_id is null
       or p_verified_email is null or length(trim(p_verified_email)) = 0 then
        raise exception 'Invalid invitation acceptance request' using errcode = '22023';
    end if;

    select * into invitation
    from public.organization_invitations
    where id = p_invitation_id
    for update;

    if not found then
        raise exception 'Invitation is invalid or unavailable' using errcode = 'P0002';
    end if;

    if invitation.status = 'pending' and invitation.expires_at <= now() then
        update public.organization_invitations
        set status = 'expired', updated_at = now()
        where id = invitation.id;
        return jsonb_build_object('success', false, 'error', 'invitation_expired');
    end if;

    if invitation.status <> 'pending' then
        raise exception 'Invitation is invalid or unavailable' using errcode = 'P0002';
    end if;

    if lower(invitation.email) <> lower(trim(p_verified_email)) then
        raise exception 'Invitation email does not match authenticated account' using errcode = '42501';
    end if;

    if exists (
        select 1
        from public.organization_memberships
        where organization_id = invitation.organization_id
          and user_id = p_user_id
    ) then
        raise exception 'User is already a member of this organization' using errcode = '23505';
    end if;

    insert into public.organization_memberships (organization_id, user_id, member_role)
    values (invitation.organization_id, p_user_id, invitation.member_role);

    update public.organization_invitations
    set status = 'accepted',
        accepted_by = p_user_id,
        accepted_at = now(),
        updated_at = now()
    where id = invitation.id;

    return jsonb_build_object(
        'success', true,
        'organization_id', invitation.organization_id,
        'member_role', invitation.member_role
    );
end;
$$;

revoke all on function public.accept_organization_invitation_by_id(uuid, uuid, text) from public, anon, authenticated;
grant execute on function public.accept_organization_invitation_by_id(uuid, uuid, text) to service_role;

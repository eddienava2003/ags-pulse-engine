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
    expires_at timestamptz not null default (now() + interval '7 days'),
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

    if invitation.status <> 'pending' or invitation.expires_at <= now() then
        if invitation.status = 'pending' and invitation.expires_at <= now() then
            update public.organization_invitations
            set status = 'expired', updated_at = now()
            where id = invitation.id;
        end if;
        raise exception 'Invitation is invalid or unavailable' using errcode = 'P0002';
    end if;

    if lower(invitation.email) <> lower(trim(p_verified_email)) then
        raise exception 'Invitation email does not match authenticated account' using errcode = '42501';
    end if;

    insert into public.organization_memberships (organization_id, user_id, member_role)
    values (invitation.organization_id, p_user_id, invitation.member_role)
    on conflict (organization_id, user_id) do nothing;

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

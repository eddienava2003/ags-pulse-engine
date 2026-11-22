-- Phase 1: make invitation authorization atomic with invitation creation.
-- Apply only to the disposable Phase 1 test project until validated.

create or replace function public.create_organization_invitation(
    p_organization_id uuid,
    p_actor_user_id uuid,
    p_email text,
    p_member_role text,
    p_token_hash text
) returns jsonb
language plpgsql
set search_path = pg_catalog, public
as $$
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
$$;

revoke all on function public.create_organization_invitation(uuid, uuid, text, text, text) from public, anon, authenticated;
grant execute on function public.create_organization_invitation(uuid, uuid, text, text, text) to service_role;

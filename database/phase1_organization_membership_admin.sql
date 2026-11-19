-- Additional organization membership administration.
-- Run only in a disposable/development Supabase project until validated.

create or replace function public.change_organization_member_role(
    p_organization_id uuid,
    p_actor_user_id uuid,
    p_target_user_id uuid,
    p_new_role text
) returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
    actor_role text;
    target_role text;
    owner_count integer;
begin
    if p_organization_id is null or p_actor_user_id is null
       or p_target_user_id is null
       or p_new_role is null or p_new_role not in ('owner','admin','dispatcher','member','viewer') then
        raise exception 'Invalid membership change request' using errcode = '22023';
    end if;

    perform 1 from public.organizations
    where id = p_organization_id
    for update;
    if not found then
        raise exception 'Organization not found' using errcode = 'P0002';
    end if;

    select member_role into actor_role
    from public.organization_memberships
    where organization_id = p_organization_id and user_id = p_actor_user_id;

    if coalesce(actor_role, '') not in ('owner','admin') then
        raise exception 'Insufficient organization role' using errcode = '42501';
    end if;

    select member_role into target_role
    from public.organization_memberships
    where organization_id = p_organization_id and user_id = p_target_user_id
    for update;

    if target_role is null then
        raise exception 'Target member not found' using errcode = 'P0002';
    end if;

    if actor_role <> 'owner' and (target_role in ('owner','admin') or p_new_role in ('owner','admin')) then
        raise exception 'Only an owner may administer owner/admin roles' using errcode = '42501';
    end if;

    if target_role = 'owner' and p_new_role <> 'owner' then
        select count(*) into owner_count
        from public.organization_memberships
        where organization_id = p_organization_id and member_role = 'owner';
        if owner_count <= 1 then
            raise exception 'Cannot demote the last organization owner' using errcode = '23514';
        end if;
    end if;

    update public.organization_memberships
    set member_role = p_new_role
    where organization_id = p_organization_id and user_id = p_target_user_id;

    return jsonb_build_object('success', true, 'organization_id', p_organization_id,
        'user_id', p_target_user_id, 'member_role', p_new_role);
end;
$$;

create or replace function public.remove_organization_member(
    p_organization_id uuid,
    p_actor_user_id uuid,
    p_target_user_id uuid
) returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
    actor_role text;
    target_role text;
    owner_count integer;
begin
    if p_organization_id is null or p_actor_user_id is null or p_target_user_id is null then
        raise exception 'Invalid membership removal request' using errcode = '22023';
    end if;

    perform 1 from public.organizations
    where id = p_organization_id
    for update;
    if not found then
        raise exception 'Organization not found' using errcode = 'P0002';
    end if;

    select member_role into actor_role
    from public.organization_memberships
    where organization_id = p_organization_id and user_id = p_actor_user_id;

    if coalesce(actor_role, '') not in ('owner','admin') then
        raise exception 'Insufficient organization role' using errcode = '42501';
    end if;

    select member_role into target_role
    from public.organization_memberships
    where organization_id = p_organization_id and user_id = p_target_user_id
    for update;

    if target_role is null then
        raise exception 'Target member not found' using errcode = 'P0002';
    end if;

    if target_role in ('owner','admin') and actor_role <> 'owner' then
        raise exception 'Only an owner may remove another owner or admin' using errcode = '42501';
    end if;

    if target_role = 'owner' then
        select count(*) into owner_count
        from public.organization_memberships
        where organization_id = p_organization_id and member_role = 'owner';
        if owner_count <= 1 then
            raise exception 'Cannot remove the last organization owner' using errcode = '23514';
        end if;
    end if;

    delete from public.organization_memberships
    where organization_id = p_organization_id and user_id = p_target_user_id;

    return jsonb_build_object('success', true, 'organization_id', p_organization_id,
        'removed_user_id', p_target_user_id);
end;
$$;

revoke all on function public.change_organization_member_role(uuid, uuid, uuid, text) from public, anon, authenticated;
revoke all on function public.remove_organization_member(uuid, uuid, uuid) from public, anon, authenticated;
grant execute on function public.change_organization_member_role(uuid, uuid, uuid, text) to service_role;
grant execute on function public.remove_organization_member(uuid, uuid, uuid) to service_role;

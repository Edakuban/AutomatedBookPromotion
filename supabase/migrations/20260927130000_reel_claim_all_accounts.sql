-- n8n may claim the next configured account for a platform. A non-empty
-- account still narrows the claim for installations with dedicated workers.
begin;

create or replace function public.bookpromo_reel_claim(p_platform text, p_queue_mode text, p_account text) returns jsonb
language plpgsql security invoker set search_path = '' as $$
declare active public.reel_publications; picked public.reel_publications; asset public.reel_assets;
        account_filter text:=nullif(btrim(p_account),'');
begin
    if p_platform not in ('instagram','facebook','youtube','tiktok') or p_queue_mode not in ('daily','scheduled') then
        raise exception 'Invalid claim' using errcode='22023';
    end if;
    perform pg_advisory_xact_lock(hashtextextended('bookpromo-reel:'||p_platform||':'||coalesce(account_filter,'*'),0));
    select * into active from public.reel_publications
     where platform=p_platform and (account_filter is null or account_id=account_filter)
       and status in ('publishing','processing','publish_uncertain')
     order by claimed_at,id limit 1 for update;
    if found then return jsonb_build_object('outcome','busy','publication_id',active.id); end if;
    select * into picked from public.reel_publications p
     where platform=p_platform and (account_filter is null or account_id=account_filter)
       and queue_mode=p_queue_mode and status='ready'
       and (p_queue_mode='daily' or scheduled_for<=now())
       and exists(select 1 from public.reel_assets a where a.id=p.reel_id and a.media_status='uploaded')
     order by priority desc,scheduled_for nulls last,created_at,id limit 1 for update skip locked;
    if not found then return jsonb_build_object('outcome','empty'); end if;
    update public.reel_publications set status='publishing',claimed_at=now(),error=null,
      revision=revision+1,action_token=gen_random_uuid() where id=picked.id returning * into picked;
    select * into asset from public.reel_assets where id=picked.reel_id;
    return jsonb_build_object('outcome','claimed','publication',to_jsonb(picked),'asset',to_jsonb(asset));
end;
$$;

revoke all on function public.bookpromo_reel_claim(text,text,text) from public,anon,authenticated;
grant execute on function public.bookpromo_reel_claim(text,text,text) to service_role;
notify pgrst,'reload schema';
commit;

-- Schema v9: Reel assets can originate from a quote or a whole chapter.
begin;

do $$
begin
    if (select version from public.bookpromo_schema limit 1) <> 8 then
        raise exception 'Schema v9 requires schema v8';
    end if;
end $$;

alter table public.reel_assets alter column quote_id drop not null;
alter table public.reel_assets
    add column chapter_id uuid references public.chapters(id) on delete restrict,
    add column source_kind text not null default 'quote';
alter table public.reel_assets
    add constraint reel_assets_source_kind check(source_kind in ('quote','chapter')),
    add constraint reel_assets_source_reference check(
        (source_kind='quote' and quote_id is not null and chapter_id is null) or
        (source_kind='chapter' and quote_id is null and chapter_id is not null)
    );
create index reel_assets_chapter_idx on public.reel_assets(chapter_id)
    where chapter_id is not null;

alter table public.reel_assets drop constraint reel_assets_duration_ms_check;
alter table public.reel_assets add constraint reel_assets_duration_ms_check
    check(duration_ms between 4000 and 60000);

create or replace function public.bookpromo_reel_enqueue(
    p_asset jsonb, p_publications jsonb
) returns jsonb
language plpgsql security invoker set search_path = '' as $$
declare asset public.reel_assets; item jsonb; publication public.reel_publications; expected integer:=0;
begin
    if jsonb_typeof(p_asset)<>'object' or jsonb_typeof(p_publications)<>'array'
       or jsonb_array_length(p_publications) not between 1 and 4 then
        raise exception 'Invalid Reel enqueue payload' using errcode='22023';
    end if;
    insert into public.reel_assets(
        id,source_kind,quote_id,chapter_id,book_id,quote_text,addition,title,description,
        book_profile,image_prompt,video_prompt,storage_provider,storage_bucket,storage_path,
        public_url,media_sha256,size_bytes,duration_ms,width,height,audio_title,audio_start_ms
    ) values (
        (p_asset->>'id')::uuid,coalesce(nullif(p_asset->>'source_kind',''),'quote'),
        nullif(p_asset->>'quote_id','')::uuid,nullif(p_asset->>'chapter_id','')::uuid,
        (p_asset->>'book_id')::uuid,p_asset->>'quote_text',coalesce(p_asset->>'addition',''),
        p_asset->>'title',p_asset->>'description',p_asset->'book_profile',p_asset->>'image_prompt',
        p_asset->>'video_prompt',p_asset->>'storage_provider',p_asset->>'storage_bucket',
        p_asset->>'storage_path',nullif(p_asset->>'public_url',''),p_asset->>'media_sha256',
        (p_asset->>'size_bytes')::bigint,(p_asset->>'duration_ms')::integer,
        (p_asset->>'width')::integer,(p_asset->>'height')::integer,
        nullif(p_asset->>'audio_title',''),(p_asset->>'audio_start_ms')::integer
    ) on conflict(id) do nothing;
    select * into asset from public.reel_assets where id=(p_asset->>'id')::uuid;
    if asset.source_kind is distinct from coalesce(nullif(p_asset->>'source_kind',''),'quote')
       or asset.quote_id is distinct from nullif(p_asset->>'quote_id','')::uuid
       or asset.chapter_id is distinct from nullif(p_asset->>'chapter_id','')::uuid
       or asset.storage_provider is distinct from p_asset->>'storage_provider'
       or asset.storage_bucket is distinct from p_asset->>'storage_bucket'
       or asset.storage_path is distinct from p_asset->>'storage_path'
       or asset.media_sha256 is distinct from p_asset->>'media_sha256'
       or asset.title is distinct from p_asset->>'title'
       or asset.description is distinct from p_asset->>'description' then
        raise exception 'Reel asset conflict' using errcode='40001';
    end if;
    for item in select value from jsonb_array_elements(p_publications) loop
        expected:=expected+1;
        insert into public.reel_publications(
            reel_id,platform,account_id,queue_mode,scheduled_for,priority,title,description,options
        ) values (
            asset.id,item->>'platform',item->>'account_id',item->>'queue_mode',
            case when nullif(item->>'scheduled_for','') is null then null else (item->>'scheduled_for')::timestamptz end,
            coalesce((item->>'priority')::smallint,0),item->>'title',item->>'description',
            coalesce(item->'options','{}'::jsonb)
        ) on conflict(reel_id,platform) do nothing;
        select * into publication from public.reel_publications
         where reel_id=asset.id and platform=item->>'platform';
        if publication.account_id is distinct from item->>'account_id'
           or publication.queue_mode is distinct from item->>'queue_mode'
           or publication.title is distinct from item->>'title'
           or publication.description is distinct from item->>'description'
           or publication.options is distinct from coalesce(item->'options','{}'::jsonb) then
            raise exception 'Reel publication conflict' using errcode='40001';
        end if;
    end loop;
    if (select count(*) from public.reel_publications where reel_id=asset.id)<>expected then
        raise exception 'Reel publication set conflict' using errcode='40001';
    end if;
    return jsonb_build_object('outcome','enqueued','asset',to_jsonb(asset),
        'publications',(select jsonb_agg(to_jsonb(p) order by p.platform)
                        from public.reel_publications p where p.reel_id=asset.id));
end;
$$;

revoke all on function public.bookpromo_reel_enqueue(jsonb,jsonb)
    from public,anon,authenticated;
grant execute on function public.bookpromo_reel_enqueue(jsonb,jsonb) to service_role;

update public.bookpromo_schema set version=9 where version=8;
notify pgrst,'reload schema';
commit;

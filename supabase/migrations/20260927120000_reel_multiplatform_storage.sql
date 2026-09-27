-- Schema v8: immutable Reel assets with independent per-platform deliveries.
begin;

do $$
begin
    if (select version from public.bookpromo_schema limit 1) <> 7 then
        raise exception 'Schema v8 requires schema v7';
    end if;
end $$;

-- Disable the v7 single-Instagram queue. Its rows are copied below and the
-- table remains as a read-inaccessible audit archive.
drop function if exists public.bookpromo_reel_claim(text);
drop function if exists public.bookpromo_reel_transition(uuid, integer, uuid, text, jsonb);
drop function if exists public.bookpromo_reel_cleanup(uuid, integer, uuid, text);
revoke all on table public.reel_publications from public, anon, authenticated, service_role;
alter table public.reel_publications rename to reel_publications_v7;

create table public.reel_assets (
    id uuid primary key,
    quote_id uuid not null references public.quotes(id) on delete restrict,
    book_id uuid not null references public.books(id) on delete restrict,
    quote_text text not null check(length(quote_text) between 1 and 12000),
    addition text not null check(length(addition) <= 4000),
    title text not null check(length(title) between 1 and 300),
    description text not null check(length(description) between 1 and 5000),
    book_profile jsonb not null check(jsonb_typeof(book_profile) = 'object'),
    image_prompt text not null check(length(image_prompt) between 1 and 8000),
    video_prompt text not null check(length(video_prompt) between 1 and 4000),
    storage_provider text not null check(storage_provider in ('supabase','cloudflare_r2')),
    storage_bucket text not null check(length(storage_bucket) between 3 and 255),
    storage_path text not null check(
        length(storage_path) between 70 and 1100 and storage_path !~ '(^|/)\.\.?(/|$)' and
        storage_path !~ '[[:cntrl:]\\]'
    ),
    public_url text check(public_url is null or (length(public_url) <= 2000 and public_url ~ '^https://')),
    media_sha256 text not null check(media_sha256 ~ '^[0-9a-f]{64}$'),
    size_bytes bigint not null check(size_bytes between 12 and 52428800),
    duration_ms integer not null check(duration_ms between 4000 and 30000),
    width integer not null check(width between 360 and 2160),
    height integer not null check(height between 640 and 3840 and height > width),
    audio_title text check(audio_title is null or length(audio_title) <= 300),
    audio_start_ms integer check(audio_start_ms is null or audio_start_ms >= 0),
    media_status text not null default 'uploaded' check(media_status in ('uploaded','cleanup_pending','deleted')),
    revision integer not null default 0 check(revision >= 0),
    action_token uuid not null default gen_random_uuid(),
    error text check(error is null or length(error) <= 4000),
    deleted_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint reel_assets_media_state check(
        (media_status in ('uploaded','cleanup_pending') and deleted_at is null) or
        (media_status = 'deleted' and deleted_at is not null and public_url is null)
    )
);

create table public.reel_publications (
    id uuid primary key default gen_random_uuid(),
    reel_id uuid not null references public.reel_assets(id) on delete restrict,
    platform text not null check(platform in ('instagram','facebook','youtube','tiktok')),
    account_id text not null check(length(btrim(account_id)) between 1 and 255),
    queue_mode text not null check(queue_mode in ('daily','scheduled')),
    scheduled_for timestamptz,
    priority smallint not null default 0 check(priority between -100 and 100),
    title text not null check(length(title) between 1 and 300),
    description text not null check(length(description) between 1 and 5000),
    options jsonb not null default '{}'::jsonb check(jsonb_typeof(options) = 'object'),
    status text not null default 'ready' check(status in
        ('ready','publishing','processing','publish_uncertain','published','failed','cancelled')),
    revision integer not null default 0 check(revision >= 0),
    action_token uuid not null default gen_random_uuid(),
    external_container_id text check(external_container_id is null or length(external_container_id) <= 500),
    external_media_id text check(external_media_id is null or length(external_media_id) <= 500),
    permalink text check(permalink is null or length(permalink) <= 2000),
    error text check(error is null or length(error) <= 4000),
    claimed_at timestamptz,
    published_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique(reel_id, platform),
    constraint reel_publications_schedule check(
        (queue_mode = 'daily' and scheduled_for is null) or
        (queue_mode = 'scheduled' and scheduled_for is not null)
    ),
    constraint reel_publications_state check(
        (status = 'ready' and claimed_at is null and published_at is null and external_media_id is null) or
        (status in ('publishing','processing','publish_uncertain') and claimed_at is not null and published_at is null) or
        (status = 'published' and claimed_at is not null and published_at is not null and external_media_id is not null) or
        (status in ('failed','cancelled') and published_at is null and external_media_id is null)
    )
);

create index reel_assets_quote_idx on public.reel_assets(quote_id);
create index reel_assets_book_idx on public.reel_assets(book_id);
create index reel_assets_cleanup_idx on public.reel_assets(updated_at) where media_status = 'cleanup_pending';
create index reel_delivery_ready_idx on public.reel_publications(
    platform, account_id, queue_mode, priority desc, scheduled_for, created_at, id
) where status = 'ready';
create index reel_delivery_reel_idx on public.reel_publications(reel_id, status);

alter table public.reel_assets enable row level security;
alter table public.reel_publications enable row level security;
revoke all on table public.reel_assets, public.reel_publications from public, anon, authenticated, service_role;
grant select, insert, update, delete on table public.reel_assets, public.reel_publications to service_role;
create trigger set_updated_at before update on public.reel_assets
    for each row execute function public.bookpromo_set_updated_at();
create trigger set_updated_at before update on public.reel_publications
    for each row execute function public.bookpromo_set_updated_at();

-- Preserve any queue rows created between v7 deployment and this migration.
insert into public.reel_assets (
    id,quote_id,book_id,quote_text,addition,title,description,book_profile,image_prompt,video_prompt,
    storage_provider,storage_bucket,storage_path,media_sha256,size_bytes,duration_ms,width,height,
    audio_title,audio_start_ms,media_status,revision,action_token,error,deleted_at,created_at,updated_at
)
select id,quote_id,book_id,quote_text,addition,
       left(coalesce(nullif(book_profile->>'title',''),'Book Reel'),300),caption,book_profile,image_prompt,video_prompt,
       'supabase','book-promotion-reels',storage_path,media_sha256,size_bytes,duration_ms,width,height,
       audio_title,audio_start_ms,media_status,revision,action_token,error,
       case when media_status='deleted' then updated_at else null end,created_at,updated_at
from public.reel_publications_v7;

insert into public.reel_publications (
    reel_id,platform,account_id,queue_mode,scheduled_for,priority,title,description,options,status,
    revision,action_token,external_container_id,external_media_id,permalink,error,claimed_at,published_at,
    created_at,updated_at
)
select id,'instagram',account_id,case when scheduled_for is null then 'daily' else 'scheduled' end,
       scheduled_for,priority,left(coalesce(nullif(book_profile->>'title',''),'Book Reel'),300),caption,
       '{"share_to_feed":true,"is_ai_generated":true}'::jsonb,status,revision,action_token,
       instagram_container_id,instagram_media_id,instagram_permalink,error,claimed_at,published_at,created_at,updated_at
from public.reel_publications_v7;

create function public.bookpromo_reel_refresh_asset(p_reel_id uuid) returns public.reel_assets
language plpgsql security invoker set search_path = '' as $$
declare asset public.reel_assets;
begin
    select * into asset from public.reel_assets where id=p_reel_id for update;
    if not found then raise exception 'Reel asset missing'; end if;
    if asset.media_status='uploaded'
       and exists(select 1 from public.reel_publications where reel_id=p_reel_id and status='published')
       and not exists(select 1 from public.reel_publications where reel_id=p_reel_id and status not in ('published','cancelled')) then
        update public.reel_assets set media_status='cleanup_pending',revision=revision+1,
            action_token=gen_random_uuid(),error=null where id=p_reel_id returning * into asset;
    end if;
    return asset;
end;
$$;

create function public.bookpromo_reel_enqueue(p_asset jsonb, p_publications jsonb) returns jsonb
language plpgsql security invoker set search_path = '' as $$
declare asset public.reel_assets; item jsonb; publication public.reel_publications; expected integer:=0;
begin
    if jsonb_typeof(p_asset)<>'object' or jsonb_typeof(p_publications)<>'array'
       or jsonb_array_length(p_publications) not between 1 and 4 then
        raise exception 'Invalid Reel enqueue payload' using errcode='22023';
    end if;
    insert into public.reel_assets(
        id,quote_id,book_id,quote_text,addition,title,description,book_profile,image_prompt,video_prompt,
        storage_provider,storage_bucket,storage_path,public_url,media_sha256,size_bytes,duration_ms,width,height,
        audio_title,audio_start_ms
    ) values (
        (p_asset->>'id')::uuid,(p_asset->>'quote_id')::uuid,(p_asset->>'book_id')::uuid,
        p_asset->>'quote_text',coalesce(p_asset->>'addition',''),p_asset->>'title',p_asset->>'description',
        p_asset->'book_profile',p_asset->>'image_prompt',p_asset->>'video_prompt',p_asset->>'storage_provider',
        p_asset->>'storage_bucket',p_asset->>'storage_path',nullif(p_asset->>'public_url',''),p_asset->>'media_sha256',
        (p_asset->>'size_bytes')::bigint,(p_asset->>'duration_ms')::integer,(p_asset->>'width')::integer,
        (p_asset->>'height')::integer,nullif(p_asset->>'audio_title',''),(p_asset->>'audio_start_ms')::integer
    ) on conflict(id) do nothing;
    select * into asset from public.reel_assets where id=(p_asset->>'id')::uuid;
    if asset.storage_provider is distinct from p_asset->>'storage_provider'
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
            coalesce((item->>'priority')::smallint,0),item->>'title',item->>'description',coalesce(item->'options','{}'::jsonb)
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
        'publications',(select jsonb_agg(to_jsonb(p) order by p.platform) from public.reel_publications p where p.reel_id=asset.id));
end;
$$;

create function public.bookpromo_reel_claim(p_platform text, p_queue_mode text, p_account text) returns jsonb
language plpgsql security invoker set search_path = '' as $$
declare active public.reel_publications; picked public.reel_publications; asset public.reel_assets;
begin
    if p_platform not in ('instagram','facebook','youtube','tiktok') or p_queue_mode not in ('daily','scheduled')
       or nullif(btrim(p_account),'') is null then raise exception 'Invalid claim' using errcode='22023'; end if;
    perform pg_advisory_xact_lock(hashtextextended('bookpromo-reel:'||p_platform||':'||p_account,0));
    select * into active from public.reel_publications where platform=p_platform and account_id=p_account
      and status in ('publishing','processing','publish_uncertain') order by claimed_at,id limit 1 for update;
    if found then return jsonb_build_object('outcome','busy','publication_id',active.id); end if;
    select * into picked from public.reel_publications p where platform=p_platform and account_id=p_account
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

create function public.bookpromo_reel_transition(
    p_id uuid,p_revision integer,p_token uuid,p_action text,p_data jsonb default '{}'::jsonb
) returns jsonb language plpgsql security invoker set search_path = '' as $$
declare publication public.reel_publications; asset public.reel_assets; payload jsonb:=coalesce(p_data,'{}'::jsonb); supplied text;
begin
    select * into publication from public.reel_publications where id=p_id for update;
    if not found then raise exception 'Reel publication missing'; end if;
    if publication.revision is distinct from p_revision or publication.action_token is distinct from p_token then
        return jsonb_build_object('outcome','stale'); end if;
    if jsonb_typeof(payload)<>'object' then raise exception 'Invalid transition data' using errcode='22023'; end if;
    if p_action='processing' and publication.status='publishing' then
        supplied:=nullif(btrim(payload->>'container_id'),'');
        if supplied is null or length(supplied)>500 then raise exception 'Invalid external container' using errcode='22023'; end if;
        publication.external_container_id:=supplied; publication.status:='processing';
    elsif p_action='published' and publication.status in ('publishing','processing','publish_uncertain') then
        supplied:=nullif(btrim(payload->>'media_id'),'');
        if supplied is null or length(supplied)>500 then raise exception 'Invalid external media' using errcode='22023'; end if;
        publication.external_media_id:=supplied; publication.permalink:=nullif(payload->>'permalink','');
        publication.published_at:=coalesce(publication.published_at,now()); publication.error:=null; publication.status:='published';
    elsif p_action='publish_uncertain' and publication.status in ('publishing','processing') then
        publication.error:=left(coalesce(nullif(payload->>'error',''),'Ambiguous publication response'),4000);
        publication.status:='publish_uncertain';
    elsif p_action='fail' and publication.status='publishing' and publication.external_container_id is null then
        publication.error:=left(coalesce(nullif(payload->>'error',''),'Pre-publication failure'),4000); publication.status:='failed';
    elsif p_action='cancel' and publication.status in ('ready','failed') then
        publication.error:=null; publication.claimed_at:=coalesce(publication.claimed_at,now()); publication.status:='cancelled';
    else return jsonb_build_object('outcome','invalid_state'); end if;
    publication.revision:=publication.revision+1; publication.action_token:=gen_random_uuid();
    update public.reel_publications set status=publication.status,revision=publication.revision,
      action_token=publication.action_token,external_container_id=publication.external_container_id,
      external_media_id=publication.external_media_id,permalink=publication.permalink,error=publication.error,
      claimed_at=publication.claimed_at,published_at=publication.published_at where id=publication.id returning * into publication;
    select * into asset from public.bookpromo_reel_refresh_asset(publication.reel_id);
    return jsonb_build_object('outcome','updated','publication',to_jsonb(publication),'asset',to_jsonb(asset));
end;
$$;

create function public.bookpromo_reel_cleanup(
    p_id uuid,p_revision integer,p_token uuid,p_deleted_path text
) returns jsonb language plpgsql security invoker set search_path = '' as $$
declare asset public.reel_assets;
begin
    select * into asset from public.reel_assets where id=p_id for update;
    if not found then raise exception 'Reel asset missing'; end if;
    if asset.revision is distinct from p_revision or asset.action_token is distinct from p_token then
        return jsonb_build_object('outcome','stale'); end if;
    if p_deleted_path is distinct from asset.storage_path then raise exception 'Invalid cleanup path' using errcode='22023'; end if;
    if asset.media_status='deleted' then return jsonb_build_object('outcome','existing','asset',to_jsonb(asset)); end if;
    if asset.media_status<>'cleanup_pending'
       or exists(select 1 from public.reel_publications where reel_id=asset.id and status not in ('published','cancelled'))
       or not exists(select 1 from public.reel_publications where reel_id=asset.id and status='published') then
        return jsonb_build_object('outcome','invalid_state'); end if;
    update public.reel_assets set media_status='deleted',public_url=null,deleted_at=now(),error=null,
      revision=revision+1,action_token=gen_random_uuid() where id=asset.id returning * into asset;
    return jsonb_build_object('outcome','complete','asset',to_jsonb(asset));
end;
$$;

revoke all on function public.bookpromo_reel_refresh_asset(uuid),
    public.bookpromo_reel_enqueue(jsonb,jsonb),
    public.bookpromo_reel_claim(text,text,text),
    public.bookpromo_reel_transition(uuid,integer,uuid,text,jsonb),
    public.bookpromo_reel_cleanup(uuid,integer,uuid,text)
from public,anon,authenticated;
grant execute on function public.bookpromo_reel_refresh_asset(uuid),
    public.bookpromo_reel_enqueue(jsonb,jsonb),
    public.bookpromo_reel_claim(text,text,text),
    public.bookpromo_reel_transition(uuid,integer,uuid,text,jsonb),
    public.bookpromo_reel_cleanup(uuid,integer,uuid,text)
to service_role;

update public.bookpromo_schema set version=8 where version=7;
notify pgrst,'reload schema';
commit;

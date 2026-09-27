-- Schema v7: frozen, ready-to-publish Reel queue and private temporary media.
--
-- This migration deliberately depends on the locally prepared v6 migration.
-- The currently deployed project may still be v5; apply v6 first and only then
-- apply this file. No remote migration is performed by the repository tooling.
begin;

do $$
begin
    if not exists (select 1 from public.bookpromo_schema where version = 6) then
        raise exception 'Schema v7 requires schema v6';
    end if;
end;
$$;

create table public.reel_publications (
    id uuid primary key default gen_random_uuid(),
    quote_id uuid not null references public.quotes(id) on delete restrict,
    book_id uuid not null references public.books(id) on delete restrict,
    account_id text not null check (btrim(account_id) <> ''),
    status text not null default 'ready' check (status in (
        'ready', 'publishing', 'publish_uncertain', 'published', 'failed'
    )),
    media_status text not null default 'uploaded' check (media_status in (
        'uploaded', 'cleanup_pending', 'deleted'
    )),
    revision integer not null default 1 check (revision >= 1),
    action_token uuid not null default gen_random_uuid(),
    priority integer not null default 0,
    scheduled_for timestamptz,

    -- Snapshots: publishing never re-reads mutable quote/book data and never
    -- invokes a model. The exact reviewed caption is sent to Instagram.
    quote_text text not null check (btrim(quote_text) <> ''),
    addition text not null default '',
    caption text not null check (
        btrim(caption) <> '' and length(caption) <= 2200 and
        position(quote_text in caption) > 0
    ),
    book_profile jsonb not null default '{}'::jsonb
        check (jsonb_typeof(book_profile) = 'object'),
    image_prompt text not null check (btrim(image_prompt) <> '' and length(image_prompt) <= 4000),
    video_prompt text not null check (btrim(video_prompt) <> '' and length(video_prompt) <= 4000),

    storage_path text not null,
    media_sha256 text not null check (media_sha256 ~ '^[0-9a-f]{64}$'),
    size_bytes bigint not null check (size_bytes between 1 and 52428800),
    duration_ms integer not null check (duration_ms between 3000 and 60000),
    width integer not null check (width between 240 and 4320),
    height integer not null check (height between 240 and 4320 and height > width),
    audio_title text,
    audio_start_ms integer check (audio_start_ms is null or audio_start_ms >= 0),

    instagram_container_id text,
    instagram_media_id text,
    instagram_permalink text,
    error text,
    claimed_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    published_at timestamptz,

    constraint reel_publications_storage_path_check check (
        storage_path = id::text || '/' || media_sha256 || '.mp4'
    ),
    constraint reel_publications_container_id_check check (
        instagram_container_id is null or
        (btrim(instagram_container_id) <> '' and length(instagram_container_id) <= 255)
    ),
    constraint reel_publications_media_id_check check (
        instagram_media_id is null or
        (btrim(instagram_media_id) <> '' and length(instagram_media_id) <= 255)
    ),
    constraint reel_publications_state_check check (
        (status = 'ready' and media_status = 'uploaded' and claimed_at is null and
            instagram_container_id is null and instagram_media_id is null and
            published_at is null)
        or (status = 'publishing' and media_status = 'uploaded' and claimed_at is not null and
            instagram_media_id is null and published_at is null)
        or (status = 'publish_uncertain' and media_status = 'uploaded' and claimed_at is not null and
            instagram_media_id is null and published_at is null)
        or (status = 'published' and media_status in ('cleanup_pending', 'deleted') and
            claimed_at is not null and instagram_container_id is not null and
            instagram_media_id is not null and published_at is not null)
        or (status = 'failed' and media_status = 'uploaded' and published_at is null)
    )
);

create unique index reel_publications_container_idx
    on public.reel_publications(instagram_container_id)
    where instagram_container_id is not null;
create unique index reel_publications_media_idx
    on public.reel_publications(instagram_media_id)
    where instagram_media_id is not null;
create index reel_publications_quote_idx on public.reel_publications(quote_id);
create index reel_publications_book_idx on public.reel_publications(book_id);
create index reel_publications_ready_idx
    on public.reel_publications(account_id, priority desc, scheduled_for, created_at)
    where status = 'ready';
create index reel_publications_cleanup_idx
    on public.reel_publications(updated_at)
    where status = 'published' and media_status = 'cleanup_pending';

alter table public.reel_publications enable row level security;
revoke all on table public.reel_publications from public, anon, authenticated, service_role;
grant select, insert, update, delete on table public.reel_publications to service_role;
create trigger set_updated_at before update on public.reel_publications
    for each row execute function public.bookpromo_set_updated_at();

-- Trusted local and n8n backends use the service credential. There are no
-- storage.objects policies for browser users and the bucket is never public.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
    'book-promotion-reels', 'book-promotion-reels', false, 52428800,
    array['video/mp4']::text[]
)
on conflict (id) do update set
    public = excluded.public,
    file_size_limit = excluded.file_size_limit,
    allowed_mime_types = excluded.allowed_mime_types;

-- Atomically claims exactly one complete ready row. A still-active publishing
-- row blocks a second execution for the account; it must be reconciled rather
-- than guessed at. publish_uncertain rows do not get returned or retried.
create function public.bookpromo_reel_claim(p_account text) returns jsonb
language plpgsql security invoker set search_path = '' as $$
declare
    active public.reel_publications;
    picked public.reel_publications;
begin
    if nullif(btrim(p_account), '') is null then
        raise exception 'Invalid account' using errcode = '22023';
    end if;

    perform pg_advisory_xact_lock(hashtextextended('bookpromo-reel:' || p_account, 0));
    select * into active
      from public.reel_publications
     where account_id = p_account and status in ('publishing', 'publish_uncertain')
     order by claimed_at, id
     limit 1
     for update;
    if found then
        return jsonb_build_object('outcome', 'busy', 'reel_id', active.id);
    end if;

    select * into picked
      from public.reel_publications
     where account_id = p_account
       and status = 'ready'
       and media_status = 'uploaded'
       and (scheduled_for is null or scheduled_for <= now())
     order by priority desc, scheduled_for nulls last, created_at, id
     limit 1
     for update skip locked;
    if not found then
        return jsonb_build_object('outcome', 'empty');
    end if;

    update public.reel_publications
       set status = 'publishing', claimed_at = now(), error = null,
           revision = revision + 1, action_token = gen_random_uuid()
     where id = picked.id
    returning * into picked;
    return jsonb_build_object('outcome', 'claimed', 'reel', to_jsonb(picked));
end;
$$;

create function public.bookpromo_reel_transition(
    p_id uuid,
    p_revision integer,
    p_token uuid,
    p_action text,
    p_data jsonb default '{}'::jsonb
) returns jsonb
language plpgsql security invoker set search_path = '' as $$
declare
    reel public.reel_publications;
    payload jsonb := coalesce(p_data, '{}'::jsonb);
    supplied text;
begin
    select * into reel from public.reel_publications where id = p_id for update;
    if not found then
        raise exception 'Reel missing';
    end if;
    if reel.revision is distinct from p_revision or reel.action_token is distinct from p_token then
        return jsonb_build_object('outcome', 'stale');
    end if;
    if jsonb_typeof(payload) <> 'object' then
        raise exception 'Invalid transition data' using errcode = '22023';
    end if;

    if p_action = 'container_ready' and reel.status = 'publishing' then
        supplied := nullif(btrim(payload->>'container_id'), '');
        if supplied is null or length(supplied) > 255 then
            raise exception 'Invalid Reel container' using errcode = '22023';
        end if;
        if reel.instagram_container_id is not null then
            if reel.instagram_container_id = supplied then
                return jsonb_build_object('outcome', 'existing', 'reel', to_jsonb(reel));
            end if;
            raise exception 'Reel container conflict' using errcode = '40001';
        end if;
        reel.instagram_container_id := supplied;

    elsif p_action = 'published' and reel.status in ('publishing', 'publish_uncertain') and
          reel.instagram_container_id is not null then
        supplied := nullif(btrim(payload->>'media_id'), '');
        if supplied is null or length(supplied) > 255 then
            raise exception 'Invalid Instagram media' using errcode = '22023';
        end if;
        if reel.instagram_media_id is not null and reel.instagram_media_id <> supplied then
            raise exception 'Instagram media conflict' using errcode = '40001';
        end if;
        reel.instagram_media_id := supplied;
        reel.instagram_permalink := nullif(payload->>'permalink', '');
        reel.published_at := coalesce(reel.published_at, now());
        reel.media_status := 'cleanup_pending';
        reel.error := null;
        reel.status := 'published';

    elsif p_action = 'publish_uncertain' and reel.status = 'publishing' then
        reel.error := left(coalesce(nullif(payload->>'error', ''), 'Ambiguous Instagram response'), 4000);
        reel.status := 'publish_uncertain';

    elsif p_action = 'fail' and reel.status = 'publishing' and
          reel.instagram_container_id is null then
        reel.error := left(coalesce(nullif(payload->>'error', ''), 'Pre-publication failure'), 4000);
        reel.status := 'failed';

    else
        return jsonb_build_object('outcome', 'invalid_state');
    end if;

    reel.revision := reel.revision + 1;
    reel.action_token := gen_random_uuid();
    update public.reel_publications set
        status = reel.status,
        media_status = reel.media_status,
        revision = reel.revision,
        action_token = reel.action_token,
        instagram_container_id = reel.instagram_container_id,
        instagram_media_id = reel.instagram_media_id,
        instagram_permalink = reel.instagram_permalink,
        published_at = reel.published_at,
        error = reel.error
    where id = reel.id;

    return jsonb_build_object('outcome', 'updated', 'reel', to_jsonb(reel));
end;
$$;

-- This function only records a delete that has already succeeded in Storage.
-- It cannot be called before the Instagram media ID was durably committed.
create function public.bookpromo_reel_cleanup(
    p_id uuid,
    p_revision integer,
    p_token uuid,
    p_deleted_path text
) returns jsonb
language plpgsql security invoker set search_path = '' as $$
declare
    reel public.reel_publications;
begin
    select * into reel from public.reel_publications where id = p_id for update;
    if not found then
        raise exception 'Reel missing';
    end if;
    if reel.revision is distinct from p_revision or reel.action_token is distinct from p_token then
        return jsonb_build_object('outcome', 'stale');
    end if;
    if p_deleted_path is distinct from reel.storage_path then
        raise exception 'Invalid Reel cleanup path' using errcode = '22023';
    end if;
    if reel.status <> 'published' or reel.instagram_media_id is null then
        return jsonb_build_object('outcome', 'invalid_state');
    end if;
    if reel.media_status = 'deleted' then
        return jsonb_build_object('outcome', 'existing', 'reel', to_jsonb(reel));
    end if;
    if reel.media_status <> 'cleanup_pending' then
        return jsonb_build_object('outcome', 'invalid_state');
    end if;

    update public.reel_publications
       set media_status = 'deleted', revision = revision + 1,
           action_token = gen_random_uuid(), error = null
     where id = reel.id
    returning * into reel;
    return jsonb_build_object('outcome', 'complete', 'reel', to_jsonb(reel));
end;
$$;

revoke all on function public.bookpromo_reel_claim(text),
    public.bookpromo_reel_transition(uuid, integer, uuid, text, jsonb),
    public.bookpromo_reel_cleanup(uuid, integer, uuid, text)
from public, anon, authenticated;
grant execute on function public.bookpromo_reel_claim(text),
    public.bookpromo_reel_transition(uuid, integer, uuid, text, jsonb),
    public.bookpromo_reel_cleanup(uuid, integer, uuid, text)
to service_role;

update public.bookpromo_schema set version = 7 where version = 6;
notify pgrst, 'reload schema';
commit;

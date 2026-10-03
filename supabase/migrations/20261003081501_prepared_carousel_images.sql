-- Schema v11: reviewed quote/chapter images can be frozen into carousel posts.
-- Storage URLs are deliberately not signed here; workers resolve a fresh URL.
begin;

do $$
begin
    if (select version from public.bookpromo_schema limit 1) <> 10 then
        raise exception 'Schema v11 requires schema v10';
    end if;
end;
$$;

create table public.carousel_source_media (
    id uuid primary key default gen_random_uuid(),
    scope text not null check (scope in ('quote', 'chapter')),
    quote_id uuid references public.quotes(id) on delete cascade,
    chapter_id uuid references public.chapters(id) on delete cascade,
    provider text not null check (provider in ('supabase', 'cloudflare_r2')),
    bucket text not null check (
        btrim(bucket) <> '' and length(bucket) <= 255
        and (provider <> 'supabase' or bucket = 'book-promotion-assets')
    ),
    path text not null check (
        btrim(path) <> '' and length(path) <= 1024 and path !~ '(^/|\\|(^|/)\.\.?(/|$))'
    ),
    public_url text check (
        public_url is null or (public_url ~ '^https://' and length(public_url) <= 2048)
    ),
    sha256 text not null check (sha256 ~ '^[0-9a-f]{64}$'),
    size_bytes integer not null check (size_bytes between 1 and 8388608),
    width integer not null check (width = 1080),
    height integer not null check (height = 1350),
    mime_type text not null check (mime_type = 'image/jpeg'),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint carousel_source_owner_check check (
        (scope = 'quote' and quote_id is not null and chapter_id is null)
        or (scope = 'chapter' and chapter_id is not null and quote_id is null)
    ),
    constraint carousel_source_path_contract_check check (
        path ~ ('^carousel-sources/' || scope || 's/'
            || '[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}/'
            || coalesce(quote_id, chapter_id)::text || '/' || sha256 || '\.jpg$')
    ),
    constraint carousel_source_public_url_provider_check check (
        provider = 'cloudflare_r2' or public_url is null
    )
);
create unique index carousel_source_quote_idx on public.carousel_source_media(quote_id)
    where quote_id is not null;
create unique index carousel_source_chapter_idx on public.carousel_source_media(chapter_id)
    where chapter_id is not null;
create unique index carousel_source_object_idx
    on public.carousel_source_media(provider, bucket, path);
create trigger set_updated_at before update on public.carousel_source_media
    for each row execute function public.bookpromo_set_updated_at();
alter table public.carousel_source_media enable row level security;
revoke all on table public.carousel_source_media from public, anon, authenticated, service_role;
grant select, insert, update, delete on table public.carousel_source_media to service_role;

alter table public.posts
    add column source_image_scope text,
    add column source_image_owner_id uuid,
    add column source_image_provider text,
    add column source_image_bucket text,
    add column source_image_path text,
    add column source_image_public_url text,
    add column source_image_sha256 text,
    add column source_image_size_bytes integer,
    add column source_image_width integer,
    add column source_image_height integer,
    add column source_image_mime_type text,
    add column error_code text;

alter table public.posts add constraint posts_source_image_snapshot_check check (
    (source_image_scope is null and source_image_owner_id is null
        and source_image_provider is null and source_image_bucket is null
        and source_image_path is null and source_image_public_url is null
        and source_image_sha256 is null and source_image_size_bytes is null
        and source_image_width is null and source_image_height is null
        and source_image_mime_type is null)
    or
    (source_image_scope in ('quote', 'chapter') and source_image_owner_id is not null
        and source_image_provider in ('supabase', 'cloudflare_r2')
        and nullif(btrim(source_image_bucket), '') is not null
        and nullif(btrim(source_image_path), '') is not null
        and (source_image_public_url is null or source_image_public_url ~ '^https://')
        and source_image_sha256 ~ '^[0-9a-f]{64}$'
        and source_image_size_bytes between 1 and 8388608
        and source_image_width = 1080 and source_image_height = 1350
        and source_image_mime_type = 'image/jpeg')
);
alter table public.posts add constraint posts_error_code_check check (
    error_code is null or error_code in ('prepared_image_unavailable')
);
create index posts_open_source_image_idx
    on public.posts(source_image_scope, source_image_owner_id)
    where source_image_owner_id is not null
      and status not in ('published', 'discarded', 'failed');

create function public.bookpromo_carousel_sources(p_book_id uuid) returns jsonb
language sql stable security invoker set search_path = '' as $$
    select jsonb_build_object('sources', coalesce(jsonb_agg(source order by source->>'scope', source->>'owner_id'), '[]'::jsonb))
    from (
        select jsonb_build_object(
            'scope', m.scope,
            'owner_id', coalesce(m.quote_id, m.chapter_id),
            'provider', m.provider, 'bucket', m.bucket, 'path', m.path,
            'public_url', m.public_url, 'sha256', m.sha256,
            'size_bytes', m.size_bytes, 'width', m.width, 'height', m.height,
            'mime_type', m.mime_type
        ) as source
        from public.carousel_source_media m
        left join public.quotes q on q.id = m.quote_id
        join public.chapters c on c.id = coalesce(q.chapter_id, m.chapter_id)
        join public.books b on b.current_version_id = c.book_version_id
        where b.id = p_book_id and c.current and (q.id is null or q.current)
    ) listed;
$$;

create function public.bookpromo_carousel_source_set(
    p_scope text, p_owner_id uuid, p_media jsonb
) returns jsonb
language plpgsql security invoker set search_path = '' as $$
declare
    existing public.carousel_source_media;
    saved public.carousel_source_media;
    owner_book uuid;
begin
    if p_scope not in ('quote', 'chapter') or p_owner_id is null then
        raise exception 'Invalid carousel source owner' using errcode = '22023';
    end if;

    if p_scope = 'quote' then
        select b.id into owner_book from public.quotes q
        join public.chapters c on c.id = q.chapter_id
        join public.books b on b.current_version_id = c.book_version_id
        where q.id = p_owner_id and q.current and c.current for update of q;
    else
        select b.id into owner_book from public.chapters c
        join public.books b on b.current_version_id = c.book_version_id
        where c.id = p_owner_id and c.current for update of c;
    end if;
    if not found then
        raise exception 'Carousel source owner missing or stale';
    end if;

    select * into existing from public.carousel_source_media
     where (p_scope = 'quote' and quote_id = p_owner_id)
        or (p_scope = 'chapter' and chapter_id = p_owner_id)
     for update;

    if exists (
        select 1 from public.posts
         where source_image_scope = p_scope and source_image_owner_id = p_owner_id
           and status not in ('published', 'discarded', 'failed')
    ) then
        raise exception 'Carousel source is frozen in an open post' using errcode = '23505';
    end if;

    if p_media is null or p_media = 'null'::jsonb then
        delete from public.carousel_source_media where id = existing.id;
        return jsonb_build_object('outcome', 'removed', 'old_media', to_jsonb(existing));
    end if;
    if jsonb_typeof(p_media) <> 'object'
       or p_media->>'provider' not in ('supabase', 'cloudflare_r2')
       or nullif(btrim(p_media->>'bucket'), '') is null
       or (p_media->>'provider' = 'supabase' and p_media->>'bucket' <> 'book-promotion-assets')
       or (p_media->>'provider' = 'supabase' and p_media->>'public_url' is not null)
       or nullif(btrim(p_media->>'path'), '') is null
       or p_media->>'path' !~ ('^carousel-sources/' || p_scope || 's/' || owner_book::text
           || '/' || p_owner_id::text || '/[0-9a-f]{64}\.jpg$')
       or p_media->>'sha256' !~ '^[0-9a-f]{64}$'
       or (p_media->>'size_bytes')::integer not between 1 and 8388608
       or (p_media->>'width')::integer <> 1080
       or (p_media->>'height')::integer <> 1350
       or p_media->>'mime_type' <> 'image/jpeg'
       or (p_media->>'public_url' is not null and p_media->>'public_url' !~ '^https://') then
        raise exception 'Invalid carousel source media' using errcode = '22023';
    end if;

    if p_scope = 'quote' then
        insert into public.carousel_source_media(
            scope, quote_id, provider, bucket, path, public_url, sha256,
            size_bytes, width, height, mime_type
        ) values (
            p_scope, p_owner_id, p_media->>'provider', p_media->>'bucket', p_media->>'path',
            nullif(p_media->>'public_url', ''), p_media->>'sha256',
            (p_media->>'size_bytes')::integer, (p_media->>'width')::integer,
            (p_media->>'height')::integer, p_media->>'mime_type'
        )
        on conflict (quote_id) where quote_id is not null do update set
            provider = excluded.provider, bucket = excluded.bucket, path = excluded.path,
            public_url = excluded.public_url, sha256 = excluded.sha256,
            size_bytes = excluded.size_bytes, width = excluded.width,
            height = excluded.height, mime_type = excluded.mime_type
        returning * into saved;
    else
        insert into public.carousel_source_media(
            scope, chapter_id, provider, bucket, path, public_url, sha256,
            size_bytes, width, height, mime_type
        ) values (
            p_scope, p_owner_id, p_media->>'provider', p_media->>'bucket', p_media->>'path',
            nullif(p_media->>'public_url', ''), p_media->>'sha256',
            (p_media->>'size_bytes')::integer, (p_media->>'width')::integer,
            (p_media->>'height')::integer, p_media->>'mime_type'
        )
        on conflict (chapter_id) where chapter_id is not null do update set
            provider = excluded.provider, bucket = excluded.bucket, path = excluded.path,
            public_url = excluded.public_url, sha256 = excluded.sha256,
            size_bytes = excluded.size_bytes, width = excluded.width,
            height = excluded.height, mime_type = excluded.mime_type
        returning * into saved;
    end if;
    return jsonb_build_object('outcome', 'set', 'media', to_jsonb(saved), 'old_media', to_jsonb(existing));
exception when invalid_text_representation or numeric_value_out_of_range then
    raise exception 'Invalid carousel source media' using errcode = '22023';
end;
$$;

create function public.bookpromo_prepared_image_fail(
    p_id uuid, p_revision integer, p_token uuid, p_error text
) returns jsonb
language plpgsql security invoker set search_path = '' as $$
declare d public.posts;
begin
    select * into d from public.posts where id = p_id for update;
    if not found then raise exception 'Draft missing'; end if;
    if d.revision is distinct from p_revision or d.action_token is distinct from p_token then
        return jsonb_build_object('outcome', 'stale');
    end if;
    if d.status <> 'generating_image' or d.source_image_path is null then
        return jsonb_build_object('outcome', 'invalid_state');
    end if;
    update public.post_media
       set status = case when storage_path is null then 'deleted' else 'cleanup_pending' end,
           signed_url_expires_at = null
     where post_id = d.id and status <> 'deleted';
    update public.posts set status = 'failed', revision = revision + 1,
        action_token = gen_random_uuid(), approved_revision = null,
        error_code = 'prepared_image_unavailable',
        error = left(coalesce(nullif(btrim(p_error), ''), 'Prepared carousel image unavailable'), 4000)
     where id = d.id returning * into d;
    return jsonb_build_object('outcome', 'failed', 'post', to_jsonb(d));
end;
$$;

-- Replace both reservation signatures so the chosen source is frozen exactly once.
drop function public.bookpromo_reserve(text, date, text);
drop function public.bookpromo_reserve(text, date, text, integer);

create function public.bookpromo_reserve(
    p_account text, p_day date, p_execution_mode text, p_generation_attempt integer
) returns jsonb
language plpgsql security invoker set search_path = '' as $$
declare
    cfg public.promotion_settings;
    draft public.posts;
    selected_book uuid;
    picked public.quotes;
    source public.carousel_source_media;
begin
    if p_execution_mode not in ('review', 'auto') then
        raise exception 'Invalid execution mode' using errcode = '22023';
    end if;
    if p_generation_attempt is null or p_generation_attempt not between 0 and 10 then
        raise exception 'Invalid generation attempt' using errcode = '22023';
    end if;
    select * into cfg from public.promotion_settings where account_id = p_account for update;
    if not found or not cfg.active then return jsonb_build_object('outcome', 'inactive'); end if;
    if p_execution_mode = 'review' and
       (nullif(cfg.telegram_chat_id, '') is null or nullif(cfg.telegram_user_id, '') is null) then
        raise exception 'Telegram approval identity missing';
    end if;
    select * into draft from public.posts
     where account_id = p_account and status not in ('published', 'discarded', 'failed')
     order by created_at, id limit 1;
    if found then
        if draft.execution_mode <> p_execution_mode then
            return jsonb_build_object('outcome', 'blocked_by_other_mode', 'post_id', draft.id,
                                      'execution_mode', draft.execution_mode);
        end if;
        return jsonb_build_object('outcome', 'existing', 'post', to_jsonb(draft));
    end if;
    perform pg_advisory_xact_lock(41020260908);
    select b.id into selected_book from public.books b
     where b.active and (cfg.mode = 'random_book' or b.id = cfg.fixed_book_id)
       and b.profile->>'publication_mode' = p_execution_mode
       and nullif(btrim(b.profile->>'overlay_path'), '') is not null
       and nullif(btrim(b.profile->>'carousel_end_slide_path'), '') is not null
       and nullif(btrim(b.profile->>'carousel_end_text'), '') is not null
       and exists (
           select 1 from public.quotes q join public.chapters c on c.id = q.chapter_id
            where c.book_version_id = b.current_version_id and c.current and q.current
              and q.approved and not q.blocked and q.spoiler_level in ('none', 'low')
              and not exists (
                  select 1 from public.posts p where p.quote_id = q.id and (
                      p.status not in ('published', 'discarded', 'failed')
                      or (p.status = 'published' and (cfg.reuse_after_days is null or
                          p.published_at > now() - make_interval(days => cfg.reuse_after_days)))
                      or (p.status = 'discarded' and
                          p.updated_at > now() - make_interval(days => cfg.discard_cooldown_days))
                      or (p.status = 'failed' and p.run_date = p_day and p.execution_mode = p_execution_mode)
                  )
              )
       ) order by random() limit 1;
    if selected_book is null then return jsonb_build_object('outcome', 'no_quote'); end if;
    select q.* into picked from public.quotes q
      join public.chapters c on c.id = q.chapter_id
      join public.books b on b.current_version_id = c.book_version_id
     where b.id = selected_book and c.current and q.current and q.approved and not q.blocked
       and q.spoiler_level in ('none', 'low')
       and not exists (
           select 1 from public.posts p where p.quote_id = q.id and (
               p.status not in ('published', 'discarded', 'failed')
               or (p.status = 'published' and (cfg.reuse_after_days is null or
                   p.published_at > now() - make_interval(days => cfg.reuse_after_days)))
               or (p.status = 'discarded' and
                   p.updated_at > now() - make_interval(days => cfg.discard_cooldown_days))
               or (p.status = 'failed' and p.run_date = p_day and p.execution_mode = p_execution_mode)
           )
       )
     order by exists (select 1 from public.posts p where p.quote_id = q.id and p.status = 'published'),
              random() limit 1;

    select * into source from public.carousel_source_media where quote_id = picked.id;
    if not found then
        select * into source from public.carousel_source_media where chapter_id = picked.chapter_id;
    end if;
    insert into public.posts(
        quote_id, account_id, run_date, quote_text, book_profile, telegram_chat_id,
        execution_mode, attempts, source_image_scope, source_image_owner_id,
        source_image_provider, source_image_bucket, source_image_path,
        source_image_public_url, source_image_sha256, source_image_size_bytes,
        source_image_width, source_image_height, source_image_mime_type
    )
    select picked.id, p_account, p_day, picked.text,
           b.profile || jsonb_build_object('chapter_position', c.position, 'chapter_name', c.name),
           cfg.telegram_chat_id, p_execution_mode, p_generation_attempt,
           source.scope, coalesce(source.quote_id, source.chapter_id), source.provider,
           source.bucket, source.path, source.public_url, source.sha256, source.size_bytes,
           source.width, source.height, source.mime_type
      from public.books b join public.chapters c on c.id = picked.chapter_id
     where b.id = selected_book returning * into draft;
    return jsonb_build_object('outcome', 'created', 'post', to_jsonb(draft));
end;
$$;

create function public.bookpromo_reserve(p_account text, p_day date, p_execution_mode text)
returns jsonb language sql security invoker set search_path = '' as $$
    select public.bookpromo_reserve(p_account, p_day, p_execution_mode, 0);
$$;

revoke all on function public.bookpromo_carousel_sources(uuid),
    public.bookpromo_carousel_source_set(text, uuid, jsonb),
    public.bookpromo_prepared_image_fail(uuid, integer, uuid, text),
    public.bookpromo_reserve(text, date, text),
    public.bookpromo_reserve(text, date, text, integer)
    from public, anon, authenticated;
grant execute on function public.bookpromo_carousel_sources(uuid),
    public.bookpromo_carousel_source_set(text, uuid, jsonb),
    public.bookpromo_prepared_image_fail(uuid, integer, uuid, text),
    public.bookpromo_reserve(text, date, text),
    public.bookpromo_reserve(text, date, text, integer)
    to service_role;

update public.bookpromo_schema set version = 11 where version = 10;
commit;

-- Schema v5: carousel drafts, ordered media manifests and private temporary
-- publication media. This migration is prepared locally and must only be
-- applied together with the matching Python sync and n8n carousel workflows.
begin;

-- The project has only been used for a few test publications. Refuse to
-- reinterpret an unexpectedly large post history as disposable test data.
do $$
declare
    existing_count bigint;
    existing_ids text;
begin
    select count(*), string_agg(id::text, ', ' order by id)
      into existing_count, existing_ids
      from public.posts;

    if existing_count > 10 then
        raise exception 'Refusing carousel migration: expected at most 10 test posts, found %', existing_count;
    end if;

    raise notice 'Carousel migration removes % test post(s): %', existing_count, coalesce(existing_ids, '(none)');
    delete from public.posts;
end;
$$;

alter table public.posts
    add column execution_mode text not null default 'review'
        constraint posts_execution_mode_check check (execution_mode in ('review', 'auto'));
alter table public.posts drop column image_path;

-- Existing v4 catalogue rows stay readable during cutover. The NOT VALID
-- constraint is enforced for every new or updated v5 book snapshot without
-- forcing old inactive rows to contain carousel fields retroactively.
alter table public.books add constraint books_carousel_profile_check check (
    jsonb_typeof(profile) = 'object'
    and coalesce(profile->>'publication_mode' in ('review', 'auto'), false)
    and length(coalesce(profile->>'carousel_end_text', '')) <= 500
    and (
        coalesce(profile->>'overlay_path', '') = ''
        or profile->>'overlay_path' ~ ('^' || id::text || '/[0-9a-f]{64}\.png$')
    )
    and (
        coalesce(profile->>'carousel_end_slide_path', '') = ''
        or profile->>'carousel_end_slide_path' ~ ('^' || id::text || '/carousel/[0-9a-f]{64}\.jpg$')
    )
    and (
        not active
        or (
            nullif(btrim(profile->>'overlay_path'), '') is not null
            and nullif(btrim(profile->>'carousel_end_slide_path'), '') is not null
            and nullif(btrim(profile->>'carousel_end_text'), '') is not null
        )
    )
) not valid;

create table public.post_media (
    id bigint generated always as identity primary key,
    post_id uuid not null references public.posts(id) on delete cascade,
    manifest_revision integer not null check (manifest_revision >= 1),
    position smallint not null check (position between 0 and 9),
    kind text not null check (kind in ('hero', 'quote', 'cta')),
    text_fragment text,
    alt_text text check (alt_text is null or (btrim(alt_text) <> '' and length(alt_text) <= 1000)),
    storage_path text,
    sha256 text,
    signed_url_expires_at timestamptz,
    instagram_container_id text,
    status text not null default 'generated' check (status in (
        'generated', 'uploaded', 'container_ready', 'cleanup_pending', 'deleted', 'failed'
    )),
    error text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint post_media_post_position_key unique (post_id, position),
    constraint post_media_kind_position_check check (
        (kind = 'hero' and position = 0 and text_fragment is null)
        or (kind = 'quote' and position between 1 and 8 and nullif(btrim(text_fragment), '') is not null)
        or (kind = 'cta' and position between 2 and 9 and text_fragment is null)
    ),
    constraint post_media_storage_pair_check check ((storage_path is null) = (sha256 is null)),
    constraint post_media_sha256_check check (sha256 is null or sha256 ~ '^[0-9a-f]{64}$'),
    constraint post_media_storage_path_check check (
        storage_path is null or storage_path =
            post_id::text || '/' || manifest_revision::text || '/' ||
            lpad(position::text, 2, '0') || '-' || sha256 || '.jpg'
    ),
    constraint post_media_uploaded_state_check check (
        status in ('generated', 'failed') or (storage_path is not null and sha256 is not null)
    ),
    constraint post_media_container_state_check check (
        status <> 'container_ready' or nullif(btrim(instagram_container_id), '') is not null
    ),
    constraint post_media_container_id_check check (
        instagram_container_id is null or
        (btrim(instagram_container_id) <> '' and length(instagram_container_id) <= 255)
    )
);

create unique index post_media_container_idx
    on public.post_media(instagram_container_id)
    where instagram_container_id is not null;
create index post_media_uploaded_idx
    on public.post_media(post_id, position)
    where status = 'uploaded';
create index post_media_cleanup_idx
    on public.post_media(updated_at, post_id)
    where status = 'cleanup_pending';

alter table public.post_media enable row level security;
revoke all on table public.post_media from public, anon, authenticated, service_role;
grant select, insert, update, delete on table public.post_media to service_role;
revoke all on sequence public.post_media_id_seq from public, anon, authenticated, service_role;
grant usage, select on sequence public.post_media_id_seq to service_role;
create trigger set_updated_at before update on public.post_media
    for each row execute function public.bookpromo_set_updated_at();

-- No storage.objects policies are needed: uploads, signed URLs and deletions
-- are performed only by trusted backends with the server/service credential.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('book-promotion-media', 'book-promotion-media', false, 8388608, array['image/jpeg']::text[])
on conflict (id) do update set
    public = excluded.public,
    file_size_limit = excluded.file_size_limit,
    allowed_mime_types = excluded.allowed_mime_types;

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
    'book-promotion-assets', 'book-promotion-assets', false, 8388608,
    array['image/png', 'image/jpeg']::text[]
)
on conflict (id) do update set
    public = excluded.public,
    file_size_limit = excluded.file_size_limit,
    allowed_mime_types = excluded.allowed_mime_types;

drop function public.bookpromo_reserve(text, date);

create function public.bookpromo_reserve(p_account text, p_day date, p_execution_mode text) returns jsonb
language plpgsql security invoker set search_path = '' as $$
declare
    cfg public.promotion_settings;
    draft public.posts;
    selected_book uuid;
    picked public.quotes;
begin
    if p_execution_mode not in ('review', 'auto') then
        raise exception 'Invalid execution mode' using errcode = '22023';
    end if;

    select * into cfg
      from public.promotion_settings
     where account_id = p_account
     for update;
    if not found or not cfg.active then
        return jsonb_build_object('outcome', 'inactive');
    end if;
    if p_execution_mode = 'review' and
       (nullif(cfg.telegram_chat_id, '') is null or nullif(cfg.telegram_user_id, '') is null) then
        raise exception 'Telegram approval identity missing';
    end if;

    select * into draft
      from public.posts
     where account_id = p_account
       and status not in ('published', 'discarded', 'failed')
     order by created_at, id
     limit 1;
    if found then
        if draft.execution_mode <> p_execution_mode then
            return jsonb_build_object(
                'outcome', 'blocked_by_other_mode',
                'post_id', draft.id,
                'execution_mode', draft.execution_mode
            );
        end if;
        return jsonb_build_object('outcome', 'existing', 'post', to_jsonb(draft));
    end if;

    perform pg_advisory_xact_lock(41020260908);
    select b.id into selected_book
      from public.books b
     where b.active
       and (cfg.mode = 'random_book' or b.id = cfg.fixed_book_id)
       and b.profile->>'publication_mode' = p_execution_mode
       and nullif(btrim(b.profile->>'overlay_path'), '') is not null
       and nullif(btrim(b.profile->>'carousel_end_slide_path'), '') is not null
       and nullif(btrim(b.profile->>'carousel_end_text'), '') is not null
       and exists (
           select 1
             from public.quotes q
             join public.chapters c on c.id = q.chapter_id
            where c.book_version_id = b.current_version_id
              and c.current and q.current and q.approved and not q.blocked
              and q.spoiler_level in ('none', 'low')
              and not exists (
                  select 1
                    from public.posts p
                   where p.quote_id = q.id
                     and (
                         p.status not in ('published', 'discarded', 'failed')
                         or (p.status = 'published' and (
                             cfg.reuse_after_days is null or
                             p.published_at > now() - make_interval(days => cfg.reuse_after_days)
                         ))
                         or (p.status = 'discarded' and
                             p.updated_at > now() - make_interval(days => cfg.discard_cooldown_days))
                     )
              )
       )
     order by random()
     limit 1;
    if selected_book is null then
        return jsonb_build_object('outcome', 'no_quote');
    end if;

    select q.* into picked
      from public.quotes q
      join public.chapters c on c.id = q.chapter_id
      join public.books b on b.current_version_id = c.book_version_id
     where b.id = selected_book
       and c.current and q.current and q.approved and not q.blocked
       and q.spoiler_level in ('none', 'low')
       and not exists (
           select 1
             from public.posts p
            where p.quote_id = q.id
              and (
                  p.status not in ('published', 'discarded', 'failed')
                  or (p.status = 'published' and (
                      cfg.reuse_after_days is null or
                      p.published_at > now() - make_interval(days => cfg.reuse_after_days)
                  ))
                  or (p.status = 'discarded' and
                      p.updated_at > now() - make_interval(days => cfg.discard_cooldown_days))
              )
       )
     order by exists (
         select 1 from public.posts p where p.quote_id = q.id and p.status = 'published'
     ), random()
     limit 1;

    insert into public.posts(
        quote_id, account_id, run_date, quote_text, book_profile,
        telegram_chat_id, execution_mode
    )
    select picked.id, p_account, p_day, picked.text,
           b.profile || jsonb_build_object(
               'chapter_position', c.position,
               'chapter_name', c.name
           ),
           cfg.telegram_chat_id, p_execution_mode
      from public.books b
      join public.chapters c on c.id = picked.chapter_id
     where b.id = selected_book
    returning * into draft;

    return jsonb_build_object('outcome', 'created', 'post', to_jsonb(draft));
end;
$$;

create or replace function public.bookpromo_transition(
    p_id uuid,
    p_revision integer,
    p_token uuid,
    p_action text,
    p_data jsonb default '{}'::jsonb
) returns jsonb
language plpgsql security invoker set search_path = '' as $$
declare
    d public.posts;
    cfg public.promotion_settings;
    payload jsonb := coalesce(p_data, '{}'::jsonb);
    media jsonb;
    media_count integer;
    media_position integer;
    expected_position integer;
    last_position integer;
    expected_path text;
    minimum_position integer;
    maximum_position integer;
    hero_count integer;
    quote_count integer;
    cta_count integer;
begin
    select * into d from public.posts where id = p_id for update;
    if not found then
        raise exception 'Draft missing';
    end if;
    if d.revision is distinct from p_revision or d.action_token is distinct from p_token then
        return jsonb_build_object('outcome', 'stale');
    end if;
    if jsonb_typeof(payload) <> 'object' then
        raise exception 'Invalid transition data' using errcode = '22023';
    end if;

    if p_action in ('approve_text', 'approve_image', 'retry_text', 'retry_image', 'discard') then
        if d.execution_mode <> 'review' then
            return jsonb_build_object('outcome', 'invalid_state');
        end if;
        select * into cfg from public.promotion_settings where account_id = d.account_id;
        if nullif(cfg.telegram_chat_id, '') is null or nullif(cfg.telegram_user_id, '') is null or
           payload->>'chat_id' is distinct from cfg.telegram_chat_id or
           payload->>'user_id' is distinct from cfg.telegram_user_id then
            raise exception 'Approval identity mismatch' using errcode = '42501';
        end if;
    end if;

    if p_action = 'text_ready' and d.status = 'generating_text' then
        if length(coalesce(payload->>'caption', '')) not between length(d.quote_text) and 2200 or
           position(d.quote_text in payload->>'caption') = 0 or
           nullif(btrim(payload->>'image_prompt'), '') is null then
            raise exception 'Invalid text';
        end if;
        d.caption := payload->>'caption';
        d.image_prompt := payload->>'image_prompt';
        d.error := null;
        if d.execution_mode = 'auto' then
            d.text_approved_by := 'system:auto';
            d.text_approved_at := now();
            d.text_approved_revision := d.text_revision;
            d.status := 'generating_image';
        else
            d.status := 'awaiting_text_approval';
        end if;

    elsif p_action = 'approve_text' and d.status = 'awaiting_text_approval' then
        d.text_approved_by := payload->>'user_id';
        d.text_approved_at := now();
        d.text_approved_revision := d.text_revision;
        d.status := 'generating_image';

    elsif p_action = 'media_ready' and d.status = 'generating_image' and
          d.text_approved_revision = d.text_revision then
        if jsonb_typeof(payload->'media') <> 'array' then
            raise exception 'Invalid media manifest' using errcode = '22023';
        end if;
        media_count := jsonb_array_length(payload->'media');
        if media_count not between 3 and 10 then
            raise exception 'Invalid media manifest' using errcode = '22023';
        end if;
        if exists (
            select 1 from public.post_media
             where post_id = d.id and status <> 'deleted'
        ) then
            raise exception 'Previous media cleanup incomplete' using errcode = '55000';
        end if;

        expected_position := 0;
        last_position := media_count - 1;
        for media in select value from jsonb_array_elements(payload->'media') loop
            if jsonb_typeof(media) <> 'object' or
               coalesce(media->>'position', '') !~ '^(0|[1-9][0-9]*)$' then
                raise exception 'Invalid media manifest' using errcode = '22023';
            end if;
            media_position := (media->>'position')::integer;
            if media_position <> expected_position or
               coalesce(media->>'sha256', '') !~ '^[0-9a-f]{64}$' then
                raise exception 'Invalid media manifest' using errcode = '22023';
            end if;
            expected_path := d.id::text || '/' || d.revision::text || '/' ||
                lpad(media_position::text, 2, '0') || '-' || (media->>'sha256') || '.jpg';
            if media->>'storage_path' is distinct from expected_path then
                raise exception 'Invalid media manifest' using errcode = '22023';
            end if;
            if media_position = 0 then
                if media->>'kind' is distinct from 'hero' or media->>'text_fragment' is not null then
                    raise exception 'Invalid media manifest' using errcode = '22023';
                end if;
            elsif media_position = last_position then
                if media->>'kind' is distinct from 'cta' or media->>'text_fragment' is not null then
                    raise exception 'Invalid media manifest' using errcode = '22023';
                end if;
            elsif media->>'kind' is distinct from 'quote' or
                  nullif(btrim(media->>'text_fragment'), '') is null then
                raise exception 'Invalid media manifest' using errcode = '22023';
            end if;
            if media->>'alt_text' is not null and
               (nullif(btrim(media->>'alt_text'), '') is null or length(media->>'alt_text') > 1000) then
                raise exception 'Invalid media manifest' using errcode = '22023';
            end if;
            expected_position := expected_position + 1;
        end loop;

        delete from public.post_media where post_id = d.id and status = 'deleted';
        for media in select value from jsonb_array_elements(payload->'media') loop
            insert into public.post_media(
                post_id, manifest_revision, position, kind, text_fragment,
                alt_text, storage_path, sha256, status
            ) values (
                d.id, d.revision, (media->>'position')::integer, media->>'kind',
                media->>'text_fragment', media->>'alt_text', media->>'storage_path',
                media->>'sha256', 'uploaded'
            );
        end loop;
        d.error := null;
        if d.execution_mode = 'auto' then
            d.approved_by := 'system:auto';
            d.approved_at := now();
            d.status := 'approved';
        else
            d.status := 'awaiting_image_approval';
        end if;

    elsif p_action = 'approve_image' and d.status = 'awaiting_image_approval' and
          d.text_approved_revision = d.text_revision then
        if not exists (select 1 from public.post_media where post_id = d.id) or
           exists (select 1 from public.post_media where post_id = d.id and status <> 'uploaded') then
            raise exception 'Media manifest is not ready' using errcode = '55000';
        end if;
        d.approved_by := payload->>'user_id';
        d.approved_at := now();
        d.status := 'approved';

    elsif p_action = 'retry_text' and
          d.status in ('awaiting_text_approval', 'awaiting_image_approval') and
          d.attempts < cfg.max_generations then
        update public.post_media
           set status = case when storage_path is null then 'deleted' else 'cleanup_pending' end,
               instagram_container_id = null,
               signed_url_expires_at = null,
               error = null
         where post_id = d.id and status <> 'deleted';
        d.text_revision := d.text_revision + 1;
        d.text_approved_revision := null;
        d.text_approved_by := null;
        d.text_approved_at := null;
        d.approved_by := null;
        d.approved_at := null;
        d.caption := null;
        d.image_prompt := null;
        d.instagram_container_id := null;
        d.error := null;
        d.status := 'generating_text';
        d.attempts := d.attempts + 1;

    elsif p_action = 'retry_image' and d.status = 'awaiting_image_approval' and
          d.attempts < cfg.max_generations then
        update public.post_media
           set status = case when storage_path is null then 'deleted' else 'cleanup_pending' end,
               instagram_container_id = null,
               signed_url_expires_at = null,
               error = null
         where post_id = d.id and status <> 'deleted';
        d.approved_by := null;
        d.approved_at := null;
        d.instagram_container_id := null;
        d.error := null;
        d.status := 'generating_image';
        d.attempts := d.attempts + 1;

    elsif p_action = 'discard' and d.status in ('awaiting_text_approval', 'awaiting_image_approval') then
        update public.post_media
           set status = case when storage_path is null then 'deleted' else 'cleanup_pending' end,
               signed_url_expires_at = null,
               error = null
         where post_id = d.id and status <> 'deleted';
        d.status := 'discarded';

    elsif p_action = 'begin_publish' and d.status = 'approved' and
          d.text_approved_revision = d.text_revision then
        select count(*), min(position), max(position),
               count(*) filter (where kind = 'hero'),
               count(*) filter (where kind = 'quote'),
               count(*) filter (where kind = 'cta')
          into media_count, minimum_position, maximum_position,
               hero_count, quote_count, cta_count
          from public.post_media where post_id = d.id;
        if media_count not between 3 and 10 or minimum_position <> 0 or
           maximum_position <> media_count - 1 or hero_count <> 1 or
           quote_count <> media_count - 2 or cta_count <> 1 or
           exists (select 1 from public.post_media where post_id = d.id and status <> 'uploaded') then
            raise exception 'Media manifest is not publishable' using errcode = '55000';
        end if;
        d.status := 'publishing';

    elsif p_action = 'carousel_container_ready' and d.status = 'publishing' then
        if nullif(btrim(payload->>'container_id'), '') is null or
           length(payload->>'container_id') > 255 then
            raise exception 'Missing carousel container';
        end if;
        select count(*), min(position), max(position),
               count(*) filter (where kind = 'hero'),
               count(*) filter (where kind = 'quote'),
               count(*) filter (where kind = 'cta')
          into media_count, minimum_position, maximum_position,
               hero_count, quote_count, cta_count
          from public.post_media where post_id = d.id;
        if media_count not between 3 and 10 or minimum_position <> 0 or
           maximum_position <> media_count - 1 or hero_count <> 1 or
           quote_count <> media_count - 2 or cta_count <> 1 or
           exists (select 1 from public.post_media where post_id = d.id and status <> 'container_ready') then
            raise exception 'Child containers are not ready' using errcode = '55000';
        end if;
        if d.instagram_container_id is not null then
            if d.instagram_container_id = payload->>'container_id' then
                return jsonb_build_object('outcome', 'existing', 'post', to_jsonb(d));
            end if;
            raise exception 'Carousel container conflict' using errcode = '40001';
        end if;
        d.instagram_container_id := payload->>'container_id';

    elsif p_action = 'published' and d.status in ('publishing', 'publish_uncertain') and
          d.instagram_container_id is not null then
        if nullif(btrim(payload->>'media_id'), '') is null or length(payload->>'media_id') > 255 then
            raise exception 'Missing Instagram media';
        end if;
        select count(*) into media_count from public.post_media where post_id = d.id;
        if media_count not between 3 and 10 or
           exists (select 1 from public.post_media where post_id = d.id and status <> 'container_ready') then
            raise exception 'Child containers are not ready' using errcode = '55000';
        end if;
        d.instagram_media_id := payload->>'media_id';
        d.instagram_permalink := payload->>'permalink';
        d.published_at := now();
        d.error := null;
        d.status := 'published';
        update public.post_media
           set status = 'cleanup_pending', signed_url_expires_at = null, error = null
         where post_id = d.id and status = 'container_ready';

    elsif p_action = 'publish_uncertain' and d.status = 'publishing' then
        d.error := left(nullif(payload->>'error', ''), 4000);
        d.status := 'publish_uncertain';

    elsif p_action = 'fail' and d.status in ('generating_text', 'generating_image', 'approved') then
        update public.post_media
           set status = case when storage_path is null then 'deleted' else 'cleanup_pending' end,
               signed_url_expires_at = null
         where post_id = d.id and status <> 'deleted';
        d.error := left(nullif(payload->>'error', ''), 4000);
        d.status := 'failed';

    else
        return jsonb_build_object('outcome', 'invalid_state');
    end if;

    d.revision := d.revision + 1;
    d.action_token := gen_random_uuid();
    if d.status in ('approved', 'publishing', 'published', 'publish_uncertain') then
        d.approved_revision := d.revision;
    else
        d.approved_revision := null;
    end if;

    update public.posts set
        status = d.status,
        revision = d.revision,
        action_token = d.action_token,
        caption = d.caption,
        image_prompt = d.image_prompt,
        text_revision = d.text_revision,
        text_approved_revision = d.text_approved_revision,
        text_approved_by = d.text_approved_by,
        text_approved_at = d.text_approved_at,
        approved_revision = d.approved_revision,
        approved_by = d.approved_by,
        approved_at = d.approved_at,
        instagram_container_id = d.instagram_container_id,
        instagram_media_id = d.instagram_media_id,
        instagram_permalink = d.instagram_permalink,
        published_at = d.published_at,
        attempts = d.attempts,
        error = d.error
    where id = d.id;

    return jsonb_build_object('outcome', 'updated', 'post', to_jsonb(d));
end;
$$;

create function public.bookpromo_media_container(
    p_id uuid,
    p_revision integer,
    p_token uuid,
    p_position smallint,
    p_container_id text
) returns jsonb
language plpgsql security invoker set search_path = '' as $$
declare
    d public.posts;
    media public.post_media;
begin
    select * into d from public.posts where id = p_id for update;
    if not found then
        raise exception 'Draft missing';
    end if;
    if d.revision is distinct from p_revision or d.action_token is distinct from p_token then
        return jsonb_build_object('outcome', 'stale');
    end if;
    if d.status <> 'publishing' then
        return jsonb_build_object('outcome', 'invalid_state');
    end if;
    if nullif(btrim(p_container_id), '') is null or length(p_container_id) > 255 then
        raise exception 'Invalid child container' using errcode = '22023';
    end if;

    select * into media
      from public.post_media
     where post_id = p_id and position = p_position
     for update;
    if not found then
        raise exception 'Media item missing';
    end if;
    if media.status = 'container_ready' then
        if media.instagram_container_id = p_container_id then
            return jsonb_build_object('outcome', 'existing', 'media', to_jsonb(media));
        end if;
        raise exception 'Child container conflict' using errcode = '40001';
    end if;
    if media.status <> 'uploaded' or media.instagram_container_id is not null then
        return jsonb_build_object('outcome', 'invalid_state');
    end if;

    update public.post_media
       set instagram_container_id = p_container_id,
           status = 'container_ready',
           error = null
     where id = media.id
    returning * into media;

    return jsonb_build_object('outcome', 'updated', 'media', to_jsonb(media));
end;
$$;

create function public.bookpromo_media_cleanup(
    p_id uuid,
    p_revision integer,
    p_token uuid,
    p_deleted_paths jsonb,
    p_error text default null
) returns jsonb
language plpgsql security invoker set search_path = '' as $$
declare
    d public.posts;
    deleted_path jsonb;
    remaining integer;
begin
    select * into d from public.posts where id = p_id for update;
    if not found then
        raise exception 'Draft missing';
    end if;
    if d.revision is distinct from p_revision or d.action_token is distinct from p_token then
        return jsonb_build_object('outcome', 'stale');
    end if;
    if d.status not in ('generating_text', 'generating_image', 'published', 'discarded', 'failed') then
        return jsonb_build_object('outcome', 'invalid_state');
    end if;
    if p_deleted_paths is null or jsonb_typeof(p_deleted_paths) <> 'array' or
       jsonb_array_length(p_deleted_paths) > 10 then
        raise exception 'Invalid cleanup paths' using errcode = '22023';
    end if;

    for deleted_path in select value from jsonb_array_elements(p_deleted_paths) loop
        if jsonb_typeof(deleted_path) <> 'string' or
           not exists (
               select 1 from public.post_media
                where post_id = d.id
                  and storage_path = deleted_path #>> '{}'
                  and status in ('cleanup_pending', 'deleted')
           ) then
            raise exception 'Invalid cleanup path' using errcode = '22023';
        end if;
    end loop;

    update public.post_media
       set status = 'deleted',
           signed_url_expires_at = null,
           error = null
     where post_id = d.id
       and status = 'cleanup_pending'
       and storage_path in (
           select value #>> '{}'
             from jsonb_array_elements(p_deleted_paths)
       );

    if nullif(p_error, '') is not null then
        update public.post_media
           set error = left(p_error, 4000)
         where post_id = d.id and status = 'cleanup_pending';
    end if;

    select count(*) into remaining
      from public.post_media
     where post_id = d.id and status = 'cleanup_pending';
    return jsonb_build_object(
        'outcome', case when remaining = 0 then 'complete' else 'pending' end,
        'remaining', remaining
    );
end;
$$;

revoke all on function public.bookpromo_reserve(text, date, text),
    public.bookpromo_transition(uuid, integer, uuid, text, jsonb),
    public.bookpromo_media_container(uuid, integer, uuid, smallint, text),
    public.bookpromo_media_cleanup(uuid, integer, uuid, jsonb, text)
from public, anon, authenticated;
grant execute on function public.bookpromo_reserve(text, date, text),
    public.bookpromo_transition(uuid, integer, uuid, text, jsonb),
    public.bookpromo_media_container(uuid, integer, uuid, smallint, text),
    public.bookpromo_media_cleanup(uuid, integer, uuid, jsonb, text)
to service_role;

update public.bookpromo_schema set version = 5;

notify pgrst, 'reload schema';
commit;

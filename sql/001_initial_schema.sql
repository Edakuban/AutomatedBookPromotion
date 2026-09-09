-- Book Promotion / schema version 1
-- Reviewed bootstrap SQL for a new Supabase project (Postgres 15+).
-- Execute ONCE in step 3.2, as postgres, after checking the target project.
-- No DROP, no existing table replacement, no changes to global default grants.
-- Existing names cause an error and roll back the entire script.
begin;

create table public.bookpromo_schema (
    singleton boolean primary key default true check (singleton),
    version integer not null check (version > 0),
    installed_at timestamptz not null default now()
);

create table public.books (
    id uuid primary key default gen_random_uuid(),
    title text not null check (btrim(title) <> ''),
    author text not null default '',
    description text not null default '',
    active boolean not null default false,
    current_version_id uuid,
    image_base_prompt text not null default '',
    caption_instructions text not null default '',
    world_description text not null default '',
    character_description text not null default '',
    target_url text not null default '',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table public.book_versions (
    id uuid primary key default gen_random_uuid(),
    book_id uuid not null references public.books(id) on delete restrict,
    filename text not null check (btrim(filename) <> ''),
    file_sha256 text not null check (file_sha256 ~ '^[0-9a-f]{64}$'),
    status text not null default 'queued'
        check (status in ('queued', 'extracting', 'analyzing', 'needs_review', 'ready', 'failed')),
    summary text not null default '',
    analysis_version text not null default '',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (book_id, file_sha256),
    unique (book_id, id)
);

-- A book cannot point at a version belonging to another book.
alter table public.books add constraint books_current_version_fk
    foreign key (id, current_version_id) references public.book_versions(book_id, id) on delete restrict;
create index books_current_version_idx on public.books(current_version_id);
create index book_versions_recent_idx on public.book_versions(book_id, created_at desc, id);

create table public.chapters (
    id uuid primary key default gen_random_uuid(),
    book_version_id uuid not null references public.book_versions(id) on delete restrict,
    position integer not null check (position >= 1),
    name text not null check (btrim(name) <> ''),
    source_text text not null default '',
    paragraphs jsonb not null default '[]'::jsonb check (jsonb_typeof(paragraphs) = 'array'),
    summary text not null default '',
    status text not null default 'queued' check (status in ('queued','analyzing','needs_review','ready','failed')),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (book_version_id, position)
);

create table public.quotes (
    id uuid primary key default gen_random_uuid(),
    chapter_id uuid not null references public.chapters(id) on delete restrict,
    text text not null check (btrim(text) <> ''),
    -- Zero-based Unicode character positions in chapters.source_text; end is exclusive.
    source_start integer not null check (source_start >= 0),
    source_end integer not null check (source_end > source_start),
    context text not null default '',
    scores jsonb not null default '{}'::jsonb check (jsonb_typeof(scores) = 'object'),
    selection_reason text not null default '',
    spoiler_level text not null default 'unknown' check (spoiler_level in ('unknown','none','low','high')),
    approved boolean not null default false,
    blocked boolean not null default false,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (chapter_id, source_start, source_end)
);

create table public.import_jobs (
    id uuid primary key default gen_random_uuid(),
    book_version_id uuid not null references public.book_versions(id) on delete restrict,
    status text not null default 'queued' check (status in ('queued','running','needs_review','completed','failed')),
    step text not null default 'extract',
    completed_units integer not null default 0 check (completed_units >= 0),
    total_units integer not null default 0 check (total_units >= completed_units),
    checkpoint jsonb not null default '{}'::jsonb check (jsonb_typeof(checkpoint) = 'object'),
    attempts integer not null default 0 check (attempts >= 0),
    worker_id text,
    lease_expires_at timestamptz,
    heartbeat_at timestamptz,
    error text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    check (status <> 'running' or (worker_id is not null and lease_expires_at is not null))
);
create index import_jobs_version_idx on public.import_jobs(book_version_id);
create unique index import_jobs_one_open_idx on public.import_jobs(book_version_id)
    where status in ('queued','running','needs_review');
create index import_jobs_queue_idx on public.import_jobs(status, created_at)
    where status in ('queued','running');

create table public.posts (
    id uuid primary key default gen_random_uuid(),
    quote_id uuid not null references public.quotes(id) on delete restrict,
    account_id text not null check (btrim(account_id) <> ''),
    -- Local scheduled day in Europe/Berlin, supplied explicitly by n8n.
    run_date date not null,
    status text not null default 'generating' check (status in (
        'generating','awaiting_approval','regenerating','approved','publishing',
        'published','discarded','failed','publish_uncertain'
    )),
    revision integer not null default 1 check (revision >= 1),
    quote_text text not null check (btrim(quote_text) <> ''),
    book_profile jsonb not null default '{}'::jsonb check (jsonb_typeof(book_profile) = 'object'),
    image_prompt text,
    caption text,
    image_path text,
    telegram_chat_id text,
    telegram_message_id text,
    approved_revision integer,
    approved_by text,
    approved_at timestamptz,
    instagram_container_id text,
    instagram_media_id text,
    instagram_permalink text,
    attempts integer not null default 0 check (attempts >= 0),
    error text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    published_at timestamptz,
    unique (account_id, run_date),
    check (approved_revision is null or approved_revision = revision),
    check ((status = 'published') = (published_at is not null)),
    check (status <> 'published' or nullif(btrim(instagram_media_id), '') is not null)
);
create index posts_quote_idx on public.posts(quote_id);
create index posts_published_idx on public.posts(quote_id, published_at desc) where status = 'published';
create unique index posts_media_idx on public.posts(account_id, instagram_media_id) where instagram_media_id is not null;
create unique index posts_one_open_account_idx on public.posts(account_id)
    where status in ('generating','awaiting_approval','regenerating','approved','publishing','publish_uncertain');
create unique index posts_one_reserved_quote_idx on public.posts(quote_id)
    where status in ('generating','awaiting_approval','regenerating','approved','publishing','publish_uncertain');

create table public.promotion_settings (
    id uuid primary key default gen_random_uuid(),
    account_id text not null unique check (btrim(account_id) <> ''),
    active boolean not null default false,
    mode text not null default 'random_book' check (mode in ('fixed_book','random_book')),
    fixed_book_id uuid references public.books(id) on delete restrict,
    reuse_after_days integer check (reuse_after_days > 0),
    discard_cooldown_days integer not null default 7 check (discard_cooldown_days >= 1),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    check (mode <> 'fixed_book' or fixed_book_id is not null)
);
create index promotion_settings_book_idx on public.promotion_settings(fixed_book_id);

create function public.bookpromo_set_updated_at() returns trigger
language plpgsql security invoker set search_path = '' as $$
begin
    new.updated_at = now();
    return new;
end;
$$;
revoke all on function public.bookpromo_set_updated_at() from public, anon, authenticated, service_role;

-- Lock down only this application's objects, not unrelated project objects.
do $$
declare table_name text;
begin
    foreach table_name in array array['books','book_versions','chapters','quotes','import_jobs','posts','promotion_settings'] loop
        execute format('alter table public.%I enable row level security', table_name);
        execute format('revoke all on table public.%I from public, anon, authenticated, service_role', table_name);
        execute format('grant select, insert, update on table public.%I to service_role', table_name);
        execute format('create trigger set_updated_at before update on public.%I for each row execute function public.bookpromo_set_updated_at()', table_name);
    end loop;
end;
$$;
alter table public.bookpromo_schema enable row level security;
revoke all on table public.bookpromo_schema from public, anon, authenticated, service_role;
grant select on table public.bookpromo_schema to service_role;
grant usage on schema public to service_role;

-- No anon/authenticated policies: book content is only accessed by the backend.
-- Invoker views preserve the caller's privileges and underlying RLS.
create view public.quote_overview with (security_invoker = true) as
select q.*, (
    select max(p.published_at) from public.posts p where p.quote_id = q.id and p.status = 'published'
) as last_published_at,
exists (select 1 from public.posts p where p.quote_id = q.id and p.status in (
    'generating','awaiting_approval','regenerating','approved','publishing','publish_uncertain'
)) as reserved
from public.quotes q;

create view public.chapter_overview with (security_invoker = true) as
select c.id, c.book_version_id, c.position, c.name, c.status,
    (select count(*) from public.quotes q where q.chapter_id = c.id) as quote_count,
    (select count(*) from public.quotes q where q.chapter_id = c.id and q.approved and not q.blocked
        and q.spoiler_level in ('none','low')) as usable_quote_count
from public.chapters c;

create view public.book_overview with (security_invoker = true) as
select b.id, b.title, b.author, b.active, b.created_at, v.id as displayed_version_id,
    coalesce(v.status, 'new') as status,
    (select count(*) from public.chapters c where c.book_version_id = v.id) as chapter_count,
    (select count(*) from public.quotes q join public.chapters c on c.id = q.chapter_id
        where c.book_version_id = v.id) as quote_count,
    (select max(p.published_at) from public.posts p
        join public.quotes q on q.id = p.quote_id join public.chapters c on c.id = q.chapter_id
        join public.book_versions bv on bv.id = c.book_version_id
        where bv.book_id = b.id and p.status = 'published') as last_published_at
from public.books b
left join lateral (
    select bv.id, bv.status from public.book_versions bv where bv.book_id = b.id
    order by (bv.id = b.current_version_id) desc nulls last, bv.created_at desc, bv.id
    limit 1
) v on true;

revoke all on public.book_overview, public.chapter_overview, public.quote_overview from public, anon, authenticated, service_role;
grant select on public.book_overview, public.chapter_overview, public.quote_overview to service_role;
insert into public.bookpromo_schema (version) values (1);
notify pgrst, 'reload schema';
commit;

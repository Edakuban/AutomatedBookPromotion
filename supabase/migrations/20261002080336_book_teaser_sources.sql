-- Schema v10: finished whole-book trailers, including landscape and longer MP4s.
-- Prepared locally only. Requires the separate v9 chapter-source migration first.
begin;

do $$
begin
    if (select version from public.bookpromo_schema limit 1) <> 9 then
        raise exception 'Schema v10 requires schema v9';
    end if;
end $$;

alter table public.reel_assets drop constraint reel_assets_source_kind;
alter table public.reel_assets add constraint reel_assets_source_kind
    check(source_kind in ('quote','chapter','book_teaser'));
alter table public.reel_assets drop constraint reel_assets_source_reference;
alter table public.reel_assets add constraint reel_assets_source_reference check(
    (source_kind='quote' and quote_id is not null and chapter_id is null) or
    (source_kind='chapter' and quote_id is null and chapter_id is not null) or
    (source_kind='book_teaser' and quote_id is null and chapter_id is null)
);
alter table public.reel_assets drop constraint reel_assets_duration_ms_check;
alter table public.reel_assets add constraint reel_assets_duration_ms_check check(
    duration_ms between 4000 and
        case when source_kind='book_teaser' then 600000 else 60000 end
);
alter table public.reel_assets drop constraint reel_assets_size_bytes_check;
alter table public.reel_assets add constraint reel_assets_size_bytes_check check(
    size_bytes between 12 and
        case when source_kind='book_teaser' then 314572800 else 52428800 end
);
-- The v8 inline check mentions height AND width, hence its generated table-check name.
alter table public.reel_assets drop constraint reel_assets_check;
alter table public.reel_assets add constraint reel_assets_height_check check(
    height between 360 and 3840 and
    (source_kind='book_teaser' or (height >= 640 and height > width))
);

-- Keep existing service-only invoker RPCs, immutable delivery snapshots and RLS.
-- The v9 enqueue RPC already handles nullable quote/chapter references.
update storage.buckets set file_size_limit=314572800 where id='book-promotion-reels';
update public.bookpromo_schema set version=10 where version=9;
notify pgrst,'reload schema';
commit;

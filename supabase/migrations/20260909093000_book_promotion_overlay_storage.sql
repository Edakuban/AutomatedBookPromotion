-- Title overlays are private; n8n downloads them with its Supabase credential.
-- Writes happen only through the local tool with a Supabase server key.
begin;

insert into storage.buckets (id, name, public)
values ('book-promotion-assets', 'book-promotion-assets', false)
on conflict (id) do update set public = excluded.public;

update public.bookpromo_schema set version = 3;

notify pgrst, 'reload schema';
commit;

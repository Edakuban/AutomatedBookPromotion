-- Schema v2: atomic local snapshot transfer and phased promotion state machine.
begin;
alter table public.books add column profile jsonb not null default '{}'::jsonb;
alter table public.books add column sync_revision integer not null default 0;
alter table public.books add column sync_hash text;
alter table public.book_versions add column extraction_revision integer not null default 0;
alter table public.chapters add column current boolean not null default true;
alter table public.chapters drop constraint chapters_book_version_id_position_key;
create unique index chapters_current_position_idx on public.chapters(book_version_id,position) where current;
alter table public.quotes add column current boolean not null default true;
alter table public.promotion_settings add column telegram_chat_id text;
alter table public.promotion_settings add column telegram_user_id text;
alter table public.promotion_settings add column max_generations integer not null default 5 check (max_generations between 1 and 20);

-- Refuse to reinterpret an existing open draft under a different approval model.
do $$ begin
  if exists(select 1 from public.posts where status not in ('published','discarded','failed')) then
    raise exception 'Open drafts must be resolved before upgrading approval phases';
  end if;
end $$;
alter table public.posts drop constraint posts_status_check;
alter table public.posts alter column status set default 'generating_text';
alter table public.posts add constraint posts_status_check check (status in (
  'generating_text','awaiting_text_approval','generating_image','awaiting_image_approval',
  'approved','publishing','published','discarded','failed','publish_uncertain'));
alter table public.posts add column text_approved_by text;
alter table public.posts add column text_approved_at timestamptz;
alter table public.posts add column text_revision integer not null default 1;
alter table public.posts add column text_approved_revision integer;
alter table public.posts add column action_token uuid not null default gen_random_uuid();
drop index public.posts_one_open_account_idx;
drop index public.posts_one_reserved_quote_idx;
create unique index posts_one_open_account_idx on public.posts(account_id)
  where status not in ('published','discarded','failed');
create unique index posts_one_reserved_quote_idx on public.posts(quote_id)
  where status not in ('published','discarded','failed');

create or replace view public.quote_overview with (security_invoker=true) as
select q.id,q.chapter_id,q.text,q.source_start,q.source_end,q.context,q.scores,q.selection_reason,
 q.spoiler_level,q.approved,q.blocked,q.created_at,q.updated_at,
 (select max(p.published_at) from public.posts p where p.quote_id=q.id and p.status='published') as last_published_at,
 exists(select 1 from public.posts p where p.quote_id=q.id and p.status not in ('published','discarded','failed')) as reserved,
 q.current
from public.quotes q;
create or replace view public.chapter_overview with (security_invoker=true) as
select c.id,c.book_version_id,c.position,c.name,c.status,
 (select count(*) from public.quotes q where q.chapter_id=c.id and q.current) as quote_count,
 (select count(*) from public.quotes q where q.chapter_id=c.id and q.current and q.approved and not q.blocked and q.spoiler_level in ('none','low')) as usable_quote_count
from public.chapters c where c.current;
create or replace view public.book_overview with (security_invoker=true) as
select b.id,b.title,b.author,b.active,b.created_at,v.id as displayed_version_id,coalesce(v.status,'new') as status,
 (select count(*) from public.chapters c where c.book_version_id=v.id and c.current) as chapter_count,
 (select count(*) from public.quotes q join public.chapters c on c.id=q.chapter_id where c.book_version_id=v.id and c.current and q.current) as quote_count,
 (select max(p.published_at) from public.posts p join public.quotes q on q.id=p.quote_id
  join public.chapters c on c.id=q.chapter_id join public.book_versions bv on bv.id=c.book_version_id
  where bv.book_id=b.id and p.status='published') as last_published_at
from public.books b left join lateral (select bv.id,bv.status from public.book_versions bv where bv.book_id=b.id
 order by (bv.id=b.current_version_id) desc nulls last,bv.created_at desc,bv.id limit 1) v on true;

create function public.bookpromo_sync(p jsonb, expected_revision integer) returns jsonb
language plpgsql security invoker set search_path='' as $$
declare b uuid := (p#>>'{book,id}')::uuid; v uuid := (p#>>'{version,id}')::uuid;
 existing public.books; c jsonb; q jsonb; source text; rev integer;
begin
 if jsonb_typeof(p->'chapters') <> 'array' or jsonb_typeof(p->'quotes') <> 'array'
    or nullif(p->>'hash','') is null then raise exception 'Invalid snapshot'; end if;
 perform pg_advisory_xact_lock(41020260908);
 perform pg_advisory_xact_lock(hashtextextended(b::text,0));
 select * into existing from public.books where id=b for update;
 if found and existing.sync_hash=p->>'hash' then return jsonb_build_object('revision',existing.sync_revision,'hash',existing.sync_hash); end if;
 if coalesce(existing.sync_revision,0) <> expected_revision then raise exception 'Snapshot conflict' using errcode='40001'; end if;
 if exists(select 1 from public.posts x join public.quotes z on z.id=x.quote_id join public.chapters k on k.id=z.chapter_id
   join public.book_versions bv on bv.id=k.book_version_id where bv.book_id=b and x.status not in ('published','discarded','failed')) then
   raise exception 'Book has an open draft' using errcode='55000'; end if;
 if exists(select 1 from public.book_versions where id=v and (book_id<>b or file_sha256<>p#>>'{version,file_sha256}')) then raise exception 'Version mismatch'; end if;
 if exists(select 1 from public.book_versions bv join public.chapters k on k.book_version_id=bv.id join public.quotes z on z.chapter_id=k.id
   join public.posts x on x.quote_id=z.id where bv.id=v and bv.extraction_revision<>(p#>>'{version,extraction_revision}')::integer) then
   raise exception 'Used source version is immutable; import a new document version' using errcode='55000'; end if;
 rev := coalesce(existing.sync_revision,0)+1;
 insert into public.books(id,title,author,active,image_base_prompt,caption_instructions,world_description,character_description,target_url,profile,sync_revision,sync_hash)
 values(b,p#>>'{book,title}',coalesce(p#>>'{book,author}',''),coalesce((p#>>'{book,promotion_enabled}')::boolean,false),
 coalesce(p#>>'{book,image_prompt_base}',''),coalesce(p#>>'{book,caption_guidelines}',''),coalesce(p#>>'{book,world}',''),
 coalesce(p#>>'{book,characters}',''),coalesce(p#>>'{book,target_url}',''),p->'book',rev,p->>'hash')
 on conflict(id) do update set title=excluded.title,author=excluded.author,active=excluded.active,image_base_prompt=excluded.image_base_prompt,
 caption_instructions=excluded.caption_instructions,world_description=excluded.world_description,character_description=excluded.character_description,
 target_url=excluded.target_url,profile=excluded.profile,sync_revision=excluded.sync_revision,sync_hash=excluded.sync_hash;
 insert into public.book_versions(id,book_id,filename,file_sha256,status,summary,analysis_version,extraction_revision)
 values(v,b,p#>>'{version,filename}',p#>>'{version,file_sha256}','ready',coalesce(p#>>'{version,summary}',''),p#>>'{version,analysis_version}',(p#>>'{version,extraction_revision}')::integer)
 on conflict(id) do update set status='ready',summary=excluded.summary,analysis_version=excluded.analysis_version,extraction_revision=excluded.extraction_revision;
 update public.chapters set current=false where book_version_id=v;
 update public.quotes set current=false where chapter_id in(select id from public.chapters where book_version_id=v);
 for c in select value from jsonb_array_elements(p->'chapters') loop
   if exists(select 1 from public.chapters where id=(c->>'id')::uuid and book_version_id<>v) then raise exception 'Chapter mismatch'; end if;
   if exists(select 1 from public.chapters k join public.quotes z on z.chapter_id=k.id join public.posts x on x.quote_id=z.id
     where k.id=(c->>'id')::uuid and k.source_text is distinct from c->>'source_text') then raise exception 'Used chapter text is immutable'; end if;
   insert into public.chapters(id,book_version_id,position,name,source_text,paragraphs,summary,status,current)
   values((c->>'id')::uuid,v,(c->>'position')::integer,c->>'name',c->>'source_text',c->'paragraphs',coalesce(c->>'summary',''),'ready',true)
   on conflict(id) do update set position=excluded.position,name=excluded.name,source_text=excluded.source_text,paragraphs=excluded.paragraphs,summary=excluded.summary,status='ready',current=true;
 end loop;
 for q in select value from jsonb_array_elements(p->'quotes') loop
   select source_text into source from public.chapters where id=(q->>'chapter_id')::uuid and book_version_id=v and current;
   if not found or (q->>'source_start')::integer<0 or (q->>'source_end')::integer>length(source)
    or substring(source from (q->>'source_start')::integer+1 for (q->>'source_end')::integer-(q->>'source_start')::integer) is distinct from q->>'text'
    then raise exception 'Quote is not verbatim'; end if;
   if exists(select 1 from public.quotes where id=(q->>'id')::uuid and (chapter_id<>(q->>'chapter_id')::uuid or text<>q->>'text' or source_start<>(q->>'source_start')::integer or source_end<>(q->>'source_end')::integer)) then raise exception 'Quote identity mismatch'; end if;
   insert into public.quotes(id,chapter_id,text,source_start,source_end,context,scores,selection_reason,spoiler_level,approved,blocked,current)
   values((q->>'id')::uuid,(q->>'chapter_id')::uuid,q->>'text',(q->>'source_start')::integer,(q->>'source_end')::integer,
    coalesce(q->>'context',''),q->'scores',q->>'reason',q->>'spoiler',(q->>'approved')::boolean,(q->>'blocked')::boolean,true)
   on conflict(id) do update set context=excluded.context,scores=excluded.scores,selection_reason=excluded.selection_reason,
    spoiler_level=excluded.spoiler_level,approved=excluded.approved,blocked=excluded.blocked,current=true;
 end loop;
 update public.books set current_version_id=v where id=b;
 return jsonb_build_object('revision',rev,'hash',p->>'hash');
end $$;

create function public.bookpromo_reserve(p_account text, p_day date) returns jsonb
language plpgsql security invoker set search_path='' as $$
declare cfg public.promotion_settings; draft public.posts; selected_book uuid; picked public.quotes;
begin
 select * into cfg from public.promotion_settings where account_id=p_account for update;
 if not found or not cfg.active then return jsonb_build_object('outcome','inactive'); end if;
 if nullif(cfg.telegram_chat_id,'') is null or nullif(cfg.telegram_user_id,'') is null then raise exception 'Telegram approval identity missing'; end if;
 select * into draft from public.posts where account_id=p_account and (run_date=p_day or status not in ('published','discarded','failed')) order by created_at limit 1;
 if found then return jsonb_build_object('outcome','existing','post',to_jsonb(draft)); end if;
 -- Serialize reservation across accounts too; selection is a short local transaction.
 perform pg_advisory_xact_lock(41020260908);
 select b.id into selected_book from public.books b where b.active and (cfg.mode='random_book' or b.id=cfg.fixed_book_id)
 and exists(select 1 from public.quotes q join public.chapters c on c.id=q.chapter_id
  where c.book_version_id=b.current_version_id and c.current and q.current and q.approved and not q.blocked and q.spoiler_level in ('none','low')
  and not exists(select 1 from public.posts p where p.quote_id=q.id and
   (p.status not in ('published','discarded','failed') or (p.status='published' and (cfg.reuse_after_days is null or p.published_at>now()-make_interval(days=>cfg.reuse_after_days)))
     or (p.status='discarded' and p.updated_at>now()-make_interval(days=>cfg.discard_cooldown_days)))))
 order by random() limit 1;
 if selected_book is null then return jsonb_build_object('outcome','no_quote'); end if;
 select q.* into picked from public.quotes q join public.chapters c on c.id=q.chapter_id join public.books b on b.current_version_id=c.book_version_id
 where b.id=selected_book and c.current and q.current and q.approved and not q.blocked and q.spoiler_level in ('none','low')
 and not exists(select 1 from public.posts p where p.quote_id=q.id and
   (p.status not in ('published','discarded','failed') or (p.status='published' and (cfg.reuse_after_days is null or p.published_at>now()-make_interval(days=>cfg.reuse_after_days)))
   or (p.status='discarded' and p.updated_at>now()-make_interval(days=>cfg.discard_cooldown_days))))
 order by exists(select 1 from public.posts p where p.quote_id=q.id and p.status='published'),random() limit 1;
 insert into public.posts(quote_id,account_id,run_date,quote_text,book_profile,telegram_chat_id)
 select picked.id,p_account,p_day,picked.text,b.profile,cfg.telegram_chat_id from public.books b where b.id=selected_book returning * into draft;
 return jsonb_build_object('outcome','created','post',to_jsonb(draft));
end $$;

-- The worker supplies the exact revision and action token it received. Every action
-- rotates both, making late HTTP responses and duplicate callbacks harmless.
create function public.bookpromo_transition(p_id uuid,p_revision integer,p_token uuid,p_action text,p_data jsonb default '{}'::jsonb) returns jsonb
language plpgsql security invoker set search_path='' as $$
declare d public.posts; cfg public.promotion_settings;
begin
 select * into d from public.posts where id=p_id for update;
 if not found then raise exception 'Draft missing'; end if;
 if d.revision<>p_revision or d.action_token<>p_token then return jsonb_build_object('outcome','stale'); end if;
 select * into cfg from public.promotion_settings where account_id=d.account_id;
 if p_action in ('approve_text','approve_image','retry_text','retry_image','discard') then
   if p_data->>'chat_id' is distinct from cfg.telegram_chat_id or p_data->>'user_id' is distinct from cfg.telegram_user_id then
    raise exception 'Approval identity mismatch' using errcode='42501'; end if;
 end if;
 if p_action='text_ready' and d.status='generating_text' then
   if length(coalesce(p_data->>'caption','')) not between length(d.quote_text) and 2200 or position(d.quote_text in (p_data->>'caption'))=0
     or nullif(btrim(p_data->>'image_prompt'),'') is null then raise exception 'Invalid text'; end if;
   d.caption:=p_data->>'caption'; d.image_prompt:=p_data->>'image_prompt'; d.status:='awaiting_text_approval';
 elsif p_action='approve_text' and d.status='awaiting_text_approval' then
   d.text_approved_by:=p_data->>'user_id'; d.text_approved_at:=now(); d.text_approved_revision:=d.text_revision; d.status:='generating_image';
 elsif p_action='image_ready' and d.status='generating_image' and d.text_approved_revision=d.text_revision then
   if coalesce(p_data->>'image_url','') !~ '^https://' then raise exception 'Invalid image URL'; end if;
   d.image_path:=p_data->>'image_url'; d.status:='awaiting_image_approval';
 elsif p_action='approve_image' and d.status='awaiting_image_approval' and d.text_approved_revision=d.text_revision then
   d.approved_by:=p_data->>'user_id'; d.approved_at:=now(); d.status:='approved';
 elsif p_action='retry_text' and d.status in ('awaiting_text_approval','awaiting_image_approval') and d.attempts<cfg.max_generations then
   d.text_revision:=d.text_revision+1; d.text_approved_revision:=null; d.text_approved_by:=null; d.text_approved_at:=null;
   d.caption:=null; d.image_prompt:=null; d.image_path:=null; d.status:='generating_text'; d.attempts:=d.attempts+1;
 elsif p_action='retry_image' and d.status='awaiting_image_approval' and d.attempts<cfg.max_generations then
   d.image_path:=null; d.status:='generating_image'; d.attempts:=d.attempts+1;
 elsif p_action='discard' and d.status in ('awaiting_text_approval','awaiting_image_approval') then d.status:='discarded';
 elsif p_action='begin_publish' and d.status='approved' and d.text_approved_revision=d.text_revision then d.status:='publishing';
 elsif p_action='container_ready' and d.status='publishing' and d.instagram_container_id is null then
   if nullif(p_data->>'container_id','') is null then raise exception 'Missing container'; end if;
   d.instagram_container_id:=p_data->>'container_id';
 elsif p_action='published' and d.status in ('publishing','publish_uncertain') and d.instagram_container_id is not null then
   if nullif(p_data->>'media_id','') is null then raise exception 'Missing media'; end if;
   d.instagram_media_id:=p_data->>'media_id'; d.instagram_permalink:=p_data->>'permalink'; d.published_at:=now(); d.status:='published';
 elsif p_action='publish_uncertain' and d.status='publishing' then d.status:='publish_uncertain';
 elsif p_action='fail' and d.status in ('generating_text','generating_image','approved') then d.status:='failed';
 else return jsonb_build_object('outcome','invalid_state'); end if;
 d.revision:=d.revision+1; d.action_token:=gen_random_uuid();
 if d.status in ('approved','publishing','published','publish_uncertain') then d.approved_revision:=d.revision; else d.approved_revision:=null; end if;
 update public.posts set status=d.status,revision=d.revision,action_token=d.action_token,caption=d.caption,image_prompt=d.image_prompt,image_path=d.image_path,
 text_revision=d.text_revision,text_approved_revision=d.text_approved_revision,text_approved_by=d.text_approved_by,text_approved_at=d.text_approved_at,
 approved_revision=d.approved_revision,approved_by=d.approved_by,approved_at=d.approved_at,instagram_container_id=d.instagram_container_id,
 instagram_media_id=d.instagram_media_id,instagram_permalink=d.instagram_permalink,published_at=d.published_at,attempts=d.attempts where id=d.id;
 return jsonb_build_object('outcome','updated','post',to_jsonb(d));
end $$;

revoke all on function public.bookpromo_sync(jsonb,integer),public.bookpromo_reserve(text,date),public.bookpromo_transition(uuid,integer,uuid,text,jsonb) from public,anon,authenticated;
grant execute on function public.bookpromo_sync(jsonb,integer),public.bookpromo_reserve(text,date),public.bookpromo_transition(uuid,integer,uuid,text,jsonb) to service_role;
update public.bookpromo_schema set version=2;
notify pgrst,'reload schema';
commit;

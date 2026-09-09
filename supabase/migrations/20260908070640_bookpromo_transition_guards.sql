-- Reject NULL concurrency tokens and missing approval identities.
begin;
create or replace function public.bookpromo_sync(p jsonb, expected_revision integer) returns jsonb
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
 if coalesce(existing.sync_revision,0) is distinct from expected_revision then raise exception 'Snapshot conflict' using errcode='40001'; end if;
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

create or replace function public.bookpromo_transition(p_id uuid,p_revision integer,p_token uuid,p_action text,p_data jsonb default '{}'::jsonb) returns jsonb
language plpgsql security invoker set search_path='' as $$
declare d public.posts; cfg public.promotion_settings;
begin
 select * into d from public.posts where id=p_id for update;
 if not found then raise exception 'Draft missing'; end if;
 if d.revision is distinct from p_revision or d.action_token is distinct from p_token then return jsonb_build_object('outcome','stale'); end if;
 select * into cfg from public.promotion_settings where account_id=d.account_id;
 if p_action in ('approve_text','approve_image','retry_text','retry_image','discard') then
   if nullif(cfg.telegram_chat_id,'') is null or nullif(cfg.telegram_user_id,'') is null or p_data->>'chat_id' is distinct from cfg.telegram_chat_id or p_data->>'user_id' is distinct from cfg.telegram_user_id then
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
notify pgrst,'reload schema';
commit;

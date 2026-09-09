-- An existing post keeps quote_text and book_profile as an immutable snapshot.
-- Updating the current book catalogue must therefore not block its draft or
-- mutate the draft itself.
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

notify pgrst, 'reload schema';
commit;

-- Persist the selected chapter context with every draft so n8n can render a
-- deterministic chapter label without asking a model for it.
begin;

create or replace function public.bookpromo_reserve(p_account text, p_day date) returns jsonb
language plpgsql security invoker set search_path='' as $$
declare cfg public.promotion_settings; draft public.posts; selected_book uuid; picked public.quotes;
begin
 select * into cfg from public.promotion_settings where account_id=p_account for update;
 if not found or not cfg.active then return jsonb_build_object('outcome','inactive'); end if;
 if nullif(cfg.telegram_chat_id,'') is null or nullif(cfg.telegram_user_id,'') is null then raise exception 'Telegram approval identity missing'; end if;
 select * into draft from public.posts where account_id=p_account and status not in ('published','discarded','failed') order by created_at limit 1;
 if found then return jsonb_build_object('outcome','existing','post',to_jsonb(draft)); end if;
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
 select picked.id,p_account,p_day,picked.text,
   b.profile || jsonb_build_object('chapter_position',c.position,'chapter_name',c.name),cfg.telegram_chat_id
 from public.books b join public.chapters c on c.id=picked.chapter_id where b.id=selected_book returning * into draft;
 return jsonb_build_object('outcome','created','post',to_jsonb(draft));
end $$;

update public.bookpromo_schema set version = 4;
notify pgrst, 'reload schema';
commit;

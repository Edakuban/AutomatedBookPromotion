// Isolated PostgreSQL contract test. No ENV or external services.
import { readFile, readdir } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
import assert from 'node:assert/strict';

const { PGlite } = await import(pathToFileURL(process.argv[2]).href);
const db = new PGlite();
let checks = 0;
const check = (value) => { assert.ok(value); checks += 1; };
const query = async (sql, params = []) => (await db.query(sql, params)).rows;
const scalar = async (sql, params = []) => Object.values((await query(sql, params))[0])[0];
const rejects = async (fn) => {
  let failed = false;
  try { await fn(); } catch { failed = true; }
  check(failed);
};

try {
  await db.exec('create role anon; create role authenticated; create role service_role bypassrls; grant usage on schema public to service_role;');
  await db.exec("create schema storage; create table storage.buckets(id text primary key,name text not null,public boolean not null default false,file_size_limit bigint,allowed_mime_types text[]);");
  await db.exec(await readFile('sql/001_initial_schema.sql', 'utf8'));

  const migrationFiles = (await readdir('supabase/migrations')).sort().filter((file) => !file.endsWith('_initial.sql'));
  const carouselMigration = migrationFiles.find((file) => file.endsWith('_carousel_contract_and_media_storage.sql'));
  check(carouselMigration);
  for (const file of migrationFiles.filter((item) => item !== carouselMigration)) {
    await db.exec(await readFile(`supabase/migrations/${file}`, 'utf8'));
  }

  // Verify the guarded migration removes only the old test publication data.
  const cleanupIds = Array.from({ length: 5 }, () => crypto.randomUUID());
  await db.query("insert into public.books(id,title) values ($1,'Cleanup test')", [cleanupIds[0]]);
  await db.query("insert into public.book_versions(id,book_id,filename,file_sha256,status) values ($1,$2,'cleanup.docx',$3,'ready')", [cleanupIds[1], cleanupIds[0], 'c'.repeat(64)]);
  await db.query('update public.books set current_version_id=$1 where id=$2', [cleanupIds[1], cleanupIds[0]]);
  await db.query("insert into public.chapters(id,book_version_id,position,name,status) values ($1,$2,1,'Cleanup','ready')", [cleanupIds[2], cleanupIds[1]]);
  await db.query("insert into public.quotes(id,chapter_id,text,source_start,source_end,spoiler_level) values ($1,$2,'Test',0,4,'none')", [cleanupIds[3], cleanupIds[2]]);
  await db.query("insert into public.posts(id,quote_id,account_id,run_date,status,quote_text) values ($1,$2,'cleanup','2026-09-09','failed','Test')", [cleanupIds[4], cleanupIds[3]]);
  check(await scalar('select count(*)=1 from public.posts'));
  await db.exec(await readFile(`supabase/migrations/${carouselMigration}`, 'utf8'));
  check(await scalar('select count(*)=0 from public.posts'));
  check(await scalar('select count(*)=1 from public.books where id=$1', [cleanupIds[0]]));
  check(await scalar('select count(*)=1 from public.quotes where id=$1', [cleanupIds[3]]));

  check(await scalar('select version from public.bookpromo_schema') === 5);
  check(await scalar("select not public and file_size_limit=8388608 and allowed_mime_types=array['image/jpeg']::text[] from storage.buckets where id='book-promotion-media'"));
  check(await scalar("select not public and file_size_limit=8388608 and allowed_mime_types=array['image/png','image/jpeg']::text[] from storage.buckets where id='book-promotion-assets'"));
  check(await scalar("select relrowsecurity from pg_class where oid='public.post_media'::regclass"));
  check(!await scalar("select has_table_privilege('anon','public.post_media','select')"));
  check(await scalar("select has_table_privilege('service_role','public.post_media','select,insert,update,delete')"));
  check(!await scalar("select exists(select 1 from information_schema.columns where table_schema='public' and table_name='posts' and column_name='image_path')"));
  check(await scalar(`select count(*)=5 and bool_and(not p.prosecdef)
    and bool_and(not has_function_privilege('anon',p.oid,'execute'))
    and bool_and(not has_function_privilege('authenticated',p.oid,'execute'))
    and bool_and(has_function_privilege('service_role',p.oid,'execute'))
    from pg_proc p join pg_namespace n on n.oid=p.pronamespace
    where n.nspname='public' and p.proname in (
      'bookpromo_sync','bookpromo_reserve','bookpromo_transition',
      'bookpromo_media_container','bookpromo_media_cleanup'
    )`));

  await db.exec('set role service_role');
  const [book, version, chapter, quote] = Array.from({ length: 4 }, () => crypto.randomUUID());
  const sourceText = 'Der Motor läuft ruhig durch die dunkle Nacht.';
  const assetDigest = 'a'.repeat(64);
  const bookFields = {
    id: book,
    title: 'Testbuch',
    promotion_enabled: true,
    publication_mode: 'review',
    overlay_path: `${book}/${assetDigest}.png`,
    carousel_end_slide_path: `${book}/carousel/${assetDigest}.jpg`,
    carousel_end_text: 'Jetzt entdecken.',
  };
  const quoteFields = {
    id: quote,
    chapter_id: chapter,
    text: sourceText,
    source_start: 0,
    source_end: sourceText.length,
    scores: { clarity: 5 },
    reason: 'Test',
    spoiler: 'none',
    approved: true,
    blocked: false,
  };
  const payload = {
    hash: 'initial',
    book: bookFields,
    version: {
      id: version,
      filename: 'test.docx',
      file_sha256: 'd'.repeat(64),
      extraction_revision: 1,
      analysis_version: 'test',
    },
    chapters: [{ id: chapter, position: 1, name: 'Kapitel', source_text: sourceText, paragraphs: [] }],
    quotes: [quoteFields],
  };
  const sync = async (body, revision) => scalar('select public.bookpromo_sync($1::jsonb,$2)', [JSON.stringify(body), revision]);
  const reserve = async (mode, account = 'test') => scalar("select public.bookpromo_reserve($1,date '2026-09-10',$2)", [account, mode]);
  const transition = async (post, action, data = {}) => scalar(
    'select public.bookpromo_transition($1,$2,$3,$4,$5::jsonb)',
    [post.id, post.revision, post.action_token, action, JSON.stringify(data)],
  );
  const childContainer = async (post, position, containerId) => scalar(
    'select public.bookpromo_media_container($1,$2,$3,$4::smallint,$5)',
    [post.id, post.revision, post.action_token, position, containerId],
  );
  const cleanup = async (post, paths, error = null) => scalar(
    'select public.bookpromo_media_cleanup($1,$2,$3,$4::jsonb,$5)',
    [post.id, post.revision, post.action_token, JSON.stringify(paths), error],
  );
  const manifest = (post, marker = 'e') => [
    { position: 0, kind: 'hero', alt_text: 'Stimmungsbild zum Buch', sha256: marker.repeat(64), storage_path: `${post.id}/${post.revision}/00-${marker.repeat(64)}.jpg` },
    { position: 1, kind: 'quote', text_fragment: sourceText, alt_text: 'Buchzitat', sha256: marker.repeat(64), storage_path: `${post.id}/${post.revision}/01-${marker.repeat(64)}.jpg` },
    { position: 2, kind: 'cta', alt_text: 'Buchcover und Hinweis', sha256: marker.repeat(64), storage_path: `${post.id}/${post.revision}/02-${marker.repeat(64)}.jpg` },
  ];

  check((await sync(payload, 0)).revision === 1);
  check((await sync(payload, 0)).revision === 1);
  await rejects(() => sync({ ...payload, hash: 'other' }, 0));
  await rejects(() => sync({
    ...payload,
    hash: 'bad-asset-path',
    book: { ...bookFields, overlay_path: '../outside.png' },
  }, 1));
  await rejects(() => reserve('invalid'));
  await rejects(() => scalar("select public.bookpromo_reserve('test',date '2026-09-10')"));
  await db.query("insert into public.promotion_settings(account_id,active,telegram_chat_id,telegram_user_id) values ('test',true,'42','7')");

  let result = await reserve('review');
  check(result.outcome === 'created');
  let draft = result.post;
  check(draft.execution_mode === 'review');
  check(draft.book_profile.chapter_position === 1 && draft.book_profile.chapter_name === 'Kapitel');
  check((await reserve('auto')).outcome === 'blocked_by_other_mode');
  check((await scalar('select public.bookpromo_transition($1,null,null,$2)', [draft.id, 'fail'])).outcome === 'stale');

  check((await sync({ ...payload, hash: 'open', book: { ...bookFields, title: 'Open-safe update' } }, 1)).revision === 2);
  check((await scalar("select book_profile->>'title' from public.posts where id=$1", [draft.id])) === 'Testbuch');
  await rejects(() => transition(draft, 'text_ready', { caption: 'Invented', image_prompt: 'Nacht' }));
  draft = (await transition(draft, 'text_ready', { caption: `${sourceText}\nEin Roman.`, image_prompt: 'Nacht' })).post;
  check(draft.status === 'awaiting_text_approval');
  await rejects(() => transition(draft, 'approve_text', { chat_id: '42', user_id: 'wrong' }));
  draft = (await transition(draft, 'approve_text', { chat_id: '42', user_id: '7' })).post;
  check(draft.status === 'generating_image' && draft.text_approved_by === '7');

  await rejects(() => transition(draft, 'media_ready', { media: manifest(draft).slice(0, 2) }));
  await rejects(() => transition(draft, 'media_ready', { media: Array.from({ length: 11 }, (_, position) => ({ position })) }));
  const wrongOrder = manifest(draft);
  wrongOrder[1] = { ...wrongOrder[1], position: 2 };
  await rejects(() => transition(draft, 'media_ready', { media: wrongOrder }));
  const firstManifest = manifest(draft, 'e');
  draft = (await transition(draft, 'media_ready', { media: firstManifest })).post;
  check(draft.status === 'awaiting_image_approval');
  check(await scalar("select count(*)=3 and bool_and(status='uploaded') from public.post_media where post_id=$1", [draft.id]));

  draft = (await transition(draft, 'retry_image', { chat_id: '42', user_id: '7' })).post;
  check(draft.status === 'generating_image');
  check(await scalar("select bool_and(status='cleanup_pending') from public.post_media where post_id=$1", [draft.id]));
  await rejects(() => transition(draft, 'media_ready', { media: manifest(draft, 'f') }));
  check((await cleanup(draft, firstManifest.map((item) => item.storage_path))).outcome === 'complete');
  check(await scalar("select bool_and(status='deleted') from public.post_media where post_id=$1", [draft.id]));

  const secondManifest = manifest(draft, 'f');
  draft = (await transition(draft, 'media_ready', { media: secondManifest })).post;
  draft = (await transition(draft, 'approve_image', { chat_id: '42', user_id: '7' })).post;
  check(draft.status === 'approved' && draft.approved_revision === draft.revision);
  draft = (await transition(draft, 'begin_publish')).post;
  check(draft.status === 'publishing');
  await rejects(() => transition(draft, 'carousel_container_ready', { container_id: 'parent-1' }));
  check((await childContainer(draft, 0, 'child-0')).outcome === 'updated');
  check((await childContainer(draft, 0, 'child-0')).outcome === 'existing');
  await rejects(() => childContainer(draft, 0, 'different-child'));
  await rejects(() => childContainer(draft, 1, 'child-0'));
  check((await childContainer(draft, 1, 'child-1')).outcome === 'updated');
  check((await childContainer(draft, 2, 'child-2')).outcome === 'updated');
  draft = (await transition(draft, 'carousel_container_ready', { container_id: 'parent-1' })).post;
  check(draft.instagram_container_id === 'parent-1');
  draft = (await transition(draft, 'publish_uncertain', { error: 'Timeout after publish request' })).post;
  check(draft.status === 'publish_uncertain' && draft.instagram_container_id === 'parent-1');
  check(await scalar("select count(*)=3 and count(instagram_container_id)=3 from public.post_media where post_id=$1", [draft.id]));
  check(await scalar("select bool_and(status='container_ready') from public.post_media where post_id=$1", [draft.id]));
  draft = (await transition(draft, 'published', { media_id: 'media-1', permalink: 'https://instagram.example/p/1' })).post;
  check(draft.status === 'published');
  check(await scalar("select bool_and(status='cleanup_pending') from public.post_media where post_id=$1", [draft.id]));
  check((await cleanup(draft, [secondManifest[0].storage_path], 'Two objects remain')).outcome === 'pending');
  check((await cleanup(draft, secondManifest.slice(1).map((item) => item.storage_path))).outcome === 'complete');
  check((await cleanup(draft, secondManifest.map((item) => item.storage_path))).outcome === 'complete');
  check(await scalar("select bool_and(status='deleted') from public.post_media where post_id=$1", [draft.id]));
  check(await scalar('select not reserved and last_published_at is not null from public.quote_overview where id=$1', [quote]));

  // Switch the book to auto mode and verify automatic approvals use a distinct identity.
  const quote2 = crypto.randomUUID();
  const quote2Fields = { ...quoteFields, id: quote2, text: 'Motor', source_start: 4, source_end: 9 };
  const autoPayload = {
    ...payload,
    hash: 'auto-settings',
    book: { ...bookFields, title: 'Auto book', publication_mode: 'auto' },
    quotes: [quoteFields, quote2Fields],
  };
  check((await sync(autoPayload, 2)).revision === 3);
  result = await reserve('auto');
  check(result.outcome === 'created');
  draft = result.post;
  check((await reserve('review')).outcome === 'blocked_by_other_mode');
  draft = (await transition(draft, 'text_ready', { caption: 'Motor\nEin Roman.', image_prompt: 'Maschine' })).post;
  check(draft.status === 'generating_image' && draft.text_approved_by === 'system:auto');
  const autoManifest = manifest(draft, '9');
  autoManifest[1].text_fragment = 'Motor';
  draft = (await transition(draft, 'media_ready', { media: autoManifest })).post;
  check(draft.status === 'approved' && draft.approved_by === 'system:auto');
  check((await transition(draft, 'approve_image', { chat_id: '42', user_id: '7' })).outcome === 'invalid_state');
  draft = (await transition(draft, 'fail', { error: 'Controlled test failure' })).post;
  check(draft.status === 'failed');
  check((await cleanup(draft, autoManifest.map((item) => item.storage_path))).outcome === 'complete');

  await db.exec('reset role; set role anon');
  await rejects(() => sync(payload, 3));
  await rejects(() => reserve('review'));
  await rejects(() => query('select * from public.books'));
  await rejects(() => query('select * from public.post_media'));
  await rejects(() => childContainer(draft, 0, 'forbidden'));
  console.log(`Integration v5: ${checks} PostgreSQL-Prüfungen erfolgreich.`);
} finally {
  await db.close();
}

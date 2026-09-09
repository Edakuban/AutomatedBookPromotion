// Optional isolated PostgreSQL verification using PGlite 0.5.8.
// Run: node tools/check_schema.mjs <path-to-pglite-dist-index.js>
// Never reads .env or connects to Supabase. All test data stays in memory.
import { readFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
import assert from 'node:assert/strict';

if (!process.argv[2]) throw new Error('Pass the path to PGlite dist/index.js. See sql/README.md.');
const { PGlite } = await import(pathToFileURL(process.argv[2]).href);
const db = new PGlite();
let checks = 0;
const check = (condition) => { assert.ok(condition); checks++; };
const scalar = async (sql) => Object.values((await db.query(sql)).rows[0])[0];
async function rejected(sql) {
  let failed = false;
  try { await db.exec(sql); } catch { failed = true; }
  check(failed);
}
try {
  await db.exec(`create role anon; create role authenticated; create role service_role bypassrls;
    grant usage on schema public to anon, authenticated, service_role;
    alter default privileges in schema public grant all on tables to anon, authenticated, service_role;
    alter default privileges in schema public grant execute on functions to anon, authenticated, service_role;`);
  const sql = await readFile(new URL('../sql/001_initial_schema.sql', import.meta.url), 'utf8');
  await db.exec(sql);
  check(await scalar('select version from public.bookpromo_schema') === 1);
  const tables = ['bookpromo_schema','books','book_versions','chapters','quotes','import_jobs','posts','promotion_settings'];
  for (const table of tables) {
    check(await scalar(`select relrowsecurity from pg_class where oid='public.${table}'::regclass`));
    for (const role of ['anon','authenticated']) {
      check(!await scalar(`select has_table_privilege('${role}','public.${table}','select')`));
    }
  }
  for (const view of ['book_overview','chapter_overview','quote_overview']) {
    check(await scalar(`select 'security_invoker=true'=any(reloptions) from pg_class where oid='public.${view}'::regclass`));
    check(!await scalar(`select has_table_privilege('anon','public.${view}','select')`));
  }
  await db.exec("set role anon");
  await rejected('select * from public.books');
  await rejected('select * from public.quote_overview');
  await db.exec('reset role; set role service_role');
  const bookId = await scalar("insert into public.books(title) values ('Testbuch') returning id");
  const otherBook = await scalar("insert into public.books(title) values ('Anderes Buch') returning id");
  const version = await scalar(`insert into public.book_versions(book_id,filename,file_sha256) values ('${bookId}','test.docx',repeat('a',64)) returning id`);
  await rejected(`insert into public.book_versions(book_id,filename,file_sha256) values ('${bookId}','same.docx',repeat('a',64))`);
  await rejected(`update public.books set current_version_id='${version}' where id='${otherBook}'`);
  await db.exec(`update public.books set current_version_id='${version}' where id='${bookId}'`);
  const chapter = await scalar(`insert into public.chapters(book_version_id,position,name,source_text) values ('${version}',1,'Kapitel 1','Der Motor läuft.') returning id`);
  await rejected(`insert into public.chapters(book_version_id,position,name) values ('${version}',1,'Doppelt')`);
  const quote = await scalar(`insert into public.quotes(chapter_id,text,source_start,source_end,approved,spoiler_level) values ('${chapter}','Der Motor läuft.',0,16,true,'none') returning id`);
  await rejected(`insert into public.quotes(chapter_id,text,source_start,source_end) values ('${chapter}','Doppelt',0,16)`);
  const post = await scalar(`insert into public.posts(quote_id,account_id,run_date,quote_text) values ('${quote}','test-account','2026-09-07','Der Motor läuft.') returning id`);
  check(await scalar(`select reserved from public.quote_overview where id='${quote}'`));
  check(await scalar(`select last_published_at is null from public.quote_overview where id='${quote}'`));
  await rejected(`insert into public.posts(quote_id,account_id,run_date,quote_text) values ('${quote}','test-account','2026-09-08','Test')`);
  await rejected(`insert into public.posts(quote_id,account_id,run_date,quote_text) values ('${quote}','other-account','2026-09-08','Test')`);
  await rejected(`update public.posts set status='published' where id='${post}'`);
  await db.exec(`update public.posts set status='published', published_at='2026-09-07T04:00:00Z',instagram_media_id='test-media' where id='${post}'`);
  check(await scalar(`select not reserved and last_published_at is not null from public.quote_overview where id='${quote}'`));
  check(await scalar(`select chapter_count=1 and quote_count=1 and last_published_at is not null from public.book_overview where id='${bookId}'`));
  await rejected(`insert into public.posts(quote_id,account_id,run_date,quote_text,status) values ('${quote}','test-account','2026-09-07','Test','discarded')`);
  await rejected(`insert into public.promotion_settings(account_id,mode) values ('test-account','fixed_book')`);
  await rejected(`delete from public.books where id='${bookId}'`);
  // Even accidental future SELECT grants cannot expose rows through an invoker view.
  await db.exec('reset role; grant select on public.books,public.book_versions,public.chapters,public.quotes,public.posts,public.quote_overview to anon; set role anon;');
  check((await db.query('select * from public.quote_overview')).rows.length === 0);
  await db.exec('reset role');
  await rejected(sql); // Fail safely on rerun; existing content must survive rollback.
  await db.exec('rollback');
  check(await scalar(`select title from public.books where id='${bookId}'`) === 'Testbuch');
  console.log(`Schema v1: ${checks} PostgreSQL-Prüfungen erfolgreich (PGlite, nur lokal).`);
} finally {
  await db.close();
}

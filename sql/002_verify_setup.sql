-- Read-only post-installation checks for step 3.2 (run as postgres).
-- Expected current version: 2 (after all supabase/migrations files).
select version, installed_at from public.bookpromo_schema;

-- Expected: 8 rows, RLS true, anon/authenticated false, service_read true.
select c.relname as object, c.relrowsecurity as rls_enabled,
    has_table_privilege('anon', c.oid, 'select') as anon_read,
    has_table_privilege('authenticated', c.oid, 'select') as authenticated_read,
    has_table_privilege('service_role', c.oid, 'select') as service_read
from pg_class c join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'public' and c.relname in (
    'bookpromo_schema','books','book_versions','chapters','quotes','import_jobs','posts','promotion_settings'
)
order by c.relname;

-- Expected: 3 views with security_invoker=true, no public-client grants.
select c.relname, c.reloptions,
    has_table_privilege('anon', c.oid, 'select') as anon_read,
    has_table_privilege('authenticated', c.oid, 'select') as authenticated_read
from pg_class c join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'public' and c.relname in ('book_overview','chapter_overview','quote_overview');

-- Expected: false, false. The helper must not be a public RPC.
select has_function_privilege('anon', 'public.bookpromo_set_updated_at()', 'execute') as anon_execute,
    has_function_privilege('authenticated', 'public.bookpromo_set_updated_at()', 'execute') as authenticated_execute;

-- RPCs: invoker, no public-client execution, service_role execution granted.
select p.proname,p.prosecdef,
 has_function_privilege('anon',p.oid,'execute') as anon_execute,
 has_function_privilege('authenticated',p.oid,'execute') as authenticated_execute,
 has_function_privilege('service_role',p.oid,'execute') as service_execute
from pg_proc p join pg_namespace n on n.oid=p.pronamespace
where n.nspname='public' and p.proname in ('bookpromo_sync','bookpromo_reserve','bookpromo_transition');

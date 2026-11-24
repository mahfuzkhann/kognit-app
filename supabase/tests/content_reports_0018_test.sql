-- Tests for migration 0018 (content_reports + submit_content_report).
-- Run by run_local_db_tests.sh after 0018 is applied. Raises on failure.
-- LOCAL THROWAWAY DATABASE ONLY - never run against a Supabase project.
\set ON_ERROR_STOP on
\set QUIET on

create or replace function pg_temp.check(ok boolean, msg text) returns void language plpgsql as $$
begin if ok is not true then raise exception 'TEST FAILED: %', msg; end if; end $$;

insert into auth.users (id, created_at, email) values
  ('00000000-0000-0000-0000-0000000000c1', now(), 'rep-a@example.test'),
  ('00000000-0000-0000-0000-0000000000c2', now(), 'rep-b@example.test')
on conflict do nothing;

-- ---- privileges: students and anon hold NOTHING on the table ----------------------
do $$
declare r text; p text;
begin
  perform pg_temp.check((select relrowsecurity from pg_class where oid = 'public.content_reports'::regclass),
                        'RLS must be ON for content_reports');
  perform pg_temp.check(not exists (select 1 from pg_policies where tablename = 'content_reports'),
                        'content_reports must have NO policy (no direct access path)');
  foreach r in array array['anon','authenticated'] loop
    foreach p in array array['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER'] loop
      perform pg_temp.check(not has_table_privilege(r, 'public.content_reports', p),
                            format('%s must not hold %s on content_reports', r, p));
    end loop;
  end loop;
  -- the function: signed-in students yes, anon / PUBLIC no
  perform pg_temp.check(has_function_privilege('authenticated',
    'public.submit_content_report(text,text,text,text,text,text,text)', 'execute'), 'authenticated can execute');
  perform pg_temp.check(not has_function_privilege('anon',
    'public.submit_content_report(text,text,text,text,text,text,text)', 'execute'), 'anon must not execute');
  -- verify_policies.sql query 5 style: pinned search_path
  perform pg_temp.check(exists (
    select 1 from pg_proc p where p.proname = 'submit_content_report' and p.prosecdef
      and exists (select 1 from unnest(p.proconfig) c where c like 'search_path=%')), 'SECURITY DEFINER with pinned search_path');
end $$;

-- ---- a student can submit; the reporter comes from auth.uid() ---------------------
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000000c1';
do $$ declare v_id uuid; begin
  v_id := public.submit_content_report('inaccurate', 'wrong formula', 'The answer text', 'my question', 'req-123', 'android', '1.2.3');
  perform pg_temp.check(v_id is not null, 'submit returns an id');
  -- direct table access stays closed even for the reporter
  begin perform count(*) from public.content_reports;
        perform pg_temp.check(false, 'student must not SELECT'); exception when insufficient_privilege then null; end;
  begin update public.content_reports set status = 'dismissed';
        perform pg_temp.check(false, 'student must not UPDATE'); exception when insufficient_privilege then null; end;
  begin delete from public.content_reports;
        perform pg_temp.check(false, 'student must not DELETE'); exception when insufficient_privilege then null; end;
  begin insert into public.content_reports (user_id, category, ai_response_excerpt)
          values (auth.uid(), 'other', 'x');
        perform pg_temp.check(false, 'student must not INSERT directly'); exception when insufficient_privilege then null; end;
end $$;
commit;

-- ---- what was stored: server-forced fields, nothing client-chosen ------------------
do $$ declare r record; begin
  select * into r from public.content_reports where request_id = 'req-123';
  perform pg_temp.check(r.user_id = '00000000-0000-0000-0000-0000000000c1', 'user_id comes from auth.uid()');
  perform pg_temp.check(r.status = 'new', 'status is forced to new');
  perform pg_temp.check(r.platform = 'android' and r.app_version = '1.2.3', 'platform/version stored');
  perform pg_temp.check(r.category = 'inaccurate' and r.note = 'wrong formula', 'category/note stored');
end $$;

-- ---- anon and unauthenticated calls are refused ------------------------------------
begin;
set local role anon;
do $$ begin
  begin perform public.submit_content_report('other', null, 'x', null, null, 'web', null);
        perform pg_temp.check(false, 'anon call must be refused'); exception when insufficient_privilege then null; end;
end $$;
commit;
begin;
set local role authenticated;
reset request.jwt.claim.sub;
set local request.jwt.claim.sub = '';
do $$ begin
  begin perform public.submit_content_report('other', null, 'x', null, null, 'web', null);
        perform pg_temp.check(false, 'no auth.uid() must be refused'); exception when sqlstate '28000' then null; end;
end $$;
commit;

-- ---- CHECK constraints reject bad values --------------------------------------------
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000000c2';
do $$ begin
  begin perform public.submit_content_report('bogus', null, 'x', null, null, 'web', null);
        perform pg_temp.check(false, 'bad category'); exception when check_violation then null; end;
  begin perform public.submit_content_report('other', repeat('a', 501), 'x', null, null, 'web', null);
        perform pg_temp.check(false, 'note too long'); exception when check_violation then null; end;
  begin perform public.submit_content_report('other', null, '', null, null, 'web', null);
        perform pg_temp.check(false, 'empty response'); exception when check_violation then null; end;
  begin perform public.submit_content_report('other', null, repeat('a', 2001), null, null, 'web', null);
        perform pg_temp.check(false, 'response too long'); exception when check_violation then null; end;
  begin perform public.submit_content_report('other', null, 'x', null, 'bad id with spaces', 'web', null);
        perform pg_temp.check(false, 'bad request id'); exception when check_violation then null; end;
  begin perform public.submit_content_report('other', null, 'x', null, null, 'ios', null);
        perform pg_temp.check(false, 'bad platform'); exception when check_violation then null; end;
  begin perform public.submit_content_report('other', null, 'x', null, null, 'web', 'v 1');
        perform pg_temp.check(false, 'bad app version'); exception when check_violation then null; end;
end $$;
commit;

-- ---- per-user cap: 20 per rolling 24h, scoped per user -------------------------------
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000000c2';
do $$ declare i int; begin
  for i in 1..20 loop
    perform public.submit_content_report('other', null, 'report ' || i, null, null, 'web', null);
  end loop;
  begin perform public.submit_content_report('other', null, 'one too many', null, null, 'web', null);
        perform pg_temp.check(false, '21st report must be refused'); exception when sqlstate '53400' then null; end;
end $$;
commit;
-- another student is unaffected by user c2's cap
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000000c1';
do $$ begin
  perform public.submit_content_report('other', null, 'still allowed', null, null, 'web', null);
end $$;
commit;
-- reports older than 24h stop counting (rolling window)
update public.content_reports set created_at = now() - interval '25 hours'
 where user_id = '00000000-0000-0000-0000-0000000000c2';
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000000c2';
do $$ begin
  perform public.submit_content_report('other', null, 'after the window', null, null, 'web', null);
end $$;
commit;

-- ---- deleting the account deletes its reports (cascade) ------------------------------
delete from auth.users where id = '00000000-0000-0000-0000-0000000000c2';
do $$ begin
  perform pg_temp.check(not exists (select 1 from public.content_reports
    where user_id = '00000000-0000-0000-0000-0000000000c2'), 'reports cascade with the account');
end $$;

\echo CONTENT REPORTS (0018) DB TESTS PASSED
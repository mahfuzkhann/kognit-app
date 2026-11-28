-- Tests for migration 0019 (user_projects.version + save_user_project compare-and-set).
-- Run by run_local_db_tests.sh after 0019 is applied. Raises on failure.
-- LOCAL THROWAWAY DATABASE ONLY - never run against a Supabase project.
\set ON_ERROR_STOP on
\set QUIET on

create or replace function pg_temp.check(ok boolean, msg text) returns void language plpgsql as $$
begin if ok is not true then raise exception 'TEST FAILED: %', msg; end if; end $$;

insert into auth.users (id, created_at, email) values
  ('00000000-0000-0000-0000-0000000000e1', now(), 'cas-a@example.test'),
  ('00000000-0000-0000-0000-0000000000e2', now(), 'cas-b@example.test')
on conflict do nothing;

-- ---- structure and privileges ------------------------------------------------------
do $$ begin
  perform pg_temp.check(exists (select 1 from information_schema.columns
     where table_schema='public' and table_name='user_projects' and column_name='version' and data_type='bigint' and is_nullable='NO'),
     'version column exists, bigint, not null');
  perform pg_temp.check(not has_function_privilege('anon','public.save_user_project(text,text,jsonb,bigint)','execute'), 'anon cannot execute');
  perform pg_temp.check(has_function_privilege('authenticated','public.save_user_project(text,text,jsonb,bigint)','execute'), 'authenticated can execute');
  perform pg_temp.check(not exists (select 1 from pg_proc p where p.proname='save_user_project' and p.prosecdef),
     'save_user_project must be SECURITY INVOKER so RLS applies');
  perform pg_temp.check(exists (select 1 from pg_proc p where p.proname='save_user_project'
     and exists (select 1 from unnest(p.proconfig) c where c like 'search_path=%')), 'search_path pinned');
  perform pg_temp.check(exists (select 1 from pg_trigger where tgname='user_projects_bump_version_trg' and tgrelid='public.user_projects'::regclass), 'bump trigger exists');
end $$;

-- a LEGACY row (as the old app wrote it) becomes version 1 by default
insert into public.user_projects (id, user_id, title, chats)
values ('proj_1700000000000', '00000000-0000-0000-0000-0000000000e1', 'Legacy', '[{"id":"chat_1700000000001","title":"Old","messages":[]}]');
do $$ begin
  perform pg_temp.check((select version from public.user_projects where id='proj_1700000000000') = 1, 'legacy row is version 1');
end $$;

-- ---- as student A: create / update / stale / replay / not_found ----------------------
create temp table res(label text, r jsonb);
grant all on res to authenticated;

begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000000e1';

-- create a new UUID-id notebook
insert into res select 'create', public.save_user_project('3f2b1c9e-8a4d-4e6f-9b1a-0c5d7e2f4a10', 'My notebook', '[{"id":"c1","messages":[{"role":"user","text":"hi"}]}]', null);
-- replay of the SAME create (lost reply) is ok, not a duplicate, not a conflict
insert into res select 'create_replay', public.save_user_project('3f2b1c9e-8a4d-4e6f-9b1a-0c5d7e2f4a10', 'My notebook', '[{"id":"c1","messages":[{"role":"user","text":"hi"}]}]', null);
-- a create of the same id with DIFFERENT content is a conflict (never overwrites)
insert into res select 'create_different', public.save_user_project('3f2b1c9e-8a4d-4e6f-9b1a-0c5d7e2f4a10', 'Other', '[]', null);
-- update with the right version
insert into res select 'update_v1', public.save_user_project('3f2b1c9e-8a4d-4e6f-9b1a-0c5d7e2f4a10', 'My notebook', '[{"id":"c1","messages":[{"role":"user","text":"hi"},{"role":"bot","text":"hello"}]}]', 1);
-- the SAME update again (reply lost): replayed ok, version NOT bumped twice
insert into res select 'update_replay', public.save_user_project('3f2b1c9e-8a4d-4e6f-9b1a-0c5d7e2f4a10', 'My notebook', '[{"id":"c1","messages":[{"role":"user","text":"hi"},{"role":"bot","text":"hello"}]}]', 1);
-- a stale writer (still at version 1) with different content: conflict, nothing written
insert into res select 'stale', public.save_user_project('3f2b1c9e-8a4d-4e6f-9b1a-0c5d7e2f4a10', 'Stale title', '[{"id":"c1","messages":[]}]', 1);
-- a future version is never accepted
insert into res select 'future', public.save_user_project('3f2b1c9e-8a4d-4e6f-9b1a-0c5d7e2f4a10', 'x', '[]', 99);
-- expecting a notebook that does not exist
insert into res select 'ghost', public.save_user_project('00000000-0000-4000-8000-000000000000', 'x', '[]', 1);
-- legacy id, right version
insert into res select 'legacy_ok', public.save_user_project('proj_1700000000000', 'Legacy renamed', '[{"id":"chat_1700000000001","title":"Old","messages":[]}]', 1);
commit;

do $$
declare get jsonb;
begin
  perform pg_temp.check((select r->>'status' from res where label='create')='ok' and (select (r->>'created')::bool from res where label='create') and (select (r->>'version')::int from res where label='create')=1, 'create ok v1');
  perform pg_temp.check((select r->>'status' from res where label='create_replay')='ok' and (select (r->>'version')::int from res where label='create_replay')=1, 'create replay is ok at v1 (idempotent)');
  perform pg_temp.check((select r->>'status' from res where label='create_different')='conflict', 'create of existing id with different content is a conflict');
  perform pg_temp.check((select r->'server'->>'title' from res where label='create_different')='My notebook', 'conflict returns the server copy');
  perform pg_temp.check((select r->>'status' from res where label='update_v1')='ok' and (select (r->>'version')::int from res where label='update_v1')=2, 'update at v1 gives v2');
  perform pg_temp.check((select r->>'status' from res where label='update_replay')='ok' and (select (r->>'version')::int from res where label='update_replay')=2, 'replayed update stays v2 (no double bump)');
  perform pg_temp.check((select r->>'status' from res where label='stale')='conflict', 'stale writer gets a conflict');
  perform pg_temp.check((select (r->'server'->>'version')::int from res where label='stale')=2, 'conflict carries the current version');
  perform pg_temp.check(jsonb_array_length((select r->'server'->'chats'->0->'messages' from res where label='stale'))=2, 'conflict carries the server messages (2) - the stale writer wrote nothing');
  perform pg_temp.check((select r->>'status' from res where label='future')='conflict', 'future version is a conflict, never accepted');
  perform pg_temp.check((select r->>'status' from res where label='ghost')='not_found', 'missing notebook with an expected version is not_found');
  perform pg_temp.check((select r->>'status' from res where label='legacy_ok')='ok' and (select (r->>'version')::int from res where label='legacy_ok')=2, 'legacy "proj_<ms>" id keeps working');
  -- the stored row is the winner's, untouched by the stale attempt
  perform pg_temp.check((select version from public.user_projects where id='3f2b1c9e-8a4d-4e6f-9b1a-0c5d7e2f4a10')=2, 'stored version is 2');
  perform pg_temp.check((select title from public.user_projects where id='3f2b1c9e-8a4d-4e6f-9b1a-0c5d7e2f4a10')='My notebook', 'stale title never stored');
  -- server sets updated_at itself
  perform pg_temp.check((select updated_at from public.user_projects where id='3f2b1c9e-8a4d-4e6f-9b1a-0c5d7e2f4a10') > now() - interval '1 minute', 'updated_at set by the server');
end $$;

-- ---- a LEGACY writer (plain upsert, no version) cannot slip past a version check ------
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000000e1';
update public.user_projects set chats = '[{"id":"c1","messages":[{"role":"user","text":"written by an old cached app"}]}]', updated_at = now()
 where id = '3f2b1c9e-8a4d-4e6f-9b1a-0c5d7e2f4a10';
insert into res select 'after_legacy_stale', public.save_user_project('3f2b1c9e-8a4d-4e6f-9b1a-0c5d7e2f4a10', 'My notebook', '[{"id":"c1","messages":[]}]', 2);
-- the old app's upsert (insert ... on conflict do update) path too
insert into public.user_projects (id, user_id, title, chats, updated_at)
  values ('3f2b1c9e-8a4d-4e6f-9b1a-0c5d7e2f4a10', auth.uid(), 'My notebook', '[{"id":"c1","messages":[{"role":"user","text":"upsert"}]}]', now())
  on conflict (id) do update set title = excluded.title, chats = excluded.chats, updated_at = excluded.updated_at;
commit;
do $$ begin
  perform pg_temp.check((select version from public.user_projects where id='3f2b1c9e-8a4d-4e6f-9b1a-0c5d7e2f4a10')=4, 'two legacy writes bumped the version 2 -> 3 -> 4 by the trigger');
  perform pg_temp.check((select r->>'status' from res where label='after_legacy_stale')='conflict', 'a CAS client holding v2 is told about the legacy write');
end $$;

-- ---- cross-user isolation: B can neither see, change, nor take over A's notebooks -----
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000000e2';
insert into res select 'b_update_a', public.save_user_project('3f2b1c9e-8a4d-4e6f-9b1a-0c5d7e2f4a10', 'HIJACK', '[]', 4);
insert into res select 'b_create_a_id', public.save_user_project('3f2b1c9e-8a4d-4e6f-9b1a-0c5d7e2f4a10', 'HIJACK', '[]', null);
insert into res select 'b_own', public.save_user_project('b-own-project', 'B notebook', '[]', null);
do $$ begin
  perform pg_temp.check((select count(*) from public.user_projects)=1, 'B sees only B''s own row');
  update public.user_projects set title = 'HIJACK' where id = '3f2b1c9e-8a4d-4e6f-9b1a-0c5d7e2f4a10';
  perform pg_temp.check(not found, 'B cannot update A''s row directly');
  delete from public.user_projects where id = '3f2b1c9e-8a4d-4e6f-9b1a-0c5d7e2f4a10';
  perform pg_temp.check(not found, 'B cannot delete A''s row');
end $$;
commit;
do $$ begin
  perform pg_temp.check((select r->>'status' from res where label='b_update_a')='not_found', 'B updating A''s id: not_found (reveals nothing)');
  perform pg_temp.check((select r->>'status' from res where label='b_create_a_id')='id_unavailable', 'B creating A''s id: id_unavailable');
  perform pg_temp.check(not exists (select 1 from res where r::text like '%My notebook%' and label like 'b_%'), 'B never receives A''s data');
  perform pg_temp.check((select title from public.user_projects where id='3f2b1c9e-8a4d-4e6f-9b1a-0c5d7e2f4a10')='My notebook', 'A''s notebook untouched by B');
  perform pg_temp.check((select r->>'status' from res where label='b_own')='ok', 'B can save own notebook');
end $$;

-- ---- authentication and validation ------------------------------------------------------
begin;
set local role anon;
do $$ begin
  begin perform public.save_user_project('x1','t','[]',null); perform pg_temp.check(false,'anon must be refused'); exception when insufficient_privilege then null; end;
end $$;
commit;
begin;
set local role authenticated;
set local request.jwt.claim.sub = '';
do $$ begin
  begin perform public.save_user_project('x1','t','[]',null); perform pg_temp.check(false,'no uid must be refused'); exception when sqlstate '28000' then null; end;
end $$;
commit;
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000000e1';
do $$ declare big text; begin
  begin perform public.save_user_project('bad id!','t','[]',null);  perform pg_temp.check(false,'bad id'); exception when sqlstate '22023' then null; end;
  begin perform public.save_user_project('','t','[]',null);        perform pg_temp.check(false,'empty id'); exception when sqlstate '22023' then null; end;
  begin perform public.save_user_project(repeat('a',101),'t','[]',null); perform pg_temp.check(false,'long id'); exception when sqlstate '22023' then null; end;
  begin perform public.save_user_project(null,'t','[]',null);     perform pg_temp.check(false,'null id'); exception when sqlstate '22023' then null; end;
  begin perform public.save_user_project('v1','t','{"a":1}',null); perform pg_temp.check(false,'chats object'); exception when sqlstate '22023' then null; end;
  begin perform public.save_user_project('v1','t',null,null);      perform pg_temp.check(false,'chats null'); exception when sqlstate '22023' then null; end;
  begin perform public.save_user_project('v1','t','[]',0);         perform pg_temp.check(false,'version 0'); exception when sqlstate '22023' then null; end;
  begin perform public.save_user_project('v1','t','[]',-3);        perform pg_temp.check(false,'negative version'); exception when sqlstate '22023' then null; end;
  big := repeat('x', 10485761);
  begin perform public.save_user_project('v1','t', jsonb_build_array(big), null); perform pg_temp.check(false,'oversize'); exception when sqlstate '54000' then null; end;
  -- title rules: blank -> default; long -> clipped to 200
  perform public.save_user_project('title-blank','   ','[]',null);
  perform public.save_user_project('title-long', repeat('t',500),'[]',null);
end $$;
commit;
do $$ begin
  perform pg_temp.check((select title from public.user_projects where id='title-blank')='Untitled notebook', 'blank title gets the default');
  perform pg_temp.check(char_length((select title from public.user_projects where id='title-long'))=200, 'long title clipped to 200');
  perform pg_temp.check(not exists (select 1 from public.user_projects where id in ('v1','x1','')), 'rejected saves wrote nothing');
end $$;

-- ---- deleting the account removes the notebooks (cascade is unchanged) ------------------
delete from auth.users where id in ('00000000-0000-0000-0000-0000000000e1','00000000-0000-0000-0000-0000000000e2');
do $$ begin
  perform pg_temp.check(not exists (select 1 from public.user_projects where user_id in ('00000000-0000-0000-0000-0000000000e1','00000000-0000-0000-0000-0000000000e2')), 'notebooks cascade with the account');
end $$;
\echo PROJECT CAS (0019) DB TESTS PASSED
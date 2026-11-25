-- Account-deletion cascade test (docs/ACCOUNT_DELETION.md).
-- Run by run_local_db_tests.sh AFTER every migration is applied. Raises on failure.
-- LOCAL THROWAWAY DATABASE ONLY - never run against a Supabase project.
--
-- What it proves
--   1. STRUCTURE: every table in schema public that has a user_id column is tied to
--      auth.users with ON DELETE CASCADE, and no foreign key from public to
--      auth.users uses any other rule. A future migration that adds a table with
--      user data but forgets the cascade makes THIS TEST FAIL, so a deleted
--      account can never silently leave rows behind.
--   2. BEHAVIOUR: deleting one auth user removes their rows from the tables we
--      populate here (including quiz_answers, which cascades through
--      quiz_attempts), and does not touch another user's rows.
--   3. SWEEP: after the delete, NO public table has any row for that user id.
\set ON_ERROR_STOP on
\set QUIET on

create or replace function pg_temp.check(ok boolean, msg text) returns void language plpgsql as $$
begin if ok is not true then raise exception 'TEST FAILED: %', msg; end if; end $$;

-- ---- 1. structure ---------------------------------------------------------------
do $$
declare t record; n int;
begin
  -- every public.<table>.user_id column
  for t in
    select c.table_name
      from information_schema.columns c
      join information_schema.tables tb
        on tb.table_schema = c.table_schema and tb.table_name = c.table_name and tb.table_type = 'BASE TABLE'
     where c.table_schema = 'public' and c.column_name = 'user_id'
  loop
    select count(*) into n
      from pg_constraint k
      join pg_attribute a on a.attrelid = k.conrelid and a.attnum = any (k.conkey)
     where k.contype = 'f'
       and k.conrelid = format('public.%I', t.table_name)::regclass
       and k.confrelid = 'auth.users'::regclass
       and a.attname = 'user_id'
       and k.confdeltype = 'c';
    perform pg_temp.check(n = 1,
      format('public.%s has a user_id column but no ON DELETE CASCADE foreign key to auth.users', t.table_name));
  end loop;

  -- no public table may reference auth.users with any rule other than CASCADE
  for t in
    select k.conrelid::regclass::text as tbl, k.confdeltype
      from pg_constraint k
     where k.contype = 'f' and k.confrelid = 'auth.users'::regclass
       and k.connamespace = 'public'::regnamespace
       and k.confdeltype <> 'c'
  loop
    raise exception 'TEST FAILED: % references auth.users without ON DELETE CASCADE (rule %)', t.tbl, t.confdeltype;
  end loop;

  -- child tables of user-owned tables must cascade too
  perform pg_temp.check((select confdeltype from pg_constraint
      where conrelid = 'public.quiz_answers'::regclass and confrelid = 'public.quiz_attempts'::regclass) = 'c',
      'quiz_answers must cascade from quiz_attempts');
end $$;

-- ---- 2. behaviour ----------------------------------------------------------------
insert into auth.users (id, created_at, email) values
  ('00000000-0000-0000-0000-0000000000d1', now(), 'victim@example.test'),
  ('00000000-0000-0000-0000-0000000000d2', now(), 'bystander@example.test')
on conflict do nothing;

-- the same shape of data for both users
do $$
declare u uuid; a uuid;
begin
  foreach u in array array['00000000-0000-0000-0000-0000000000d1'::uuid, '00000000-0000-0000-0000-0000000000d2'::uuid] loop
    insert into public.quiz_attempts (user_id, board, user_class, subject, topic, total_questions, score)
      values (u, 'NCTB', 'Class 9-10', 'Physics', 'Force', 5, 3) returning id into a;
    insert into public.quiz_answers (attempt_id, question_index, question_text, selected_index, correct_index, is_correct)
      values (a, 0, 'Q?', 1, 1, true), (a, 1, 'Q2?', null, 2, false);
    insert into public.user_projects (id, user_id, title, chats)
      values ('proj_' || u::text, u, 'Notebook', '[{"id":"chat_1","messages":[{"role":"user","text":"private"}]}]'::jsonb);
    insert into public.content_reports (user_id, category, ai_response_excerpt)
      values (u, 'other', 'answer text');
  end loop;
end $$;

do $$ begin
  perform pg_temp.check((select count(*) from public.quiz_answers qa join public.quiz_attempts q on q.id = qa.attempt_id
                          where q.user_id = '00000000-0000-0000-0000-0000000000d1') = 2, 'setup: victim answers exist');
end $$;

delete from auth.users where id = '00000000-0000-0000-0000-0000000000d1';

do $$
declare t record; n bigint;
begin
  perform pg_temp.check(not exists (select 1 from public.quiz_attempts where user_id = '00000000-0000-0000-0000-0000000000d1'), 'quiz_attempts removed');
  perform pg_temp.check(not exists (select 1 from public.user_projects where user_id = '00000000-0000-0000-0000-0000000000d1'), 'user_projects (chats) removed');
  perform pg_temp.check(not exists (select 1 from public.content_reports where user_id = '00000000-0000-0000-0000-0000000000d1'), 'content_reports removed');
  perform pg_temp.check((select count(*) from public.quiz_answers qa
      where not exists (select 1 from public.quiz_attempts q where q.id = qa.attempt_id)) = 0, 'no orphaned quiz_answers');

  -- the bystander is untouched
  perform pg_temp.check((select count(*) from public.quiz_attempts where user_id = '00000000-0000-0000-0000-0000000000d2') = 1, 'bystander quiz_attempts kept');
  perform pg_temp.check((select count(*) from public.quiz_answers qa join public.quiz_attempts q on q.id = qa.attempt_id
                          where q.user_id = '00000000-0000-0000-0000-0000000000d2') = 2, 'bystander quiz_answers kept');
  perform pg_temp.check((select count(*) from public.user_projects where user_id = '00000000-0000-0000-0000-0000000000d2') = 1, 'bystander project kept');
  perform pg_temp.check((select count(*) from public.content_reports where user_id = '00000000-0000-0000-0000-0000000000d2') = 1, 'bystander report kept');

  -- ---- 3. sweep: no public table keeps a single row for the deleted user ------------
  for t in
    select c.table_name
      from information_schema.columns c
      join information_schema.tables tb
        on tb.table_schema = c.table_schema and tb.table_name = c.table_name and tb.table_type = 'BASE TABLE'
     where c.table_schema = 'public' and c.column_name = 'user_id'
  loop
    execute format('select count(*) from public.%I where user_id = %L', t.table_name, '00000000-0000-0000-0000-0000000000d1') into n;
    perform pg_temp.check(n = 0, format('public.%s still has rows for the deleted user', t.table_name));
  end loop;
end $$;

delete from auth.users where id = '00000000-0000-0000-0000-0000000000d2';
\echo ACCOUNT DELETION CASCADE TESTS PASSED
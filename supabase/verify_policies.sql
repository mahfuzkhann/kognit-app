-- Kloriz: READ-ONLY policy audit for the DEPLOYED Supabase database.
--
-- Run this whole file in the Supabase SQL editor. It changes nothing.
--
-- WHY A SEPARATE FILE: verify_schema.sql proves the tables/columns exist and RLS
-- is switched on. It cannot see HOW each policy is written. Row level security
-- policies are additive (OR-ed): a single over-broad policy - for example one
-- created by hand on user_projects long before the repository had a migration
-- for it - silently lets every student read every other student's rows even
-- though RLS is "on". This file lists every policy that is not clearly scoped to
-- the signed-in user so a human can review it.
--
-- EXPECTED RESULT: ZERO ROWS from query 1 (and query 2 lists only objects you
-- recognise). Any row in query 1 is a policy to review and, if it is too broad,
-- drop - decide deliberately; this file will not do it for you.
--
-- Query 1: policies on student-data tables whose USING and WITH CHECK
-- expressions BOTH fail to mention auth.uid() (for example "using (true)" or a
-- hand-made "allow all" policy), or that are granted explicitly to the anon role.
-- A policy written for role "public" that is scoped by auth.uid() = user_id is
-- fine: an unauthenticated caller has no uid and matches no row.
select schemaname, tablename, policyname, cmd, roles, qual, with_check
from pg_policies
where schemaname = 'public'
  and tablename in ('user_projects', 'quiz_attempts', 'quiz_answers', 'learning_evidence',
                    'conversation_index', 'student_profiles', 'pocket_notes',
                    'payments', 'subscriptions', 'ai_usage_events')
  and (
        (coalesce(qual, '') not ilike '%auth.uid()%' and coalesce(with_check, '') not ilike '%auth.uid()%')
     or roles::text ilike '%anon%'
      )
order by tablename, policyname;

-- Query 2: tables in the public schema that have row level security OFF.
-- Reference tables you intend to be public should be the only ones listed.
select c.relname as table_without_rls
from pg_class c
join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'public' and c.relkind = 'r' and not c.relrowsecurity
order by 1;

-- Query 3: privileges granted to anon / authenticated on the authority tables
-- from migration 0008. Students must hold SELECT only on plan_limits, plan_prices,
-- payments and subscriptions, and nothing at all on quota_ledger.
select table_name, grantee, string_agg(privilege_type, ', ' order by privilege_type) as privileges
from information_schema.role_table_grants
where table_schema = 'public'
  and table_name in ('plan_limits', 'plan_prices', 'payments', 'subscriptions', 'quota_ledger', 'ai_usage_events')
  and grantee in ('anon', 'authenticated')
group by table_name, grantee
order by table_name, grantee;
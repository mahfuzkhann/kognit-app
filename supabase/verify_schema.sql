-- Kognit: READ-ONLY schema check for the DEPLOYED Supabase database.
--
-- Run this whole file in the Supabase SQL editor. It changes nothing.
--
-- It compares the database you actually deployed against what the repository's
-- migrations (supabase/migrations/*.sql) define. A deployed database can silently
-- lag the code, and a missing column only surfaces at runtime, e.g.
--   PGRST204: Could not find the 'stream' column of 'quiz_attempts' in the schema cache
-- (that column is added by 0002_quiz_topic_identity.sql).
--
-- EXPECTED RESULT: ZERO ROWS. Every row returned is something to fix:
--
--   run_migration  the migration file to run (run them IN NUMBER ORDER, lowest first)
--   problem        MISSING                        a column, or its whole table, does not exist
--                  NOT NULL BUT MUST BE NULLABLE  the column exists but a later migration that
--                                                 relaxes it has not been applied
--                  ROW LEVEL SECURITY IS OFF      a table holding student data is unprotected
--   details        which table.column entries are affected
--
-- Re-run this after applying a migration; when it returns zero rows you are in step
-- with the repository. (0002, 0004 and 0006 are safe to run twice. 0001, 0003 and
-- 0005 create policies - run each of those only once.)
--
-- This file is kept in step with the migrations by test_schema_contract.py.

with expected (table_name, column_name, added_by, relaxed_by) as (
  values
    ('quiz_answers', 'attempt_id', '0001_quiz_persistence.sql', null),
    ('quiz_answers', 'correct_index', '0001_quiz_persistence.sql', null),
    ('quiz_answers', 'id', '0001_quiz_persistence.sql', null),
    ('quiz_answers', 'is_correct', '0001_quiz_persistence.sql', null),
    ('quiz_answers', 'question_index', '0001_quiz_persistence.sql', null),
    ('quiz_answers', 'question_text', '0001_quiz_persistence.sql', null),
    ('quiz_answers', 'selected_index', '0001_quiz_persistence.sql', null),
    ('quiz_attempts', 'board', '0001_quiz_persistence.sql', null),
    ('quiz_attempts', 'created_at', '0001_quiz_persistence.sql', null),
    ('quiz_attempts', 'id', '0001_quiz_persistence.sql', null),
    ('quiz_attempts', 'score', '0001_quiz_persistence.sql', null),
    ('quiz_attempts', 'subject', '0001_quiz_persistence.sql', null),
    ('quiz_attempts', 'topic', '0001_quiz_persistence.sql', null),
    ('quiz_attempts', 'total_questions', '0001_quiz_persistence.sql', null),
    ('quiz_attempts', 'user_class', '0001_quiz_persistence.sql', null),
    ('quiz_attempts', 'user_id', '0001_quiz_persistence.sql', null),
    ('quiz_attempts', 'stream', '0002_quiz_topic_identity.sql', null),
    ('quiz_attempts', 'topic_key', '0002_quiz_topic_identity.sql', null),
    ('conversation_index', 'attribution_confidence', '0003_learning_memory_foundation.sql', null),
    ('conversation_index', 'chat_id', '0003_learning_memory_foundation.sql', null),
    ('conversation_index', 'created_at', '0003_learning_memory_foundation.sql', null),
    ('conversation_index', 'id', '0003_learning_memory_foundation.sql', null),
    ('conversation_index', 'occurred_at', '0003_learning_memory_foundation.sql', null),
    ('conversation_index', 'short_label', '0003_learning_memory_foundation.sql', null),
    ('conversation_index', 'subject', '0003_learning_memory_foundation.sql', null),
    ('conversation_index', 'user_id', '0003_learning_memory_foundation.sql', null),
    ('learning_evidence', 'attribution_confidence', '0003_learning_memory_foundation.sql', null),
    ('learning_evidence', 'chat_id', '0003_learning_memory_foundation.sql', null),
    ('learning_evidence', 'created_at', '0003_learning_memory_foundation.sql', null),
    ('learning_evidence', 'id', '0003_learning_memory_foundation.sql', null),
    ('learning_evidence', 'occurred_at', '0003_learning_memory_foundation.sql', null),
    ('learning_evidence', 'signal_strength', '0003_learning_memory_foundation.sql', null),
    ('learning_evidence', 'signal_type', '0003_learning_memory_foundation.sql', null),
    ('learning_evidence', 'source', '0003_learning_memory_foundation.sql', null),
    ('learning_evidence', 'subject', '0003_learning_memory_foundation.sql', null),
    ('learning_evidence', 'topic', '0003_learning_memory_foundation.sql', '0004_learning_evidence_subject_only_attribution.sql'),
    ('learning_evidence', 'topic_key', '0003_learning_memory_foundation.sql', '0004_learning_evidence_subject_only_attribution.sql'),
    ('learning_evidence', 'user_id', '0003_learning_memory_foundation.sql', null),
    ('student_profiles', 'created_at', '0005_student_profile.sql', null),
    ('student_profiles', 'name', '0005_student_profile.sql', null),
    ('student_profiles', 'stream', '0005_student_profile.sql', '0006_student_profile_stream_optional.sql'),
    ('student_profiles', 'updated_at', '0005_student_profile.sql', null),
    ('student_profiles', 'user_class', '0005_student_profile.sql', null),
    ('student_profiles', 'user_id', '0005_student_profile.sql', null)
),
column_problems as (
  select e.table_name,
         e.column_name,
         case when c.column_name is null
              then 'MISSING'
              else 'NOT NULL BUT MUST BE NULLABLE' end as problem,
         case when c.column_name is null
              then e.added_by
              else e.relaxed_by end as run_migration
  from expected e
  left join information_schema.columns c
         on c.table_schema = 'public'
        and c.table_name   = e.table_name
        and c.column_name  = e.column_name
  where c.column_name is null
     or (e.relaxed_by is not null and c.is_nullable = 'NO')
),
rls_problems as (
  select tablename::text as table_name,
         '(all columns)'::text as column_name,
         'ROW LEVEL SECURITY IS OFF'::text as problem,
         'enable row level security - see the migration that creates ' || tablename::text as run_migration
  from pg_tables
  where schemaname = 'public'
    and tablename in ('conversation_index', 'learning_evidence', 'quiz_answers', 'quiz_attempts', 'student_profiles')
    and not rowsecurity
)
select run_migration,
       problem,
       string_agg(table_name || '.' || column_name, ', ' order by table_name, column_name) as details
from (select * from column_problems union all select * from rls_problems) all_problems
group by run_migration, problem
order by run_migration, problem;
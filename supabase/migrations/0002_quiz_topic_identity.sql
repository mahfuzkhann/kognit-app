-- Kognit: Phase 5A - quiz subject/stream/topic identity correction.
--
-- Run this once in the Supabase SQL editor (or via the Supabase CLI),
-- AFTER 0001_quiz_persistence.sql has already been applied.
--
-- PURELY ADDITIVE. No existing column is renamed, retyped, or dropped. No
-- existing row is modified. Backend writes to quiz_attempts using the
-- STUDENT'S OWN access token (see backend/database.py) - these two new
-- columns ride on the RLS policies already created in 0001 (select_own /
-- insert_own / delete_own all reference the whole row via user_id, so no
-- new policy is required for either new column).
--
-- ---------------------------------------------------------------------
-- WHY THESE TWO COLUMNS, AND WHAT THEY DO NOT FIX
-- ---------------------------------------------------------------------
--
-- `stream` (new): the board-level track - Science / Commerce / Arts. This
-- is what the pre-existing `subject` column has actually been storing
-- since 0001. Going forward (see backend/main.py + backend/database.py),
-- `subject` is corrected to store a real academic subject (Physics,
-- Chemistry, Bangla, ...) and `stream` captures what `subject` used to
-- mean, so no historical meaning is silently lost for new rows.
--
-- `topic_key` (new): a deterministic normalization of the free-text
-- `topic` column - trim + collapse internal whitespace + casefold. This
-- catches exact-formatting duplicates ("Force & Motion " vs
-- "force  &  motion") for topic-level aggregation. It does NOT unify
-- Bangla/English/Banglish equivalents, synonyms, or spelling variants -
-- those remain genuinely distinct topic_key values on purpose. A
-- curriculum-aware topic vocabulary (replacing free text entirely) is the
-- real long-term fix and is an explicitly separate, later piece of work.
--
-- ---------------------------------------------------------------------
-- LEGACY ROWS - READ THIS BEFORE WRITING ANY QUERY AGAINST THIS TABLE
-- ---------------------------------------------------------------------
--
-- Every row inserted before this migration/the matching backend change
-- shipped has `topic_key IS NULL` AND has its `subject` column holding a
-- STREAM value (Science/Commerce/Arts), not a real subject - these two
-- facts are the SAME legacy boundary, not two independent ones. Do not
-- backfill `topic_key` for these rows by normalizing their `topic` text
-- and leave `subject` untouched - that would silently reintroduce
-- corrupted subject data into topic-level aggregation while looking like
-- clean Phase 5A evidence. If historical quiz evidence is ever
-- reconciled, `subject` must be corrected at the same time `topic_key` is
-- backfilled, not before or after. No such reconciliation is being done
-- in this migration - existing rows are left exactly as they are, and
-- `topic_key IS NULL` is the intentional, sufficient marker that a row
-- predates this correction. Any read path aggregating by topic (see
-- backend/database.py:get_user_topic_profile) must exclude
-- `topic_key IS NULL` rows rather than guessing at their meaning.

alter table public.quiz_attempts
    add column if not exists stream    text,
    add column if not exists topic_key text;

-- Supports backend/database.py:get_user_topic_profile, which always
-- filters by the authenticated user (RLS-enforced) and groups by
-- (subject, topic_key) ordered by recency.
create index if not exists idx_quiz_attempts_user_subject_topickey
    on public.quiz_attempts (user_id, subject, topic_key, created_at desc);
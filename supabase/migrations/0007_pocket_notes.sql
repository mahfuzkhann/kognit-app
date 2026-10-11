-- Kognit: Pocket Notes data foundation.
--
-- Run this once in the Supabase SQL editor (or via the Supabase CLI), AFTER
-- 0001-0006 have already been applied. THIS FILE HAS NOT BEEN APPLIED BY
-- ANYONE - the engineer who prepared it never touched a live database.
-- Review it, then apply it yourself.
--
-- PURELY ADDITIVE - a new table only. Nothing in user_projects,
-- quiz_attempts, quiz_answers, learning_evidence, conversation_index or
-- student_profiles is touched. Pocket Notes deliberately lives OUTSIDE the
-- user_projects.chats JSONB blob: deleting a chat or project must never
-- delete a note a student chose to keep, and the blob's whole-project
-- last-write-wins sync must never be able to overwrite a note.
--
-- POCKET NOTES ARE NOT LEARNING EVIDENCE: nothing in this table (including
-- the AI-generated flashcards / mini_quiz) is ever read by
-- backend/mastery_engine.py, backend/learning_memory.py or any
-- quiz_attempts / learning_evidence write path. Mini-quiz scoring is
-- client-side only. Do not wire this table into those systems without a
-- separate, explicit decision.
--
-- The backend writes to this table with the STUDENT'S OWN Supabase access
-- token (never a service-role key) - see backend/database.py (Pocket Notes
-- section) - so the RLS policies below are the actual isolation boundary.
-- The backend additionally filters every query by user_id (defence in
-- depth) and never accepts a user_id from a request body.
--
-- IDEMPOTENT CREATE: unique (user_id, client_id). The client derives
-- client_id deterministically from the saved answer, so a retried or
-- double-clicked save collides on this constraint and the backend returns
-- the existing note instead of creating a duplicate.
--
-- Size/shape limits are enforced BOTH in backend/notes_engine.py (which
-- returns friendly 400s) AND here as CHECK constraints, so a bug in either
-- layer cannot let oversized or malformed rows in. If you change a limit,
-- change it in both places (backend/notes_engine.py constants).
--
-- tags jsonb shape: [{"label": "Physics", "kind": "subject", "origin": "inferred"}, ...]
--   kind   in: subject | class | stream | topic | type | other
--   origin in: known | inferred | student | ai
-- The kind/origin vocabularies are validated in Python only (same
-- single-source-of-truth reasoning as 0005's class/stream values).

create table if not exists public.pocket_notes (
    id           uuid primary key default gen_random_uuid(),
    user_id      uuid not null references auth.users(id) on delete cascade,
    client_id    text not null,
    content_md   text not null,
    takeaway     text,
    question_md  text,
    sources      jsonb not null default '[]'::jsonb,
    tags         jsonb not null default '[]'::jsonb,
    needs_review boolean not null default false,
    source_ref   jsonb,
    flashcards   jsonb,
    mini_quiz    jsonb,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now(),

    constraint pocket_notes_client_id_format
        check (client_id ~ '^[A-Za-z0-9_-]{8,64}$'),
    constraint pocket_notes_content_length
        check (char_length(content_md) between 1 and 20000),
    constraint pocket_notes_takeaway_length
        check (takeaway is null or char_length(takeaway) <= 600),
    constraint pocket_notes_question_length
        check (question_md is null or char_length(question_md) <= 4000),
    constraint pocket_notes_sources_shape
        check (jsonb_typeof(sources) = 'array' and jsonb_array_length(sources) <= 10),
    constraint pocket_notes_tags_shape
        check (jsonb_typeof(tags) = 'array' and jsonb_array_length(tags) <= 12),
    constraint pocket_notes_source_ref_shape
        check (source_ref is null or jsonb_typeof(source_ref) = 'object'),
    constraint pocket_notes_flashcards_shape
        check (flashcards is null
               or (jsonb_typeof(flashcards) = 'array'
                   and jsonb_array_length(flashcards) <= 10
                   and char_length(flashcards::text) <= 60000)),
    constraint pocket_notes_mini_quiz_shape
        check (mini_quiz is null
               or (jsonb_typeof(mini_quiz) = 'array'
                   and jsonb_array_length(mini_quiz) <= 5
                   and char_length(mini_quiz::text) <= 60000)),
    constraint pocket_notes_user_client_unique
        unique (user_id, client_id)
);

-- The list endpoint always reads "this user's notes, newest first".
create index if not exists pocket_notes_user_created_idx
    on public.pocket_notes (user_id, created_at desc);

-- Auto-maintains updated_at on every UPDATE (tag edits, generated study
-- material), exactly like 0005's student_profiles trigger - the backend
-- never computes or trusts a client-adjacent timestamp.
create or replace function public.set_pocket_notes_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists trg_pocket_notes_updated_at on public.pocket_notes;
create trigger trg_pocket_notes_updated_at
    before update on public.pocket_notes
    for each row
    execute function public.set_pocket_notes_updated_at();

alter table public.pocket_notes enable row level security;

-- A student can only see/create/change/delete THEIR OWN notes. Unlike
-- student_profiles, Pocket Notes needs DELETE (removing a saved note is a
-- required feature).
create policy "pocket_notes_select_own"
    on public.pocket_notes for select
    using (auth.uid() = user_id);

create policy "pocket_notes_insert_own"
    on public.pocket_notes for insert
    with check (auth.uid() = user_id);

create policy "pocket_notes_update_own"
    on public.pocket_notes for update
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

create policy "pocket_notes_delete_own"
    on public.pocket_notes for delete
    using (auth.uid() = user_id);

-- ---------------------------------------------------------------------
-- ROLLBACK (manual; DESTROYS ALL SAVED NOTES - only run if you really
-- intend to remove the feature's data):
--
--   drop table if exists public.pocket_notes;
--   drop function if exists public.set_pocket_notes_updated_at();
--
-- Dropping the table also drops its policies, trigger and index.
-- ---------------------------------------------------------------------
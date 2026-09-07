-- Kognit: Phase 5B/5C/5D - Learning Memory foundation.
--
-- Run this once in the Supabase SQL editor (or via the Supabase CLI), AFTER
-- 0001_quiz_persistence.sql and 0002_quiz_topic_identity.sql have already
-- been applied.
--
-- PURELY ADDITIVE. Nothing in quiz_attempts/quiz_answers is touched, renamed,
-- or reinterpreted. backend/mastery_engine.py's compute_topic_status() is
-- NOT changed by this migration and continues to run only against
-- quiz_attempts evidence, exactly as before - see backend/database.py and
-- backend/learning_memory.py for why chat evidence is deliberately kept
-- separate rather than merged into the same numeric computation.
--
-- Backend writes to these tables using the STUDENT'S OWN access token
-- (never a service-role key) - see backend/database.py - so these RLS
-- policies are the actual enforcement boundary, not a formality.
--
-- ---------------------------------------------------------------------
-- WHY TWO TABLES, NOT ONE
-- ---------------------------------------------------------------------
--
-- `learning_evidence`: structured, subject/topic-attributed evidence,
-- written ONLY when backend.learning_memory.resolve_academic_context()
-- returns Known or Probable confidence. As of this migration, NOTHING
-- currently produces Known/Probable chat context (see backend/main.py) -
-- this table exists so the write path and read path are both real and
-- tested, ready for the day a reliable context source is added, without
-- requiring a second migration.
--
-- `conversation_index`: a lightweight, session-level record of "a chat
-- happened, roughly about this, at this time" - written once per chat
-- title-generation event (reusing the EXISTING /api/chat/title Gemini
-- call - no second AI call is introduced by this migration), regardless
-- of whether academic context is Known/Probable/Unknown. This is what
-- powers "what did I study recently" recall even for chats that could not
-- be reliably attributed to a subject - see GET /api/learning/history in
-- backend/main.py.
--
-- Neither table stores raw chat messages, PDF content, or image content.

create table if not exists public.learning_evidence (
    id                     uuid primary key default gen_random_uuid(),
    user_id                uuid not null references auth.users(id) on delete cascade,
    source                 text not null check (source in ('chat')),
    subject                text not null,
    topic                  text not null,
    topic_key              text not null,
    attribution_confidence text not null check (attribution_confidence in ('known', 'probable')),
    signal_type            text not null check (signal_type in (
                               'repeated_question', 're_explanation_request',
                               'confusion', 'explicit_misconception',
                               'successful_understanding'
                           )),
    signal_strength        text not null check (signal_strength in ('weak', 'strong')),
    chat_id                text,
    occurred_at            timestamptz not null default now(),
    created_at             timestamptz not null default now()
);

-- Supports backend/database.py:get_learning_history's topic-grouped read
-- and a future extension of GET /api/profile/topics to include chat
-- evidence alongside quiz evidence for the same (subject, topic_key).
create index if not exists idx_learning_evidence_user_subject_topickey
    on public.learning_evidence (user_id, subject, topic_key, occurred_at desc);

create index if not exists idx_learning_evidence_user_occurred
    on public.learning_evidence (user_id, occurred_at desc);

alter table public.learning_evidence enable row level security;

-- Append-oriented, same convention as quiz_attempts: select/insert/delete
-- own rows only, no UPDATE policy. Evidence is immutable once recorded.
create policy "learning_evidence_select_own"
    on public.learning_evidence for select
    using (auth.uid() = user_id);

create policy "learning_evidence_insert_own"
    on public.learning_evidence for insert
    with check (auth.uid() = user_id);

create policy "learning_evidence_delete_own"
    on public.learning_evidence for delete
    using (auth.uid() = user_id);


create table if not exists public.conversation_index (
    id                     uuid primary key default gen_random_uuid(),
    user_id                uuid not null references auth.users(id) on delete cascade,
    chat_id                text not null,
    subject                text,
    attribution_confidence text not null check (attribution_confidence in ('known', 'probable', 'unknown')),
    short_label            text not null,
    occurred_at            timestamptz not null default now(),
    created_at             timestamptz not null default now()
);

-- Supports GET /api/learning/history's bounded, ordered, user-scoped read.
create index if not exists idx_conversation_index_user_occurred
    on public.conversation_index (user_id, occurred_at desc);

alter table public.conversation_index enable row level security;

-- Same append-oriented convention. NOTE: a single chat_id may legitimately
-- have more than one conversation_index row over its lifetime (the label
-- is captured at whatever point /api/chat/title happens to run - see
-- backend/main.py) - this is intentional, not a duplicate-data bug. Each
-- row is a truthful snapshot of "what this chat looked like at time X",
-- not a single mutable summary of the chat.
create policy "conversation_index_select_own"
    on public.conversation_index for select
    using (auth.uid() = user_id);

create policy "conversation_index_insert_own"
    on public.conversation_index for insert
    with check (auth.uid() = user_id);

create policy "conversation_index_delete_own"
    on public.conversation_index for delete
    using (auth.uid() = user_id);
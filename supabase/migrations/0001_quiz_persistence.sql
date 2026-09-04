-- Kognit: Phase 1 database foundation - quiz attempt persistence.
--
-- Run this once in the Supabase SQL editor (or via the Supabase CLI).
-- Assumes the built-in Supabase `auth.users` table already exists (it
-- does, by default, in every Supabase project) and that gen_random_uuid()
-- is available (it is, by default, in Supabase Postgres via pgcrypto).
--
-- Backend writes to these tables using the STUDENT'S OWN access token
-- (never a service-role key) - see backend/database.py. That means these
-- RLS policies are the actual enforcement boundary, not a formality.

create table if not exists public.quiz_attempts (
    id              uuid primary key default gen_random_uuid(),
    user_id         uuid not null references auth.users(id) on delete cascade,
    board           text not null,
    user_class      text not null,
    subject         text not null,
    topic           text not null,
    total_questions integer not null check (total_questions > 0),
    score           integer not null check (score >= 0 and score <= total_questions),
    created_at      timestamptz not null default now()
);

create index if not exists idx_quiz_attempts_user_created
    on public.quiz_attempts (user_id, created_at desc);

create table if not exists public.quiz_answers (
    id              uuid primary key default gen_random_uuid(),
    attempt_id      uuid not null references public.quiz_attempts(id) on delete cascade,
    question_index  integer not null check (question_index >= 0),
    question_text   text not null,
    selected_index  integer,
    correct_index   integer not null,
    is_correct      boolean not null
);

create index if not exists idx_quiz_answers_attempt
    on public.quiz_answers (attempt_id);

alter table public.quiz_attempts enable row level security;
alter table public.quiz_answers  enable row level security;

-- quiz_attempts: a student can only see/insert/delete THEIR OWN attempts.
-- No UPDATE policy - attempts are immutable once recorded; Kognit's
-- backend never updates one. If that ever changes, add an explicit
-- UPDATE policy then - don't assume one is implied by this comment.
create policy "quiz_attempts_select_own"
    on public.quiz_attempts for select
    using (auth.uid() = user_id);

create policy "quiz_attempts_insert_own"
    on public.quiz_attempts for insert
    with check (auth.uid() = user_id);

-- Needed for backend/database.py's compensating rollback (deleting an
-- orphaned attempt row if the quiz_answers insert fails), and doubles as
-- the foundation for a future "delete my quiz history" student feature.
create policy "quiz_attempts_delete_own"
    on public.quiz_attempts for delete
    using (auth.uid() = user_id);

-- quiz_answers has no user_id column of its own - ownership is derived
-- through its parent quiz_attempts row.
create policy "quiz_answers_select_own"
    on public.quiz_answers for select
    using (
        exists (
            select 1 from public.quiz_attempts qa
            where qa.id = quiz_answers.attempt_id
              and qa.user_id = auth.uid()
        )
    );

create policy "quiz_answers_insert_own"
    on public.quiz_answers for insert
    with check (
        exists (
            select 1 from public.quiz_attempts qa
            where qa.id = quiz_answers.attempt_id
              and qa.user_id = auth.uid()
        )
    );

-- No quiz_answers delete policy: deleting a quiz_attempts row cascades to
-- its quiz_answers rows automatically (`on delete cascade` above), which
-- covers both the backend's rollback path and any future "delete my quiz
-- history" feature.
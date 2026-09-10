-- Kognit: Phase 6A - Student Profile data foundation.
--
-- Run this once in the Supabase SQL editor (or via the Supabase CLI), AFTER
-- 0001-0004 have already been applied.
--
-- PURELY ADDITIVE - a new table only. Nothing in quiz_attempts,
-- quiz_answers, learning_evidence, or conversation_index is touched.
--
-- PROFILE IS NOT A NEW SOURCE OF TRUTH FOR LEARNING: this table is
-- deliberately NOT merged with any of the Phase 5A-5E tables above. It
-- holds only identity/context (name, class, stream) - it is never read by
-- backend/mastery_engine.py or backend/learning_memory.py, and must not
-- become so without a separate, explicit decision.
--
-- Backend writes to this table using the STUDENT'S OWN Supabase access
-- token (never a service-role key) - see backend/database.py:
-- get_student_profile / upsert_student_profile - so the RLS policies below
-- are the actual enforcement boundary, not a formality.
--
-- ONE ROW PER STUDENT: user_id is both the primary key and the foreign key
-- to auth.users, which is what makes "exactly one profile per authenticated
-- user" a database-level guarantee rather than an application convention -
-- a second write for the same user_id can only ever be an update (see the
-- upsert-on-conflict(user_id) pattern in
-- backend/database.py:upsert_student_profile), never a duplicate row.
--
-- CLASS/STREAM VALUES ARE NOT A DB-LEVEL ENUM/CHECK CONSTRAINT ON PURPOSE:
-- validation lives in backend/main.py (VALID_PROFILE_CLASSES /
-- VALID_PROFILE_STREAMS). Keeping this validation in exactly one place
-- (Python) rather than also duplicating an independent CHECK constraint
-- here avoids two enum definitions silently drifting apart - see that
-- module's comment for the exact values that must stay in sync with the
-- frontend's class/stream dropdowns.

create table if not exists public.student_profiles (
    user_id     uuid primary key references auth.users(id) on delete cascade,
    name        text not null,
    user_class  text not null,
    stream      text not null,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

-- Auto-maintains updated_at on every UPDATE (including the UPDATE half of
-- an upsert's ON CONFLICT DO UPDATE), so the backend never has to compute
-- or trust a client-adjacent timestamp for this - see backend/database.py:
-- upsert_student_profile, which deliberately never sets updated_at itself.
create or replace function public.set_student_profiles_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists trg_student_profiles_updated_at on public.student_profiles;
create trigger trg_student_profiles_updated_at
    before update on public.student_profiles
    for each row
    execute function public.set_student_profiles_updated_at();

alter table public.student_profiles enable row level security;

-- A student can only see/create/update THEIR OWN profile. No DELETE policy
-- yet - Phase 6A does not require account/profile deletion; add one
-- explicitly if that becomes a real requirement later, do not assume it is
-- implied by this comment.
create policy "student_profiles_select_own"
    on public.student_profiles for select
    using (auth.uid() = user_id);

create policy "student_profiles_insert_own"
    on public.student_profiles for insert
    with check (auth.uid() = user_id);

create policy "student_profiles_update_own"
    on public.student_profiles for update
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);
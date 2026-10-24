-- Kloriz: Pre-Phase 10 remediation - make public.user_projects reproducible.
--
-- WHY: the browser stores notebooks/chats in public.user_projects (static/js/
-- app.js: loadProjectsFromDatabase, syncProjectToDatabase, deleteProject-
-- FromDatabase), but no migration in this repository ever defined the table or
-- its row level security. The live project's copy was created by hand, so a
-- fresh environment could not be reproduced and its RLS could not be reviewed
-- from the repository.
--
-- SAFE FOR THE LIVE DATABASE: every statement is idempotent and additive.
--   * "create table if not exists" does nothing when the table already exists.
--   * RLS is enabled (already on in the live project, per the founder's record).
--   * Policies are created ONLY IF no policy of that name exists. Policies are
--     additive (OR-ed), so these own-row-only policies cannot loosen anything.
--   * Nothing is dropped, renamed or rewritten. Existing rows are untouched.
--
-- EXPECTED SHAPE (from the frontend's payload and its onConflict: "id" upsert):
--   id          text primary key  (client-generated, e.g. "proj_<timestamp>")
--   user_id     uuid  -> auth.users(id)
--   title       text
--   chats       jsonb (the notebook's chats and messages)
--   updated_at  timestamptz
--
-- AFTER RUNNING IT on the live project, run the user_projects section of
-- supabase/verify_schema.sql: it lists every policy on the table that is NOT
-- scoped to auth.uid() = user_id (a looser hand-made policy would still be in
-- force and must be removed by the founder). This migration cannot safely
-- decide that on its own.
--
-- KNOWN LIMITATION (unchanged here): chat history lives in a client-owned JSONB
-- column, so the server cannot independently verify conversation content. A
-- normalised chats/messages model is a later, evidence-driven change.

create table if not exists public.user_projects (
    id          text primary key,
    user_id     uuid not null references auth.users(id) on delete cascade,
    title       text not null default 'New Notebook',
    chats       jsonb not null default '[]'::jsonb,
    updated_at  timestamptz not null default now()
);

create index if not exists user_projects_user_updated_idx
    on public.user_projects (user_id, updated_at desc);

alter table public.user_projects enable row level security;

do $$
begin
    if not exists (select 1 from pg_policies where schemaname = 'public'
                   and tablename = 'user_projects' and policyname = 'user_projects_select_own') then
        create policy user_projects_select_own on public.user_projects
            for select to authenticated using (auth.uid() = user_id);
    end if;
    if not exists (select 1 from pg_policies where schemaname = 'public'
                   and tablename = 'user_projects' and policyname = 'user_projects_insert_own') then
        create policy user_projects_insert_own on public.user_projects
            for insert to authenticated with check (auth.uid() = user_id);
    end if;
    if not exists (select 1 from pg_policies where schemaname = 'public'
                   and tablename = 'user_projects' and policyname = 'user_projects_update_own') then
        create policy user_projects_update_own on public.user_projects
            for update to authenticated using (auth.uid() = user_id) with check (auth.uid() = user_id);
    end if;
    if not exists (select 1 from pg_policies where schemaname = 'public'
                   and tablename = 'user_projects' and policyname = 'user_projects_delete_own') then
        create policy user_projects_delete_own on public.user_projects
            for delete to authenticated using (auth.uid() = user_id);
    end if;
end
$$;
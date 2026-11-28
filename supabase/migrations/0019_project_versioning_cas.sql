-- Kloriz: version-checked ("compare-and-set") saving of notebooks / chats (batch B06).
--
-- Run this once in the Supabase SQL editor, AFTER 0001-0018. SAFE TO RE-RUN.
-- THIS FILE HAS NOT BEEN APPLIED TO ANY SUPABASE PROJECT: it was run only against a
-- throwaway local PostgreSQL (supabase/tests/project_cas_0019_test.sql and
-- concurrency_project_cas_test.sh). Review it, then apply it yourself.
--
-- THE PROBLEM THIS FIXES (verified in static/js/app.js, syncProjectToDatabase)
--   The browser saves a whole notebook - EVERY chat in it, as one JSON value - with a
--   plain upsert and a client-chosen updated_at. Two tabs, or a phone and a laptop,
--   that both have the notebook open overwrite each other: whoever saves last silently
--   erases the other's chats. Nothing detects it and nothing can recover it.
--
-- THE FIX, IN ONE SENTENCE
--   Every notebook row carries a `version`; a save says "I last saw version N" and is
--   accepted ONLY if the row is still at version N, in one atomic step. Otherwise the
--   server answers `conflict` with its current copy and writes nothing, so the app can
--   merge and retry without losing anyone's work.
--
-- WHAT CHANGES
--   1. user_projects.version bigint NOT NULL DEFAULT 1.   Existing rows become version 1.
--   2. A trigger that bumps `version` on ANY update that did not bump it itself. This
--      covers OLD CACHED COPIES of the web app (and any direct write) that still use the
--      plain upsert: their writes can no longer slip past a version check unnoticed.
--   3. public.save_user_project(...)  - the atomic compare-and-set save (below).
--
-- NOTHING ELSE CHANGES: same table, same RLS policies, same privileges, same ids. Old
-- ids ("proj_<ms>", "chat_<ms>", "proj_recent_default_<uuid>") stay valid; new ids are
-- UUIDs (the column is text, so both live side by side). No data is rewritten.
--
-- THE FUNCTION IS SECURITY INVOKER ON PURPOSE
--   It runs with the CALLER's rights, so Row Level Security (user_id = auth.uid()) is
--   enforced by the database itself exactly as for the old upsert: a student can only
--   ever read or change their own rows. There is no way to name another user.
--
-- RESULT (jsonb), always with a "status":
--   ok              saved. {version, updated_at}. `created: true` when a new notebook.
--   conflict        someone saved in between. {server: {id,title,chats,version,updated_at}}
--                   - nothing was written.
--   not_found       the caller expected an existing notebook but it is gone (deleted on
--                   another device). Nothing was written; the app decides what to do.
--   id_unavailable  a NEW notebook id is already used by a notebook the caller does not
--                   own. Nothing is revealed about it.
-- RETRY SAFETY: if a save committed but the reply was lost, repeating the SAME save
-- returns ok (the stored content already equals what is being saved) instead of a
-- false conflict - so a retry never duplicates or bumps twice.
-- ERRORS (raised): 28000 not signed in; 22023 invalid id / chats not an array;
-- 54000 payload larger than 10 MB.

alter table public.user_projects
    add column if not exists version bigint not null default 1;

do $$
begin
    if not exists (select 1 from pg_constraint
                    where conrelid = 'public.user_projects'::regclass
                      and conname = 'user_projects_version_positive') then
        alter table public.user_projects
            add constraint user_projects_version_positive check (version >= 1);
    end if;
end $$;

-- Any update that leaves `version` unchanged is, by definition, a writer that does not
-- know about versions (an old cached client, a script). Count it as a new version so a
-- compare-and-set client can never miss it.
create or replace function public.user_projects_bump_version()
returns trigger
language plpgsql
security invoker
set search_path = public, pg_temp
as $$
begin
    if new.version is not distinct from old.version then
        new.version := old.version + 1;
    end if;
    return new;
end;
$$;

drop trigger if exists user_projects_bump_version_trg on public.user_projects;
create trigger user_projects_bump_version_trg
    before update on public.user_projects
    for each row execute function public.user_projects_bump_version();

create or replace function public.save_user_project(
    p_id               text,
    p_title            text,
    p_chats            jsonb,
    p_expected_version bigint
)
returns jsonb
language plpgsql
security invoker
set search_path = public, pg_temp
as $$
declare
    v_uid   uuid := auth.uid();
    v_title text;
    r       public.user_projects%rowtype;
begin
    if v_uid is null then
        raise exception 'not authenticated' using errcode = '28000';
    end if;
    if p_id is null or p_id !~ '^[A-Za-z0-9_-]{1,100}$' then
        raise exception 'invalid project id' using errcode = '22023';
    end if;
    if p_chats is null or jsonb_typeof(p_chats) <> 'array' then
        raise exception 'chats must be a JSON array' using errcode = '22023';
    end if;
    if octet_length(p_chats::text) > 10485760 then
        raise exception 'project is too large' using errcode = '54000';
    end if;
    if p_expected_version is not null and p_expected_version < 1 then
        raise exception 'invalid expected version' using errcode = '22023';
    end if;

    -- Same trimming rule the app uses; a blank title never reaches the NOT NULL column.
    v_title := left(coalesce(nullif(btrim(p_title), ''), 'Untitled notebook'), 200);

    -- Serialise saves of THIS notebook by THIS user. Two simultaneous saves with the same
    -- expected version are then decided strictly one after the other: exactly one wins.
    perform pg_advisory_xact_lock(hashtextextended('user_projects:' || v_uid::text || ':' || p_id, 0));

    -- RLS limits this to the caller's own row.
    select * into r from public.user_projects where id = p_id and user_id = v_uid;

    if not found then
        if p_expected_version is not null then
            return jsonb_build_object('status', 'not_found');
        end if;
        insert into public.user_projects (id, user_id, title, chats, version)
             values (p_id, v_uid, v_title, p_chats, 1)
        on conflict (id) do nothing
          returning * into r;
        if not found then
            -- The id exists but belongs to someone else (RLS hid the row above).
            return jsonb_build_object('status', 'id_unavailable');
        end if;
        return jsonb_build_object('status', 'ok', 'created', true, 'version', r.version, 'updated_at', r.updated_at);
    end if;

    -- The row exists and is the caller's.
    if p_expected_version is not null and r.version = p_expected_version then
        update public.user_projects
           set title = v_title, chats = p_chats, version = r.version + 1, updated_at = now()
         where id = p_id and user_id = v_uid
        returning * into r;
        return jsonb_build_object('status', 'ok', 'version', r.version, 'updated_at', r.updated_at);
    end if;

    -- Not at the expected version. Two harmless cases first: the exact same content is
    -- already stored (the earlier attempt committed and only the reply was lost, or the
    -- caller thought it was creating a notebook that is already there with this content).
    if r.title = v_title and r.chats = p_chats
       and (p_expected_version is null or r.version = p_expected_version + 1) then
        return jsonb_build_object('status', 'ok', 'replayed', true, 'version', r.version, 'updated_at', r.updated_at);
    end if;

    -- A real conflict: hand back the server's copy so the app can merge. Nothing written.
    return jsonb_build_object(
        'status', 'conflict',
        'server', jsonb_build_object('id', r.id, 'title', r.title, 'chats', r.chats,
                                     'version', r.version, 'updated_at', r.updated_at));
end;
$$;

-- Executable by signed-in students only (a new function is PUBLIC-executable by default).
revoke all on function public.save_user_project(text, text, jsonb, bigint) from public, anon, authenticated;
grant execute on function public.save_user_project(text, text, jsonb, bigint) to authenticated;
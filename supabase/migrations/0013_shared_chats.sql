-- Kloriz: opt-in, revocable chat sharing (snapshot model).
--
-- Run this once in the Supabase SQL editor (or via the Supabase CLI), AFTER
-- 0001-0012 have already been applied. THIS FILE HAS NOT BEEN APPLIED BY
-- ANYONE - the engineer who prepared it never touched a live database.
-- Review it, then apply it yourself. Until it is applied the app keeps working:
-- the share endpoints answer 503 SHARING_UNAVAILABLE and the dialog says so.
--
-- PURELY ADDITIVE - one new table, three small functions. Nothing in
-- user_projects, quiz_*, learning_*, pocket_notes, plans or quota tables is
-- touched, and no existing policy or privilege changes.
--
-- WHAT A "SHARE" IS
--   A frozen COPY (snapshot) of one chat, created only when the student presses
--   "Create link" after previewing it. The link never points at the student's
--   real chat or project: the recipient reads this table's snapshot column and
--   nothing else. Later messages in the original chat are NOT included, and the
--   student's private user_projects row is never exposed - there is no
--   authorization path from a share token to the original chat.
--
-- THE TOKEN
--   share_token is generated HERE (BEFORE INSERT trigger), 40 hex characters
--   from two gen_random_uuid() values (160 bits from the database CSPRNG). Any
--   value a client sends for it is overwritten, so a client can neither choose
--   a weak token nor probe for a known one. It is the only credential: anyone
--   who has the link can read the snapshot, which is exactly what the dialog
--   tells the student before the link is created.
--
-- REVOCATION = DELETE
--   "Stop sharing" deletes the row. The content is gone immediately (no
--   soft-delete copy lingers), and a revoked link is indistinguishable from a
--   link that never existed - the public function returns null for both, so a
--   probe learns nothing. Deleting the account cascades.
--
-- WHO CAN DO WHAT
--   owner (authenticated)  select / insert / delete OWN rows (RLS). No UPDATE
--                          privilege at all: a snapshot can not be edited after
--                          the preview the student approved.
--   anyone (anon + auth)   may only call get_shared_chat(token); anon has NO
--                          direct table privilege and RLS has no anon policy,
--                          so the table can not be listed or enumerated.
--   The backend uses the student's OWN token for owner operations (never a
--   service-role key) and the public anon key only for get_shared_chat().
--
-- LIMITS (also enforced in backend/share_engine.py; change both together)
--   1 share per (user, chat)           - unique (user_id, chat_ref)
--   at most 50 shares per user         - trigger below
--   1..200 messages, <= 500,000 chars  - CHECK constraints
--   title 1..120 chars
--
-- WHAT IS DELIBERATELY NOT STORED
--   No email, name, user_id in anything the public function returns, no
--   images (the backend strips them), no research/source metadata, no
--   timestamps of individual messages. Only: title, role, text.

create table if not exists public.shared_chats (
    id            uuid primary key default gen_random_uuid(),
    user_id       uuid not null references auth.users(id) on delete cascade,
    chat_ref      text not null,
    share_token   text not null,
    title         text not null,
    snapshot      jsonb not null,
    message_count integer not null,
    created_at    timestamptz not null default now(),

    constraint shared_chats_token_format
        check (share_token ~ '^[0-9a-f]{40}$'),
    constraint shared_chats_chat_ref_format
        check (chat_ref ~ '^[A-Za-z0-9_-]{1,80}$'),
    constraint shared_chats_title_length
        check (char_length(title) between 1 and 120),
    constraint shared_chats_snapshot_shape
        check (jsonb_typeof(snapshot) = 'array'
               and jsonb_array_length(snapshot) between 1 and 200
               and char_length(snapshot::text) <= 500000),
    constraint shared_chats_message_count_range
        check (message_count between 1 and 200),
    constraint shared_chats_token_unique unique (share_token),
    constraint shared_chats_one_per_chat unique (user_id, chat_ref)
);

create index if not exists shared_chats_user_created_idx
    on public.shared_chats (user_id, created_at desc);

-- ---------------------------------------------------------------------------
-- BEFORE INSERT: server-side token + per-user cap. Runs for every insert path,
-- including a client that talks to PostgREST directly and skips the backend.
-- ---------------------------------------------------------------------------
create or replace function public.shared_chats_before_insert()
returns trigger
language plpgsql
set search_path = public, pg_temp
as $$
declare
    v_count integer;
begin
    select count(*) into v_count from public.shared_chats where user_id = new.user_id;
    if v_count >= 50 then
        raise exception 'share limit reached' using errcode = '53400';
    end if;

    -- Always overwrite: the client never chooses its own token.
    new.share_token := substr(
        replace(gen_random_uuid()::text || gen_random_uuid()::text, '-', ''),
        1, 40
    );
    return new;
end;
$$;

drop trigger if exists trg_shared_chats_before_insert on public.shared_chats;
create trigger trg_shared_chats_before_insert
    before insert on public.shared_chats
    for each row
    execute function public.shared_chats_before_insert();

-- ---------------------------------------------------------------------------
-- RLS + privileges
-- ---------------------------------------------------------------------------
alter table public.shared_chats enable row level security;

drop policy if exists "shared_chats_select_own" on public.shared_chats;
create policy "shared_chats_select_own"
    on public.shared_chats for select
    using (auth.uid() = user_id);

drop policy if exists "shared_chats_insert_own" on public.shared_chats;
create policy "shared_chats_insert_own"
    on public.shared_chats for insert
    with check (auth.uid() = user_id);

drop policy if exists "shared_chats_delete_own" on public.shared_chats;
create policy "shared_chats_delete_own"
    on public.shared_chats for delete
    using (auth.uid() = user_id);

-- No UPDATE policy and no UPDATE privilege: a snapshot is immutable.
revoke all on public.shared_chats from anon, authenticated;
grant select, insert, delete on public.shared_chats to authenticated;

-- ---------------------------------------------------------------------------
-- The ONLY public read path.
-- Returns {title, snapshot, message_count, created_at} for a live token, and
-- null for anything else (malformed, unknown, or revoked) - callers can not
-- tell those apart. Never returns user_id, chat_ref or the internal id.
-- ---------------------------------------------------------------------------
create or replace function public.get_shared_chat(p_token text)
returns jsonb
language sql
stable
security definer
set search_path = public, pg_temp
as $$
    select jsonb_build_object(
               'title',         s.title,
               'snapshot',      s.snapshot,
               'message_count', s.message_count,
               'created_at',    s.created_at
           )
    from public.shared_chats s
    where p_token ~ '^[0-9a-f]{40}$'
      and s.share_token = p_token
    limit 1
$$;

revoke all on function public.get_shared_chat(text) from public;
grant execute on function public.get_shared_chat(text) to anon, authenticated;

-- ---------------------------------------------------------------------------
-- ROLLBACK (run manually if you ever need to remove the feature; this deletes
-- every shared snapshot and breaks every shared link):
--
--   drop function if exists public.get_shared_chat(text);
--   drop trigger  if exists trg_shared_chats_before_insert on public.shared_chats;
--   drop function if exists public.shared_chats_before_insert();
--   drop table    if exists public.shared_chats;
-- ---------------------------------------------------------------------------
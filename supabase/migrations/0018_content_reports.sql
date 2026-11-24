-- Kloriz: student reports of AI-generated content (batch B02).
--
-- Run this once in the Supabase SQL editor (or via the Supabase CLI), AFTER
-- 0001-0017 have already been applied. THIS FILE HAS NOT BEEN APPLIED TO ANY
-- SUPABASE PROJECT - it was run only against a throwaway local PostgreSQL by the
-- engineer who prepared it (see supabase/tests/content_reports_0018_test.sql).
-- Review it, then apply it yourself. Until it is applied the app keeps working:
-- POST /api/report answers 503 REPORTING_UNAVAILABLE and the report dialog says so.
--
-- WHY THIS EXISTS
--   Google Play's policy for apps that generate content with AI expects an
--   in-app way for a user to report offensive or harmful AI output. A student
--   presses "Report" on an answer, picks a reason, and the report lands here for
--   a human to review.
--
-- PURELY ADDITIVE - one new table and one function. No existing table, policy,
-- privilege or function is changed.
--
-- WHO CAN DO WHAT (this is the whole security model)
--   student (authenticated)  can call submit_content_report(...) and NOTHING
--                            else. There is no table privilege at all: a student
--                            can not SELECT, UPDATE or DELETE a report - not even
--                            their own - and can not INSERT directly. A report,
--                            once sent, is a record the student can not alter or
--                            erase (otherwise a report of harmful content could be
--                            hidden again).
--   anonymous (anon)         can do nothing: no table privilege, no EXECUTE.
--   reviewers                read and update reports in the Supabase dashboard
--                            (or with the service role on a server you control).
--                            Row level security is ON with no policy, so every
--                            non-privileged role sees zero rows even if a grant is
--                            added by mistake later.
--
-- WHY A FUNCTION INSTEAD OF A TABLE INSERT POLICY
--   A direct INSERT policy would let a client choose every column, including
--   user_id, status and created_at, and would give no way to cap how many
--   reports one account can file. The function (SECURITY DEFINER, pinned
--   search_path):
--     * takes the reporter from auth.uid() - there is no user_id parameter, so a
--       client can never report "as" someone else;
--     * forces status = 'new' and created_at = now();
--     * enforces a per-user cap of 20 reports per rolling 24 hours IN THE
--       DATABASE, so a client that skips the backend still can not spam. A
--       per-user advisory lock makes the cap exact under concurrent requests.
--
-- WHAT IS DELIBERATELY STORED - AND WHAT IS NOT
--   Stored:  category, an optional note (<= 500 chars), an excerpt of the AI
--            answer being reported (<= 2000 chars), an optional excerpt of the
--            question that produced it (<= 1000 chars), the request id (to find
--            the server log line), platform (web/android) and app version.
--   Not stored: no email, no name, no IP address, no device identifiers, no
--            chat or project ids, no attachments or PDF text. The reporter is
--            identified only by user_id, which is needed to apply the daily cap
--            and to follow up on abuse.
--
-- ACCOUNT DELETION
--   user_id references auth.users ON DELETE CASCADE, so deleting an account also
--   deletes the reports that account filed (data minimisation). If counsel later
--   decides that safety reports must be retained after account deletion, change
--   this to ON DELETE SET NULL (and drop NOT NULL) in a NEW migration - and
--   record that decision in the privacy documentation.
--
-- STATUS VALUES   new -> reviewing -> actioned | dismissed   (set by reviewers)

create table if not exists public.content_reports (
    id                  uuid primary key default gen_random_uuid(),
    user_id             uuid not null references auth.users(id) on delete cascade,
    category            text not null,
    note                text,
    ai_response_excerpt text not null,
    prompt_excerpt      text,
    request_id          text,
    platform            text not null default 'web',
    app_version         text,
    status              text not null default 'new',
    created_at          timestamptz not null default now(),

    constraint content_reports_category_valid
        check (category in ('unsafe', 'inappropriate', 'inaccurate', 'privacy', 'other')),
    constraint content_reports_note_length
        check (note is null or char_length(note) between 1 and 500),
    constraint content_reports_response_length
        check (char_length(ai_response_excerpt) between 1 and 2000),
    constraint content_reports_prompt_length
        check (prompt_excerpt is null or char_length(prompt_excerpt) between 1 and 1000),
    constraint content_reports_request_id_format
        check (request_id is null or request_id ~ '^[A-Za-z0-9_.-]{1,64}$'),
    constraint content_reports_platform_valid
        check (platform in ('web', 'android')),
    constraint content_reports_app_version_format
        check (app_version is null or app_version ~ '^[0-9A-Za-z._+-]{1,32}$'),
    constraint content_reports_status_valid
        check (status in ('new', 'reviewing', 'actioned', 'dismissed'))
);

-- The per-user daily cap reads "this user's reports in the last 24 hours".
create index if not exists content_reports_user_created_idx
    on public.content_reports (user_id, created_at desc);

-- The reviewer's work queue: newest unreviewed reports first.
create index if not exists content_reports_status_created_idx
    on public.content_reports (status, created_at desc);

-- ---------------------------------------------------------------------------
-- RLS + privileges: ON, with NO policy, and NO grant to students or anon.
-- ---------------------------------------------------------------------------
alter table public.content_reports enable row level security;

revoke all on public.content_reports from public, anon, authenticated;

-- ---------------------------------------------------------------------------
-- The ONLY write path for students.
-- Returns the new report's id. Raises:
--   28000  no authenticated user (auth.uid() is null)
--   53400  the user already filed 20 reports in the last 24 hours
--   23514  a value broke a CHECK constraint (the backend validates first, so
--          this only happens if something calls the function directly)
-- ---------------------------------------------------------------------------
create or replace function public.submit_content_report(
    p_category    text,
    p_note        text,
    p_ai_response text,
    p_prompt      text,
    p_request_id  text,
    p_platform    text,
    p_app_version text
)
returns uuid
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_uid   uuid := auth.uid();
    v_count integer;
    v_id    uuid;
begin
    if v_uid is null then
        raise exception 'not authenticated' using errcode = '28000';
    end if;

    -- Serialise this user's submissions so the cap below is exact even when many
    -- requests arrive at the same instant. Released automatically at commit.
    perform pg_advisory_xact_lock(hashtextextended('content_reports:' || v_uid::text, 0));

    select count(*) into v_count
      from public.content_reports
     where user_id = v_uid
       and created_at > now() - interval '24 hours';
    if v_count >= 20 then
        raise exception 'report limit reached' using errcode = '53400';
    end if;

    insert into public.content_reports
        (user_id, category, note, ai_response_excerpt, prompt_excerpt,
         request_id, platform, app_version)
    values
        (v_uid, p_category, p_note, p_ai_response, p_prompt,
         p_request_id, coalesce(p_platform, 'web'), p_app_version)
    returning id into v_id;

    return v_id;
end;
$$;

-- A new function is executable by PUBLIC by default. Close that, then open it to
-- signed-in students only.
revoke all on function public.submit_content_report(text, text, text, text, text, text, text)
    from public, anon, authenticated;
grant execute on function public.submit_content_report(text, text, text, text, text, text, text)
    to authenticated;
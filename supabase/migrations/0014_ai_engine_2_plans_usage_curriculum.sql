-- ===========================================================================
-- 0014  AI Engine 2.0: Free/Pro limits, plan lookup, per-attempt usage
--       metadata, and the student's curriculum medium.
--
-- ADDITIVE AND IDEMPOTENT. Nothing is dropped except two CHECK constraints that
-- are re-created immediately, wider. Safe to apply twice. Old application code
-- keeps working against the new schema (every new column is nullable and the
-- writer function ignores nothing it used to accept).
--
-- DEPLOY ORDER: apply this migration FIRST, then deploy the application. The
-- new application sends the extra usage fields; an old database would reject
-- the unknown ones only if they were validated - they are not, but they would
-- simply not be stored, and `get_my_plan()` would be missing (feature gating
-- would fail closed with a 503). So: database first.
--
-- ROLLBACK: set PLAN_GATING=off and AI_* flags back in the application (no
-- schema change needed). If the schema itself must be reverted:
--   delete from public.plan_limits where plan = 'pro';          -- Pro becomes plan_not_configured again
--   drop function if exists public.get_my_plan();
--   alter table public.student_profiles drop column if exists curriculum;
--   (ai_usage_events columns can simply stay; they are nullable.)
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- 1. Pro plan limits (CEO decision D-4)
-- ---------------------------------------------------------------------------
--   Pro chat   : 20 per day
--   Pro quiz   : 15 per day. A quiz generated from a saved note counts here
--                (the application routes note quizzes to the 'quiz' meter).
--   Pro notes_ai: 200 per day - an ABUSE GUARD standing in for "unlimited
--                flashcards" (quotas are integers); no student reaches it by
--                studying. It is not a product limit and may be raised.
-- Free limits are unchanged (chat 1/day with 3 on the first day, quiz 3/week,
-- notes_ai 6/day). The Free quiz reset period is NOT specified by the CEO; it
-- stays 'week' and is one UPDATE away from changing:
--   update public.plan_limits set period = 'day' where plan='free' and kind='quiz';
--
-- Ledger growth cap (0012): 500 rows / user / 24 h. Pro worst case here is
-- 20 + 15 + 200 = 235 counted rows, so the cap is not reached.
insert into public.plan_limits (plan, kind, period, limit_count, first_day_limit_count) values
    ('pro', 'chat',     'day', 20,  null),
    ('pro', 'quiz',     'day', 15,  null),
    ('pro', 'notes_ai', 'day', 200, null)
on conflict (plan, kind) do nothing;

-- ---------------------------------------------------------------------------
-- 2. get_my_plan(): the caller's current plan, from the database
-- ---------------------------------------------------------------------------
-- Same rule consume_quota() uses (an active, in-date subscription => 'pro').
-- Used by the backend to gate Pro-only features server-side. SECURITY DEFINER
-- only so it can read public.subscriptions (which students cannot read
-- directly); it returns nothing but the caller's OWN plan label.
create or replace function public.get_my_plan()
returns text
language plpgsql
stable
security definer
set search_path = public, pg_temp
as $$
declare
    v_uid uuid := auth.uid();
    v_now timestamptz := now();
begin
    if v_uid is null then
        raise exception 'not authenticated' using errcode = '28000';
    end if;
    if exists (
        select 1 from public.subscriptions s
        where s.user_id = v_uid and s.status = 'active'
          and s.starts_at <= v_now and s.expires_at > v_now
    ) then
        return 'pro';
    end if;
    return 'free';
end;
$$;

revoke all on function public.get_my_plan() from public, anon;
grant execute on function public.get_my_plan() to authenticated;

-- ---------------------------------------------------------------------------
-- 3. Per-attempt usage metadata
-- ---------------------------------------------------------------------------
-- One row per generation ATTEMPT (primary, retry, recovery, fallback, verify),
-- not one per request. All new columns are nullable; nothing here stores
-- prompts, answers, documents or free text.
alter table public.ai_usage_events add column if not exists attempt_kind          text;
alter table public.ai_usage_events add column if not exists task_category         text;
alter table public.ai_usage_events add column if not exists route_reason          text;
alter table public.ai_usage_events add column if not exists route_policy_version  text;
alter table public.ai_usage_events add column if not exists fallback_from         text;
alter table public.ai_usage_events add column if not exists verification_status   text;
alter table public.ai_usage_events add column if not exists cached_tokens         integer;

alter table public.ai_usage_events drop constraint if exists ai_usage_events_attempt_kind_check;
alter table public.ai_usage_events add constraint ai_usage_events_attempt_kind_check
    check (attempt_kind is null or attempt_kind in ('primary', 'retry', 'recovery', 'fallback', 'verify'));

alter table public.ai_usage_events drop constraint if exists ai_usage_events_verification_status_check;
alter table public.ai_usage_events add constraint ai_usage_events_verification_status_check
    check (verification_status is null or verification_status in ('correct', 'incorrect', 'unsure', 'error'));

alter table public.ai_usage_events drop constraint if exists ai_usage_events_cached_tokens_check;
alter table public.ai_usage_events add constraint ai_usage_events_cached_tokens_check
    check (cached_tokens is null or cached_tokens >= 0);

alter table public.ai_usage_events drop constraint if exists ai_usage_events_new_label_lengths;
alter table public.ai_usage_events add constraint ai_usage_events_new_label_lengths
    check (coalesce(length(task_category), 0) <= 32
       and coalesce(length(route_reason), 0) <= 64
       and coalesce(length(route_policy_version), 0) <= 32
       and coalesce(length(fallback_from), 0) <= 64);

-- 'verify' (shadow verification) joins the allowed operations.
alter table public.ai_usage_events drop constraint if exists ai_usage_events_operation_check;
alter table public.ai_usage_events add constraint ai_usage_events_operation_check
    check (operation in ('chat', 'stream', 'quiz', 'title', 'notes', 'verify'));

-- Helper: a bounded non-negative numeric from the event JSON (cost in USD).
create or replace function public._usage_numeric(p jsonb, k text, maxv numeric)
returns numeric
language plpgsql
immutable
set search_path = pg_catalog, public
as $$
declare
    v jsonb := p -> k;
    n numeric;
begin
    if v is null or jsonb_typeof(v) = 'null' then
        return null;
    end if;
    if jsonb_typeof(v) <> 'number' then
        raise exception 'invalid usage field %', k using errcode = '22023';
    end if;
    n := (v #>> '{}')::numeric;
    if n < 0 or n > maxv then
        raise exception 'invalid usage field %', k using errcode = '22023';
    end if;
    return n;
end;
$$;
revoke all on function public._usage_numeric(jsonb, text, numeric) from public, anon, authenticated;

-- record_ai_usage_events: identical to 0011/0012 plus the new fields.
create or replace function public.record_ai_usage_events(
    p_write_key  text,
    p_events     jsonb
) returns integer
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_uid     uuid := auth.uid();
    v_now     timestamptz := now();
    v_plan    text := 'free';
    v_recent  integer;
    v_n       integer;
    e         jsonb;
    v_op      text;
    v_status  text;
    v_kind    text;
    v_verif   text;
    v_inserted integer := 0;
begin
    if v_uid is null then
        raise exception 'not authenticated' using errcode = '28000';
    end if;
    if p_write_key is null or length(p_write_key) < 16 or not exists (
        select 1 from public.usage_write_keys k
        where k.key_hash = encode(sha256(convert_to(p_write_key, 'UTF8')), 'hex')
    ) then
        raise exception 'usage writer not authorised' using errcode = '42501';
    end if;
    if p_events is null or jsonb_typeof(p_events) <> 'array' then
        raise exception 'events must be a json array' using errcode = '22023';
    end if;
    v_n := jsonb_array_length(p_events);
    if v_n = 0 or v_n > 20 then
        raise exception 'events must contain 1 to 20 items' using errcode = '22023';
    end if;

    -- Row-growth cap per user (silent: telemetry never breaks an answer).
    select count(*) into v_recent from public.ai_usage_events
    where user_id = v_uid and created_at > v_now - interval '24 hours';
    if v_recent + v_n > 1000 then
        return 0;
    end if;

    if exists (
        select 1 from public.subscriptions s
        where s.user_id = v_uid and s.status = 'active'
          and s.starts_at <= v_now and s.expires_at > v_now
    ) then
        v_plan := 'pro';
    end if;

    for e in select value from jsonb_array_elements(p_events) loop
        if jsonb_typeof(e) <> 'object' then
            raise exception 'each event must be an object' using errcode = '22023';
        end if;
        v_op := public._usage_slug(e, 'operation', 16);
        v_status := public._usage_slug(e, 'status', 16);
        if v_op is null or v_op not in ('chat', 'stream', 'quiz', 'title', 'notes', 'verify')
           or v_status is null or v_status not in ('success', 'error', 'interrupted') then
            raise exception 'invalid operation or status' using errcode = '22023';
        end if;
        v_kind := public._usage_slug(e, 'attempt_kind', 16);
        if v_kind is not null and v_kind not in ('primary', 'retry', 'recovery', 'fallback', 'verify') then
            raise exception 'invalid usage field attempt_kind' using errcode = '22023';
        end if;
        v_verif := public._usage_slug(e, 'verification_status', 16);
        if v_verif is not null and v_verif not in ('correct', 'incorrect', 'unsure', 'error') then
            raise exception 'invalid usage field verification_status' using errcode = '22023';
        end if;
        if jsonb_typeof(e -> 'recovery_used') not in ('boolean', 'null') and (e ? 'recovery_used') then
            raise exception 'invalid usage field recovery_used' using errcode = '22023';
        end if;

        insert into public.ai_usage_events (
            user_id, request_id, plan, provider, model, operation, attempt,
            input_tokens, output_tokens, thinking_tokens, total_tokens, cached_tokens,
            duration_ms, ttft_ms, status, error_code, retry_count, recovery_used,
            attempt_kind, task_category, route_reason, route_policy_version, fallback_from,
            verification_status,
            estimated_cost_usd, pricing_version, created_at
        ) values (
            v_uid,
            public._usage_slug(e, 'request_id', 64),
            v_plan,
            coalesce(public._usage_slug(e, 'provider', 32), 'gemini'),
            public._usage_slug(e, 'model', 64),
            v_op,
            public._usage_int(e, 'attempt', 20),
            public._usage_int(e, 'input_tokens', 10000000),
            public._usage_int(e, 'output_tokens', 10000000),
            public._usage_int(e, 'thinking_tokens', 10000000),
            public._usage_int(e, 'total_tokens', 20000000),
            public._usage_int(e, 'cached_tokens', 10000000),
            public._usage_int(e, 'duration_ms', 3600000),
            public._usage_int(e, 'ttft_ms', 3600000),
            v_status,
            public._usage_slug(e, 'error_code', 64),
            public._usage_int(e, 'retry_count', 20),
            coalesce((e ->> 'recovery_used')::boolean, false),
            v_kind,
            public._usage_slug(e, 'task_category', 32),
            public._usage_slug(e, 'route_reason', 64),
            public._usage_slug(e, 'route_policy_version', 32),
            public._usage_slug(e, 'fallback_from', 64),
            v_verif,
            public._usage_numeric(e, 'estimated_cost_usd', 1000),
            public._usage_slug(e, 'pricing_version', 64),
            v_now
        );
        v_inserted := v_inserted + 1;
    end loop;
    return v_inserted;
end;
$$;

revoke all on function public.record_ai_usage_events(text, jsonb) from public, anon;
grant execute on function public.record_ai_usage_events(text, jsonb) to authenticated;

-- ---------------------------------------------------------------------------
-- 4. Curriculum medium on the student profile (CEO decision D-7)
-- ---------------------------------------------------------------------------
-- Nullable on purpose: existing students have not chosen one, and the product
-- must never silently assign it. NULL means "not recorded" and the AI is told
-- not to assume a medium. Cambridge/Edexcel are deliberately NOT allowed yet.
-- The profile RLS policies (0005) already restrict rows to the owner, and the
-- API validates the value; the CHECK is the last line of defence.
alter table public.student_profiles add column if not exists curriculum text;
alter table public.student_profiles drop constraint if exists student_profiles_curriculum_check;
alter table public.student_profiles add constraint student_profiles_curriculum_check
    check (curriculum is null or curriculum in ('nctb_bangla', 'nctb_english'));
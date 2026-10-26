-- Kloriz: pre-Phase 10 finalization - separate Notes/title AI meter (Q-05),
-- trusted-only usage telemetry (Q-06), Pro-limit guard (Q-08), function
-- hardening (Q-13).
--
-- Run AFTER 0010_user_projects_baseline.sql. SAFE TO RE-RUN (constraints are
-- dropped-and-recreated by name, functions are "create or replace", the seed row
-- uses "on conflict do nothing" so a later edit by the founder is never
-- overwritten). It deletes NO data and rewrites NO applied migration: 0008 and
-- 0009 stay exactly as they were applied; this file supersedes the parts of them
-- described below.
--
-- ===========================================================================
-- 1. NEW QUOTA KIND  'notes_ai'   (Q-05)
-- ===========================================================================
-- Counts the AI operations that are NOT a chat and NOT a quiz:
--     POST /api/chat/title                      (AI chat title)
--     POST /api/notes/{id}/suggest-tags         (AI topic suggestions)
--     POST /api/notes/{id}/flashcards           (only when it really generates)
--     POST /api/notes/{id}/mini-quiz            (only when it really generates)
-- It is a separate row in plan_limits and separate ledger rows, so a title or a
-- flashcard set can never spend one of the student's chats, and vice versa.
-- consume_quota()/refund_quota() are the SAME functions as for chat/quiz (atomic,
-- advisory-locked per user+kind, token-gated refunds) - only the allowed kinds
-- list grows.
--
-- FREE LIMIT - *PROPOSED, NOT YET APPROVED BY THE CEO*:  6 per day (Dhaka day).
--   Reasoning: Free gets 1 chat per day (3 on the first day) and a title is made
--   once per chat, so titles use 1-3 of the 6; the rest leaves room for a few
--   Pocket Notes study sets per day. Each unit is one small provider call (a
--   bounded note plus a short prompt), far smaller than a chat answer, but it is
--   still real cost: worst case is 6 provider calls per Free student per day.
--   To change it, UPDATE the row (data, not code - no deploy needed):
--     update public.plan_limits set limit_count = <N>
--      where plan = 'free' and kind = 'notes_ai';
--   If you want titles NOT to count, say so before launch: that is a code change.
--
-- MISSING LIMIT BEHAVIOUR: if no plan_limits row exists for (plan, 'notes_ai'),
-- consume_quota() returns reason 'plan_not_configured' and the API answers 403
-- PLAN_NOT_CONFIGURED - the AI provider is never called. No Pro row is seeded
-- (Pro limits are still undecided); see section 3.
-- ===========================================================================
alter table public.plan_limits drop constraint if exists plan_limits_kind_check;
alter table public.plan_limits
    add constraint plan_limits_kind_check check (kind in ('chat', 'quiz', 'notes_ai'));

alter table public.quota_ledger drop constraint if exists quota_ledger_kind_check;
alter table public.quota_ledger
    add constraint quota_ledger_kind_check check (kind in ('chat', 'quiz', 'notes_ai'));

insert into public.plan_limits (plan, kind, period, limit_count, first_day_limit_count) values
    ('free', 'notes_ai', 'day', 6, null)
on conflict (plan, kind) do nothing;

-- consume_quota: identical to 0008 except the accepted kinds.
create or replace function public.consume_quota(
    p_kind               text,
    p_request_id         text,
    p_refund_token_hash  text default null
) returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_uid        uuid := auth.uid();
    v_now        timestamptz := now();
    v_local      timestamp := (now() at time zone 'Asia/Dhaka');
    v_today      date := (now() at time zone 'Asia/Dhaka')::date;
    v_plan       text := 'free';
    v_row        public.plan_limits%rowtype;
    v_limit      integer;
    v_key        text;
    v_start      timestamptz;
    v_resets     timestamptz;
    v_used       integer;
    v_created    timestamptz;
    v_existing   public.quota_ledger%rowtype;
    v_resets_txt text;
begin
    if v_uid is null then
        raise exception 'not authenticated' using errcode = '28000';
    end if;
    if p_kind is null or p_kind not in ('chat', 'quiz', 'notes_ai') then
        raise exception 'invalid kind' using errcode = '22023';
    end if;
    if p_request_id is null or length(p_request_id) not between 8 and 64 then
        raise exception 'invalid request id' using errcode = '22023';
    end if;

    -- Serialise concurrent requests of the SAME user and kind. Different users
    -- (and different kinds of the same user) never block each other.
    perform pg_advisory_xact_lock(hashtextextended(v_uid::text || ':' || p_kind, 0));

    if exists (
        select 1 from public.subscriptions s
        where s.user_id = v_uid and s.status = 'active'
          and s.starts_at <= v_now and s.expires_at > v_now
    ) then
        v_plan := 'pro';
    end if;

    select * into v_row from public.plan_limits where plan = v_plan and kind = p_kind;
    if not found then
        return jsonb_build_object(
            'allowed', false, 'reason', 'plan_not_configured',
            'plan', v_plan, 'kind', p_kind
        );
    end if;

    if v_row.period = 'week' then
        v_start := date_trunc('week', v_local) at time zone 'Asia/Dhaka';
        v_key := 'week:' || to_char(date_trunc('week', v_local), 'YYYY-MM-DD');
        v_resets := v_start + interval '7 days';
    else
        v_start := v_today::timestamp at time zone 'Asia/Dhaka';
        v_key := 'day:' || v_today::text;
        v_resets := v_start + interval '1 day';
    end if;
    v_resets_txt := to_char(v_resets at time zone 'Asia/Dhaka', 'YYYY-MM-DD"T"HH24:MI:SS"+06:00"');

    v_limit := v_row.limit_count;
    if v_row.first_day_limit_count is not null then
        select u.created_at into v_created from auth.users u where u.id = v_uid;
        if v_created is not null and (v_created at time zone 'Asia/Dhaka')::date = v_today then
            v_limit := v_row.first_day_limit_count;
        end if;
    end if;

    -- Replay of the same request id: never double-count.
    select * into v_existing from public.quota_ledger
    where user_id = v_uid and kind = p_kind and request_id = p_request_id;
    if found then
        if v_existing.refunded_at is null then
            return jsonb_build_object(
                'allowed', true, 'replay', true, 'reason', 'ok', 'plan', v_plan,
                'kind', p_kind, 'limit', v_limit, 'resets_at', v_resets_txt
            );
        end if;
        return jsonb_build_object(
            'allowed', false, 'reason', 'request_id_reused', 'plan', v_plan, 'kind', p_kind
        );
    end if;

    select count(*) into v_used from public.quota_ledger
    where user_id = v_uid and kind = p_kind and period_key = v_key and refunded_at is null;

    if v_used >= v_limit then
        return jsonb_build_object(
            'allowed', false, 'reason', 'quota_exceeded', 'plan', v_plan, 'kind', p_kind,
            'limit', v_limit, 'used', v_used, 'remaining', 0, 'resets_at', v_resets_txt
        );
    end if;

    insert into public.quota_ledger (user_id, kind, period_key, request_id, refund_token_hash)
    values (v_uid, p_kind, v_key, p_request_id, p_refund_token_hash);

    return jsonb_build_object(
        'allowed', true, 'reason', 'ok', 'plan', v_plan, 'kind', p_kind,
        'limit', v_limit, 'used', v_used + 1, 'remaining', v_limit - (v_used + 1),
        'resets_at', v_resets_txt
    );
end;
$$;

-- Grants are re-stated (create or replace keeps them, but being explicit makes
-- this file self-checking).
revoke all on function public.consume_quota(text, text, text) from public, anon;
grant execute on function public.consume_quota(text, text, text) to authenticated;

-- ===========================================================================
-- 2. TRUSTED-ONLY USAGE TELEMETRY   (Q-06)
-- ===========================================================================
-- PROBLEM (reproduced on a scratch database): 0009 let any signed-in student
-- INSERT rows into ai_usage_events for themselves. Through the public REST API
-- a student could write rows with a forged plan ('pro'), model, token counts,
-- cost, pricing_version and created_at, and could write unlimited rows.
--
-- FIX: students lose INSERT entirely. The backend writes through
-- record_ai_usage_events(), a SECURITY DEFINER function that
--   * needs the SERVER SECRET (USAGE_WRITE_KEY; only its SHA-256 is stored in
--     public.usage_write_keys). A student does not have it, so a direct REST
--     call is refused;
--   * takes the user from the verified JWT (auth.uid()) - never from the payload;
--   * derives 'plan' itself from public.subscriptions - never from the payload;
--   * forces estimated_cost_usd / pricing_version to NULL and created_at to
--     now() - those columns cannot be supplied at all;
--   * accepts only whitelisted fields, validates every type/length/range and
--     rejects the whole batch if anything is malformed (the backend sends clean
--     data, so a violation means tampering or a bug - both should be visible);
--   * takes at most 20 events per call and at most 1000 events per user per
--     rolling 24 h (extra events are dropped silently - telemetry must never
--     break an answer). 1000 is a tunable hypothesis: legitimate use is far
--     lower because quotas bound the number of AI requests.
-- It is NOT a service-role key: it can do exactly one thing - add usage rows
-- for the user whose token accompanies the call.
--
-- ONE-TIME SETUP (founder, once per environment; see docs/DEPLOYMENT.md):
--   1. generate a long random value, put it in the server env as USAGE_WRITE_KEY;
--   2. compute its SHA-256 locally and store ONLY the hash:
--        python3 -c "import hashlib,sys;print(hashlib.sha256(sys.argv[1].encode()).hexdigest())" '<key>'
--        insert into public.usage_write_keys (key_hash) values ('<that hash>');
--   Until both are done, usage rows are simply not recorded (the app logs one
--   warning); chat, quiz and quotas are unaffected. Rotate by inserting the new
--   hash, switching the env var, then deleting the old hash.
--
-- READS: unchanged and still strict - students cannot read this table at all
-- (0009 already revoked SELECT); reporting is done by the founder in the SQL
-- editor (docs/USAGE_REPORTS.sql).
--
-- EXISTING ROWS ARE NOT TOUCHED. Rows written before this migration by students
-- may be forged; docs/USAGE_REPORTS.sql has a query to list suspicious ones for
-- a deliberate decision. RETENTION: unchanged (180-day suggestion in that file).
-- ===========================================================================
create table if not exists public.usage_write_keys (
    key_hash    text primary key check (key_hash ~ '^[0-9a-f]{64}$'),
    created_at  timestamptz not null default now()
);
alter table public.usage_write_keys enable row level security;
revoke all on public.usage_write_keys from public, anon, authenticated;

drop policy if exists ai_usage_events_insert_own on public.ai_usage_events;
revoke insert, update, delete, truncate on public.ai_usage_events from public, anon, authenticated;
revoke usage, select, update on sequence public.ai_usage_events_id_seq from public, anon, authenticated;

-- Internal validators (not callable by API roles).
create or replace function public._usage_int(p jsonb, k text, maxv bigint)
returns integer
language plpgsql
immutable
set search_path = pg_catalog, public
as $$
declare
    v jsonb := p -> k;
    t text;
begin
    if v is null or jsonb_typeof(v) = 'null' then
        return null;
    end if;
    t := v #>> '{}';
    if jsonb_typeof(v) <> 'number' or t !~ '^[0-9]{1,12}$' or t::bigint > maxv then
        raise exception 'invalid usage field %', k using errcode = '22023';
    end if;
    return t::integer;
end;
$$;

create or replace function public._usage_slug(p jsonb, k text, maxlen integer)
returns text
language plpgsql
immutable
set search_path = pg_catalog, public
as $$
declare
    v jsonb := p -> k;
    t text;
begin
    if v is null or jsonb_typeof(v) = 'null' then
        return null;
    end if;
    t := v #>> '{}';
    if jsonb_typeof(v) <> 'string' or length(t) = 0 or length(t) > maxlen or t !~ '^[A-Za-z0-9_.-]+$' then
        raise exception 'invalid usage field %', k using errcode = '22023';
    end if;
    return t;
end;
$$;

revoke all on function public._usage_int(jsonb, text, bigint) from public, anon, authenticated;
revoke all on function public._usage_slug(jsonb, text, integer) from public, anon, authenticated;

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
        if v_op is null or v_op not in ('chat', 'stream', 'quiz', 'title', 'notes')
           or v_status is null or v_status not in ('success', 'error', 'interrupted') then
            raise exception 'invalid operation or status' using errcode = '22023';
        end if;
        if jsonb_typeof(e -> 'recovery_used') not in ('boolean', 'null') and (e ? 'recovery_used') then
            raise exception 'invalid usage field recovery_used' using errcode = '22023';
        end if;

        insert into public.ai_usage_events (
            user_id, request_id, plan, provider, model, operation, attempt,
            input_tokens, output_tokens, thinking_tokens, total_tokens,
            duration_ms, ttft_ms, status, error_code, retry_count, recovery_used,
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
            public._usage_int(e, 'duration_ms', 3600000),
            public._usage_int(e, 'ttft_ms', 3600000),
            v_status,
            public._usage_slug(e, 'error_code', 64),
            public._usage_int(e, 'retry_count', 20),
            coalesce((e ->> 'recovery_used')::boolean, false),
            null, null, v_now
        );
        v_inserted := v_inserted + 1;
    end loop;
    return v_inserted;
end;
$$;

revoke all on function public.record_ai_usage_events(text, jsonb) from public, anon;
grant execute on function public.record_ai_usage_events(text, jsonb) to authenticated;

-- ===========================================================================
-- 3. PRO LIMITS MUST EXIST BEFORE A PRO SUBSCRIPTION CAN BE SOLD   (Q-08)
-- ===========================================================================
-- Without ('pro', <kind>) rows consume_quota() correctly denies with
-- plan_not_configured (fail closed). But that would lock out a customer who has
-- just PAID. So activate_pro_subscription() now refuses to create a payment or
-- subscription until pro limits exist for every kind. It is still executable by
-- service_role only, and nothing in the app calls it yet. Add the rows first:
--   insert into public.plan_limits (plan, kind, period, limit_count) values
--     ('pro','chat','day',<N>), ('pro','quiz','day',<M>), ('pro','notes_ai','day',<K>);
-- Body is otherwise identical to 0008.
-- ===========================================================================
create or replace function public.activate_pro_subscription(
    p_user_id              uuid,
    p_billing_period       text,
    p_provider             text,
    p_provider_payment_id  text,
    p_amount_bdt           integer,
    p_currency             text default 'BDT'
) returns uuid
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_price        integer;
    v_pay          public.payments%rowtype;
    v_now          timestamptz := now();
    v_current_end  timestamptz;
    v_start        timestamptz;
    v_end          timestamptz;
    v_sub          uuid;
begin
    if not exists (select 1 from auth.users where id = p_user_id) then
        raise exception 'unknown user' using errcode = '22023';
    end if;
    select amount_bdt into v_price from public.plan_prices
     where plan = 'pro' and billing_period = p_billing_period;
    if v_price is null then
        raise exception 'unknown billing period' using errcode = '22023';
    end if;
    if p_currency is distinct from 'BDT' or p_amount_bdt is distinct from v_price then
        raise exception 'payment does not match the approved price' using errcode = '22023';
    end if;

    perform pg_advisory_xact_lock(hashtextextended('activate:' || p_user_id::text, 0));

    select * into v_pay from public.payments
     where provider = p_provider and provider_payment_id = p_provider_payment_id;
    if found then
        if v_pay.user_id <> p_user_id then
            raise exception 'payment belongs to another user' using errcode = '22023';
        end if;
        select id into v_sub from public.subscriptions where payment_id = v_pay.id;
        return v_sub;   -- replayed callback: no second period
    end if;

    -- Q-08 guard: never take money for a plan that would then be denied.
    if (select count(*) from public.plan_limits
         where plan = 'pro' and kind in ('chat', 'quiz', 'notes_ai')) < 3 then
        raise exception 'pro limits are not configured' using errcode = '22023';
    end if;

    insert into public.payments
        (user_id, provider, provider_payment_id, amount_bdt, currency, billing_period, status, verified_at)
    values
        (p_user_id, p_provider, p_provider_payment_id, p_amount_bdt, 'BDT', p_billing_period, 'verified', v_now)
    returning * into v_pay;

    select max(expires_at) into v_current_end from public.subscriptions
     where user_id = p_user_id and status = 'active' and expires_at > v_now;
    v_start := coalesce(v_current_end, v_now);
    v_end := v_start + case when p_billing_period = 'yearly' then interval '1 year' else interval '1 month' end;

    insert into public.subscriptions (user_id, plan, billing_period, status, starts_at, expires_at, payment_id)
    values (p_user_id, 'pro', p_billing_period, 'active', v_start, v_end, v_pay.id)
    returning id into v_sub;
    return v_sub;
end;
$$;

revoke all on function public.activate_pro_subscription(uuid, text, text, text, integer, text)
    from public, anon, authenticated;
grant execute on function public.activate_pro_subscription(uuid, text, text, text, integer, text)
    to service_role;

-- ===========================================================================
-- 4. FUNCTION HARDENING   (Q-13)
-- ===========================================================================
-- The two updated_at trigger functions had no fixed search_path and were
-- executable by API roles. A trigger function needs no EXECUTE grant to fire,
-- and a fixed search_path removes the mutable-path warning. Behaviour is
-- unchanged (they only assign now()).
alter function public.set_pocket_notes_updated_at()    set search_path = pg_catalog, public;
alter function public.set_student_profiles_updated_at() set search_path = pg_catalog, public;
revoke all on function public.set_pocket_notes_updated_at()    from public, anon, authenticated;
revoke all on function public.set_student_profiles_updated_at() from public, anon, authenticated;

-- ===========================================================================
-- ROLLBACK (forward-safe; nothing here deletes student data)
-- ===========================================================================
-- Code first: deploy the previous backend (it only uses kinds chat/quiz and the
-- old direct usage insert). Then, only if you really must:
--   * Re-enabling direct student INSERT on ai_usage_events re-opens Q-06; do not.
--     Instead leave record_ai_usage_events in place (an older backend simply
--     cannot write usage rows - that is harmless).
--   * The notes_ai constraint widening and seed row are harmless to keep. To
--     remove the meter: delete from public.plan_limits where kind='notes_ai';
--     (ledger rows are history; keep them).
--   * Dropping public.usage_write_keys / record_ai_usage_events is safe but
--     stops all usage recording.
-- This migration cannot be "undone" without re-introducing the vulnerabilities
-- it closes; the recovery path is always forward.
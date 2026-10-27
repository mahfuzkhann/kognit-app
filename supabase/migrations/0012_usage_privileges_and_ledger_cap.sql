-- Kloriz: pre-Phase 10 remediation (audit findings F-02 and F-07).
--
-- Run AFTER 0011_quota_notes_meter_and_telemetry_integrity.sql. SAFE TO RE-RUN
-- (revoke is idempotent, "create index if not exists", "create or replace function").
-- It deletes NO data and rewrites no applied migration; 0009 and 0011 stay as applied.
--
-- ===========================================================================
-- 1. LEAST PRIVILEGE ON ai_usage_events   (F-02)
-- ===========================================================================
-- 0009 revoked SELECT and 0011 revoked INSERT/UPDATE/DELETE/TRUNCATE, but Supabase's
-- default privileges had also granted REFERENCES and TRIGGER on this table to anon and
-- authenticated, so supabase/verify_policies.sql query 4 ("expected zero rows") returned
-- four rows. Nothing needs those privileges: the application writes ONLY through the
-- SECURITY DEFINER function public.record_ai_usage_events (owner = table owner, so it
-- needs no grant), and founders read as the project owner in the SQL editor.
-- REFERENCES/TRIGGER are not reachable through the REST API (no DDL), so this was a
-- hygiene and verification fix, not an exploitable hole. Revoking ALL (rather than the
-- two leftovers) also covers any privilege a future default-privilege rule might add to
-- this one table. Default privileges for FUTURE tables are deliberately NOT altered here:
-- that is a platform-wide setting, and every new table's migration must revoke explicitly
-- (the SQL tests assert it for the server-owned tables).
-- ===========================================================================
revoke all on table public.ai_usage_events from public, anon, authenticated;
revoke all on sequence public.ai_usage_events_id_seq from public, anon, authenticated;

-- ===========================================================================
-- 2. BOUND quota_ledger GROWTH   (F-07)
-- ===========================================================================
-- CONFIRMED on a local database (isolated, no live service): a signed-in student can call
-- consume_quota() with a refund-token hash they chose and then refund_quota() with the
-- matching token, repeatedly. Quota is returned each time, so they gain nothing - but every
-- cycle leaves a ledger row: 5,000 cycles took 2.3 s database-side and added 5,001 rows
-- (about 2.5 MB; roughly 500 bytes/row with indexes). That is unbounded storage growth by
-- one account.
--
-- MITIGATION CHOSEN (smallest that fits the architecture): consume_quota() refuses to
-- insert another row when the user already has v_cap (500) ledger rows in the last 24 h,
-- counting refunded rows. Why this and not a server secret on the RPCs:
--   * no new secret, no signature change, no coordinated deploy, no way to lock every
--     student out by mis-setting an environment variable;
--   * refunds are never capped (a refund creates no row), so legitimate settlement still
--     works at the cap;
--   * legitimate volume is far below it (Free: about 3-12 rows a day).
-- Trade-offs: an abuser can still create 500 rows/day/account (about 250 KB/day), and
-- many accounts remain a separate problem (finding F-09: Supabase Auth settings). A
-- secret-gated RPC would block direct calls completely but needs a founder decision
-- (see docs/PRE_PHASE10_REMEDIATION_REPORT.md).
-- WHEN YOU SET PRO LIMITS: a Pro student's legitimate rows per day (chat + quiz + notes_ai
-- daily limits plus refunds) must stay well below 500, or raise v_cap here first.
-- The cap check runs inside the per-user-and-kind advisory lock, so two different kinds
-- racing at the cap can overshoot it by at most 2 rows (the number of kinds minus one): fine for a bound.
-- ===========================================================================
create index if not exists quota_ledger_user_created_idx on public.quota_ledger (user_id, created_at);

-- consume_quota: identical to 0011 plus the growth cap above.
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
    -- Q-07/F-07 ledger-growth cap (see header): at most this many ledger rows (ALL kinds,
    -- refunded or not) per user per rolling 24 h. A tunable hypothesis, not a validated number.
    v_cap        constant integer := 500;
    v_recent     integer;
    v_oldest     timestamptz;
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

    -- Growth cap. A request that gets this far is a NEW row. Counting refunded rows too is
    -- the point: consume -> refund -> consume ... in a loop leaves every row behind, so
    -- without a cap a signed-in student could grow quota_ledger without bound while never
    -- losing quota. Denying here inserts nothing, so a denied loop adds no rows.
    select count(*), min(created_at) into v_recent, v_oldest from public.quota_ledger
    where user_id = v_uid and created_at > v_now - interval '24 hours';
    if v_recent >= v_cap then
        return jsonb_build_object(
            'allowed', false, 'reason', 'rate_limited', 'plan', v_plan, 'kind', p_kind,
            'resets_at', to_char((v_oldest + interval '24 hours') at time zone 'Asia/Dhaka', 'YYYY-MM-DD"T"HH24:MI:SS"+06:00"')
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

revoke all on function public.consume_quota(text, text, text) from public, anon;
grant execute on function public.consume_quota(text, text, text) to authenticated;

-- ===========================================================================
-- OPTIONAL HOUSEKEEPING (never run for you). Ledger rows older than the current period
-- no longer affect any count; they are only history. If you want to bound storage over
-- time, run this deliberately, e.g. monthly:
--   delete from public.quota_ledger where created_at < now() - interval '90 days';
-- (A deleted row's request id could in theory be reused; ids are generated by the server
-- and never repeat, so this is harmless.)
--
-- ROLLBACK (forward-safe): redeploy nothing - the backend works with or without this
-- migration. To remove the cap, re-apply the consume_quota definition from 0011. To
-- restore the two revoked privileges (not recommended) grant them explicitly.
-- ===========================================================================
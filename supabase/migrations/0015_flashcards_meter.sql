-- ===========================================================================
-- 0015  Dedicated "flashcards" meter (AI Engine 2.0, CEO decisions D-4 / final decisions)
--
-- WHY: Pro flashcards were metered on the shared `notes_ai` meter (0014), which ALSO
-- counts chat-title generation and tag suggestion. A 200/day abuse guard meant for
-- flashcards would therefore silently throttle those unrelated features. Flashcards now
-- have their own meter; `notes_ai` keeps counting titles and tag suggestions only.
--
-- WHAT:
--   * `flashcards` is accepted as a meter kind (plan_limits + quota_ledger constraints).
--   * consume_quota() is re-created from the shipped 0012 definition with ONE change: the
--     accepted-kind list gains 'flashcards'. Everything else (advisory lock, Asia/Dhaka
--     day/week keys, replay handling, ledger cap, grants) is byte-for-byte the 0012 text.
--   * Pro limit: 200 per DAY. This is a TEMPORARY, CONFIGURABLE ABUSE GUARD, not a product
--     allowance: the product says "Unlimited flashcards". Change it without a deploy:
--         update public.plan_limits set limit_count = <N> where plan = 'pro' and kind = 'flashcards';
--   * There is deliberately NO Free row: Free students may not generate flashcards (the
--     application refuses with PLAN_REQUIRED first), and if that check were ever bypassed
--     consume_quota() answers `plan_not_configured`, so the database also refuses.
--
-- ADDITIVE AND IDEMPOTENT. Safe to apply twice. Apply AFTER 0014 and BEFORE deploying the
-- application version that sends kind 'flashcards'.
--
-- ROLLBACK: delete the Pro flashcards row and set the application's flashcards meter back
-- (code rollback) -- or simply leave the schema; the extra kind is harmless:
--     delete from public.plan_limits where plan = 'pro' and kind = 'flashcards';
-- ===========================================================================

alter table public.plan_limits drop constraint if exists plan_limits_kind_check;
alter table public.plan_limits
    add constraint plan_limits_kind_check check (kind in ('chat', 'quiz', 'notes_ai', 'flashcards'));

alter table public.quota_ledger drop constraint if exists quota_ledger_kind_check;
alter table public.quota_ledger
    add constraint quota_ledger_kind_check check (kind in ('chat', 'quiz', 'notes_ai', 'flashcards'));

insert into public.plan_limits (plan, kind, period, limit_count, first_day_limit_count) values
    ('pro', 'flashcards', 'day', 200, null)
on conflict (plan, kind) do nothing;

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
    if p_kind is null or p_kind not in ('chat', 'quiz', 'notes_ai', 'flashcards') then
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
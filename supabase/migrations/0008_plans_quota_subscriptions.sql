-- Kloriz: Pre-Phase 10 remediation - plan entitlements, quotas and subscriptions.
--
-- Run AFTER 0007_pocket_notes.sql. SAFE TO RE-RUN (tables are "if not exists",
-- policies are dropped-and-recreated, functions are "create or replace",
-- seed rows use "on conflict do nothing" so later edits are never overwritten).
-- Purely additive: it creates new objects only and touches no existing table.
--
-- WHAT THIS GIVES THE APPLICATION
--   * public.plan_limits       what each plan may do   (data, not code)
--   * public.plan_prices       approved Pro prices     (299 BDT/month, 3,199 BDT/year)
--   * public.subscriptions     who currently has Pro   (written ONLY by the
--                              service_role-only activate_pro_subscription())
--   * public.payments          verified payment history (never deleted by expiry)
--   * public.quota_ledger      one row per counted request
--   * public.consume_quota()   atomic "check and count" for the signed-in user
--   * public.refund_quota()    gives one counted request back (secret-token gated)
--
-- WHY THE BROWSER CANNOT CHEAT
--   Supabase exposes the REST API to browsers with the public anon key, so every
--   table privilege and every function granted to `authenticated` is reachable
--   by any student with their own JWT. Therefore:
--     - limits, prices, subscriptions, payments and the ledger have NO insert /
--       update / delete policy for students, and write privileges are revoked;
--     - consume_quota() takes no limit and no plan argument. It derives the plan
--       from public.subscriptions and the limit from public.plan_limits for
--       auth.uid(), so a caller can only spend their OWN quota;
--     - refund_quota() would otherwise be a way to get unlimited chats (call it
--       after every answer). It therefore requires the refund token whose hash
--       the BACKEND stored when it counted the request. The token never leaves
--       the server, so a student cannot refund a request the backend counted;
--     - activate_pro_subscription() is executable by service_role only.
--
-- TIME RULES (CEO decision): days and weeks reset in Asia/Dhaka (UTC+6, no DST);
-- weeks start on Monday. "First login day" is the Dhaka calendar day the auth
-- account was created.
--
-- PRO LIMITS ARE DELIBERATELY NOT SEEDED. The latest decision fixes Pro's price
-- but not its message/quiz numbers. Until rows exist for ('pro','chat') and
-- ('pro','quiz'), consume_quota() returns reason "plan_not_configured" for a Pro
-- user instead of inventing numbers. Insert them in the SQL editor when decided:
--   insert into public.plan_limits (plan, kind, period, limit_count)
--   values ('pro','chat','day', <N>), ('pro','quiz','day', <M>);
--
-- ROLLBACK: drop the functions and tables created here (see docs/DEPLOYMENT.md).
-- Dropping them loses ledger/subscription/payment history, so export first.

-- ---------------------------------------------------------------------------
-- Reference data
-- ---------------------------------------------------------------------------
create table if not exists public.plan_limits (
    plan                    text    not null check (plan in ('free', 'pro')),
    kind                    text    not null check (kind in ('chat', 'quiz')),
    period                  text    not null check (period in ('day', 'week')),
    limit_count             integer not null check (limit_count >= 0),
    first_day_limit_count   integer          check (first_day_limit_count is null or first_day_limit_count >= 0),
    primary key (plan, kind)
);

create table if not exists public.plan_prices (
    plan            text    not null check (plan = 'pro'),
    billing_period  text    not null check (billing_period in ('monthly', 'yearly')),
    amount_bdt      integer not null check (amount_bdt > 0),
    primary key (plan, billing_period)
);

insert into public.plan_limits (plan, kind, period, limit_count, first_day_limit_count) values
    ('free', 'chat', 'day',  1, 3),
    ('free', 'quiz', 'week', 3, null)
on conflict (plan, kind) do nothing;

insert into public.plan_prices (plan, billing_period, amount_bdt) values
    ('pro', 'monthly',  299),
    ('pro', 'yearly',  3199)
on conflict (plan, billing_period) do nothing;

-- ---------------------------------------------------------------------------
-- Payments and subscriptions (history is append-only from the app's view)
-- ---------------------------------------------------------------------------
create table if not exists public.payments (
    id                   uuid primary key default gen_random_uuid(),
    user_id              uuid not null references auth.users(id) on delete cascade,
    provider             text not null check (provider in ('bkash')),
    provider_payment_id  text not null check (length(provider_payment_id) between 1 and 128),
    amount_bdt           integer not null check (amount_bdt > 0),
    currency             text not null default 'BDT' check (currency = 'BDT'),
    billing_period       text not null check (billing_period in ('monthly', 'yearly')),
    status               text not null default 'verified' check (status in ('verified', 'refunded')),
    verified_at          timestamptz not null default now(),
    created_at           timestamptz not null default now(),
    constraint payments_provider_payment_unique unique (provider, provider_payment_id)
);
create index if not exists payments_user_idx on public.payments (user_id, created_at desc);

create table if not exists public.subscriptions (
    id              uuid primary key default gen_random_uuid(),
    user_id         uuid not null references auth.users(id) on delete cascade,
    plan            text not null default 'pro' check (plan = 'pro'),
    billing_period  text not null check (billing_period in ('monthly', 'yearly')),
    status          text not null default 'active' check (status in ('active', 'cancelled')),
    starts_at       timestamptz not null,
    expires_at      timestamptz not null,
    payment_id      uuid references public.payments(id),
    created_at      timestamptz not null default now(),
    constraint subscriptions_window check (expires_at > starts_at)
);
create index if not exists subscriptions_user_active_idx
    on public.subscriptions (user_id, expires_at desc);

-- ---------------------------------------------------------------------------
-- Quota ledger: one row per counted request. NO student-facing policies at all.
-- ---------------------------------------------------------------------------
create table if not exists public.quota_ledger (
    id                  bigserial primary key,
    user_id             uuid not null references auth.users(id) on delete cascade,
    kind                text not null check (kind in ('chat', 'quiz')),
    period_key          text not null,
    request_id          text not null check (length(request_id) between 8 and 64),
    refund_token_hash   text,
    refunded_at         timestamptz,
    created_at          timestamptz not null default now(),
    constraint quota_ledger_request_unique unique (user_id, kind, request_id)
);
create index if not exists quota_ledger_period_idx
    on public.quota_ledger (user_id, kind, period_key) where refunded_at is null;

-- ---------------------------------------------------------------------------
-- Row level security
-- ---------------------------------------------------------------------------
alter table public.plan_limits   enable row level security;
alter table public.plan_prices   enable row level security;
alter table public.payments      enable row level security;
alter table public.subscriptions enable row level security;
alter table public.quota_ledger  enable row level security;

drop policy if exists plan_limits_read_all on public.plan_limits;
create policy plan_limits_read_all on public.plan_limits
    for select to authenticated using (true);

drop policy if exists plan_prices_read_all on public.plan_prices;
create policy plan_prices_read_all on public.plan_prices
    for select to authenticated using (true);

drop policy if exists payments_select_own on public.payments;
create policy payments_select_own on public.payments
    for select to authenticated using (user_id = auth.uid());

drop policy if exists subscriptions_select_own on public.subscriptions;
create policy subscriptions_select_own on public.subscriptions
    for select to authenticated using (user_id = auth.uid());

-- quota_ledger: RLS on, deliberately no policy. Only the SECURITY DEFINER
-- functions below (owned by the table owner, which bypasses RLS) touch it.

-- Defence in depth: students never write any of these directly.
revoke insert, update, delete, truncate on
    public.plan_limits, public.plan_prices, public.payments,
    public.subscriptions, public.quota_ledger
    from anon, authenticated;
revoke all on public.quota_ledger from anon, authenticated;
revoke usage, select, update on sequence public.quota_ledger_id_seq from anon, authenticated;

-- ---------------------------------------------------------------------------
-- consume_quota: atomic check-and-count for the signed-in user.
-- Returns jsonb: allowed, plan, kind, reason, limit, used, remaining,
-- resets_at, replay.
-- ---------------------------------------------------------------------------
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
    if p_kind is null or p_kind not in ('chat', 'quiz') then
        raise exception 'invalid kind' using errcode = '22023';
    end if;
    if p_request_id is null or length(p_request_id) not between 8 and 64 then
        raise exception 'invalid request id' using errcode = '22023';
    end if;

    -- Serialise concurrent requests of the SAME user and kind. Different users
    -- never block each other. This is what makes "limit 1 per day" hold when a
    -- client fires several requests at once.
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

-- ---------------------------------------------------------------------------
-- refund_quota: give back ONE counted request, only with the secret token.
-- ---------------------------------------------------------------------------
create or replace function public.refund_quota(
    p_kind          text,
    p_request_id    text,
    p_refund_token  text
) returns boolean
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_uid uuid := auth.uid();
    v_n   integer;
begin
    if v_uid is null or p_refund_token is null or length(p_refund_token) < 16 then
        return false;
    end if;
    perform pg_advisory_xact_lock(hashtextextended(v_uid::text || ':' || coalesce(p_kind, ''), 0));

    update public.quota_ledger
       set refunded_at = now()
     where user_id = v_uid
       and kind = p_kind
       and request_id = p_request_id
       and refunded_at is null
       and refund_token_hash is not null
       and refund_token_hash = encode(sha256(convert_to(p_refund_token, 'UTF8')), 'hex');
    get diagnostics v_n = row_count;
    return v_n > 0;
end;
$$;

-- ---------------------------------------------------------------------------
-- activate_pro_subscription: the ONLY way a subscription row is created.
-- Callable by service_role only (the payment-verification component), never by
-- a student. Idempotent on (provider, provider_payment_id). Renewal while Pro is
-- still active extends from the current expiry (manual renewal, no auto-renew).
-- ---------------------------------------------------------------------------
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

-- ---------------------------------------------------------------------------
-- Function privileges
-- ---------------------------------------------------------------------------
revoke all on function public.consume_quota(text, text, text) from public, anon;
revoke all on function public.refund_quota(text, text, text) from public, anon;
revoke all on function public.activate_pro_subscription(uuid, text, text, text, integer, text)
    from public, anon, authenticated;

grant execute on function public.consume_quota(text, text, text) to authenticated;
grant execute on function public.refund_quota(text, text, text) to authenticated;
grant execute on function public.activate_pro_subscription(uuid, text, text, text, integer, text)
    to service_role;
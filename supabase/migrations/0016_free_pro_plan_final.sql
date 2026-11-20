-- ===========================================================================
-- 0016  Final Free / Pro plan rules
--
-- WHAT THIS CHANGES (and nothing else)
--   * Two new quota meters: 'photo_solve' and 'pdf_upload'.
--   * A new quota period, 'lifetime' (never resets) - used by Free's one-time Photo Solve trial.
--   * plan_limits is set to the finalized product numbers:
--         Free  chat        1 / day, 3 on the Asia/Dhaka calendar day the account was created  (unchanged)
--         Free  quiz        1 / week                       (was 3 / week)
--         Free  photo_solve 1 / lifetime                   (new)
--         Free  pdf_upload  no row on purpose - the application answers PLAN_REQUIRED first and
--                           consume_quota() answers plan_not_configured if that were bypassed
--         Pro   chat        20 / day                       (unchanged)
--         Pro   quiz        5 / day                        (was 15 / day)
--         Pro   photo_solve 5 / day                        (new)
--         Pro   pdf_upload  2 / day                        (new)
--         Pro   notes_ai 200 / day, flashcards 200 / day   (unchanged internal abuse guards)
--   * Pro monthly price is set to 399 BDT in plan_prices (was 299). The YEARLY price is NOT touched:
--     the finalized plan specification does not state one (see docs/PLANS_AND_QUOTAS.md).
--   * Pocket Notes are Pro-only at the DATABASE too: the INSERT policy on public.pocket_notes now
--     requires an active Pro subscription, so a Free student cannot create a note by calling
--     PostgREST directly with their own JWT. Reading, listing and deleting one's OWN existing notes
--     is unchanged, and no existing note is touched.
--   * A lifetime ledger row can no longer be deleted while its account exists, so a retention purge
--     (docs/USAGE_REPORTS.sql) can never silently hand a Free student a second Photo Solve trial.
--     Deleting the ACCOUNT still cascades normally.
--   * consume_quota() is re-created from the shipped 0015 text with exactly two changes: the accepted
--     kind list, and the 'lifetime' period. Advisory lock, Asia/Dhaka day/week keys, replay handling,
--     ledger-growth cap and grants are unchanged.
--
-- DEPLOY ORDER: apply this migration FIRST, then deploy the application that sends the new kinds.
-- IDEMPOTENT: safe to apply twice. NOTE that re-applying it re-asserts the numbers above, so any limit
-- you later tune by hand in plan_limits is reset to them - apply it once, tune afterwards.
--
-- ROLLBACK (data-preserving):
--   update public.plan_limits set limit_count = 3  where plan = 'free' and kind = 'quiz';
--   update public.plan_limits set limit_count = 15 where plan = 'pro'  and kind = 'quiz';
--   update public.plan_prices set amount_bdt = 299 where plan = 'pro' and billing_period = 'monthly';
--   delete from public.plan_limits where kind in ('photo_solve', 'pdf_upload');
--   drop policy if exists "pocket_notes_insert_own" on public.pocket_notes;
--   create policy "pocket_notes_insert_own" on public.pocket_notes for insert with check (auth.uid() = user_id);
--   (leave consume_quota() as it is: the extra kinds are harmless once their plan_limits rows are gone.)
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- 1. Constraints: accept the new kinds and the 'lifetime' period
-- ---------------------------------------------------------------------------
alter table public.plan_limits drop constraint if exists plan_limits_kind_check;
alter table public.plan_limits
    add constraint plan_limits_kind_check
    check (kind in ('chat', 'quiz', 'notes_ai', 'flashcards', 'photo_solve', 'pdf_upload'));

alter table public.plan_limits drop constraint if exists plan_limits_period_check;
alter table public.plan_limits
    add constraint plan_limits_period_check check (period in ('day', 'week', 'lifetime'));

alter table public.quota_ledger drop constraint if exists quota_ledger_kind_check;
alter table public.quota_ledger
    add constraint quota_ledger_kind_check
    check (kind in ('chat', 'quiz', 'notes_ai', 'flashcards', 'photo_solve', 'pdf_upload'));

-- ---------------------------------------------------------------------------
-- 2. The finalized limits (set explicitly - see the idempotency note above)
-- ---------------------------------------------------------------------------
insert into public.plan_limits (plan, kind, period, limit_count, first_day_limit_count) values
    ('free', 'chat',        'day',      1,  3),
    ('free', 'quiz',        'week',     1,  null),
    ('free', 'photo_solve', 'lifetime', 1,  null),
    ('pro',  'chat',        'day',      20, null),
    ('pro',  'quiz',        'day',      5,  null),
    ('pro',  'photo_solve', 'day',      5,  null),
    ('pro',  'pdf_upload',  'day',      2,  null)
on conflict (plan, kind) do update
    set period                = excluded.period,
        limit_count           = excluded.limit_count,
        first_day_limit_count = excluded.first_day_limit_count;

-- ---------------------------------------------------------------------------
-- 3. Pro monthly price (CEO decision for the finalized plan: 399 BDT / month)
-- ---------------------------------------------------------------------------
-- activate_pro_subscription() reads this table, so a payment for any other amount is refused.
update public.plan_prices set amount_bdt = 399 where plan = 'pro' and billing_period = 'monthly';

-- ---------------------------------------------------------------------------
-- 4. consume_quota: 0015 + new kinds + 'lifetime' period
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
    -- Ledger-growth cap (0012): at most this many ledger rows (ALL kinds, refunded or not) per user
    -- per rolling 24 h. A tunable hypothesis, not a validated number. Pro worst case counted today:
    -- 20 chat + 5 quiz + 5 photo + 2 pdf + 200 notes_ai + 200 flashcards = 432 < 500.
    v_cap        constant integer := 500;
    v_recent     integer;
    v_oldest     timestamptz;
begin
    if v_uid is null then
        raise exception 'not authenticated' using errcode = '28000';
    end if;
    if p_kind is null or p_kind not in ('chat', 'quiz', 'notes_ai', 'flashcards', 'photo_solve', 'pdf_upload') then
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

    if v_row.period = 'lifetime' then
        -- Never resets: one fixed key, no reset time. Persisted per account, so logging out, another
        -- device, cleared browser storage or a new chat thread cannot restore it.
        v_key := 'lifetime';
        v_resets_txt := null;
    else
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
    end if;

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

    -- Growth cap (see 0012/0015). Denying here inserts nothing, so a denied loop adds no rows.
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

-- ---------------------------------------------------------------------------
-- 5. A lifetime ledger row outlives any retention purge
-- ---------------------------------------------------------------------------
-- A purge such as "delete from quota_ledger where created_at < now() - interval '90 days'" would
-- otherwise erase the only record that a Free student has used their one Photo Solve and quietly give
-- them another. The guard raises instead. When the ACCOUNT is deleted the cascade removes the user row
-- first, so the check below no longer finds it and the delete proceeds.
create or replace function public.quota_ledger_protect_lifetime()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
    if old.period_key = 'lifetime'
       and exists (select 1 from auth.users u where u.id = old.user_id) then
        raise exception 'lifetime quota rows cannot be deleted while the account exists'
            using errcode = '42501';
    end if;
    return old;
end;
$$;
revoke all on function public.quota_ledger_protect_lifetime() from public, anon, authenticated;

drop trigger if exists trg_quota_ledger_protect_lifetime on public.quota_ledger;
create trigger trg_quota_ledger_protect_lifetime
    before delete on public.quota_ledger
    for each row execute function public.quota_ledger_protect_lifetime();

-- ---------------------------------------------------------------------------
-- 6. Pocket Notes are a Pro feature at the database as well
-- ---------------------------------------------------------------------------
-- Only the INSERT policy changes. SELECT / UPDATE / DELETE stay "own rows" so an expired-Pro student
-- keeps access to (and can delete) what they already saved; nothing is deleted by an expiry.
-- public.get_my_plan() is the same SECURITY DEFINER lookup the backend uses (migration 0014).
drop policy if exists "pocket_notes_insert_own" on public.pocket_notes;
create policy "pocket_notes_insert_own"
    on public.pocket_notes for insert
    with check (auth.uid() = user_id and public.get_my_plan() = 'pro');
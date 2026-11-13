-- Behaviour tests for the final CEO quota decisions (migration 0015 + the shipped 0008-0012 logic):
--   Free quiz = 3 per WEEK, weeks reset in Asia/Dhaka (Monday 00:00 +06:00)
--   Pro flashcards = their OWN meter, 200/day temporary guard, configurable in plan_limits
-- Every check raises on failure (psql ON_ERROR_STOP). Fixtures that could disturb later suites
-- are inside transactions that roll back. Run by run_local_db_tests.sh.
\set ON_ERROR_STOP on
\set QUIET on

create or replace function pg_temp.check(ok boolean, msg text) returns void language plpgsql as $$
begin if ok is not true then raise exception 'TEST FAILED: %', msg; end if; end $$;

insert into auth.users (id, created_at, email) values
  ('00000000-0000-0000-0000-0000000000e1', now() - interval '6 days', 'wk-free-1@example.test'),
  ('00000000-0000-0000-0000-0000000000e2', now() - interval '6 days', 'wk-free-2@example.test'),
  ('00000000-0000-0000-0000-0000000000e3', now() - interval '6 days', 'wk-free-3@example.test'),
  ('00000000-0000-0000-0000-0000000000e4', now() - interval '6 days', 'fc-pro@example.test'),
  ('00000000-0000-0000-0000-0000000000e5', now() - interval '6 days', 'fc-free@example.test')
on conflict do nothing;

-- =====================================================================================
-- A. Free quiz: three per week, the fourth is refused with the structured response
-- =====================================================================================
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000000e1';
do $$
declare r jsonb; i int;
        expected_reset text := to_char(date_trunc('week', now() at time zone 'Asia/Dhaka') + interval '7 days',
                                       'YYYY-MM-DD"T"HH24:MI:SS"+06:00"');
begin
  perform pg_temp.check(expected_reset like '%T00:00:00+06:00', 'independent calc: the reset instant is midnight Dhaka');
  perform pg_temp.check(extract(isodow from (date_trunc('week', now() at time zone 'Asia/Dhaka') + interval '7 days')) = 1,
                        'independent calc: the reset day is a Monday');
  for i in 1..3 loop
    r := public.consume_quota('quiz', 'wk-quiz-' || lpad(i::text, 4, '0'), null);
    perform pg_temp.check((r->>'allowed')::boolean and r->>'plan' = 'free' and (r->>'limit')::int = 3
                          and (r->>'used')::int = i, 'free quiz ' || i || ' of 3 allowed');
    perform pg_temp.check(r->>'resets_at' = expected_reset, 'resets_at = next Monday 00:00 Asia/Dhaka (' || expected_reset || ')');
  end loop;
  r := public.consume_quota('quiz', 'wk-quiz-0004', null);
  perform pg_temp.check(not (r->>'allowed')::boolean and r->>'reason' = 'quota_exceeded'
                        and (r->>'limit')::int = 3 and (r->>'used')::int = 3 and (r->>'remaining')::int = 0
                        and r->>'kind' = 'quiz' and r->>'resets_at' = expected_reset,
                        'fourth free quiz refused with the structured quota_exceeded response');
  -- replaying an already-counted request id is never double counted
  r := public.consume_quota('quiz', 'wk-quiz-0001', null);
  perform pg_temp.check((r->>'allowed')::boolean and (r->>'replay')::boolean, 'replay of an allowed quiz is not double counted');
  -- the weekly quiz meter never touches the chat meter
  r := public.consume_quota('chat', 'wk-chat-0001', null);
  perform pg_temp.check((r->>'allowed')::boolean and r->>'kind' = 'chat', 'chat meter independent of the quiz meter');
end $$;
rollback;

-- =====================================================================================
-- B. The week boundary. consume_quota() reads now() (the transaction clock cannot be faked),
--    so the boundary is proven two ways: (1) the period KEY logic with rows from other weeks,
--    and (2) the identical expressions evaluated at fixed instants around the boundary.
-- =====================================================================================
-- B1. rows from the PREVIOUS week do not count: a fresh week starts at zero
begin;
insert into public.quota_ledger (user_id, kind, period_key, request_id, created_at) values
  ('00000000-0000-0000-0000-0000000000e2', 'quiz',
   'week:' || to_char(date_trunc('week', now() at time zone 'Asia/Dhaka') - interval '7 days', 'YYYY-MM-DD'), 'prevwk-0001', now() - interval '8 days'),
  ('00000000-0000-0000-0000-0000000000e2', 'quiz',
   'week:' || to_char(date_trunc('week', now() at time zone 'Asia/Dhaka') - interval '7 days', 'YYYY-MM-DD'), 'prevwk-0002', now() - interval '8 days'),
  ('00000000-0000-0000-0000-0000000000e2', 'quiz',
   'week:' || to_char(date_trunc('week', now() at time zone 'Asia/Dhaka') - interval '7 days', 'YYYY-MM-DD'), 'prevwk-0003', now() - interval '8 days');
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000000e2';
do $$ declare r jsonb; begin
  r := public.consume_quota('quiz', 'wk2-quiz-0001', null);
  perform pg_temp.check((r->>'allowed')::boolean and (r->>'used')::int = 1,
                        'three quizzes used LAST week do not reduce this week''s allowance');
end $$;
rollback;

-- B2. when the week key rolls over the allowance resets (the reset IS a key change)
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000000e3';
do $$ declare r jsonb; i int; begin
  for i in 1..3 loop perform public.consume_quota('quiz', 'wk3-quiz-' || lpad(i::text, 4, '0'), null); end loop;
  r := public.consume_quota('quiz', 'wk3-quiz-0004', null);
  perform pg_temp.check(not (r->>'allowed')::boolean, 'exhausted within the week');
end $$;
reset role;
-- simulate "the next Monday 00:00 Dhaka has passed": every row now belongs to the previous week's key
update public.quota_ledger
   set period_key = 'week:' || to_char(date_trunc('week', now() at time zone 'Asia/Dhaka') - interval '7 days', 'YYYY-MM-DD')
 where user_id = '00000000-0000-0000-0000-0000000000e3' and kind = 'quiz';
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000000e3';
do $$ declare r jsonb; begin
  r := public.consume_quota('quiz', 'wk3-quiz-0005', null);
  perform pg_temp.check((r->>'allowed')::boolean and (r->>'used')::int = 1, 'the allowance is fresh after the week key changes');
end $$;
rollback;

-- B3. fixed-instant checks of the SAME expressions the function uses (Asia/Dhaka, ISO weeks)
do $$
declare
  key_fn text := 'x';
  k_before text; k_after text; k_utc text;
  ts_before timestamptz := '2026-11-08 17:59:59+00';   -- Sunday 23:59:59 in Dhaka
  ts_after  timestamptz := '2026-11-08 18:00:00+00';   -- Monday 00:00:00 in Dhaka
begin
  k_before := 'week:' || to_char(date_trunc('week', ts_before at time zone 'Asia/Dhaka'), 'YYYY-MM-DD');
  k_after  := 'week:' || to_char(date_trunc('week', ts_after  at time zone 'Asia/Dhaka'), 'YYYY-MM-DD');
  perform pg_temp.check(k_before = 'week:2026-11-02', 'Sunday 23:59:59 Dhaka is still the OLD week (' || k_before || ')');
  perform pg_temp.check(k_after  = 'week:2026-11-09', 'Monday 00:00:00 Dhaka starts the NEW week (' || k_after || ')');
  -- a UTC-based week would be wrong for 6 hours every Sunday evening: prove Dhaka, not UTC, is what we use
  k_utc := 'week:' || to_char(date_trunc('week', ts_after at time zone 'UTC'), 'YYYY-MM-DD');
  perform pg_temp.check(k_utc = 'week:2026-11-02' and k_utc <> k_after, 'a UTC week would still be the old week at that instant');
  -- the reset instant of the old week is exactly the Monday boundary
  perform pg_temp.check((date_trunc('week', ts_before at time zone 'Asia/Dhaka') at time zone 'Asia/Dhaka') + interval '7 days' = ts_after,
                        'the old week resets exactly at Monday 00:00 +06:00');
  -- Asia/Dhaka has no daylight saving: the offset is +06:00 all year
  perform pg_temp.check(extract(timezone from ('2026-07-01 12:00:00+00'::timestamptz at time zone 'Asia/Dhaka')::timestamptz) is not null, 'tz database knows Asia/Dhaka');
  perform pg_temp.check(('2026-01-15 00:00:00+06'::timestamptz at time zone 'Asia/Dhaka') = '2026-01-15 00:00:00'::timestamp
                    and ('2026-07-15 00:00:00+06'::timestamptz at time zone 'Asia/Dhaka') = '2026-07-15 00:00:00'::timestamp,
                        'fixed +06:00 offset in winter and summer (no DST)');
end $$;

-- =====================================================================================
-- C. Refund policy for a FAILED quiz generation (existing approved policy, re-proved here)
-- =====================================================================================
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000000e1';
do $$ declare r jsonb; i int; ok boolean; begin
  -- earlier section rolled back, so this user starts the week at zero
  for i in 1..2 loop perform public.consume_quota('quiz', 'rf-quiz-' || lpad(i::text, 4, '0'), null); end loop;
  r := public.consume_quota('quiz', 'rf-quiz-0003', encode(sha256(convert_to('refund-secret-token-0001', 'UTF8')), 'hex'));
  perform pg_temp.check((r->>'allowed')::boolean and (r->>'used')::int = 3, 'third quiz consumed');
  perform pg_temp.check(not (public.consume_quota('quiz', 'rf-quiz-0004', null)->>'allowed')::boolean, 'fourth refused while three are live');
  -- generation FAILED: the application refunds with the secret token it holds
  ok := public.refund_quota('quiz', 'rf-quiz-0003', 'refund-secret-token-0001');
  perform pg_temp.check(ok, 'the failed generation is refunded with its own token');
  perform pg_temp.check(not public.refund_quota('quiz', 'rf-quiz-0003', 'refund-secret-token-0001'), 'a refund can only happen once');
  perform pg_temp.check(not public.refund_quota('quiz', 'rf-quiz-0001', 'wrong-token-wrong-token'), 'a wrong token cannot refund');
  r := public.consume_quota('quiz', 'rf-quiz-0005', null);
  perform pg_temp.check((r->>'allowed')::boolean and (r->>'used')::int = 3, 'the refunded unit is available again');
  -- a refunded request id can never be reused to get a second free unit
  perform pg_temp.check(public.consume_quota('quiz', 'rf-quiz-0003', null)->>'reason' = 'request_id_reused', 'refunded request id cannot be replayed');
end $$;
rollback;

-- =====================================================================================
-- D. Pro flashcards: own meter, 200/day temporary guard, configurable, never throttles other features
-- =====================================================================================
do $$ begin
  perform pg_temp.check((select limit_count from public.plan_limits where plan='pro' and kind='flashcards') = 200
                        and (select period from public.plan_limits where plan='pro' and kind='flashcards') = 'day',
                        'Pro flashcards guard = 200 per day (0015)');
  perform pg_temp.check(not exists (select 1 from public.plan_limits where plan='free' and kind='flashcards'),
                        'Free has no flashcards row (the app gates it and the database also refuses)');
end $$;

begin;
insert into public.payments (id, user_id, provider, provider_payment_id, amount_bdt, billing_period)
  values ('00000000-0000-0000-0000-00000000cc01','00000000-0000-0000-0000-0000000000e4','bkash','fc-pay-1',299,'monthly');
insert into public.subscriptions (user_id, billing_period, starts_at, expires_at, payment_id)
  values ('00000000-0000-0000-0000-0000000000e4','monthly', now() - interval '1 day', now() + interval '10 days','00000000-0000-0000-0000-00000000cc01');
-- the guard is configurable WITHOUT a code change: lower it to 3 for the test
update public.plan_limits set limit_count = 3 where plan='pro' and kind='flashcards';
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000000e4';
do $$ declare r jsonb; i int; begin
  for i in 1..3 loop
    r := public.consume_quota('flashcards', 'fc-card-' || lpad(i::text, 4, '0'), null);
    perform pg_temp.check((r->>'allowed')::boolean and r->>'plan'='pro' and r->>'kind'='flashcards' and (r->>'limit')::int = 3, 'pro flashcards ' || i || ' allowed');
  end loop;
  r := public.consume_quota('flashcards', 'fc-card-0004', null);
  perform pg_temp.check(not (r->>'allowed')::boolean and r->>'reason'='quota_exceeded' and r->>'kind'='flashcards',
                        'the guard refuses with the structured response once reached');
  -- exhausting flashcards must NOT throttle any unrelated feature
  r := public.consume_quota('notes_ai', 'fc-notes-0001', null);
  perform pg_temp.check((r->>'allowed')::boolean and r->>'kind'='notes_ai', 'titles/tag suggestions (notes_ai) unaffected by the flashcards guard');
  r := public.consume_quota('quiz', 'fc-quiz-0001', null);
  perform pg_temp.check((r->>'allowed')::boolean and r->>'kind'='quiz', 'quiz meter unaffected');
  r := public.consume_quota('chat', 'fc-chat-0001', null);
  perform pg_temp.check((r->>'allowed')::boolean and r->>'kind'='chat', 'chat meter unaffected');
end $$;
reset role;
do $$ begin
  perform pg_temp.check((select count(*) from public.quota_ledger where user_id='00000000-0000-0000-0000-0000000000e4' and kind='flashcards') = 3,
                        'only flashcards rows were written to the flashcards meter');
end $$;
-- raising the guard takes effect immediately
update public.plan_limits set limit_count = 5 where plan='pro' and kind='flashcards';
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000000e4';
do $$ declare r jsonb; begin
  r := public.consume_quota('flashcards', 'fc-card-0005', null);
  perform pg_temp.check((r->>'allowed')::boolean and (r->>'limit')::int = 5, 'a changed guard value applies without any deploy');
end $$;
rollback;

-- Free students: the database refuses the flashcards meter even if the application check were bypassed
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000000e5';
do $$ declare r jsonb; begin
  r := public.consume_quota('flashcards', 'fc-free-0001', null);
  perform pg_temp.check(not (r->>'allowed')::boolean and r->>'reason' = 'plan_not_configured', 'Free + flashcards => plan_not_configured');
end $$;
rollback;

select ' WEEKLY QUIZ + FLASHCARDS (0015) DB TESTS PASSED' as result;
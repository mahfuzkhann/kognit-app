-- Behaviour tests for migration 0014 (AI Engine 2.0). Run FIRST by run_local_db_tests.sh,
-- before the older suites mutate plan_limits, so it sees exactly what 0014 seeds.
-- Every check raises on failure (psql runs with ON_ERROR_STOP). All fixtures that
-- would disturb later suites are inside transactions that roll back.
\set ON_ERROR_STOP on
\set QUIET on

create or replace function pg_temp.check(ok boolean, msg text) returns void language plpgsql as $$
begin if ok is not true then raise exception 'TEST FAILED: %', msg; end if; end $$;

-- ---- 1. seeded limits match the written policy (backend/entitlements.py QUOTA_POLICY)
do $$ declare r record; begin
  perform pg_temp.check((select limit_count from public.plan_limits where plan='pro' and kind='chat') = 20
     and (select period from public.plan_limits where plan='pro' and kind='chat') = 'day', 'Pro chat = 20/day');
  perform pg_temp.check((select limit_count from public.plan_limits where plan='pro' and kind='quiz') = 15
     and (select period from public.plan_limits where plan='pro' and kind='quiz') = 'day', 'Pro quiz = 15/day');
  perform pg_temp.check((select limit_count from public.plan_limits where plan='pro' and kind='notes_ai') = 200, 'Pro notes_ai abuse guard = 200/day');
  perform pg_temp.check((select limit_count from public.plan_limits where plan='free' and kind='chat') = 1
     and (select first_day_limit_count from public.plan_limits where plan='free' and kind='chat') = 3, 'Free chat 1/day, 3 on first day (unchanged)');
  perform pg_temp.check((select limit_count from public.plan_limits where plan='free' and kind='quiz') = 3
     and (select period from public.plan_limits where plan='free' and kind='quiz') = 'week', 'Free quiz 3/week (unchanged, reset period flagged to CEO)');
end $$;

-- fixtures
insert into auth.users (id, created_at, email) values
  ('00000000-0000-0000-0000-0000000000d1', now() - interval '5 days', 'free-d@example.test'),
  ('00000000-0000-0000-0000-0000000000d2', now() - interval '5 days', 'pro-d@example.test')
on conflict do nothing;
insert into public.usage_write_keys (key_hash)
values (encode(sha256(convert_to('test-usage-write-key-0123456789','UTF8')),'hex')) on conflict do nothing;

-- ---- 2. get_my_plan: anon denied, free by default, pro with an active subscription
begin;
set local role anon;
do $$ declare denied boolean := false; begin
  begin perform public.get_my_plan(); exception when insufficient_privilege then denied := true; end;
  perform pg_temp.check(denied, 'anon cannot execute get_my_plan');
end $$;
rollback;

begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000000d1';
do $$ begin perform pg_temp.check(public.get_my_plan() = 'free', 'student without subscription is free'); end $$;
rollback;

begin;
insert into public.payments (id, user_id, provider, provider_payment_id, amount_bdt, billing_period)
  values ('00000000-0000-0000-0000-00000000bb01','00000000-0000-0000-0000-0000000000d2','bkash','e2-pay-1',299,'monthly');
insert into public.subscriptions (user_id, billing_period, starts_at, expires_at, payment_id)
  values ('00000000-0000-0000-0000-0000000000d2','monthly', now() - interval '1 day', now() + interval '10 days','00000000-0000-0000-0000-00000000bb01');
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000000d2';
do $$ declare r jsonb; i int; begin
  perform pg_temp.check(public.get_my_plan() = 'pro', 'active subscription -> pro');
  -- another student's plan is not readable through this function (it takes no user id at all)
  -- ---- 3. Pro daily limits are enforced: chat 20, then denied; quiz 15, then denied
  for i in 1..20 loop
    r := public.consume_quota('chat', 'e2-chat-' || lpad(i::text, 4, '0'), null);
    perform pg_temp.check((r->>'allowed')::boolean and r->>'plan' = 'pro' and (r->>'limit')::int = 20, 'Pro chat ' || i || ' allowed');
  end loop;
  r := public.consume_quota('chat', 'e2-chat-0021', null);
  perform pg_temp.check(not (r->>'allowed')::boolean and r->>'reason' = 'quota_exceeded', 'Pro chat 21 denied');
  for i in 1..15 loop
    r := public.consume_quota('quiz', 'e2-quiz-' || lpad(i::text, 4, '0'), null);
    perform pg_temp.check((r->>'allowed')::boolean and (r->>'limit')::int = 15, 'Pro quiz ' || i || ' allowed');
  end loop;
  r := public.consume_quota('quiz', 'e2-quiz-0016', null);
  perform pg_temp.check(not (r->>'allowed')::boolean, 'Pro quiz 16 denied (note-generated quizzes spend this same pool)');
  -- replay of an already counted request is not double counted
  r := public.consume_quota('quiz', 'e2-quiz-0001', null);
  perform pg_temp.check((r->>'allowed')::boolean and (r->>'replay')::boolean, 'quiz replay allowed, not double counted');
end $$;
rollback;

-- ---- 4. usage writer: new fields stored, bad values rejected, privacy fields only
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000000d1';
do $$ declare n int; bad int := 0; begin
  n := public.record_ai_usage_events('test-usage-write-key-0123456789', jsonb_build_array(
    jsonb_build_object('request_id','e2-req-1','model','gemini-3.5-flash-lite','operation','stream','status','success',
      'attempt',2,'attempt_kind','recovery','task_category','mathematics','route_reason','advanced_mathematics',
      'route_policy_version','2026-11-03.r1','cached_tokens',10,'input_tokens',1000,'output_tokens',200,
      'estimated_cost_usd',0.0008,'pricing_version','gemini-paid-standard-2026-11-03'),
    jsonb_build_object('request_id','e2-req-1','model','gemini-3.5-flash-lite','operation','verify','status','success',
      'attempt_kind','verify','verification_status','incorrect')));
  perform pg_temp.check(n = 2, 'two usage events recorded');
  begin perform public.record_ai_usage_events('test-usage-write-key-0123456789', jsonb_build_array(
    jsonb_build_object('operation','stream','status','success','attempt_kind','hack'))); exception when others then bad := bad + 1; end;
  begin perform public.record_ai_usage_events('test-usage-write-key-0123456789', jsonb_build_array(
    jsonb_build_object('operation','stream','status','success','verification_status','maybe'))); exception when others then bad := bad + 1; end;
  begin perform public.record_ai_usage_events('test-usage-write-key-0123456789', jsonb_build_array(
    jsonb_build_object('operation','stream','status','success','estimated_cost_usd',-1))); exception when others then bad := bad + 1; end;
  begin perform public.record_ai_usage_events('test-usage-write-key-0123456789', jsonb_build_array(
    jsonb_build_object('operation','stream','status','success','estimated_cost_usd','free'))); exception when others then bad := bad + 1; end;
  begin perform public.record_ai_usage_events('wrong-key-wrong-key-123', jsonb_build_array(
    jsonb_build_object('operation','stream','status','success'))); exception when others then bad := bad + 1; end;
  perform pg_temp.check(bad = 5, 'invalid attempt_kind, verification_status, cost and wrong key are all rejected');
end $$;
reset role;
do $$ begin
  perform pg_temp.check((select count(*) from public.ai_usage_events where request_id='e2-req-1' and plan='free') = 2, 'plan is server-derived, not client supplied');
  perform pg_temp.check((select attempt_kind from public.ai_usage_events where request_id='e2-req-1' and operation='stream') = 'recovery', 'attempt_kind stored');
  perform pg_temp.check((select estimated_cost_usd from public.ai_usage_events where request_id='e2-req-1' and operation='stream') = 0.0008, 'cost stored');
  perform pg_temp.check((select verification_status from public.ai_usage_events where operation='verify' and request_id='e2-req-1') = 'incorrect', 'verification status stored');
end $$;
rollback;

-- ---- 5. curriculum: only the two approved codes, NULL allowed, owner-only via existing RLS
begin;
do $$ declare bad int := 0; begin
  insert into public.student_profiles (user_id, name, user_class, stream, curriculum)
    values ('00000000-0000-0000-0000-0000000000d1', 'T', 'Class 9-10 (SSC)', 'Science', 'nctb_english');
  begin update public.student_profiles set curriculum = 'cambridge' where user_id = '00000000-0000-0000-0000-0000000000d1'; exception when check_violation then bad := bad + 1; end;
  begin update public.student_profiles set curriculum = 'NCTB' where user_id = '00000000-0000-0000-0000-0000000000d1'; exception when check_violation then bad := bad + 1; end;
  update public.student_profiles set curriculum = null where user_id = '00000000-0000-0000-0000-0000000000d1';
  perform pg_temp.check(bad = 2, 'Cambridge/Edexcel/free-text curricula rejected by the database');
end $$;
rollback;

select ' ENGINE 2.0 (0014) DB TESTS PASSED' as result;
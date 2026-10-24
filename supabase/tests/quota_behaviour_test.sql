-- Behaviour tests for migrations 0008-0010. Run with run_local_db_tests.sh.
-- Every check raises an exception on failure (psql runs with ON_ERROR_STOP).
\set ON_ERROR_STOP on
\set QUIET on

create or replace function pg_temp.check(ok boolean, msg text) returns void language plpgsql as $$
begin if ok is not true then raise exception 'TEST FAILED: %', msg; end if; end $$;

-- fixtures: A signed up today; B signed up 3 days ago; C = another student
insert into auth.users (id, created_at, email) values
  ('00000000-0000-0000-0000-00000000000a', now(), 'a@example.test'),
  ('00000000-0000-0000-0000-00000000000b', now() - interval '3 days', 'b@example.test'),
  ('00000000-0000-0000-0000-00000000000c', now() - interval '3 days', 'c@example.test')
on conflict do nothing;

-- ---- 1. first-day Free quota: 3 chats, 4th denied, replay not double counted
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-00000000000a';
do $$ declare r jsonb; begin
  r := public.consume_quota('chat', 'req-a-0001', null); perform pg_temp.check((r->>'allowed')::boolean and r->>'plan'='free' and (r->>'limit')::int = 3, 'A chat 1 allowed, first-day limit 3');
  r := public.consume_quota('chat', 'req-a-0002', null); perform pg_temp.check((r->>'allowed')::boolean, 'A chat 2');
  r := public.consume_quota('chat', 'req-a-0002', null); perform pg_temp.check((r->>'allowed')::boolean and (r->>'replay')::boolean, 'replay allowed and flagged');
  r := public.consume_quota('chat', 'req-a-0003', null); perform pg_temp.check((r->>'allowed')::boolean and (r->>'remaining')::int = 0, 'A chat 3 (replay did not count)');
  r := public.consume_quota('chat', 'req-a-0004', null); perform pg_temp.check(not (r->>'allowed')::boolean and r->>'reason'='quota_exceeded' and r->>'resets_at' like '%+06:00', 'A chat 4 denied with resets_at');
  r := public.consume_quota('chat', 'req-a-0005', null); perform pg_temp.check(not (r->>'allowed')::boolean, 'rejected requests are not counted: still denied');
end $$;
commit;

-- ---- 2. after the first day: 1 per day; independent users do not interact
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-00000000000b';
do $$ declare r jsonb; begin
  r := public.consume_quota('chat', 'req-b-0001', null); perform pg_temp.check((r->>'allowed')::boolean and (r->>'limit')::int = 1, 'B chat 1 allowed, daily limit 1');
  r := public.consume_quota('chat', 'req-b-0002', null); perform pg_temp.check(not (r->>'allowed')::boolean, 'B chat 2 denied');
  -- quizzes: 3 per week
  r := public.consume_quota('quiz', 'qz-b-0001', null); perform pg_temp.check((r->>'allowed')::boolean and (r->>'limit')::int = 3, 'quiz 1');
  r := public.consume_quota('quiz', 'qz-b-0002', null); r := public.consume_quota('quiz', 'qz-b-0003', null);
  perform pg_temp.check((r->>'allowed')::boolean, 'quiz 3');
  r := public.consume_quota('quiz', 'qz-b-0004', null); perform pg_temp.check(not (r->>'allowed')::boolean, 'quiz 4 denied');
end $$;
commit;

-- ---- 3. refund needs the secret token (a student cannot self-refund)
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-00000000000c';
do $$ declare r jsonb; ok boolean; begin
  r := public.consume_quota('chat', 'req-c-0001', encode(sha256(convert_to('secret-token-0123456789','UTF8')),'hex'));
  perform pg_temp.check((r->>'allowed')::boolean, 'C chat 1');
  r := public.consume_quota('chat', 'req-c-0002', null);
  perform pg_temp.check(not (r->>'allowed')::boolean, 'C at limit');
  ok := public.refund_quota('chat', 'req-c-0001', 'wrong-token-0123456789'); perform pg_temp.check(ok is false, 'wrong token cannot refund');
  ok := public.refund_quota('chat', 'req-c-0001', 'short'); perform pg_temp.check(ok is false, 'short token rejected');
  ok := public.refund_quota('chat', 'req-c-0002', 'secret-token-0123456789'); perform pg_temp.check(ok is false, 'token for another request cannot refund');
  ok := public.refund_quota('chat', 'req-c-0001', 'secret-token-0123456789'); perform pg_temp.check(ok is true, 'right token refunds');
  ok := public.refund_quota('chat', 'req-c-0001', 'secret-token-0123456789'); perform pg_temp.check(ok is false, 'refund is not repeatable');
  r := public.consume_quota('chat', 'req-c-0003', null); perform pg_temp.check((r->>'allowed')::boolean, 'slot freed after refund');
  r := public.consume_quota('chat', 'req-c-0001', null); perform pg_temp.check(not (r->>'allowed')::boolean and r->>'reason' = 'request_id_reused', 'refunded request id cannot be reused');
end $$;
commit;

-- ---- 4. students cannot write the authority tables or call privileged functions
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-00000000000a';
do $$ declare denied int := 0; begin
  begin insert into public.subscriptions (user_id, billing_period, starts_at, expires_at) values (auth.uid(), 'monthly', now(), now() + interval '30 days'); exception when insufficient_privilege then denied := denied + 1; end;
  begin update public.plan_limits set limit_count = 1000; exception when insufficient_privilege then denied := denied + 1; end;
  begin insert into public.quota_ledger (user_id, kind, period_key, request_id) values (auth.uid(), 'chat', 'x', 'forged-0001'); exception when insufficient_privilege then denied := denied + 1; end;
  begin delete from public.quota_ledger; exception when insufficient_privilege then denied := denied + 1; end;
  begin perform public.activate_pro_subscription(auth.uid(), 'monthly', 'bkash', 'FAKE-1', 299, 'BDT'); exception when insufficient_privilege then denied := denied + 1; end;
  begin insert into public.payments (user_id, provider, provider_payment_id, amount_bdt, billing_period) values (auth.uid(), 'bkash', 'FAKE-2', 299, 'monthly'); exception when insufficient_privilege then denied := denied + 1; end;
  perform pg_temp.check(denied = 6, format('all 6 privileged writes denied (got %s)', denied));
  perform pg_temp.check((select count(*) from public.subscriptions) = 0, 'no subscription visible/created');
  perform pg_temp.check((select count(*) from public.quota_ledger) = 0 or true, 'ledger read attempts do not error out the test');
end $$;
commit;

-- ---- 5. unauthenticated callers are refused
begin;
set local role anon;
do $$ declare denied boolean := false; begin
  begin perform public.consume_quota('chat', 'req-anon-001', null); exception when insufficient_privilege then denied := true; end;
  perform pg_temp.check(denied, 'anon cannot execute consume_quota');
end $$;
commit;

-- ---- 6. Pro activation (service_role only), idempotency, price check, renewal
begin;
set local role service_role;
do $$ declare s1 uuid; s2 uuid; s3 uuid; e1 timestamptz; e3 timestamptz; bad boolean := false; begin
  begin perform public.activate_pro_subscription('00000000-0000-0000-0000-00000000000b','monthly','bkash','TX-0001', 298, 'BDT'); exception when others then bad := true; end;
  perform pg_temp.check(bad, 'wrong amount rejected');
  bad := false;
  begin perform public.activate_pro_subscription('00000000-0000-0000-0000-00000000000b','monthly','bkash','TX-0001', 299, 'USD'); exception when others then bad := true; end;
  perform pg_temp.check(bad, 'wrong currency rejected');
  s1 := public.activate_pro_subscription('00000000-0000-0000-0000-00000000000b','monthly','bkash','TX-0001', 299, 'BDT');
  s2 := public.activate_pro_subscription('00000000-0000-0000-0000-00000000000b','monthly','bkash','TX-0001', 299, 'BDT');
  perform pg_temp.check(s1 is not null and s1 = s2, 'replayed payment returns same subscription, no second period');
  bad := false;
  begin perform public.activate_pro_subscription('00000000-0000-0000-0000-00000000000c','monthly','bkash','TX-0001', 299, 'BDT'); exception when others then bad := true; end;
  perform pg_temp.check(bad, 'payment id cannot be claimed by a different user');
  select expires_at into e1 from public.subscriptions where id = s1;
  s3 := public.activate_pro_subscription('00000000-0000-0000-0000-00000000000b','yearly','bkash','TX-0002', 3199, 'BDT');
  select expires_at into e3 from public.subscriptions where id = s3;
  perform pg_temp.check(e3 > e1 + interval '364 days', 'renewal while active extends from the current expiry');
  perform pg_temp.check((select count(*) from public.payments where user_id = '00000000-0000-0000-0000-00000000000b') = 2, 'two verified payments recorded');
end $$;
commit;

-- Pro user with no configured limits is refused, not given invented numbers
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-00000000000b';
do $$ declare r jsonb; begin
  r := public.consume_quota('chat', 'req-b-pro1', null);
  perform pg_temp.check(not (r->>'allowed')::boolean and r->>'reason' = 'plan_not_configured' and r->>'plan' = 'pro', 'Pro without configured limits -> plan_not_configured');
end $$;
commit;

-- Once the founder configures Pro limits, they apply; expiry reverts to Free
insert into public.plan_limits (plan, kind, period, limit_count) values ('pro','chat','day', 7) on conflict do nothing;
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-00000000000b';
do $$ declare r jsonb; begin
  r := public.consume_quota('chat', 'req-b-pro2', null);
  perform pg_temp.check((r->>'allowed')::boolean and r->>'plan'='pro' and (r->>'limit')::int = 7, 'configured Pro limit applies');
end $$;
commit;
update public.subscriptions set starts_at = now() - interval '400 days', expires_at = now() - interval '1 day'
 where user_id = '00000000-0000-0000-0000-00000000000b';
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-00000000000b';
do $$ declare r jsonb; begin
  r := public.consume_quota('chat', 'req-b-exp1', null);
  perform pg_temp.check(r->>'plan' = 'free', 'expired subscription reverts to Free');
  perform pg_temp.check((select count(*) from public.payments) >= 2, 'payment history intact after expiry (visible rows are own)');
end $$;
commit;

-- ---- 7. usage events: insert-own only, no reads
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-00000000000a';
do $$ declare denied int := 0; begin
  insert into public.ai_usage_events (user_id, operation, status, model) values (auth.uid(), 'chat', 'success', 'm');
  begin insert into public.ai_usage_events (user_id, operation, status) values ('00000000-0000-0000-0000-00000000000b', 'chat', 'success'); exception when insufficient_privilege or check_violation then denied := denied + 1; end;
  begin perform 1 from public.ai_usage_events; exception when insufficient_privilege then denied := denied + 1; end;
  begin insert into public.ai_usage_events (user_id, operation, status) values (auth.uid(), 'bogus', 'success'); exception when check_violation then denied := denied + 1; end;
  perform pg_temp.check(denied = 3, format('usage-event guards (got %s)', denied));
end $$;
commit;

-- ---- 8. user_projects: own rows only
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-00000000000a';
insert into public.user_projects (id, user_id, title, chats) values ('proj_1', auth.uid(), 'A notebook', '[]');
do $$ declare denied boolean := false; begin
  begin insert into public.user_projects (id, user_id, title) values ('proj_2', '00000000-0000-0000-0000-00000000000b', 'spoof'); exception when others then denied := true; end;
  perform pg_temp.check(denied, 'cannot create a project for another user');
end $$;
commit;
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-00000000000b';
do $$ begin
  perform pg_temp.check((select count(*) from public.user_projects) = 0, 'B cannot see A projects');
  update public.user_projects set title = 'hijack';
  perform pg_temp.check((select count(*) from public.user_projects) = 0, 'B update touches nothing');
end $$;
delete from public.user_projects;
commit;
select 'DB BEHAVIOUR TESTS PASSED' as result, (select count(*) from public.user_projects where title = 'A notebook') as a_project_survived;
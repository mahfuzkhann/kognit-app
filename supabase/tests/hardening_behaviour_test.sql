-- Behaviour/security tests for migration 0011 (pre-Phase 10 finalization).
-- Run by run_local_db_tests.sh AFTER quota_behaviour_test.sql. Every check raises
-- on failure (psql runs with ON_ERROR_STOP). Needs the sandbox_stubs.sql roles.
\set ON_ERROR_STOP on
\set QUIET on

create or replace function pg_temp.check(ok boolean, msg text) returns void language plpgsql as $$
begin if ok is not true then raise exception 'TEST FAILED: %', msg; end if; end $$;

-- fixtures: E (old account), F (old account, other student), G (signed up today)
insert into auth.users (id, created_at, email) values
  ('00000000-0000-0000-0000-0000000000e1', now() - interval '5 days', 'e@example.test'),
  ('00000000-0000-0000-0000-0000000000f1', now() - interval '5 days', 'f@example.test'),
  ('00000000-0000-0000-0000-0000000000a9', now(), 'g@example.test')
on conflict do nothing;

-- The founder's one-time setup: store only the HASH of the server secret.
insert into public.usage_write_keys (key_hash)
values (encode(sha256(convert_to('test-usage-write-key-0123456789','UTF8')),'hex'))
on conflict do nothing;

-- ---- Q-05: separate notes_ai meter ---------------------------------------
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000000e1';
do $$ declare r jsonb; begin
  r := public.consume_quota('notes_ai', 'nai-e-000001', null);
  perform pg_temp.check((r->>'allowed')::boolean and r->>'kind'='notes_ai' and (r->>'limit')::int = 6, 'notes_ai allowed, proposed free limit 6');
  -- it did not touch the chat meter: the single daily chat is still available
  r := public.consume_quota('chat', 'chat-e-000001', null);
  perform pg_temp.check((r->>'allowed')::boolean and (r->>'used')::int = 1, 'chat meter independent of notes_ai');
  -- exhaust notes_ai (5 more allowed, 7th denied) and prove chat is unaffected by it
  for i in 2..6 loop
    r := public.consume_quota('notes_ai', 'nai-e-00000' || i, null);
    perform pg_temp.check((r->>'allowed')::boolean, 'notes_ai unit ' || i);
  end loop;
  r := public.consume_quota('notes_ai', 'nai-e-000007', null);
  perform pg_temp.check(not (r->>'allowed')::boolean and r->>'reason'='quota_exceeded' and (r->>'limit')::int = 6, 'notes_ai 7th denied');
  r := public.consume_quota('notes_ai', 'nai-e-000008', null);
  perform pg_temp.check(not (r->>'allowed')::boolean, 'denied requests are not counted and stay denied');
  -- an unknown kind is rejected, not silently metered
  begin
    perform public.consume_quota('notes', 'bad-kind-0001', null);
    perform pg_temp.check(false, 'unknown kind must raise');
  exception when sqlstate '22023' then null; end;
end $$;
commit;

-- refund works for notes_ai with the secret token, only once, and frees one unit
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000000f1';
do $$ declare r jsonb; ok boolean; h text := encode(sha256(convert_to('refund-token-notes-0123456789','UTF8')),'hex'); begin
  r := public.consume_quota('notes_ai', 'nai-f-000001', h);
  perform pg_temp.check((r->>'allowed')::boolean, 'F notes_ai 1');
  ok := public.refund_quota('notes_ai', 'nai-f-000001', 'someone-elses-token-0123456789'); perform pg_temp.check(ok is false, 'wrong token cannot refund notes_ai');
  ok := public.refund_quota('notes_ai', 'nai-f-000001', 'refund-token-notes-0123456789');   perform pg_temp.check(ok is true, 'right token refunds notes_ai');
  ok := public.refund_quota('notes_ai', 'nai-f-000001', 'refund-token-notes-0123456789');   perform pg_temp.check(ok is false, 'second refund is a harmless no-op');
  -- Q-04: refunding a request that was never recorded creates no credit
  ok := public.refund_quota('notes_ai', 'never-consumed-001', 'refund-token-notes-0123456789'); perform pg_temp.check(ok is false, 'refund of a missing record is a no-op');
  r := public.consume_quota('notes_ai', 'nai-f-000002', null);
  perform pg_temp.check((r->>'used')::int = 1, 'ledger consistent: exactly one live unit after refund + missing-record refund');
end $$;
commit;

-- cross-user: a refund from another account with the right token still fails
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000000e1';
do $$ declare ok boolean; begin
  ok := public.refund_quota('notes_ai', 'nai-f-000002', 'anything-at-all-0123456789');
  perform pg_temp.check(ok is false, 'cross-user refund rejected');
end $$;
commit;

-- a missing plan limit fails safe (no row for kind) - simulate by a Pro user
begin;
insert into public.payments (id, user_id, provider, provider_payment_id, amount_bdt, billing_period)
  values ('00000000-0000-0000-0000-00000000aa01','00000000-0000-0000-0000-0000000000f1','bkash','test-pay-guard-1',299,'monthly');
insert into public.subscriptions (user_id, billing_period, starts_at, expires_at, payment_id)
  values ('00000000-0000-0000-0000-0000000000f1','monthly', now() - interval '1 day', now() + interval '1 day','00000000-0000-0000-0000-00000000aa01');
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000000f1';
do $$ declare r jsonb; begin
  r := public.consume_quota('notes_ai', 'nai-f-pro-001', null);
  perform pg_temp.check(not (r->>'allowed')::boolean and r->>'reason'='plan_not_configured', 'Pro without configured notes_ai limit is denied, not unmetered');
end $$;
rollback;

-- ---- Q-08: activation refuses while pro limits are missing -----------------
begin;
do $$ begin
  begin
    perform public.activate_pro_subscription('00000000-0000-0000-0000-0000000000f1','monthly','bkash','test-pay-guard-2',299,'BDT');
    perform pg_temp.check(false, 'activation must fail while pro limits are not configured');
  exception when sqlstate '22023' then
    perform pg_temp.check(sqlerrm = 'pro limits are not configured', 'refused for the right reason: ' || sqlerrm);
  end;
  perform pg_temp.check(not exists (select 1 from public.payments where provider_payment_id = 'test-pay-guard-2'), 'no payment row was created');
end $$;
rollback;

-- ---- Q-06: telemetry integrity --------------------------------------------
-- 1. a student can no longer INSERT directly (forged plan/cost/model/time)
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000000e1';
do $$ begin
  begin
    insert into public.ai_usage_events (user_id, operation, status, plan, estimated_cost_usd, pricing_version, created_at)
    values ('00000000-0000-0000-0000-0000000000e1','chat','success','pro',0.000001,'forged','2000-01-01');
    perform pg_temp.check(false, 'direct insert by an authenticated student must fail');
  exception when insufficient_privilege then null; end;
  begin
    insert into public.ai_usage_events (user_id, operation, status)
    select '00000000-0000-0000-0000-0000000000e1','chat','success' from generate_series(1,100);
    perform pg_temp.check(false, 'bulk insert by an authenticated student must fail');
  exception when insufficient_privilege then null; end;
end $$;
commit;

-- 2. anonymous clients cannot insert or call the writer
begin;
set local role anon;
do $$ begin
  begin
    insert into public.ai_usage_events (user_id, operation, status) values ('00000000-0000-0000-0000-0000000000e1','chat','success');
    perform pg_temp.check(false, 'anon insert must fail');
  exception when insufficient_privilege then null; end;
  begin
    perform public.record_ai_usage_events('test-usage-write-key-0123456789', '[{"operation":"chat","status":"success"}]'::jsonb);
    perform pg_temp.check(false, 'anon cannot execute the writer');
  exception when insufficient_privilege then null; end;
end $$;
commit;

-- 3. the writer needs the server secret
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000000e1';
do $$ begin
  begin
    perform public.record_ai_usage_events('guessed-key-0123456789abcdef', '[{"operation":"chat","status":"success"}]'::jsonb);
    perform pg_temp.check(false, 'wrong key must be refused');
  exception when sqlstate '42501' then null; end;
  begin
    perform public.record_ai_usage_events(null, '[{"operation":"chat","status":"success"}]'::jsonb);
    perform pg_temp.check(false, 'null key must be refused');
  exception when sqlstate '42501' then null; end;
end $$;
commit;

-- 4. legitimate write works; server-derived columns cannot be forged
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000000e1';
do $$ declare n int; begin
  n := public.record_ai_usage_events('test-usage-write-key-0123456789', jsonb_build_array(
    jsonb_build_object('request_id','req-legit-1','provider','gemini','model','gemini-test','operation','stream','status','success',
        'attempt',1,'input_tokens',120,'output_tokens',45,'thinking_tokens',10,'total_tokens',175,'duration_ms',900,'ttft_ms',200,
        'retry_count',0,'recovery_used',false,
        -- attempts to forge server-derived values are IGNORED, not honoured:
        'plan','pro','user_id','00000000-0000-0000-0000-0000000000f1','estimated_cost_usd',99,'pricing_version','forged','created_at','2000-01-01')
  ));
  perform pg_temp.check(n = 1, 'legit write inserted one row');
end $$;
commit;
do $$ declare r record; begin
  select * into r from public.ai_usage_events where request_id = 'req-legit-1';
  perform pg_temp.check(r.user_id = '00000000-0000-0000-0000-0000000000e1', 'user_id is the JWT user, not the payload');
  perform pg_temp.check(r.plan = 'free', 'plan derived server-side (payload said pro)');
  perform pg_temp.check(r.estimated_cost_usd is null and r.pricing_version is null, 'cost columns cannot be supplied');
  perform pg_temp.check(r.created_at > now() - interval '1 minute', 'created_at is server time');
  perform pg_temp.check(r.input_tokens = 120 and r.model = 'gemini-test', 'valid fields stored');
end $$;

-- 5. invalid / oversized input is rejected and nothing is stored
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000000e1';
do $$
declare bad jsonb[] := array[
  '[{"operation":"chat","status":"success","input_tokens":-5}]'::jsonb,
  '[{"operation":"chat","status":"success","input_tokens":99999999999}]'::jsonb,
  '[{"operation":"chat","status":"success","input_tokens":"12"}]'::jsonb,
  '[{"operation":"chat","status":"success","input_tokens":1.5}]'::jsonb,
  '[{"operation":"hack","status":"success"}]'::jsonb,
  '[{"operation":"chat","status":"ok"}]'::jsonb,
  '[{"operation":"chat","status":"success","model":"bad model; drop table"}]'::jsonb,
  '[{"operation":"chat","status":"success","model":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}]'::jsonb,
  '[{"operation":"chat","status":"success","recovery_used":"yes"}]'::jsonb,
  '[]'::jsonb,
  '{"operation":"chat"}'::jsonb,
  '["not an object"]'::jsonb
];
  b jsonb; i int := 0;
begin
  foreach b in array bad loop
    i := i + 1;
    begin
      perform public.record_ai_usage_events('test-usage-write-key-0123456789', b);
      perform pg_temp.check(false, 'invalid payload #' || i || ' was accepted');
    exception when sqlstate '22023' then null; end;
  end loop;
  -- more than 20 events in one call
  begin
    perform public.record_ai_usage_events('test-usage-write-key-0123456789',
      (select jsonb_agg(jsonb_build_object('operation','chat','status','success')) from generate_series(1,21)));
    perform pg_temp.check(false, '21 events must be rejected');
  exception when sqlstate '22023' then null; end;
end $$;
commit;

-- 6. unbounded growth is capped per user per rolling 24h (silent drop)
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000000f1';
do $$ declare n int; total int := 0; i int; begin
  for i in 1..60 loop
    n := public.record_ai_usage_events('test-usage-write-key-0123456789',
      (select jsonb_agg(jsonb_build_object('operation','chat','status','success')) from generate_series(1,20)));
    total := total + n;
  end loop;
  perform pg_temp.check(total = 1000, 'exactly 1000 rows accepted in 24h, got ' || total);
  n := public.record_ai_usage_events('test-usage-write-key-0123456789', '[{"operation":"chat","status":"success"}]'::jsonb);
  perform pg_temp.check(n = 0, 'further events are silently dropped');
end $$;
commit;

-- 7. isolation: students cannot read telemetry at all (so never another user's)
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000000f1';
do $$ begin
  begin
    perform count(*) from public.ai_usage_events;
    perform pg_temp.check(false, 'a student must not be able to read ai_usage_events');
  exception when insufficient_privilege then null; end;
  begin
    perform count(*) from public.usage_write_keys;
    perform pg_temp.check(false, 'a student must not be able to read usage_write_keys');
  exception when insufficient_privilege then null; end;
  begin
    delete from public.ai_usage_events;
    perform pg_temp.check(false, 'a student must not delete telemetry');
  exception when insufficient_privilege then null; end;
end $$;
commit;

-- ---- Q-13: hardening of internal functions -----------------------------------
do $$ begin
  perform pg_temp.check(not has_function_privilege('authenticated','public._usage_int(jsonb,text,bigint)','execute'), 'internal validator not callable by students');
  perform pg_temp.check(not has_function_privilege('anon','public._usage_slug(jsonb,text,integer)','execute'), 'internal validator not callable by anon');
  perform pg_temp.check(not has_function_privilege('anon','public.record_ai_usage_events(text,jsonb)','execute'), 'writer not callable by anon');
  perform pg_temp.check(not has_function_privilege('authenticated','public.activate_pro_subscription(uuid,text,text,text,integer,text)','execute'), 'activation not callable by students');
  perform pg_temp.check(not has_function_privilege('authenticated','public.set_pocket_notes_updated_at()','execute'), 'trigger fn not executable by students');
  perform pg_temp.check(not exists (
    select 1 from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and p.prosecdef and (p.proconfig is null or not exists (select 1 from unnest(p.proconfig) c where c like 'search_path=%'))
  ), 'every SECURITY DEFINER function in public has a fixed search_path');
  perform pg_temp.check(not exists (
    select 1 from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and (p.proconfig is null or not exists (select 1 from unnest(p.proconfig) c where c like 'search_path=%'))
  ), 'every function in public has a fixed search_path');
end $$;

-- the updated_at triggers still fire after the grant changes (as a student)
begin;
insert into public.student_profiles (user_id, name, user_class, stream) values ('00000000-0000-0000-0000-0000000000e1', 'Test', 'Class 9', 'Science') on conflict do nothing;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000000e1';
do $$ begin
  update public.student_profiles set updated_at = '2000-01-01' where user_id = '00000000-0000-0000-0000-0000000000e1';
  perform pg_temp.check((select updated_at from public.student_profiles where user_id = '00000000-0000-0000-0000-0000000000e1') > now() - interval '1 minute', 'updated_at trigger still works for students');
end $$;
rollback;

select 'HARDENING DB TESTS PASSED' as result;
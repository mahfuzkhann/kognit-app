-- Tests for migration 0012 (audit findings F-02 least privilege, F-07 ledger growth).
-- Run by run_local_db_tests.sh after hardening_behaviour_test.sql. Raises on failure.
\set ON_ERROR_STOP on
\set QUIET on

create or replace function pg_temp.check(ok boolean, msg text) returns void language plpgsql as $$
begin if ok is not true then raise exception 'TEST FAILED: %', msg; end if; end $$;

insert into auth.users (id, created_at, email) values
  ('00000000-0000-0000-0000-0000000000b1', now() - interval '5 days', 'cap-a@example.test'),
  ('00000000-0000-0000-0000-0000000000b2', now() - interval '5 days', 'cap-b@example.test')
on conflict do nothing;
insert into public.usage_write_keys (key_hash)
values (encode(sha256(convert_to('test-usage-write-key-0123456789','UTF8')),'hex')) on conflict do nothing;

-- ---- F-02: API roles hold NO privilege on the server-owned objects ------------------
do $$
declare r text; p text; obj text;
begin
  foreach r in array array['anon','authenticated'] loop
    foreach obj in array array['public.ai_usage_events','public.usage_write_keys','public.quota_ledger'] loop
      foreach p in array array['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER'] loop
        perform pg_temp.check(not has_table_privilege(r, obj, p), format('%s must not hold %s on %s', r, p, obj));
      end loop;
    end loop;
    foreach obj in array array['public.ai_usage_events_id_seq','public.quota_ledger_id_seq'] loop
      foreach p in array array['USAGE','SELECT','UPDATE'] loop
        perform pg_temp.check(not has_sequence_privilege(r, obj, p), format('%s must not hold %s on %s', r, p, obj));
      end loop;
    end loop;
  end loop;
  -- verify_policies.sql query 4 (documented expected result: zero rows)
  perform pg_temp.check(not exists (
    select 1 from pg_class c join pg_namespace n on n.oid = c.relnamespace
    cross join lateral aclexplode(coalesce(c.relacl, acldefault((case c.relkind when 'S' then 's' else 'r' end)::"char", c.relowner))) a
    left join pg_roles ro on ro.oid = a.grantee
    where n.nspname = 'public'
      and c.relname in ('ai_usage_events','ai_usage_events_id_seq','usage_write_keys','quota_ledger','quota_ledger_id_seq')
      and (a.grantee = 0 or ro.rolname in ('anon','authenticated'))
  ), 'verify_policies query 4 must return zero rows');
end $$;

-- ---- F-02: the legitimate telemetry path still works; forgery paths stay closed ----
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000000b1';
do $$ declare n int; begin
  n := public.record_ai_usage_events('test-usage-write-key-0123456789', '[{"operation":"chat","status":"success","model":"m-1"}]'::jsonb);
  perform pg_temp.check(n = 1, 'correct secret still records an event after 0012');
  begin perform public.record_ai_usage_events('wrong-secret-0123456789abcdef', '[{"operation":"chat","status":"success"}]'::jsonb);
        perform pg_temp.check(false, 'wrong secret must be rejected'); exception when sqlstate '42501' then null; end;
  begin insert into public.ai_usage_events (user_id, operation, status) values (auth.uid(), 'chat', 'success');
        perform pg_temp.check(false, 'direct insert must be refused'); exception when insufficient_privilege then null; end;
end $$;
commit;
begin;
set local role anon;
do $$ begin
  begin insert into public.ai_usage_events (user_id, operation, status) values ('00000000-0000-0000-0000-0000000000b1','chat','success');
        perform pg_temp.check(false, 'anon insert must be refused'); exception when insufficient_privilege then null; end;
  begin perform public.record_ai_usage_events('test-usage-write-key-0123456789', '[{"operation":"chat","status":"success"}]'::jsonb);
        perform pg_temp.check(false, 'anon cannot call the writer'); exception when insufficient_privilege then null; end;
end $$;
commit;

-- ---- F-07: the consume/refund loop is bounded -----------------------------------------
-- Normal service is unchanged: the first request of a 5-day-old Free account is allowed,
-- the second is denied by the PLAN limit (not the cap), and refunds still work.
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000000b1';
do $$ declare r jsonb; ok boolean; h text := encode(sha256(convert_to('legit-refund-token-0123456789','UTF8')),'hex'); begin
  r := public.consume_quota('chat', 'legit-req-00001', h);
  perform pg_temp.check((r->>'allowed')::boolean and (r->>'used')::int = 1, 'first chat allowed');
  r := public.consume_quota('chat', 'legit-req-00002', null);
  perform pg_temp.check(not (r->>'allowed')::boolean and r->>'reason' = 'quota_exceeded', 'second chat denied by the PLAN limit, not the cap');
  ok := public.refund_quota('chat', 'legit-req-00001', 'legit-refund-token-0123456789');
  perform pg_temp.check(ok is true, 'legitimate refund works');
  ok := public.refund_quota('chat', 'legit-req-00001', 'legit-refund-token-0123456789');
  perform pg_temp.check(ok is false, 'duplicate refund is a no-op');
  ok := public.refund_quota('chat', 'legit-req-00001', 'attacker-guess-0123456789abcd');
  perform pg_temp.check(ok is false, 'wrong token cannot refund');
end $$;
commit;

-- The attack: consume with a chosen token, refund, repeat. Must stop at the cap.
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000000b2';
do $$
declare r jsonb; ok boolean; i int := 0; tok text; rid text; denied_reason text; cycles int := 0; legit jsonb;
begin
  loop
    i := i + 1;
    exit when i > 2000;                       -- safety stop; the cap must hit long before
    rid := 'attack-' || lpad(i::text, 6, '0');
    tok := 'attacker-chosen-token-' || i;
    r := public.consume_quota('chat', rid, encode(sha256(convert_to(tok,'UTF8')),'hex'));
    if not (r->>'allowed')::boolean then denied_reason := r->>'reason'; exit; end if;
    ok := public.refund_quota('chat', rid, tok);
    perform pg_temp.check(ok is true, 'refund inside the loop works');
    cycles := cycles + 1;
  end loop;
  perform pg_temp.check(denied_reason = 'rate_limited', 'the loop is stopped by the growth cap, got ' || coalesce(denied_reason,'(never)'));
  perform pg_temp.check(cycles = 500, 'exactly 500 rows were created before the cap, got ' || cycles);
  -- bounded: no further rows are created however long the attacker keeps trying
  for i in 1..200 loop
    r := public.consume_quota('chat', 'more-' || lpad(i::text, 6, '0'), null);
    perform pg_temp.check(not (r->>'allowed')::boolean, 'still denied');
  end loop;
  perform pg_temp.check(r ? 'resets_at', 'the denial carries a reset time for the UI');
end $$;
commit;
-- (students cannot read the ledger, so the row counts are checked as the project owner)
do $$ begin
  perform pg_temp.check((select count(*) from public.quota_ledger where user_id = '00000000-0000-0000-0000-0000000000b2') = 500, 'ledger rows for the attacker stay at 500');
  -- no free quota was gained: all 500 rows are refunded, so the plan limit itself is intact
  perform pg_temp.check((select count(*) from public.quota_ledger where user_id = '00000000-0000-0000-0000-0000000000b2' and refunded_at is null) = 0, 'attack rows are all refunded (nothing consumed)');
end $$;

-- Refunds are never blocked by the cap (a refund adds no row) ...
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000000b2';
do $$ begin
  perform pg_temp.check(public.refund_quota('chat', 'attack-000001', 'attacker-chosen-token-1') is false, 'already-refunded row: harmless no-op at the cap');
end $$;
commit;

-- ... other users are unaffected ...
begin;
insert into auth.users (id, created_at, email) values ('00000000-0000-0000-0000-0000000000a9', now() - interval '5 days', 'cap-c@example.test') on conflict do nothing;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000000a9';
do $$ declare r jsonb; begin
  r := public.consume_quota('notes_ai', 'other-user-0001', null);
  perform pg_temp.check((r->>'allowed')::boolean, 'another student is not affected by the attacker''s cap');
end $$;
commit;

-- ... and the cap is a rolling 24 hours: aged rows stop counting.
update public.quota_ledger set created_at = now() - interval '25 hours' where user_id = '00000000-0000-0000-0000-0000000000b2';
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000000b2';
do $$ declare r jsonb; begin
  r := public.consume_quota('chat', 'after-window-0001', null);
  perform pg_temp.check((r->>'allowed')::boolean, 'after 24 h the account can consume again');
end $$;
commit;

-- All kinds share the cap (a student cannot dodge it by spreading across meters).
begin;
insert into auth.users (id, created_at, email) values ('00000000-0000-0000-0000-0000000000b3', now() - interval '5 days', 'cap-d@example.test') on conflict do nothing;
insert into public.quota_ledger (user_id, kind, period_key, request_id, refunded_at)
  select '00000000-0000-0000-0000-0000000000b3', (array['chat','quiz','notes_ai'])[1 + (g % 3)], 'old:' || g, 'seed-' || lpad(g::text, 6, '0'), now()
  from generate_series(1, 500) g;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000000b3';
do $$ declare r jsonb; begin
  r := public.consume_quota('notes_ai', 'mixed-kind-0001', null);
  perform pg_temp.check(not (r->>'allowed')::boolean and r->>'reason' = 'rate_limited', 'rows of every kind count toward the cap');
end $$;
rollback;

select 'LEDGER CAP + PRIVILEGE DB TESTS PASSED' as result;
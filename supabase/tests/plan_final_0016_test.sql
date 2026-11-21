-- Behaviour tests for migrations 0016 + 0017 (the finalized Free / Pro plan and prices):
--   * the seeded limits and the 399 BDT monthly price
--   * Free Photo Solve = ONE per account for life (period 'lifetime', no reset time, refundable)
--   * Pro Photo Solve = 5/day, Pro PDF upload = 2/day, Free PDF upload has no row (refused)
--   * a retention purge can never erase a lifetime row, but deleting the ACCOUNT still cascades
--   * Pocket Notes cannot be INSERTED by a Free student even straight through PostgREST/RLS,
--     while a student keeps read/delete access to notes they already own
-- Every check raises on failure (psql ON_ERROR_STOP). Fixtures that could disturb later suites are
-- inside transactions that roll back. Run by run_local_db_tests.sh AFTER 0016 is applied.
\set ON_ERROR_STOP on
\set QUIET on

create or replace function pg_temp.check(ok boolean, msg text) returns void language plpgsql as $$
begin if ok is not true then raise exception 'TEST FAILED: %', msg; end if; end $$;

insert into auth.users (id, created_at, email) values
  ('00000000-0000-0000-0000-0000000001a1', now() - interval '20 days', 'p16-free-1@example.test'),
  ('00000000-0000-0000-0000-0000000001a2', now() - interval '20 days', 'p16-free-2@example.test'),
  ('00000000-0000-0000-0000-0000000001a3', now() - interval '20 days', 'p16-pro@example.test'),
  ('00000000-0000-0000-0000-0000000001a4', now() - interval '20 days', 'p16-free-3@example.test'),
  ('00000000-0000-0000-0000-0000000001a5', now(),                      'p16-newuser@example.test')
on conflict do nothing;

-- =====================================================================================
-- A. The seeded numbers and the price
-- =====================================================================================
do $$
begin
  perform pg_temp.check((select limit_count from public.plan_limits where plan='free' and kind='quiz') = 1
                        and (select period from public.plan_limits where plan='free' and kind='quiz') = 'week', 'Free quiz = 1 per week');
  perform pg_temp.check((select limit_count from public.plan_limits where plan='pro' and kind='quiz') = 5
                        and (select period from public.plan_limits where plan='pro' and kind='quiz') = 'day', 'Pro quiz = 5 per day');
  perform pg_temp.check((select limit_count from public.plan_limits where plan='free' and kind='chat') = 1
                        and (select first_day_limit_count from public.plan_limits where plan='free' and kind='chat') = 3
                        and (select period from public.plan_limits where plan='free' and kind='chat') = 'day', 'Free chat = 1/day, 3 on the signup day');
  perform pg_temp.check((select limit_count from public.plan_limits where plan='pro' and kind='chat') = 20, 'Pro chat = 20/day');
  perform pg_temp.check((select limit_count from public.plan_limits where plan='free' and kind='photo_solve') = 1
                        and (select period from public.plan_limits where plan='free' and kind='photo_solve') = 'lifetime', 'Free photo_solve = 1 lifetime');
  perform pg_temp.check((select limit_count from public.plan_limits where plan='pro' and kind='photo_solve') = 5
                        and (select period from public.plan_limits where plan='pro' and kind='photo_solve') = 'day', 'Pro photo_solve = 5/day');
  perform pg_temp.check((select limit_count from public.plan_limits where plan='pro' and kind='pdf_upload') = 2
                        and (select period from public.plan_limits where plan='pro' and kind='pdf_upload') = 'day', 'Pro pdf_upload = 2/day');
  perform pg_temp.check(not exists (select 1 from public.plan_limits where plan='free' and kind='pdf_upload'), 'Free has no pdf_upload row');
  perform pg_temp.check((select amount_bdt from public.plan_prices where plan='pro' and billing_period='monthly') = 399, 'Pro monthly = 399 BDT');
  -- migration 0017 (applied by the runner right after 0016): the CEO's explicit yearly figure
  perform pg_temp.check((select amount_bdt from public.plan_prices where plan='pro' and billing_period='yearly') = 4548,
                        'Pro yearly = 4548 (note: 12 x 399 would be 4788 - flagged to the CEO)');
end $$;

-- =====================================================================================
-- A2. Payment activation only accepts the final prices (service role only; rolled back)
-- =====================================================================================
begin;
set local role service_role;
do $$ declare bad boolean; s uuid; begin
  bad := false;
  begin perform public.activate_pro_subscription('00000000-0000-0000-0000-0000000001a3','yearly','bkash','p17-old-yearly', 3199, 'BDT'); exception when others then bad := true; end;
  perform pg_temp.check(bad, 'the old yearly price (3199) is refused after 0017');
  bad := false;
  begin perform public.activate_pro_subscription('00000000-0000-0000-0000-0000000001a3','monthly','bkash','p17-old-monthly', 299, 'BDT'); exception when others then bad := true; end;
  perform pg_temp.check(bad, 'the old monthly price (299) is refused');
  s := public.activate_pro_subscription('00000000-0000-0000-0000-0000000001a3','yearly','bkash','p17-yearly-ok', 4548, 'BDT');
  perform pg_temp.check(s is not null, 'a yearly payment of exactly 4548 BDT activates');
end $$;
rollback;

-- =====================================================================================
-- B. Free Photo Solve: exactly one per account, for life
-- =====================================================================================
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000001a1';
do $$ declare r jsonb; begin
  r := public.consume_quota('photo_solve', 'p16-photo-0001', encode(sha256(convert_to('p16-refund-token-0001', 'UTF8')), 'hex'));
  perform pg_temp.check((r->>'allowed')::boolean and r->>'plan' = 'free' and (r->>'limit')::int = 1 and (r->>'used')::int = 1
                        and (r->>'remaining')::int = 0, 'the one Free photo solve is allowed');
  perform pg_temp.check(r ? 'resets_at' and jsonb_typeof(r->'resets_at') = 'null', 'a lifetime allowance has NO reset time');
  r := public.consume_quota('photo_solve', 'p16-photo-0002', null);
  perform pg_temp.check(not (r->>'allowed')::boolean and r->>'reason' = 'quota_exceeded' and (r->>'used')::int = 1
                        and jsonb_typeof(r->'resets_at') = 'null', 'a second Free photo solve is refused and never resets');
  -- the photo meter is independent of the chat meter (a photo does not spend the daily chat)
  r := public.consume_quota('chat', 'p16-chat-0001', null);
  perform pg_temp.check((r->>'allowed')::boolean and r->>'kind' = 'chat', 'chat meter independent of the photo meter');
  -- replay of the counted request is not double counted
  r := public.consume_quota('photo_solve', 'p16-photo-0001', null);
  perform pg_temp.check((r->>'allowed')::boolean and (r->>'replay')::boolean, 'replay of the counted photo solve is not double counted');
end $$;
commit;

-- Time cannot restore it: age the ledger row by a year (a later calendar day / week / month) - still used.
reset role;
update public.quota_ledger set created_at = now() - interval '400 days'
 where user_id = '00000000-0000-0000-0000-0000000001a1' and kind = 'photo_solve';
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000001a1';
do $$ declare r jsonb; begin
  r := public.consume_quota('photo_solve', 'p16-photo-0003', null);
  perform pg_temp.check(not (r->>'allowed')::boolean and r->>'reason' = 'quota_exceeded',
                        'the lifetime trial is still used 400 days later (no daily / weekly / monthly reset)');
end $$;
commit;

-- A FAILED photo solve is refunded (existing approved policy) and the trial is available again;
-- the refunded request id can never be replayed for a second free unit.
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000001a2';
do $$ declare r jsonb; ok boolean; begin
  r := public.consume_quota('photo_solve', 'p16-photo-1001', encode(sha256(convert_to('p16-refund-token-1001', 'UTF8')), 'hex'));
  perform pg_temp.check((r->>'allowed')::boolean, 'trial consumed');
  perform pg_temp.check(not public.refund_quota('photo_solve', 'p16-photo-1001', 'p16-wrong-token-xxxx'), 'a wrong token cannot refund the trial');
  ok := public.refund_quota('photo_solve', 'p16-photo-1001', 'p16-refund-token-1001');
  perform pg_temp.check(ok, 'a failed generation gets the trial back with its own token');
  r := public.consume_quota('photo_solve', 'p16-photo-1002', null);
  perform pg_temp.check((r->>'allowed')::boolean and (r->>'used')::int = 1, 'the refunded trial can be used again');
  perform pg_temp.check(public.consume_quota('photo_solve', 'p16-photo-1001', null)->>'reason' = 'request_id_reused', 'refunded request id cannot be replayed');
end $$;
commit;

-- =====================================================================================
-- C. Free PDF upload has no allowance; Pro photo 5/day and PDF 2/day
-- =====================================================================================
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000001a4';
do $$ declare r jsonb; begin
  r := public.consume_quota('pdf_upload', 'p16-pdf-free-01', null);
  perform pg_temp.check(not (r->>'allowed')::boolean and r->>'reason' = 'plan_not_configured', 'Free PDF upload is refused by the database too');
end $$;
commit;

begin;
insert into public.payments (id, user_id, provider, provider_payment_id, amount_bdt, billing_period)
  values ('00000000-0000-0000-0000-00000000dd01','00000000-0000-0000-0000-0000000001a3','bkash','p16-pay-1',399,'monthly');
insert into public.subscriptions (user_id, billing_period, starts_at, expires_at, payment_id)
  values ('00000000-0000-0000-0000-0000000001a3','monthly', now() - interval '1 day', now() + interval '10 days','00000000-0000-0000-0000-00000000dd01');
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000001a3';
do $$ declare r jsonb; i int; begin
  for i in 1..5 loop
    r := public.consume_quota('photo_solve', 'p16-photo-p' || lpad(i::text, 3, '0'), null);
    perform pg_temp.check((r->>'allowed')::boolean and r->>'plan' = 'pro' and (r->>'limit')::int = 5, 'Pro photo solve ' || i || ' of 5 allowed');
    perform pg_temp.check(r->>'resets_at' like '%T00:00:00+06:00', 'Pro photo solve resets at midnight Asia/Dhaka');
  end loop;
  r := public.consume_quota('photo_solve', 'p16-photo-p006', null);
  perform pg_temp.check(not (r->>'allowed')::boolean and r->>'reason' = 'quota_exceeded', 'the 6th Pro photo solve is refused');
  for i in 1..2 loop
    r := public.consume_quota('pdf_upload', 'p16-pdf-pro-' || lpad(i::text, 3, '0'), null);
    perform pg_temp.check((r->>'allowed')::boolean and (r->>'limit')::int = 2, 'Pro pdf upload ' || i || ' of 2 allowed');
  end loop;
  r := public.consume_quota('pdf_upload', 'p16-pdf-pro-003', null);
  perform pg_temp.check(not (r->>'allowed')::boolean and r->>'reason' = 'quota_exceeded', 'the 3rd Pro pdf upload is refused');
  -- Pro photo / pdf meters never touch chat or quiz
  perform pg_temp.check((public.consume_quota('chat', 'p16-chat-pro-01', null)->>'used')::int = 1, 'chat meter untouched by photo / pdf');
end $$;
rollback;

-- =====================================================================================
-- D. Signup-day chat allowance (3, then 1/day) is unchanged and tied to the account, not the session
-- =====================================================================================
begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000001a5';
do $$ declare r jsonb; i int; begin
  for i in 1..3 loop
    r := public.consume_quota('chat', 'p16-day1-chat-' || i, null);
    perform pg_temp.check((r->>'allowed')::boolean and (r->>'limit')::int = 3, 'signup-day chat ' || i || ' of 3 allowed');
  end loop;
  r := public.consume_quota('chat', 'p16-day1-chat-4', null);
  perform pg_temp.check(not (r->>'allowed')::boolean and r->>'reason' = 'quota_exceeded', 'fourth chat on the signup day refused');
end $$;
commit;
reset role;
delete from public.quota_ledger where user_id = '00000000-0000-0000-0000-0000000001a5' and kind = 'chat';

-- =====================================================================================
-- E. Lifetime rows survive a purge; deleting the ACCOUNT still cascades
-- =====================================================================================
do $$ declare denied boolean := false; begin
  begin
    delete from public.quota_ledger where kind = 'photo_solve' and period_key = 'lifetime';
  exception when insufficient_privilege then denied := true; end;
  perform pg_temp.check(denied, 'a purge cannot delete lifetime rows while the account exists');
  denied := false;
  begin
    delete from public.quota_ledger where created_at < now() - interval '90 days';   -- the documented retention purge
  exception when insufficient_privilege then denied := true; end;
  perform pg_temp.check(denied, 'the documented age-based purge is refused while it would erase a lifetime row');
  perform pg_temp.check(exists (select 1 from public.quota_ledger where user_id = '00000000-0000-0000-0000-0000000001a1' and kind = 'photo_solve'),
                        'the lifetime row is still there');
  -- the same purge with the lifetime rows excluded (what docs/USAGE_REPORTS.sql now says) works
  delete from public.quota_ledger where created_at < now() - interval '90 days' and period_key <> 'lifetime';
end $$;
begin;
delete from auth.users where id = '00000000-0000-0000-0000-0000000001a1';
do $$ begin
  perform pg_temp.check(not exists (select 1 from public.quota_ledger where user_id = '00000000-0000-0000-0000-0000000001a1'),
                        'deleting the account cascades to its lifetime rows');
end $$;
rollback;

-- =====================================================================================
-- F. Pocket Notes: Pro-only INSERT in the database; existing notes stay readable / deletable
-- =====================================================================================
insert into public.pocket_notes (id, user_id, client_id, content_md) values
  ('00000000-0000-0000-0000-00000000ee01', '00000000-0000-0000-0000-0000000001a2', 'p16-oldnote-0001', 'saved before the plan changed')
on conflict do nothing;

begin;
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000001a2';
do $$ declare blocked boolean := false; n int; begin
  begin
    insert into public.pocket_notes (user_id, client_id, content_md)
    values ('00000000-0000-0000-0000-0000000001a2', 'p16-newnote-0001', 'a Free student tries to save a note');
  exception when insufficient_privilege then blocked := true; end;
  perform pg_temp.check(blocked, 'a Free student cannot INSERT a Pocket Note, even directly through RLS');
  select count(*) into n from public.pocket_notes where user_id = '00000000-0000-0000-0000-0000000001a2';
  perform pg_temp.check(n = 1, 'a Free student can still READ the note they already own (nothing is deleted by a plan change)');
  delete from public.pocket_notes where id = '00000000-0000-0000-0000-00000000ee01';
  select count(*) into n from public.pocket_notes where user_id = '00000000-0000-0000-0000-0000000001a2';
  perform pg_temp.check(n = 0, 'a Free student can still DELETE their own note');
end $$;
rollback;

begin;
insert into public.payments (id, user_id, provider, provider_payment_id, amount_bdt, billing_period)
  values ('00000000-0000-0000-0000-00000000dd02','00000000-0000-0000-0000-0000000001a3','bkash','p16-pay-2',399,'monthly');
insert into public.subscriptions (user_id, billing_period, starts_at, expires_at, payment_id)
  values ('00000000-0000-0000-0000-0000000001a3','monthly', now() - interval '1 day', now() + interval '10 days','00000000-0000-0000-0000-00000000dd02');
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000001a3';
do $$ declare n int; begin
  insert into public.pocket_notes (user_id, client_id, content_md)
  values ('00000000-0000-0000-0000-0000000001a3', 'p16-pronote-0001', 'a Pro student saves a note');
  select count(*) into n from public.pocket_notes where user_id = '00000000-0000-0000-0000-0000000001a3';
  perform pg_temp.check(n = 1, 'a Pro student can INSERT and read a Pocket Note');
  -- another student's id cannot be used (ownership is still enforced)
  begin
    insert into public.pocket_notes (user_id, client_id, content_md)
    values ('00000000-0000-0000-0000-0000000001a2', 'p16-spoof-0001', 'spoofed owner');
    perform pg_temp.check(false, 'a Pro student must not insert a note for someone else');
  exception when insufficient_privilege then null; end;
end $$;
rollback;

-- Pro EXPIRED: the plan check follows the subscription window, so an expired student is Free again.
begin;
insert into public.payments (id, user_id, provider, provider_payment_id, amount_bdt, billing_period)
  values ('00000000-0000-0000-0000-00000000dd03','00000000-0000-0000-0000-0000000001a4','bkash','p16-pay-3',399,'monthly');
insert into public.subscriptions (user_id, billing_period, starts_at, expires_at, payment_id)
  values ('00000000-0000-0000-0000-0000000001a4','monthly', now() - interval '40 days', now() - interval '10 days','00000000-0000-0000-0000-00000000dd03');
set local role authenticated;
set local request.jwt.claim.sub = '00000000-0000-0000-0000-0000000001a4';
do $$ declare blocked boolean := false; r jsonb; begin
  perform pg_temp.check(public.get_my_plan() = 'free', 'an expired subscription is Free');
  begin
    insert into public.pocket_notes (user_id, client_id, content_md)
    values ('00000000-0000-0000-0000-0000000001a4', 'p16-expired-0001', 'expired Pro tries to save');
  exception when insufficient_privilege then blocked := true; end;
  perform pg_temp.check(blocked, 'an expired Pro student cannot INSERT a note');
  r := public.consume_quota('pdf_upload', 'p16-pdf-exp-01', null);
  perform pg_temp.check(not (r->>'allowed')::boolean and r->>'plan' = 'free', 'an expired Pro student has no PDF allowance');
end $$;
rollback;

select ' PLAN 0016 DB TESTS PASSED' as result;
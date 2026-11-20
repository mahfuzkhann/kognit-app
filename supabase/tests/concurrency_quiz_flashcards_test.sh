#!/usr/bin/env bash
# Fires N simultaneous consume_quota calls and proves the limits cannot be bypassed:
#   (1) a FREE student's weekly quiz allowance (1, migration 0016) -> exactly 1 of N allowed
#   (2) a PRO student's flashcards guard, temporarily set to 5 -> exactly 5 of N allowed,
#       while that student's quiz and notes_ai meters are untouched (separate counters)
# Run by run_local_db_tests.sh against a throw-away database ONLY: it inserts test rows
# and temporarily changes the Pro flashcards limit (restored on exit).
set -euo pipefail
N="${1:-24}"
PSQL="${PSQL:-psql}"
psql_base=("$PSQL" -v ON_ERROR_STOP=1 -X -q -At)
FREE=00000000-0000-0000-0000-0000000000f6
PRO=00000000-0000-0000-0000-0000000000f7
# Earlier suites in the runner delete/replace the Pro plan_limits rows, so this script creates the row it
# needs itself (upsert) and puts the production value (200) back when it finishes.
set_flashcards_limit() { "${psql_base[@]}" -c "insert into public.plan_limits (plan, kind, period, limit_count, first_day_limit_count) values ('pro','flashcards','day',$1,null) on conflict (plan, kind) do update set limit_count = excluded.limit_count" >/dev/null; }
restore() { set_flashcards_limit 200 || true; }
trap restore EXIT

"${psql_base[@]}" -c "insert into auth.users (id, created_at, email) values ('$FREE', now() - interval '4 days', 'cq-free@example.test'), ('$PRO', now() - interval '4 days', 'cq-pro@example.test') on conflict do nothing"
"${psql_base[@]}" -c "insert into public.payments (id, user_id, provider, provider_payment_id, amount_bdt, billing_period) values ('00000000-0000-0000-0000-00000000cc07','$PRO','bkash','cq-pay-1',299,'monthly') on conflict do nothing"
"${psql_base[@]}" -c "insert into public.subscriptions (user_id, billing_period, starts_at, expires_at, payment_id) select '$PRO','monthly', now() - interval '1 day', now() + interval '10 days','00000000-0000-0000-0000-00000000cc07' where not exists (select 1 from public.subscriptions where user_id='$PRO')"

run_kind() { # $1=user $2=kind $3=prefix
  seq 1 "$N" | xargs -P "$N" -I{} "${psql_base[@]}" -c "
begin; set local role authenticated; set local request.jwt.claim.sub='$1';
select 'r:' || (public.consume_quota('$2','$3-{}0000', null)->>'allowed'); commit;" | grep -E '^r:(true|false)$' | sort | uniq -c
}

out="$(run_kind "$FREE" quiz cq-quiz)"
allowed="$(echo "$out" | awk '$2=="r:true"{print $1}')"
echo "free weekly quiz concurrency result: ${out//$'\n'/ | }"
if [ "${allowed:-0}" != "1" ]; then echo "QUIZ CONCURRENCY TEST FAILED: expected exactly 1 allowed, got ${allowed:-0}" >&2; exit 1; fi

set_flashcards_limit 5
out="$(run_kind "$PRO" flashcards cq-card)"
allowed="$(echo "$out" | awk '$2=="r:true"{print $1}')"
echo "pro flashcards concurrency result: ${out//$'\n'/ | }"
if [ "${allowed:-0}" != "5" ]; then echo "FLASHCARDS CONCURRENCY TEST FAILED: expected exactly 5 allowed, got ${allowed:-0}" >&2; exit 1; fi
other="$("${psql_base[@]}" -c "select count(*) from public.quota_ledger where user_id='$PRO' and kind <> 'flashcards'")"
if [ "$other" != "0" ]; then echo "FLASHCARDS CONCURRENCY TEST FAILED: flashcards calls leaked into another meter ($other)" >&2; exit 1; fi
echo "QUIZ + FLASHCARDS CONCURRENCY TESTS PASSED (exactly 1 of $N weekly quizzes; exactly 5 of $N flashcards; other meters untouched)"
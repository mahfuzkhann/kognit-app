#!/usr/bin/env bash
# Fires N simultaneous consume_quota calls and proves the finalized plan limits cannot be bypassed:
#   (1) a FREE student's one-time Photo Solve (lifetime, migration 0016)  -> exactly 1 of N allowed
#   (2) a PRO student's Photo Solve (5/day)                                -> exactly 5 of N allowed
#   (3) a PRO student's PDF uploads (2/day)                                -> exactly 2 of N allowed
# Run by run_local_db_tests.sh against a throw-away database ONLY. Earlier suites in the runner replace
# plan_limits rows, so this script upserts the production values it needs itself and re-asserts them.
set -euo pipefail
N="${1:-24}"
PSQL="${PSQL:-psql}"
psql_base=("$PSQL" -v ON_ERROR_STOP=1 -X -q -At)
FREE=00000000-0000-0000-0000-0000000000f8
PRO=00000000-0000-0000-0000-0000000000f9

"${psql_base[@]}" -c "insert into public.plan_limits (plan, kind, period, limit_count, first_day_limit_count) values ('free','photo_solve','lifetime',1,null), ('pro','photo_solve','day',5,null), ('pro','pdf_upload','day',2,null) on conflict (plan, kind) do update set period = excluded.period, limit_count = excluded.limit_count, first_day_limit_count = excluded.first_day_limit_count" >/dev/null
"${psql_base[@]}" -c "insert into auth.users (id, created_at, email) values ('$FREE', now() - interval '4 days', 'cp-free@example.test'), ('$PRO', now() - interval '4 days', 'cp-pro@example.test') on conflict do nothing"
"${psql_base[@]}" -c "insert into public.payments (id, user_id, provider, provider_payment_id, amount_bdt, billing_period) values ('00000000-0000-0000-0000-00000000cc09','$PRO','bkash','cp-pay-1',399,'monthly') on conflict do nothing"
"${psql_base[@]}" -c "insert into public.subscriptions (user_id, billing_period, starts_at, expires_at, payment_id) select '$PRO','monthly', now() - interval '1 day', now() + interval '10 days','00000000-0000-0000-0000-00000000cc09' where not exists (select 1 from public.subscriptions where user_id='$PRO')"

run_kind() { # $1=user $2=kind $3=prefix
  seq 1 "$N" | xargs -P "$N" -I{} "${psql_base[@]}" -c "
begin; set local role authenticated; set local request.jwt.claim.sub='$1';
select 'r:' || (public.consume_quota('$2','$3-{}0000', null)->>'allowed'); commit;" | grep -E '^r:(true|false)$' | sort | uniq -c
}
check() { # $1=label $2=expected $3=output
  local allowed; allowed="$(echo "$3" | awk '$2=="r:true"{print $1}')"
  echo "$1 concurrency result: ${3//$'\n'/ | }"
  if [ "${allowed:-0}" != "$2" ]; then echo "$1 CONCURRENCY TEST FAILED: expected exactly $2 allowed, got ${allowed:-0}" >&2; exit 1; fi
}

check "free lifetime photo solve" 1 "$(run_kind "$FREE" photo_solve cp-photo-f)"
check "pro photo solve"           5 "$(run_kind "$PRO"  photo_solve cp-photo-p)"
check "pro pdf upload"            2 "$(run_kind "$PRO"  pdf_upload  cp-pdf-p)"
other="$("${psql_base[@]}" -c "select count(*) from public.quota_ledger where user_id='$PRO' and kind not in ('photo_solve','pdf_upload')")"
if [ "$other" != "0" ]; then echo "PHOTO/PDF CONCURRENCY TEST FAILED: calls leaked into another meter ($other)" >&2; exit 1; fi
echo "PHOTO + PDF CONCURRENCY TESTS PASSED (exactly 1 of $N lifetime photo trials; exactly 5 of $N Pro photo solves; exactly 2 of $N Pro PDF uploads; other meters untouched)"
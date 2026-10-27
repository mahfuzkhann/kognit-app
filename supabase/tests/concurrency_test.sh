#!/usr/bin/env bash
# Fires N simultaneous consume_quota calls for ONE user whose daily limit is 1
# and asserts exactly one is allowed. Called by run_local_db_tests.sh.
set -euo pipefail
N="${1:-24}"
PSQL="${PSQL:-psql}"
psql_base=("$PSQL" -v ON_ERROR_STOP=1 -X -q -At)
"${psql_base[@]}" -c "insert into auth.users (id, created_at) values ('00000000-0000-0000-0000-0000000000d1', now() - interval '3 days') on conflict do nothing"
out="$(seq 1 "$N" | xargs -P "$N" -I{} "${psql_base[@]}" -c "
begin; set local role authenticated; set local request.jwt.claim.sub='00000000-0000-0000-0000-0000000000d1';
select (public.consume_quota('chat','conc-req-{}0000', null)->>'allowed'); commit;" | grep -E '^(true|false)$' | sort | uniq -c)"
allowed="$(echo "$out" | awk '$2=="true"{print $1}')"
echo "concurrency result: ${out//$'\n'/ | }"
if [ "${allowed:-0}" != "1" ]; then echo "CONCURRENCY TEST FAILED: expected exactly 1 allowed, got ${allowed:-0}" >&2; exit 1; fi
echo "CONCURRENCY TEST PASSED (exactly 1 of $N simultaneous requests counted)"
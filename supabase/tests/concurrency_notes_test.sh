#!/usr/bin/env bash
# Q-05: fires N simultaneous notes_ai consume_quota calls for ONE user whose daily
# limit is 6 and asserts exactly 6 are allowed, and that none of them touched the
# user's chat meter (separate counters). The chat counter's own concurrency proof
# is concurrency_test.sh; the two are deliberately separate files. Called by
# run_local_db_tests.sh against a throw-away database - never point PGDATABASE at a
# real one: it inserts test rows.
set -euo pipefail
N="${1:-24}"
PSQL="${PSQL:-psql}"
psql_base=("$PSQL" -v ON_ERROR_STOP=1 -X -q -At)
"${psql_base[@]}" -c "insert into auth.users (id, created_at) values ('00000000-0000-0000-0000-0000000000d2', now() - interval '3 days') on conflict do nothing"
out="$(seq 1 "$N" | xargs -P "$N" -I{} "${psql_base[@]}" -c "
begin; set local role authenticated; set local request.jwt.claim.sub='00000000-0000-0000-0000-0000000000d2';
select 'notes:' || (public.consume_quota('notes_ai','conc-notes-{}0000', null)->>'allowed'); commit;" | grep -E '^notes:(true|false)$' | sort | uniq -c)"
allowed="$(echo "$out" | awk '$2=="notes:true"{print $1}')"
echo "notes_ai concurrency result: ${out//$'\n'/ | }"
if [ "${allowed:-0}" != "6" ]; then echo "NOTES CONCURRENCY TEST FAILED: expected exactly 6 allowed, got ${allowed:-0}" >&2; exit 1; fi
chat_used="$("${psql_base[@]}" -c "select count(*) from public.quota_ledger where user_id='00000000-0000-0000-0000-0000000000d2' and kind='chat'")"
if [ "$chat_used" != "0" ]; then echo "NOTES CONCURRENCY TEST FAILED: notes calls leaked into the chat meter ($chat_used)" >&2; exit 1; fi
echo "NOTES CONCURRENCY TEST PASSED (exactly 6 of $N simultaneous requests counted; chat meter untouched)"
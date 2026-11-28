#!/usr/bin/env bash
# B06: N devices all holding version 1 of the SAME notebook save at the SAME instant.
# Exactly ONE may win; every other writer must get a conflict; the winner's content is
# stored untouched and the version is exactly 2. Also: N simultaneous CREATES of one new id
# with different content -> exactly one created, the rest conflict (no duplicate, no overwrite).
# Called by run_local_db_tests.sh against a throw-away database - never a real one.
set -euo pipefail
N="${1:-24}"
PSQL="${PSQL:-psql}"
psql_base=("$PSQL" -v ON_ERROR_STOP=1 -X -q -At)
U=00000000-0000-0000-0000-0000000000e5
"${psql_base[@]}" -c "insert into auth.users (id, created_at, email) values ('$U', now(), 'cas-conc@example.test') on conflict do nothing"
"${psql_base[@]}" -c "begin; set local role authenticated; set local request.jwt.claim.sub='$U'; select public.save_user_project('conc-nb','base','[]',null); commit;" >/dev/null

out="$(seq 1 "$N" | xargs -P "$N" -I{} "${psql_base[@]}" -c "
begin; set local role authenticated; set local request.jwt.claim.sub='$U';
select 'save:' || (public.save_user_project('conc-nb','writer-{}', jsonb_build_array(jsonb_build_object('id','c','n',{})), 1)->>'status'); commit;" | grep -E '^save:' | sort | uniq -c)"
ok="$(echo "$out" | awk '$2=="save:ok"{print $1}')"
conf="$(echo "$out" | awk '$2=="save:conflict"{print $1}')"
echo "CAS concurrency result: ${out//$'\n'/ | }"
if [ "${ok:-0}" != "1" ] || [ "${conf:-0}" != "$((N-1))" ]; then
  echo "PROJECT CAS CONCURRENCY TEST FAILED: expected exactly 1 ok and $((N-1)) conflicts" >&2; exit 1; fi
ver="$("${psql_base[@]}" -c "select version from public.user_projects where id='conc-nb'")"
if [ "$ver" != "2" ]; then echo "PROJECT CAS CONCURRENCY TEST FAILED: version is $ver, expected 2" >&2; exit 1; fi
title="$("${psql_base[@]}" -c "select title from public.user_projects where id='conc-nb'")"
case "$title" in writer-*) ;; *) echo "PROJECT CAS CONCURRENCY TEST FAILED: stored title '$title' is not a writer's" >&2; exit 1;; esac
# the stored title and the stored chat must come from the SAME writer (no half-merged row)
n="$("${psql_base[@]}" -c "select chats->0->>'n' from public.user_projects where id='conc-nb'")"
if [ "$title" != "writer-$n" ]; then echo "PROJECT CAS CONCURRENCY TEST FAILED: title $title and chat $n come from different writers" >&2; exit 1; fi

out2="$(seq 1 "$N" | xargs -P "$N" -I{} "${psql_base[@]}" -c "
begin; set local role authenticated; set local request.jwt.claim.sub='$U';
select 'create:' || (public.save_user_project('conc-new','creator-{}','[]',null)->>'status'); commit;" | grep -E '^create:' | sort | uniq -c)"
created="$(echo "$out2" | awk '$2=="create:ok"{print $1}')"
rows="$("${psql_base[@]}" -c "select count(*) from public.user_projects where id='conc-new'")"
echo "CAS concurrent-create result: ${out2//$'\n'/ | }"
if [ "${created:-0}" != "1" ] || [ "$rows" != "1" ]; then
  echo "PROJECT CAS CONCURRENCY TEST FAILED: expected exactly 1 created and 1 row (got ${created:-0} / $rows)" >&2; exit 1; fi
"${psql_base[@]}" -c "delete from auth.users where id='$U'"
echo "PROJECT CAS CONCURRENCY TEST PASSED (exactly 1 of $N simultaneous saves won; $((N-1)) conflicts; 1 of $N simultaneous creates)"
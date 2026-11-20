#!/usr/bin/env bash
# Applies every migration to a THROWAWAY database on a local PostgreSQL server
# and runs the behaviour + concurrency tests. Never touches Supabase.
#
#   PGHOST=/tmp/pgsock PGUSER=postgres supabase/tests/run_local_db_tests.sh
#
# Requires: psql and a reachable PostgreSQL 14+ server whose user may create
# databases. See docs/DEPLOYMENT.md ("Running the database tests").
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
db="kloriz_dbtest_$$"
createdb "$db"
trap 'dropdb --if-exists "$db" >/dev/null 2>&1 || true' EXIT
export PGDATABASE="$db"
psql -v ON_ERROR_STOP=1 -X -q -f "$here/sandbox_stubs.sql"
# Migrations are applied ONCE each, in order (as in production). The idempotency re-apply of the
# older migrations runs BEFORE the newer ones exist: re-applying 0011 AFTER 0015 would re-add the
# narrower plan_limits kind constraint over rows that 0015 legitimately introduced, which is a
# property of re-running history out of order, not of a deployment.
apply_one() { echo "applying $(basename "$1")"; psql -v ON_ERROR_STOP=1 -X -q -f "$1" >/dev/null; }
for f in "$repo"/supabase/migrations/*.sql; do
  case "$(basename "$f")" in 0014_*|0015_*|0016_*|001[7-9]_*|0[2-9]*) continue ;; esac
  apply_one "$f"
done
echo "re-applying idempotent migrations 0008-0012 (must succeed twice)"
for f in "$repo"/supabase/migrations/0008*.sql "$repo"/supabase/migrations/0009*.sql "$repo"/supabase/migrations/0010*.sql "$repo"/supabase/migrations/0011*.sql "$repo"/supabase/migrations/0012*.sql; do
  psql -v ON_ERROR_STOP=1 -X -q -f "$f" >/dev/null
done
apply_one "$repo"/supabase/migrations/0014_ai_engine_2_plans_usage_curriculum.sql
apply_one "$repo"/supabase/migrations/0015_flashcards_meter.sql
echo "re-applying idempotent migrations 0014 and 0015 (must succeed twice)"
for f in "$repo"/supabase/migrations/0014*.sql "$repo"/supabase/migrations/0015*.sql; do
  psql -v ON_ERROR_STOP=1 -X -q -f "$f" >/dev/null
done
# 0016 (final Free/Pro plan) is applied after 0014/0015 have been proven idempotent, then re-applied once.
apply_one "$repo"/supabase/migrations/0016_free_pro_plan_final.sql
echo "re-applying idempotent migration 0016 (must succeed twice)"
psql -v ON_ERROR_STOP=1 -X -q -f "$repo"/supabase/migrations/0016_free_pro_plan_final.sql >/dev/null
psql -v ON_ERROR_STOP=1 -X -q -f "$here/plan_final_0016_test.sql"
psql -v ON_ERROR_STOP=1 -X -q -f "$here/engine2_behaviour_test.sql"
psql -v ON_ERROR_STOP=1 -X -q -f "$here/quota_weekly_and_flashcards_test.sql"
psql -v ON_ERROR_STOP=1 -X -q -f "$here/quota_behaviour_test.sql"
psql -v ON_ERROR_STOP=1 -X -q -f "$here/hardening_behaviour_test.sql"
psql -v ON_ERROR_STOP=1 -X -q -f "$here/ledger_cap_and_privileges_test.sql"
bash "$here/concurrency_test.sh" 24
bash "$here/concurrency_notes_test.sh" 24
bash "$here/concurrency_quiz_flashcards_test.sh" 24
bash "$here/concurrency_photo_pdf_test.sh" 24
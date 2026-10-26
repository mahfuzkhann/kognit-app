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
for f in "$repo"/supabase/migrations/*.sql; do
  echo "applying $(basename "$f")"
  psql -v ON_ERROR_STOP=1 -X -q -f "$f" >/dev/null
done
echo "re-applying idempotent migrations 0008-0011 (must succeed twice)"
for f in "$repo"/supabase/migrations/0008*.sql "$repo"/supabase/migrations/0009*.sql "$repo"/supabase/migrations/0010*.sql "$repo"/supabase/migrations/0011*.sql; do
  psql -v ON_ERROR_STOP=1 -X -q -f "$f" >/dev/null
done
psql -v ON_ERROR_STOP=1 -X -q -f "$here/quota_behaviour_test.sql"
psql -v ON_ERROR_STOP=1 -X -q -f "$here/hardening_behaviour_test.sql"
bash "$here/concurrency_test.sh" 24
bash "$here/concurrency_notes_test.sh" 30
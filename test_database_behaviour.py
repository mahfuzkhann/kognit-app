"""
Runs the real-PostgreSQL behaviour suite (supabase/tests/run_local_db_tests.sh)
when a server is available; SKIPS otherwise (it never fails because Postgres is
missing). It applies every migration to a throwaway database, re-applies the
idempotent ones, runs the quota/subscription/RLS behaviour tests and a 24-way
concurrency test.

Enable with:   PGHOST=<socket dir or host> PGUSER=<user that may CREATE DATABASE> pytest test_database_behaviour.py
"""
import os
import shutil
import subprocess

import pytest

REPO = os.path.dirname(os.path.abspath(__file__))
RUNNER = os.path.join(REPO, "supabase", "tests", "run_local_db_tests.sh")


@pytest.mark.skipif(not (shutil.which("psql") and shutil.which("createdb") and os.getenv("PGHOST")),
                    reason="needs psql/createdb and PGHOST pointing at a throwaway PostgreSQL server")
def test_migrations_apply_and_database_behaviour_holds():
    result = subprocess.run(["bash", RUNNER], capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert "DB BEHAVIOUR TESTS PASSED" in result.stdout
    assert "CONCURRENCY TEST PASSED" in result.stdout
"""
Pre-Phase 10: static contract tests for migrations 0008-0010. They read the SQL
text, so they run anywhere. The BEHAVIOURAL tests (real PostgreSQL: limits,
concurrency, refunds, privileges, renewal, RLS) are in supabase/tests/ and are
run by test_database_behaviour.py when a PostgreSQL server is available.
"""
import os
import re

REPO = os.path.dirname(os.path.abspath(__file__))
MIGRATIONS = os.path.join(REPO, "supabase", "migrations")


def read(prefix):
    name = next(n for n in sorted(os.listdir(MIGRATIONS)) if n.startswith(prefix))
    return open(os.path.join(MIGRATIONS, name), encoding="utf-8").read()


def sql_only(text):
    return re.sub(r"--[^\n]*", "", text)


M8, M9, M10 = read("0008"), read("0009"), read("0010")
NEW_TABLES_8 = ("plan_limits", "plan_prices", "payments", "subscriptions", "quota_ledger")


class TestAdditiveAndSafeToRerun:
    def test_nothing_destructive_is_executable(self):
        for sql in (M8, M9, M10):
            body = re.sub(r"revoke[^;]*;", "", sql_only(sql).lower())   # REVOKE ... TRUNCATE removes a privilege, it truncates nothing
            assert not re.search(r"\bdrop\s+table\b|\btruncate\b|\bdelete\s+from\b|\bdrop\s+column\b|\balter\s+table\s+\S+\s+drop\b", body)

    def test_tables_use_if_not_exists_and_seeds_never_overwrite(self):
        for sql in (M8, M9, M10):
            for m in re.finditer(r"create table (if not exists )?(\S+)", sql_only(sql), re.I):
                assert m.group(1), f"{m.group(2)} must be 'create table if not exists'"
        assert sql_only(M8).lower().count("on conflict") >= 2

    def test_policies_are_recreated_or_guarded(self):
        for sql in (M8, M9):
            body = sql_only(sql).lower()
            for m in re.finditer(r"create policy (\w+) on (public\.\w+)", body):
                assert f"drop policy if exists {m.group(1)} on {m.group(2)}" in body
        assert "not exists (select 1 from pg_policies" in sql_only(M10).lower()

    def test_0010_touches_only_user_projects(self):
        body = sql_only(M10).lower()
        assert set(re.findall(r"alter table (?:public\.)?(\w+)", body)) == {"user_projects"}
        for other in ("quiz_attempts", "learning_evidence", "pocket_notes", "student_profiles"):
            assert other not in body


class TestRowLevelSecurity:
    def test_every_new_table_has_rls_enabled(self):
        enabled = set(re.findall(r"alter table public\.(\w+)\s+enable row level security", sql_only(M8 + M9 + M10).lower()))
        assert enabled == set(NEW_TABLES_8) | {"ai_usage_events", "user_projects"}

    def test_students_have_no_write_policy_on_authority_tables(self):
        body = sql_only(M8).lower()
        for m in re.finditer(r"create policy \w+ on public\.(\w+)\s+for (\w+)", body):
            table, op = m.groups()
            assert op == "select", f"{table} must not give students a {op} policy"
        assert "public.quota_ledger" not in " ".join(re.findall(r"create policy \w+ on (public\.\w+)", body)), \
            "quota_ledger must have NO student-facing policy"

    def test_write_privileges_are_revoked_from_students(self):
        body = sql_only(M8).lower()
        assert re.search(r"revoke insert, update, delete, truncate on\s+public\.plan_limits, public\.plan_prices, public\.payments,\s*public\.subscriptions, public\.quota_ledger\s+from anon, authenticated", body)

    def test_usage_events_are_insert_own_only(self):
        body = sql_only(M9).lower()
        policies = re.findall(r"create policy (\w+) on public\.ai_usage_events\s+for (\w+) to (\w+)\s+with check \(user_id = auth\.uid\(\)\)", body)
        assert policies == [("ai_usage_events_insert_own", "insert", "authenticated")]
        assert "revoke select, update, delete, truncate on public.ai_usage_events from anon, authenticated" in body

    def test_user_projects_policies_are_owner_scoped_for_every_operation(self):
        body = re.sub(r"\s+", " ", sql_only(M10).lower())
        for op, clause in (("select", "using (auth.uid() = user_id)"), ("insert", "with check (auth.uid() = user_id)"),
                           ("update", "using (auth.uid() = user_id) with check (auth.uid() = user_id)"),
                           ("delete", "using (auth.uid() = user_id)")):
            assert f"for {op} to authenticated {clause}" in body, op


class TestQuotaFunctionDesign:
    def test_consume_quota_takes_no_limit_or_plan_from_the_caller(self):
        sig = re.search(r"function public\.consume_quota\((.*?)\)\s*returns", M8, re.S).group(1).lower()
        assert re.findall(r"p_\w+", sig) == ["p_kind", "p_request_id", "p_refund_token_hash"], \
            "limits and plan must come from the database, never from the caller"

    def test_quota_functions_are_security_definer_with_a_pinned_search_path(self):
        for fn in ("consume_quota", "refund_quota", "activate_pro_subscription"):
            block = re.search(rf"function public\.{fn}\(.*?\$\$;", M8, re.S).group(0).lower()
            assert "security definer" in block and "set search_path = public, pg_temp" in block, fn

    def test_concurrent_requests_are_serialised_per_user_and_kind(self):
        assert "pg_advisory_xact_lock(hashtextextended(v_uid::text || ':' || p_kind, 0))" in M8

    def test_refund_requires_the_secret_token_hash_match(self):
        body = re.sub(r"\s+", " ", sql_only(M8).lower())
        assert "refund_token_hash = encode(sha256(convert_to(p_refund_token, 'utf8')), 'hex')" in body
        assert "refund_token_hash is not null" in body

    def test_reset_rules_use_dhaka_time_and_monday_weeks(self):
        assert M8.count("'Asia/Dhaka'") >= 6 and "date_trunc('week'" in M8

    def test_free_limits_match_the_ceo_decision_and_pro_limits_are_not_invented(self):
        seed = re.search(r"insert into public\.plan_limits.*?on conflict", sql_only(M8), re.S).group(0)
        rows = re.findall(r"\('(\w+)',\s*'(\w+)',\s*'(\w+)',\s*(\d+),\s*(\d+|null)\)", seed)
        assert rows == [("free", "chat", "day", "1", "3"), ("free", "quiz", "week", "3", "null")]
        assert "'pro'" not in seed, "Pro limits are an open CEO decision and must not be seeded"
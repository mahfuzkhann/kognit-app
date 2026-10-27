"""
Pre-Phase 10 finalization - Q-06: ai_usage_events can only be written by the
trusted backend path (public.record_ai_usage_events, authenticated by the server
secret USAGE_WRITE_KEY), never directly by a student.

The database side (direct INSERT revoked, forged plan/cost/model/time ignored,
validation, per-user cap, anon refused, cross-user reads impossible) is proven
against PostgreSQL by supabase/tests/hardening_behaviour_test.sql. This file
covers the backend half and static guards.
"""
import asyncio
import pathlib
import re

import pytest

from backend import database, quota_service, usage_telemetry

ROOT = pathlib.Path(__file__).resolve().parent
KEY = "unit-test-usage-write-key-0123456789"


@pytest.fixture
def rpc_log(monkeypatch):
    calls = []

    async def fake_rpc(user_token, function, params):
        calls.append({"function": function, "params": params, "token": user_token})
        return 1
    monkeypatch.setattr(quota_service, "_rpc", fake_rpc)
    return calls


@pytest.fixture
def real_writer(monkeypatch):
    """conftest replaces save_ai_usage_events with a recorder; these tests need the real one."""
    import importlib
    import backend.usage_telemetry as ut
    monkeypatch.undo()  # drop the autouse fake
    return importlib.reload(ut)


def run(coro):
    return asyncio.run(coro)


ROW = {
    "request_id": "req-1", "plan": "pro", "provider": "gemini", "model": "gemini-x", "operation": "stream",
    "attempt": 1, "input_tokens": 10, "output_tokens": 5, "thinking_tokens": None, "total_tokens": 15,
    "duration_ms": 100, "ttft_ms": 20, "status": "success", "error_code": None, "retry_count": 0,
    "recovery_used": False,
    # things that must never be sent to the database:
    "user_id": "someone-else", "estimated_cost_usd": 99, "pricing_version": "forged", "created_at": "2000-01-01",
    "prompt": "SECRET STUDENT PROMPT",
}


class TestBackendWriter:
    def test_rows_go_through_the_trusted_rpc_with_the_students_token_and_the_server_key(self, real_writer, rpc_log, monkeypatch):
        monkeypatch.setenv("USAGE_WRITE_KEY", KEY)
        run(real_writer.save_ai_usage_events("student-token", "user-1", [dict(ROW)]))
        call = rpc_log[0]
        assert call["function"] == "record_ai_usage_events" and call["token"] == "student-token"
        assert call["params"]["p_write_key"] == KEY

    def test_server_derived_fields_are_never_sent(self, real_writer, rpc_log, monkeypatch):
        monkeypatch.setenv("USAGE_WRITE_KEY", KEY)
        run(real_writer.save_ai_usage_events("student-token", "user-1", [dict(ROW)]))
        sent = rpc_log[0]["params"]["p_events"][0]
        for forbidden in ("user_id", "plan", "estimated_cost_usd", "pricing_version", "created_at", "prompt"):
            assert forbidden not in sent
        assert sent["input_tokens"] == 10 and sent["operation"] == "stream"

    def test_without_a_key_nothing_is_sent_and_chat_is_unaffected(self, real_writer, rpc_log, monkeypatch, caplog):
        monkeypatch.delenv("USAGE_WRITE_KEY", raising=False)
        real_writer._warned_missing_key = False
        with caplog.at_level("WARNING"):
            run(real_writer.save_ai_usage_events("t", "u", [dict(ROW)]))
            run(real_writer.save_ai_usage_events("t", "u", [dict(ROW)]))
        assert rpc_log == []
        assert caplog.text.count("USAGE_WRITE_KEY is not set") == 1, "warned once, not per request"

    def test_database_failure_is_a_database_error_not_a_crash(self, real_writer, monkeypatch):
        monkeypatch.setenv("USAGE_WRITE_KEY", KEY)

        async def down(user_token, function, params):
            raise quota_service.QuotaUnavailable("down")
        monkeypatch.setattr(quota_service, "_rpc", down)
        with pytest.raises(database.DatabaseError):
            run(real_writer.save_ai_usage_events("t", "u", [dict(ROW)]))

    def test_settlement_never_raises_even_if_persistence_fails(self, real_writer, monkeypatch):
        monkeypatch.setenv("USAGE_WRITE_KEY", KEY)

        async def down(user_token, function, params):
            raise quota_service.QuotaUnavailable("down")
        monkeypatch.setattr(quota_service, "_rpc", down)

        async def go():
            c = real_writer.start_collection("req-9", "")
            c.bind("u", "t")
            real_writer.record_ai_call(operation="chat", status="success")
            await real_writer.settle_request(c)
        run(go())  # must not raise

    def test_the_write_key_is_never_logged(self, real_writer, monkeypatch, caplog):
        monkeypatch.setenv("USAGE_WRITE_KEY", KEY)

        async def down(user_token, function, params):
            raise quota_service.QuotaUnavailable("down")
        monkeypatch.setattr(quota_service, "_rpc", down)
        with caplog.at_level("DEBUG"):
            with pytest.raises(database.DatabaseError):
                run(real_writer.save_ai_usage_events("secret-student-token", "u", [dict(ROW)]))
        assert KEY not in caplog.text and "secret-student-token" not in caplog.text

    def test_rows_are_persisted_once_even_if_settled_twice(self, real_writer, rpc_log, monkeypatch):
        monkeypatch.setenv("USAGE_WRITE_KEY", KEY)

        async def go():
            c = real_writer.start_collection("req-10", "")
            c.bind("u", "t")
            real_writer.record_ai_call(operation="chat", status="success")
            await real_writer.settle_request(c)
            await real_writer.settle_request(c)
        run(go())
        assert len([c for c in rpc_log if c["function"] == "record_ai_usage_events"]) == 1


class TestStaticGuards:
    def _source_files(self, *suffixes):
        for base in ("backend", "static", "templates"):
            for p in (ROOT / base).rglob("*"):
                if p.is_file() and p.suffix in suffixes and "vendor" not in p.parts:
                    yield p

    def test_no_code_path_inserts_into_ai_usage_events_over_rest(self):
        for p in self._source_files(".py", ".js", ".html"):
            text = p.read_text(errors="ignore")
            assert "rest/v1/ai_usage_events" not in text, f"{p} writes telemetry directly"
            assert not re.search(r"from\(['\"]ai_usage_events['\"]\)", text), f"{p} touches the table from the browser"

    def test_no_service_role_key_is_used_or_exposed(self):
        for p in self._source_files(".py", ".js", ".html"):
            text = p.read_text(errors="ignore")
            assert not re.search(r"SERVICE_ROLE|service_role_key|SUPABASE_SERVICE", text, re.I) or p.name == "payments.py", p

    def test_the_write_key_never_reaches_the_browser(self):
        for p in self._source_files(".js", ".html"):
            assert "USAGE_WRITE_KEY" not in p.read_text(errors="ignore"), p

    def test_no_secret_value_is_committed(self):
        for p in ROOT.rglob("*"):
            if p.is_file() and ".git" not in p.parts and p.suffix in {".py", ".md", ".sql", ".sh", ".txt", ".example"}:
                if p.name.startswith("test_"):
                    continue
                assert not re.search(r"USAGE_WRITE_KEY\s*=\s*\S{12,}", p.read_text(errors="ignore")), p


class TestMigrationContract:
    SQL = (ROOT / "supabase/migrations/0011_quota_notes_meter_and_telemetry_integrity.sql").read_text()

    def test_direct_student_writes_are_removed(self):
        assert "drop policy if exists ai_usage_events_insert_own" in self.SQL
        assert re.search(r"revoke insert, update, delete, truncate on public\.ai_usage_events from public, anon, authenticated", self.SQL)

    def test_the_writer_is_pinned_and_minimally_granted(self):
        assert re.search(r"record_ai_usage_events[\s\S]*?security definer\s+set search_path = public, pg_temp", self.SQL)
        assert "revoke all on function public.record_ai_usage_events(text, jsonb) from public, anon" in self.SQL
        assert "grant execute on function public.record_ai_usage_events(text, jsonb) to authenticated" in self.SQL

    def test_cost_and_time_columns_cannot_be_supplied(self):
        assert "null, null, v_now" in self.SQL

    def test_every_function_in_the_migration_sets_a_search_path(self):
        for m in re.finditer(r"create or replace function ([\w.]+\([^)]*\))([\s\S]*?)\$\$", self.SQL):
            assert "set search_path" in m.group(2), m.group(1)

    def test_migration_does_not_delete_or_drop_student_data(self):
        body = re.sub(r"--.*", "", self.SQL)
        assert not re.search(r"\b(drop table|truncate table|delete from public\.(?!plan_limits)|alter table .* drop column)", body, re.I)

class TestMigration0012Contract:
    SQL = (ROOT / "supabase/migrations/0012_usage_privileges_and_ledger_cap.sql").read_text(encoding="utf-8")

    def test_it_only_removes_privileges_from_api_roles(self):
        assert "revoke all on table public.ai_usage_events from public, anon, authenticated" in self.SQL
        body = re.sub(r"--.*", "", self.SQL)
        grants = re.findall(r"\bgrant\b[^;]*;", body, re.I)
        assert grants == ["grant execute on function public.consume_quota(text, text, text) to authenticated;"], grants

    def test_consume_quota_keeps_its_hardening_and_has_the_cap(self):
        m = re.search(r"create or replace function public\.consume_quota\(([\s\S]*?)\n\$\$;", self.SQL)
        assert m and "security definer" in m.group(0) and "set search_path = public, pg_temp" in m.group(0)
        assert "v_cap        constant integer := 500" in m.group(0) and "'rate_limited'" in m.group(0)
        assert "'chat', 'quiz', 'notes_ai'" in m.group(0), "all three kinds are still accepted"

    def test_it_deletes_nothing(self):
        body = re.sub(r"--.*", "", self.SQL)
        assert not re.search(r"\b(delete\s+from|drop\s+table|truncate|alter\s+table[^;]*drop)", body, re.I)
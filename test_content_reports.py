"""
Tests for AI-content reports (batch B02): backend/report_engine.py, the report
section of backend/database.py, POST /api/report in backend/main.py, and the
static contract of supabase/migrations/0018_content_reports.sql.

Run with: GEMINI_API_KEY=dummy python3 -m pytest test_content_reports.py -v

HOW THESE TESTS ARE BUILT
  The REAL route and the REAL database.py function run. Only the network edge is
  faked: httpx.MockTransport plays PostgREST's /rest/v1/rpc/submit_content_report.
  The SQL function itself (privileges, cap, cascade, CHECKs) is tested against a
  real PostgreSQL by supabase/tests/content_reports_0018_test.sql, which
  run_local_db_tests.sh runs.
"""
import json as jsonlib
import logging
import os
import re
import sys
import uuid

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import database, report_engine
import backend.main as main_module

REPO = os.path.abspath(os.path.dirname(__file__))
MIGRATION = os.path.join(REPO, "supabase", "migrations", "0018_content_reports.sql")
ALICE, BOB = "aaaaaaaa-0000-0000-0000-000000000001", "bbbbbbbb-0000-0000-0000-000000000002"
SECRET_NOTE = "PRIVATE-NOTE-জমা-রাখা-9931"
SECRET_ANSWER = "PRIVATE-ANSWER-TEXT-7742"


def _sql():
    text = open(MIGRATION, encoding="utf-8").read()
    return re.sub(r"--[^\n]*", "", text)


def body(**over):
    base = {"category": "inaccurate", "ai_response": "The answer text", "prompt": "the question",
            "note": "wrong formula", "request_id": "abc123DEF"}
    base.update(over)
    return base


# ===========================================================================
# 1. report_engine (pure)
# ===========================================================================

class TestBuildReport:
    def test_happy_path_returns_exactly_the_rpc_parameters(self):
        out = report_engine.build_report(body())
        assert out == {
            "p_category": "inaccurate", "p_note": "wrong formula", "p_ai_response": "The answer text",
            "p_prompt": "the question", "p_request_id": "abc123DEF", "p_platform": "web", "p_app_version": None,
        }

    @pytest.mark.parametrize("cat", ["unsafe", "inappropriate", "inaccurate", "privacy", "other"])
    def test_every_category_is_accepted_case_insensitively(self, cat):
        assert report_engine.build_report(body(category=cat.upper()))["p_category"] == cat

    @pytest.mark.parametrize("bad", [None, "", "  ", "spam", "other; drop table", 5, ["other"], {"a": 1}])
    def test_bad_category_is_refused(self, bad):
        with pytest.raises(report_engine.ReportValidationError) as e:
            report_engine.build_report(body(category=bad))
        assert e.value.code == "REPORT_INVALID_CATEGORY"

    @pytest.mark.parametrize("raw", [None, [], "text", 5])
    def test_non_object_body_is_refused(self, raw):
        with pytest.raises(report_engine.ReportValidationError) as e:
            report_engine.build_report(raw)
        assert e.value.code == "REPORT_INVALID"

    @pytest.mark.parametrize("missing", [None, "", "   ", 123, ["x"]])
    def test_missing_answer_is_refused(self, missing):
        with pytest.raises(report_engine.ReportValidationError) as e:
            report_engine.build_report(body(ai_response=missing))
        assert e.value.code == "REPORT_MISSING_CONTENT"

    def test_note_over_limit_is_refused_and_at_limit_is_kept(self):
        with pytest.raises(report_engine.ReportValidationError) as e:
            report_engine.build_report(body(note="a" * 501))
        assert e.value.code == "REPORT_NOTE_TOO_LONG"
        assert len(report_engine.build_report(body(note="a" * 500))["p_note"]) == 500

    def test_note_is_optional(self):
        assert report_engine.build_report(body(note=None))["p_note"] is None
        assert report_engine.build_report(body(note="   "))["p_note"] is None

    def test_long_excerpts_are_truncated_not_refused(self):
        out = report_engine.build_report(body(ai_response="b" * 9000, prompt="p" * 9000))
        assert len(out["p_ai_response"]) == report_engine.MAX_RESPONSE_EXCERPT_CHARS
        assert len(out["p_prompt"]) == report_engine.MAX_PROMPT_EXCERPT_CHARS

    def test_control_and_bidi_characters_are_stripped(self):
        out = report_engine.build_report(body(ai_response="a\x00b\x07c\u202ed", note="n\x1fote"))
        assert out["p_ai_response"] == "abcd" and out["p_note"] == "note"

    def test_newlines_and_bangla_are_preserved(self):
        out = report_engine.build_report(body(ai_response="লাইন ১\nলাইন ২"))
        assert out["p_ai_response"] == "লাইন ১\nলাইন ২"

    @pytest.mark.parametrize("rid", ["bad id", "x" * 65, "a/b", "", None, 5])
    def test_malformed_request_id_is_dropped_not_refused(self, rid):
        assert report_engine.build_report(body(request_id=rid))["p_request_id"] is None

    def test_client_supplied_identity_and_status_fields_are_ignored(self):
        out = report_engine.build_report(body(user_id="x", status="actioned", platform="android",
                                              app_version="9.9.9", created_at="2020-01-01", id="z"))
        assert set(out) == {"p_category", "p_note", "p_ai_response", "p_prompt", "p_request_id",
                            "p_platform", "p_app_version"}
        assert out["p_platform"] == "web" and out["p_app_version"] is None

    @pytest.mark.parametrize("header,expected", [
        ("android/1.2.3", ("android", "1.2.3")),
        (" ANDROID/10.0.1 ", ("android", "10.0.1")),
        ("web/1.0.0", ("web", "1.0.0")),
        ("ios/1.0.0", ("web", None)),
        ("android/1.2", ("web", None)),
        ("android/1.2.3; DROP", ("web", None)),
        ("", ("web", None)), (None, ("web", None)),
    ])
    def test_platform_and_version_come_only_from_the_header(self, header, expected):
        out = report_engine.build_report(body(), header)
        assert (out["p_platform"], out["p_app_version"]) == expected


# ===========================================================================
# 2. migration 0018: static contract + parity with the Python limits
# ===========================================================================

class TestMigrationContract:
    def test_categories_in_sql_equal_python(self):
        m = re.search(r"category in \(([^)]*)\)", _sql())
        assert set(re.findall(r"'(\w+)'", m.group(1))) == set(report_engine.CATEGORIES)

    def test_limits_in_sql_equal_python(self):
        sql = re.sub(r"\s+", " ", _sql())
        assert f"char_length(note) between 1 and {report_engine.MAX_NOTE_CHARS}" in sql
        assert f"char_length(ai_response_excerpt) between 1 and {report_engine.MAX_RESPONSE_EXCERPT_CHARS}" in sql
        assert f"char_length(prompt_excerpt) between 1 and {report_engine.MAX_PROMPT_EXCERPT_CHARS}" in sql
        assert f">= {report_engine.MAX_REPORTS_PER_DAY}" in sql

    def test_platforms_in_sql_equal_python(self):
        m = re.search(r"platform in \(([^)]*)\)", _sql())
        assert set(re.findall(r"'(\w+)'", m.group(1))) == set(report_engine.PLATFORMS)

    def test_request_id_and_version_patterns_match_python(self):
        sql = _sql()
        assert report_engine.REQUEST_ID_RE.pattern in sql
        assert report_engine.APP_VERSION_RE.pattern in sql

    def test_students_get_no_table_privilege_and_no_policy(self):
        sql = _sql().lower()
        assert "alter table public.content_reports enable row level security" in sql
        assert "revoke all on public.content_reports from public, anon, authenticated" in sql
        assert "create policy" not in sql
        assert not re.search(r"grant\s+(select|insert|update|delete|all)[^;]*on\s+public\.content_reports", sql)

    def test_function_is_security_definer_with_pinned_search_path_and_authenticated_only(self):
        sql = re.sub(r"\s+", " ", _sql().lower())
        assert "security definer" in sql and "set search_path = public, pg_temp" in sql
        assert "revoke all on function public.submit_content_report(text, text, text, text, text, text, text) from public, anon, authenticated" in sql
        assert "grant execute on function public.submit_content_report(text, text, text, text, text, text, text) to authenticated" in sql
        assert "grant execute" not in sql.replace(
            "grant execute on function public.submit_content_report(text, text, text, text, text, text, text) to authenticated", "")

    def test_reporter_comes_from_auth_uid_and_there_is_no_user_id_parameter(self):
        sql = re.sub(r"\s+", " ", _sql().lower())
        assert "v_uid uuid := auth.uid()" in sql
        sig = re.search(r"function public\.submit_content_report\((.*?)\) returns", sql).group(1)
        assert "user_id" not in sig and "status" not in sig

    def test_account_deletion_cascades_and_migration_is_additive(self):
        sql = re.sub(r"\s+", " ", _sql().lower())
        assert "user_id uuid not null references auth.users(id) on delete cascade" in sql
        assert not re.search(r"drop\s+table|truncate|delete\s+from|alter\s+table\s+(?!public\.content_reports)", sql)

    def test_python_function_arguments_match_sql_signature(self):
        sql = re.sub(r"\s+", " ", _sql())
        sig = re.search(r"function public\.submit_content_report\((.*?)\) returns", sql).group(1)
        names = [p.strip().split(" ")[0] for p in sig.split(",")]
        assert names == list(report_engine.build_report(body()).keys())


# ===========================================================================
# 3. HTTP endpoint with a faked PostgREST
# ===========================================================================

class FakeReportsDb:
    """Plays /rest/v1/rpc/submit_content_report. Records every call so the tests
    can inspect exactly what the backend sent."""

    def __init__(self):
        self.calls = []
        self.mode = "ok"  # ok | limit | missing | server_error | network | odd

    def handler(self, request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith("/rpc/submit_content_report"):
            return httpx.Response(404)
        self.calls.append({"auth": request.headers.get("authorization"),
                           "apikey": request.headers.get("apikey"),
                           "json": jsonlib.loads(request.content)})
        if self.mode == "network":
            raise httpx.ConnectError("boom", request=request)
        if self.mode == "limit":
            return httpx.Response(400, json={"code": "53400", "message": "report limit reached"})
        if self.mode == "missing":
            return httpx.Response(404, json={"code": "PGRST202", "message": "Could not find the function"})
        if self.mode == "server_error":
            return httpx.Response(500, json={"message": f"echo {SECRET_NOTE}"})
        if self.mode == "odd":
            return httpx.Response(200, json={"id": 1})
        return httpx.Response(200, json=str(uuid.uuid4()))


@pytest.fixture
def db(monkeypatch):
    fake = FakeReportsDb()
    real = httpx.AsyncClient

    def factory(*a, **k):
        k["transport"] = httpx.MockTransport(fake.handler)
        return real(*a, **k)

    monkeypatch.setattr(database.httpx, "AsyncClient", factory)
    monkeypatch.setattr(database, "SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setattr(database, "SUPABASE_ANON_KEY", "sb_publishable_testkey")
    return fake


@pytest.fixture
def client():
    return TestClient(main_module.app)


@pytest.fixture(autouse=True)
def clean_state():
    main_module._rate_limit_buckets.clear()
    yield
    main_module.app.dependency_overrides.clear()
    main_module._rate_limit_buckets.clear()


def as_user(user_id=ALICE, token="alice-token"):
    async def _fake():
        return (user_id, token)
    main_module.app.dependency_overrides[main_module.get_current_user_and_token] = _fake


class TestReportEndpoint:
    def test_happy_path_returns_201_no_store_and_an_id(self, client, db):
        as_user()
        r = client.post("/api/report", json=body())
        assert r.status_code == 201
        assert r.headers["cache-control"] == "no-store"
        j = r.json()
        assert j["status"] == "received" and uuid.UUID(j["report_id"])

    def test_the_students_own_token_is_forwarded_never_a_service_key(self, client, db):
        as_user(ALICE, "alice-token")
        client.post("/api/report", json=body())
        call = db.calls[0]
        assert call["auth"] == "Bearer alice-token"
        assert call["apikey"] == "sb_publishable_testkey"

    def test_a_user_id_in_the_body_never_reaches_the_database(self, client, db):
        as_user(ALICE)
        client.post("/api/report", json=body(user_id=BOB, status="actioned", platform="android"))
        sent = db.calls[0]["json"]
        assert "user_id" not in sent and "status" not in sent
        assert sent["p_platform"] == "web"
        assert BOB not in jsonlib.dumps(sent)

    def test_android_header_labels_the_report(self, client, db):
        as_user()
        client.post("/api/report", json=body(), headers={"X-Client-Version": "android/1.4.0"})
        assert db.calls[0]["json"]["p_platform"] == "android"
        assert db.calls[0]["json"]["p_app_version"] == "1.4.0"

    def test_unauthenticated_request_is_401_and_touches_no_database(self, client, db):
        r = client.post("/api/report", json=body())
        assert r.status_code == 401
        assert db.calls == []

    def test_invalid_token_is_401(self, client, db, monkeypatch):
        class R:  # Supabase Auth says the token is bad
            status_code = 401
        async def get(self, *a, **k): return R()
        monkeypatch.setattr(main_module.http_client, "client",
                            lambda **k: httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(401))))
        r = client.post("/api/report", json=body(), headers={"Authorization": "Bearer forged"})
        assert r.status_code == 401
        assert db.calls == []

    @pytest.mark.parametrize("payload,code", [
        (body(category="nope"), "REPORT_INVALID_CATEGORY"),
        (body(note="a" * 501), "REPORT_NOTE_TOO_LONG"),
        (body(ai_response=""), "REPORT_MISSING_CONTENT"),
    ])
    def test_invalid_reports_are_400_with_a_stable_code_and_no_database_call(self, client, db, payload, code):
        as_user()
        r = client.post("/api/report", json=payload)
        assert r.status_code == 400 and r.json()["detail"]["code"] == code
        assert db.calls == []

    def test_non_json_and_non_object_bodies_are_400(self, client, db):
        as_user()
        assert client.post("/api/report", content=b"not json", headers={"Content-Type": "application/json"}).status_code == 400
        assert client.post("/api/report", json=["a"]).status_code == 400
        assert client.post("/api/report", content=b"").status_code == 400
        assert db.calls == []

    def test_oversized_body_is_413(self, client, db):
        as_user()
        r = client.post("/api/report", json=body(ai_response="x" * (report_engine.MAX_REQUEST_BODY_BYTES + 10)))
        assert r.status_code == 413
        assert db.calls == []

    def test_daily_cap_from_the_database_is_429_with_a_code(self, client, db):
        as_user()
        db.mode = "limit"
        r = client.post("/api/report", json=body())
        assert r.status_code == 429 and r.json()["detail"]["code"] == "REPORT_LIMIT_REACHED"

    def test_migration_not_applied_is_503_unavailable(self, client, db):
        as_user()
        db.mode = "missing"
        r = client.post("/api/report", json=body())
        assert r.status_code == 503 and r.json()["detail"]["code"] == "REPORTING_UNAVAILABLE"

    @pytest.mark.parametrize("mode", ["server_error", "network", "odd"])
    def test_database_failures_are_502_and_never_leak_internals(self, client, db, mode):
        as_user()
        db.mode = mode
        r = client.post("/api/report", json=body(note=SECRET_NOTE))
        assert r.status_code == 502 and r.json()["detail"]["code"] == "REPORT_FAILED"
        assert SECRET_NOTE not in r.text and "Traceback" not in r.text

    def test_per_minute_rate_limit_applies_per_user(self, client, db):
        as_user(ALICE)
        codes = [client.post("/api/report", json=body()).status_code for _ in range(7)]
        assert codes == [201] * 5 + [429, 429]
        as_user(BOB, "bob-token")
        assert client.post("/api/report", json=body()).status_code == 201

    def test_rate_limited_requests_do_not_reach_the_database(self, client, db):
        as_user()
        for _ in range(5):
            client.post("/api/report", json=body())
        n = len(db.calls)
        client.post("/api/report", json=body())
        assert len(db.calls) == n

    def test_report_text_is_never_logged(self, client, db, caplog):
        as_user()
        caplog.set_level(logging.DEBUG)
        client.post("/api/report", json=body(note=SECRET_NOTE, ai_response=SECRET_ANSWER))
        db.mode = "server_error"
        client.post("/api/report", json=body(note=SECRET_NOTE, ai_response=SECRET_ANSWER))
        db.mode = "limit"
        client.post("/api/report", json=body(note=SECRET_NOTE, ai_response=SECRET_ANSWER))
        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert SECRET_NOTE not in logged and SECRET_ANSWER not in logged
        assert "alice-token" not in logged

    def test_get_is_not_allowed(self, client, db):
        as_user()
        assert client.get("/api/report").status_code == 405

    def test_route_is_registered_exactly_once(self):
        routes = [r for r in main_module.app.routes if getattr(r, "path", None) == "/api/report"]
        assert len(routes) == 1 and routes[0].methods == {"POST"}


class TestDatabaseFunction:
    def test_missing_supabase_configuration_fails_closed(self, monkeypatch):
        import asyncio
        monkeypatch.setattr(database, "SUPABASE_URL", "")
        with pytest.raises(Exception):
            asyncio.run(database.submit_content_report("t", ALICE, report_engine.build_report(body())))

    def test_429_with_53400_is_also_the_limit(self, monkeypatch):
        import asyncio
        real = httpx.AsyncClient

        def factory(*a, **k):
            k["transport"] = httpx.MockTransport(lambda r: httpx.Response(429, json={"code": "53400"}))
            return real(*a, **k)
        monkeypatch.setattr(database.httpx, "AsyncClient", factory)
        monkeypatch.setattr(database, "SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.setattr(database, "SUPABASE_ANON_KEY", "k")
        with pytest.raises(database.ReportLimitError):
            asyncio.run(database.submit_content_report("t", ALICE, report_engine.build_report(body())))
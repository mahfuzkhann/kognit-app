"""
Tests for account deletion: backend/account_deletion.py, POST /api/account/delete in
backend/main.py, the discard_user() additions in backend/state_limits.py, and static
guards that keep the service-role key where it belongs.

Run with: GEMINI_API_KEY=dummy python3 -m pytest test_account_deletion.py -v

The REAL route and the REAL admin-call code run; only the network edge is faked
(httpx.MockTransport plays Supabase Auth's admin API). The database side - that
deleting the auth user really removes every row - is proven against a real PostgreSQL
by supabase/tests/account_deletion_cascade_test.sql (run_local_db_tests.sh).
"""
import asyncio
import base64
import glob
import json as jsonlib
import logging
import os
import re
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import account_deletion as ad
import backend.main as main_module

REPO = os.path.abspath(os.path.dirname(__file__))
ALICE = "aaaaaaaa-0000-4000-8000-000000000001"
BOB = "bbbbbbbb-0000-4000-8000-000000000002"
SERVICE_KEY = "SERVICE-ROLE-SECRET-xyz-8841"


def make_token(amr_ts=None, iat=None, claims=None):
    def b64(d):
        return base64.urlsafe_b64encode(jsonlib.dumps(d).encode()).decode().rstrip("=")
    payload = dict(claims or {})
    if amr_ts is not None:
        payload["amr"] = [{"method": "password", "timestamp": amr_ts}]
    if iat is not None:
        payload["iat"] = iat
    return f"{b64({'alg': 'HS256'})}.{b64(payload)}.signature"


def fresh_token(age=30):
    now = time.time()
    return make_token(amr_ts=int(now - age), iat=int(now))


# ===========================================================================
# 1. pure helpers
# ===========================================================================

class TestConfirmation:
    @pytest.mark.parametrize("v", ["DELETE", "delete", " Delete ", "dElEtE\n"])
    def test_accepts_the_word_in_any_case(self, v):
        assert ad.confirmation_is_valid(v)

    @pytest.mark.parametrize("v", ["", None, 5, True, ["DELETE"], {"a": 1}, "DELETE ME", "yes", "DEL ETE", "ডিলিট"])
    def test_rejects_everything_else(self, v):
        assert not ad.confirmation_is_valid(v)


class TestLoginAge:
    NOW = 1_800_000_000.0

    def test_uses_the_newest_amr_timestamp(self):
        tok = make_token(claims={"amr": [{"method": "oauth", "timestamp": self.NOW - 5000},
                                         {"method": "password", "timestamp": self.NOW - 100}]})
        assert ad.login_age_seconds(tok, self.NOW) == pytest.approx(100)

    def test_amr_wins_over_a_fresh_iat_so_a_token_refresh_does_not_count_as_a_login(self):
        tok = make_token(amr_ts=self.NOW - 5000, iat=self.NOW - 1)
        assert ad.login_age_seconds(tok, self.NOW) == pytest.approx(5000)
        assert not ad.login_is_recent(tok, self.NOW)

    def test_falls_back_to_iat_when_amr_is_absent(self):
        assert ad.login_age_seconds(make_token(iat=self.NOW - 42), self.NOW) == pytest.approx(42)

    @pytest.mark.parametrize("tok", ["", "abc", "a.b.c", "a..c", None, "a.%%%.c", make_token(), make_token(claims={"amr": "x"}),
                                     make_token(claims={"amr": [{"timestamp": "yesterday"}]}),
                                     make_token(claims={"amr": [{"timestamp": True}]}),
                                     make_token(claims={"iat": -5}), make_token(claims={"iat": "now"})])
    def test_unreadable_or_missing_claims_fail_closed(self, tok):
        assert ad.login_age_seconds(tok, self.NOW) is None
        assert not ad.login_is_recent(tok, self.NOW)

    def test_a_timestamp_in_the_future_is_not_recent(self):
        assert not ad.login_is_recent(make_token(amr_ts=self.NOW + 3600), self.NOW)

    def test_window_boundary(self):
        edge = ad.REAUTH_WINDOW_SECONDS
        assert ad.login_is_recent(make_token(amr_ts=self.NOW - edge), self.NOW)
        assert not ad.login_is_recent(make_token(amr_ts=self.NOW - edge - 1), self.NOW)


# ===========================================================================
# 2. the privileged call (delete_auth_user)
# ===========================================================================

class FakeAuthAdmin:
    def __init__(self):
        self.calls = []
        self.status = 200
        self.network_error = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append({"method": request.method, "path": request.url.path, "headers": dict(request.headers),
                           "json": jsonlib.loads(request.content or b"null")})
        if self.network_error:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(self.status, json={"message": f"echo {SERVICE_KEY}"})


@pytest.fixture
def admin(monkeypatch):
    fake = FakeAuthAdmin()
    real = httpx.AsyncClient

    def factory(*a, **k):
        k["transport"] = httpx.MockTransport(fake.handler)
        return real(*a, **k)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv(ad.SERVICE_ROLE_ENV, SERVICE_KEY)
    return fake


class TestDeleteAuthUser:
    def test_sends_one_hard_delete_to_the_admin_endpoint_for_exactly_that_user(self, admin):
        assert asyncio.run(ad.delete_auth_user(ALICE)) == "deleted"
        assert len(admin.calls) == 1
        c = admin.calls[0]
        assert c["method"] == "DELETE"
        assert c["path"] == f"/auth/v1/admin/users/{ALICE}"
        assert c["json"] == {"should_soft_delete": False}
        assert c["headers"]["authorization"] == f"Bearer {SERVICE_KEY}"
        assert c["headers"]["apikey"] == SERVICE_KEY

    @pytest.mark.parametrize("status,expected", [(200, "deleted"), (204, "deleted"), (404, "already_deleted")])
    def test_success_statuses_including_already_deleted_are_idempotent(self, admin, status, expected):
        admin.status = status
        assert asyncio.run(ad.delete_auth_user(ALICE)) == expected

    @pytest.mark.parametrize("status", [401, 403])
    def test_a_rejected_service_key_is_an_operator_problem_not_a_student_one(self, admin, status):
        admin.status = status
        with pytest.raises(ad.AccountDeletionUnavailable):
            asyncio.run(ad.delete_auth_user(ALICE))

    @pytest.mark.parametrize("status", [400, 409, 422, 429, 500, 502, 503])
    def test_other_statuses_are_a_retryable_failure(self, admin, status):
        admin.status = status
        with pytest.raises(ad.AccountDeletionFailed):
            asyncio.run(ad.delete_auth_user(ALICE))

    def test_network_error_is_a_retryable_failure(self, admin):
        admin.network_error = True
        with pytest.raises(ad.AccountDeletionFailed):
            asyncio.run(ad.delete_auth_user(ALICE))

    @pytest.mark.parametrize("missing", [ad.SERVICE_ROLE_ENV, "SUPABASE_URL"])
    def test_missing_configuration_fails_closed_without_any_network_call(self, admin, monkeypatch, missing):
        monkeypatch.delenv(missing)
        with pytest.raises(ad.AccountDeletionUnavailable):
            asyncio.run(ad.delete_auth_user(ALICE))
        assert admin.calls == []

    @pytest.mark.parametrize("bad", ["", "../../users", f"{ALICE}/../{BOB}", f"{ALICE}?x=1", "not-a-uuid", None, 5,
                                     "aaaaaaaa-0000-4000-8000-00000000000g"])
    def test_a_non_uuid_id_can_never_reach_the_url(self, admin, bad):
        with pytest.raises(ad.AccountDeletionFailed):
            asyncio.run(ad.delete_auth_user(bad))
        assert admin.calls == []

    @pytest.mark.parametrize("status", [401, 500])
    def test_the_key_never_appears_in_an_exception_or_a_log(self, admin, caplog, status):
        caplog.set_level(logging.DEBUG)
        admin.status = status
        with pytest.raises(Exception) as e:
            asyncio.run(ad.delete_auth_user(ALICE))
        assert SERVICE_KEY not in str(e.value) and SERVICE_KEY not in repr(e.value)
        assert SERVICE_KEY not in "\n".join(r.getMessage() for r in caplog.records)

    def test_the_key_is_not_stored_in_any_module_attribute(self, admin):
        asyncio.run(ad.delete_auth_user(ALICE))
        for name, value in vars(ad).items():
            assert SERVICE_KEY not in repr(value), name


# ===========================================================================
# 3. the HTTP endpoint
# ===========================================================================

@pytest.fixture
def client():
    return TestClient(main_module.app)


@pytest.fixture(autouse=True)
def clean_state():
    main_module._rate_limit_buckets.clear()
    yield
    main_module.app.dependency_overrides.clear()
    main_module._rate_limit_buckets.clear()
    main_module.active_pdf_contexts.clear()
    main_module.active_quiz_definitions.clear()


def as_user(user_id=ALICE, token=None):
    tok = token if token is not None else fresh_token()

    async def _fake():
        return (user_id, tok)
    main_module.app.dependency_overrides[main_module.get_current_user_and_token] = _fake


class TestEndpoint:
    def test_happy_path(self, client, admin):
        as_user()
        r = client.post("/api/account/delete", json={"confirm": "DELETE"})
        assert r.status_code == 200 and r.json() == {"status": "deleted"}
        assert r.headers["cache-control"] == "no-store"
        assert [c["path"] for c in admin.calls] == [f"/auth/v1/admin/users/{ALICE}"]

    def test_only_the_verified_users_account_is_ever_deleted(self, client, admin):
        as_user(ALICE)
        client.post("/api/account/delete", json={"confirm": "DELETE", "user_id": BOB, "id": BOB, "email": "bob@x.test"})
        assert [c["path"] for c in admin.calls] == [f"/auth/v1/admin/users/{ALICE}"]
        assert BOB not in jsonlib.dumps(admin.calls)

    def test_unauthenticated_is_401_and_nothing_is_called(self, client, admin):
        r = client.post("/api/account/delete", json={"confirm": "DELETE"})
        assert r.status_code == 401 and admin.calls == []

    @pytest.mark.parametrize("payload", [{}, {"confirm": ""}, {"confirm": "yes"}, {"confirm": "DELETE ME"}, {"confirm": 1},
                                         {"confirm": None}])
    def test_without_the_typed_word_nothing_is_deleted(self, client, admin, payload):
        as_user()
        r = client.post("/api/account/delete", json=payload)
        assert r.status_code == 400 and r.json()["detail"]["code"] == "ACCOUNT_DELETE_CONFIRMATION_REQUIRED"
        assert admin.calls == []

    @pytest.mark.parametrize("payload", [["DELETE"], "DELETE", 5, True])
    def test_a_body_that_is_not_a_json_object_is_refused_by_the_shared_reader(self, client, admin, payload):
        as_user()
        assert client.post("/api/account/delete", json=payload).status_code == 400
        assert admin.calls == []

    def test_bad_and_oversized_bodies(self, client, admin):
        as_user()
        assert client.post("/api/account/delete", content=b"not json", headers={"Content-Type": "application/json"}).status_code == 400
        assert client.post("/api/account/delete", content=b"").status_code == 400
        assert client.post("/api/account/delete", json={"confirm": "DELETE", "pad": "x" * 4000}).status_code == 413
        assert admin.calls == []

    @pytest.mark.parametrize("token", [fresh_token(age=ad.REAUTH_WINDOW_SECONDS + 5), "opaque-token", make_token(),
                                       make_token(amr_ts=time.time() - 86400, iat=time.time())])
    def test_an_old_login_must_sign_in_again_first(self, client, admin, token):
        as_user(token=token)
        r = client.post("/api/account/delete", json={"confirm": "DELETE"})
        assert r.status_code == 403 and r.json()["detail"]["code"] == "REAUTH_REQUIRED"
        assert admin.calls == []

    def test_not_configured_is_503_and_keeps_all_in_memory_data(self, client, admin, monkeypatch):
        as_user()
        monkeypatch.delenv(ad.SERVICE_ROLE_ENV)
        main_module._pdf_contexts.put(main_module.active_pdf_contexts, ALICE, "chat1", "pdf text")
        r = client.post("/api/account/delete", json={"confirm": "DELETE"})
        assert r.status_code == 503 and r.json()["detail"]["code"] == "ACCOUNT_DELETION_UNAVAILABLE"
        assert main_module._pdf_contexts.get(main_module.active_pdf_contexts, ALICE, "chat1") == "pdf text"

    @pytest.mark.parametrize("status", [500, 429])
    def test_provider_failure_is_502_nothing_is_purged_and_a_retry_works(self, client, admin, status):
        as_user()
        main_module._pdf_contexts.put(main_module.active_pdf_contexts, ALICE, "chat1", "pdf text")
        admin.status = status
        r = client.post("/api/account/delete", json={"confirm": "DELETE"})
        assert r.status_code == 502 and r.json()["detail"]["code"] == "ACCOUNT_DELETE_FAILED"
        assert SERVICE_KEY not in r.text
        assert main_module._pdf_contexts.get(main_module.active_pdf_contexts, ALICE, "chat1") == "pdf text"
        admin.status = 200
        assert client.post("/api/account/delete", json={"confirm": "DELETE"}).status_code == 200

    def test_already_deleted_account_is_a_success(self, client, admin):
        as_user()
        admin.status = 404
        assert client.post("/api/account/delete", json={"confirm": "DELETE"}).status_code == 200

    def test_memory_of_the_deleted_user_is_purged_and_other_users_are_untouched(self, client, admin):
        as_user(ALICE)
        m = main_module
        m._pdf_contexts.put(m.active_pdf_contexts, ALICE, "c1", "alice pdf")
        m._pdf_contexts.put(m.active_pdf_contexts, ALICE, "c2", "alice pdf 2")
        m._pdf_contexts.put(m.active_pdf_contexts, BOB, "c1", "bob pdf")
        m._quiz_definitions.put(m.active_quiz_definitions, "q-alice", {"user_id": ALICE, "questions": [{"correct_index": 1}]})
        m._quiz_definitions.put(m.active_quiz_definitions, "q-bob", {"user_id": BOB, "questions": []})
        m._check_rate_limit(ALICE, "chat")
        m._check_rate_limit(BOB, "chat")
        assert client.post("/api/account/delete", json={"confirm": "DELETE"}).status_code == 200
        assert m._pdf_contexts.get(m.active_pdf_contexts, ALICE, "c1") == ""
        assert m._pdf_contexts.get(m.active_pdf_contexts, ALICE, "c2") == ""
        assert m._pdf_contexts.get(m.active_pdf_contexts, BOB, "c1") == "bob pdf"
        assert "q-alice" not in m.active_quiz_definitions and "q-bob" in m.active_quiz_definitions
        assert not [k for k in m._rate_limit_buckets if k[0] == ALICE]
        assert [k for k in m._rate_limit_buckets if k[0] == BOB]

    def test_rate_limited_to_three_attempts_per_ten_minutes(self, client, admin):
        as_user()
        admin.status = 500
        codes = [client.post("/api/account/delete", json={"confirm": "DELETE"}).status_code for _ in range(5)]
        assert codes == [502, 502, 502, 429, 429]
        assert len(admin.calls) == 3

    def test_secrets_and_ids_stay_out_of_logs_and_responses(self, client, admin, caplog):
        as_user()
        caplog.set_level(logging.DEBUG)
        r = client.post("/api/account/delete", json={"confirm": "DELETE"})
        logged = "\n".join(x.getMessage() for x in caplog.records)
        assert SERVICE_KEY not in logged and SERVICE_KEY not in r.text
        assert ALICE not in logged, "the deleted account's id must not live on in the logs"
        assert "account deleted outcome=deleted" in logged

    def test_httpx_request_urls_are_not_logged_at_info(self):
        """httpx would otherwise write every Supabase URL - user ids included - to the logs."""
        assert logging.getLogger("httpx").level >= logging.WARNING
        assert logging.getLogger("httpcore").level >= logging.WARNING

    def test_route_is_registered_once_and_is_post_only(self, client):
        routes = [r for r in main_module.app.routes if getattr(r, "path", None) == "/api/account/delete"]
        assert len(routes) == 1 and routes[0].methods == {"POST"}
        as_user()
        assert client.get("/api/account/delete").status_code == 405


# ===========================================================================
# 4. discard_user in the in-memory stores
# ===========================================================================

class TestDiscardUser:
    def test_pdf_store(self):
        from backend import state_limits
        store, d = state_limits.BoundedPdfContexts(), {}
        store.put(d, ALICE, "c1", "a"); store.put(d, ALICE, "c2", "b"); store.put(d, BOB, "c1", "c")
        assert store.discard_user(d, ALICE) == 2
        assert store.get(d, ALICE, "c1") == "" and store.get(d, BOB, "c1") == "c"
        assert store.discard_user(d, ALICE) == 0  # idempotent
        assert all(k[0] != ALICE for k in store._last_used)

    def test_quiz_store_drops_even_in_flight_definitions(self):
        from backend import state_limits
        store, d = state_limits.BoundedQuizDefinitions(), {}
        store.put(d, "q1", {"user_id": ALICE, "submitted": True}); store.put(d, "q2", {"user_id": ALICE})
        store.put(d, "q3", {"user_id": BOB})
        assert store.discard_user(d, ALICE) == 2
        assert list(d) == ["q3"] and set(store._stored_at) == {"q3"}


# ===========================================================================
# 5. static guards
# ===========================================================================

def _read(path):
    return open(path, encoding="utf-8", errors="replace").read()


class TestStaticGuards:
    def test_only_account_deletion_reads_the_service_role_key(self):
        offenders = []
        for path in glob.glob(os.path.join(REPO, "backend", "**", "*.py"), recursive=True):
            if path.endswith("account_deletion.py"):
                continue
            if re.search(r"SERVICE_ROLE|service_role_key", _read(path), re.I):
                # payments.py / comments only MENTION the role; they must not read an env key
                if re.search(r"getenv\([^)]*SERVICE|environ[^\n]*SERVICE", _read(path), re.I):
                    offenders.append(path)
        assert offenders == []

    def test_the_key_never_reaches_the_browser_or_the_repo(self):
        for pattern in ("static/**/*", "templates/**/*"):
            for path in glob.glob(os.path.join(REPO, pattern), recursive=True):
                if os.path.isfile(path) and not path.endswith((".woff", ".woff2", ".png", ".ico", ".jpg", ".svg", ".webp")):
                    assert not re.search(r"SUPABASE_SERVICE_ROLE_KEY|service_role", _read(path)), path
        example = _read(os.path.join(REPO, ".env.example"))
        for line in example.splitlines():
            if line.strip().startswith("SUPABASE_SERVICE_ROLE_KEY="):
                assert line.strip() == "SUPABASE_SERVICE_ROLE_KEY=" or line.strip().startswith("# "), "no real value in .env.example"

    def test_env_example_documents_the_key_without_a_value(self):
        example = _read(os.path.join(REPO, ".env.example"))
        assert "SUPABASE_SERVICE_ROLE_KEY" in example
        assert not re.search(r"^SUPABASE_SERVICE_ROLE_KEY=\S", example, re.M)

    def test_every_user_id_column_in_every_migration_cascades_from_auth_users(self):
        """Static twin of the real-database test: a new table with user data must be
        deleted with the account."""
        bad = []
        for path in sorted(glob.glob(os.path.join(REPO, "supabase", "migrations", "*.sql"))):
            sql = re.sub(r"--[^\n]*", "", _read(path))
            for m in re.finditer(r"create table (?:if not exists )?public\.(\w+)\s*\((.*?)\n\);", sql, re.S | re.I):
                table, body = m.group(1), m.group(2)
                if re.search(r"^\s*user_id\s", body, re.M):
                    if not re.search(r"user_id\s+uuid[^,\n]*references\s+auth\.users\s*\(id\)\s+on\s+delete\s+cascade", body, re.I):
                        bad.append((os.path.basename(path), table))
        assert bad == []

    def test_inventory_document_lists_every_user_owned_table(self):
        doc = _read(os.path.join(REPO, "docs", "ACCOUNT_DELETION.md"))
        tables = set()
        for path in glob.glob(os.path.join(REPO, "supabase", "migrations", "*.sql")):
            sql = re.sub(r"--[^\n]*", "", _read(path))
            for m in re.finditer(r"create table (?:if not exists )?public\.(\w+)\s*\((.*?)\n\);", sql, re.S | re.I):
                if re.search(r"^\s*user_id\s", m.group(2), re.M):
                    tables.add(m.group(1))
        missing = sorted(t for t in tables if f"`{t}`" not in doc)
        assert missing == [], f"docs/ACCOUNT_DELETION.md does not list: {missing}"
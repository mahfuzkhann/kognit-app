"""
Token verification (backend.main._verify_supabase_token) - authentication failure modes.

Part of the pre-Phase 10 remediation (finding F-09 area: Supabase Auth assumptions). Covers
the paths the endpoint tests stub out: missing/malformed headers, server misconfiguration
(fail closed), a student's invalid or expired session (401), and - fixed here - Supabase Auth
itself failing (5xx / 429 / unreachable), which must be 503 "try again" and never 401
"your session expired", because the frontend turns every 401 into a login prompt.

Part of the suite talks to a REAL local HTTP server that imitates GET /auth/v1/user, so the
httpx client code path is exercised end to end (no external service is contacted).
"""
import http.server
import json
import logging
import threading

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import backend.main as main
from backend.providers import set_provider
from backend.providers.mock import MockProvider, text_attempt

SECRET_TOKEN = "super-secret-session-token-abc123"


class _FakeSupabaseAuth(http.server.BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        token = (self.headers.get("Authorization") or "").replace("Bearer ", "")
        type(self).seen.append({"path": self.path, "apikey": self.headers.get("apikey"), "token": token})
        table = {
            "good-token": (200, {"id": "user-from-supabase"}),
            "expired-token": (401, {"message": "token expired"}),
            "forbidden-token": (403, {"message": "forbidden"}),
            "weird-payload-token": (200, {"unexpected": True}),
            "outage-500-token": (500, {"message": "internal"}),
            "outage-502-token": (502, {"message": "bad gateway"}),
            "outage-503-token": (503, {"message": "unavailable"}),
            "throttled-token": (429, {"message": "rate limit"}),
        }
        status, body = table.get(token, (401, {"message": "unknown"}))
        raw = b"<html>not json</html>" if token == "html-token" else json.dumps(body).encode()
        self.send_response(200 if token == "html-token" else status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def supabase_auth(monkeypatch):
    _FakeSupabaseAuth.seen = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeSupabaseAuth)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    monkeypatch.setattr(main, "SUPABASE_URL", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setattr(main, "SUPABASE_ANON_KEY", "anon-key-for-tests")
    yield _FakeSupabaseAuth
    server.shutdown()
    server.server_close()


async def verify(header):
    return await main._verify_supabase_token(header)


def run(coro):
    import asyncio
    return asyncio.run(coro)


def status_of(header):
    with pytest.raises(HTTPException) as info:
        run(verify(header))
    return info.value.status_code, info.value.detail


class TestHeaderValidation:
    @pytest.mark.parametrize("header", [None, "", "Basic abc", "Bearer", "bearer ", "Token abc", "abc"])
    def test_missing_or_malformed_headers_are_401_and_never_call_supabase(self, supabase_auth, header):
        code, _ = status_of(header)
        assert code == 401
        assert supabase_auth.seen == []

    def test_bearer_scheme_is_case_insensitive(self, supabase_auth):
        assert run(verify("bEaReR good-token")) == ("user-from-supabase", "good-token")


class TestFailClosedConfiguration:
    @pytest.mark.parametrize("url,key", [("", "anon"), ("http://x.invalid", ""), ("", "")])
    def test_missing_configuration_refuses_instead_of_letting_requests_through(self, monkeypatch, url, key):
        monkeypatch.setattr(main, "SUPABASE_URL", url)
        monkeypatch.setattr(main, "SUPABASE_ANON_KEY", key)
        code, detail = status_of("Bearer good-token")
        assert code == 500 and "not configured" in detail


class TestStudentSessions:
    def test_a_valid_token_returns_the_supabase_user_id_and_the_token(self, supabase_auth):
        assert run(verify("Bearer good-token")) == ("user-from-supabase", "good-token")
        assert supabase_auth.seen == [{"path": "/auth/v1/user", "apikey": "anon-key-for-tests", "token": "good-token"}]

    @pytest.mark.parametrize("token", ["expired-token", "forbidden-token", "unknown-token"])
    def test_invalid_or_expired_sessions_are_401(self, supabase_auth, token):
        code, detail = status_of(f"Bearer {token}")
        assert code == 401 and "log in again" in detail

    def test_an_unexpected_success_payload_is_401(self, supabase_auth):
        assert status_of("Bearer weird-payload-token")[0] == 401

    def test_a_non_json_success_body_is_401_not_a_crash(self, supabase_auth):
        assert status_of("Bearer html-token")[0] == 401


class TestSupabaseAuthOutage:
    @pytest.mark.parametrize("token", ["outage-500-token", "outage-502-token", "outage-503-token", "throttled-token"])
    def test_upstream_failure_is_503_never_a_fake_session_expired(self, supabase_auth, token):
        code, detail = status_of(f"Bearer {token}")
        assert code == 503
        assert "try again" in detail and "log in" not in detail.lower()

    def test_an_unreachable_supabase_is_503(self, monkeypatch):
        monkeypatch.setattr(main, "SUPABASE_URL", "http://127.0.0.1:9")   # nothing listens on port 9
        monkeypatch.setattr(main, "SUPABASE_ANON_KEY", "anon")
        code, _ = status_of("Bearer good-token")
        assert code == 503


class TestNoSecretsInLogs:
    @pytest.mark.parametrize("token", ["outage-500-token", "expired-token", "html-token"])
    def test_the_token_is_never_logged(self, supabase_auth, caplog, token):
        with caplog.at_level(logging.DEBUG):
            status_of(f"Bearer {token}")
        assert token not in caplog.text

    def test_the_token_is_never_logged_when_supabase_is_unreachable(self, monkeypatch, caplog):
        monkeypatch.setattr(main, "SUPABASE_URL", "http://127.0.0.1:9")
        monkeypatch.setattr(main, "SUPABASE_ANON_KEY", "anon")
        with caplog.at_level(logging.DEBUG):
            status_of(f"Bearer {SECRET_TOKEN}")
        assert SECRET_TOKEN not in caplog.text


class TestThroughTheRealDependencyChain:
    """No dependency overrides: the real auth dependency runs against the local Supabase stand-in."""

    def test_a_streamed_chat_with_a_good_token_works_and_identity_comes_from_supabase(self, supabase_auth, monkeypatch):
        async def ctx(**kw):
            return ("Class 9-10 (SSC)", "Science")

        async def name(**kw):
            return None
        monkeypatch.setattr(main, "_get_academic_context", ctx)
        monkeypatch.setattr(main, "_get_student_display_name", name)
        set_provider(MockProvider([text_attempt("Hi.")]))
        try:
            resp = TestClient(main.app).post(
                "/api/chat/stream", data={"prompt": "hi", "chat_id": "c1", "user_id": "someone-else"},
                headers={"Authorization": "Bearer good-token"},
            )
        finally:
            set_provider(None)
        assert resp.status_code == 200
        assert supabase_auth.seen and supabase_auth.seen[0]["token"] == "good-token"

    @pytest.mark.parametrize("token,expected", [("expired-token", 401), ("outage-503-token", 503), (None, 401)])
    def test_endpoint_status_codes(self, supabase_auth, token, expected):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        resp = TestClient(main.app).post("/api/chat/stream", data={"prompt": "hi", "chat_id": "c1"}, headers=headers)
        assert resp.status_code == expected and "Traceback" not in resp.text
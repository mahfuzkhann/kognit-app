"""
P0 regression - "NameError: name 'http_client' is not defined" in quota_service._rpc.

WHAT WENT WRONG
    AI Engine 2.0 (decision D-9) moved every Supabase call onto the pooled client in
    backend/http_client.py. backend/quota_service.py was switched to `http_client.client(...)`
    but never got `from backend import http_client`, so the FIRST real quota call of every
    authenticated chat / quiz / notes request raised NameError -> HTTP 500.

WHY NO TEST CAUGHT IT
    Every older test replaces quota_service._rpc wholesale with a fake. _rpc is the one
    function that does the real HTTP, so its body had never executed under test.

WHAT THESE TESTS DO DIFFERENTLY
    They run the REAL _rpc, the REAL pooled httpx client and a REAL socket, against a tiny local
    stand-in for Supabase (127.0.0.1, random port). No external network, no real quota, no
    Gemini call (the mock provider is used for the chat tests).
"""
import ast
import asyncio
import hashlib
import json
import logging
import os
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import backend.main as main
from backend import database, http_client, quota_service, state_limits
from backend.providers import set_provider
from backend.providers.base import ProviderErrorKind, ProviderUsage
from backend.providers.mock import MockProvider, failing_attempt, text_attempt

ANON_KEY = "anon-key-for-tests-only"
USER_TOKEN = "student-access-token-SECRET-123"

ALLOWED = {"allowed": True, "reason": "ok", "plan": "free", "limit": 3, "used": 1, "remaining": 2,
           "resets_at": "2026-11-19T00:00:00+06:00"}
DENIED = {"allowed": False, "reason": "quota_exceeded", "plan": "free", "limit": 1, "used": 1, "remaining": 0,
          "resets_at": "2026-11-19T00:00:00+06:00"}


# ------------------------------------------------------------------ fake Supabase
class FakeSupabase:
    """A real HTTP server on 127.0.0.1. `rules` maps a URL path to either a (status, body, delay)
    tuple or a callable(request_body) returning one. Everything it receives is recorded."""

    def __init__(self):
        self.requests = []
        self.user_id = "user-1"
        self.rules = {}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):          # keep test output quiet
                pass

            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = None
                owner.requests.append({"method": self.command, "path": self.path, "body": body,
                                       "headers": {k.lower(): v for k, v in self.headers.items()}})
                status, payload, delay = owner.respond(self.path, body)
                if delay:
                    time.sleep(delay)
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass                              # the client already gave up (timeout test)

            do_GET = do_POST = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.02), daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def respond(self, path, body):
        rule = self.rules.get(path)
        if rule is not None:
            return rule(body) if callable(rule) else rule
        if path == "/auth/v1/user":
            return 200, {"id": self.user_id}, 0
        if path == "/rest/v1/rpc/get_my_plan":
            return 200, "free", 0
        if path == "/rest/v1/rpc/consume_quota":
            return 200, ALLOWED, 0
        if path == "/rest/v1/rpc/refund_quota":
            return 200, True, 0
        return 404, {"message": "not found"}, 0

    def calls(self, name):
        return [r for r in self.requests if r["path"].endswith("/" + name)]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def supabase(monkeypatch):
    fake = FakeSupabase()
    for module in (database, main):
        monkeypatch.setattr(module, "SUPABASE_URL", fake.url)
        monkeypatch.setattr(module, "SUPABASE_ANON_KEY", ANON_KEY)
    monkeypatch.setenv("QUOTA_MODE", "enforce")
    yield fake
    fake.close()


def run(coro_fn, *args):
    """Runs one coroutine on a fresh loop and closes the pooled clients that loop created."""
    async def go():
        try:
            return await coro_fn(*args)
        finally:
            await http_client.aclose_all()
    return asyncio.run(go())


def unused_port_url():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


# ================================================================= A. client resolution
class TestHttpClientResolution:
    def test_quota_service_has_the_shared_http_client_module(self):
        # This single assertion is the incident: it was a NameError.
        assert quota_service.http_client is http_client

    def test_rpc_borrows_the_pooled_client_with_the_database_timeout(self, supabase, monkeypatch):
        seen = []
        real = http_client.client

        def spy(*, timeout):
            seen.append(timeout)
            return real(timeout=timeout)

        monkeypatch.setattr(http_client, "client", spy)
        assert run(quota_service._rpc, USER_TOKEN, "get_my_plan", {}) == "free"
        assert seen == [database.DB_REQUEST_TIMEOUT_SECONDS]      # same constant as before the refactor

    def test_rpc_does_not_build_a_second_http_client(self, supabase, monkeypatch):
        import httpx
        created = []
        real_cls = httpx.AsyncClient

        class Counting(real_cls):                                   # would only be hit via a stray httpx.AsyncClient(...)
            def __init__(self, *a, **k):
                created.append(1)
                super().__init__(*a, **k)

        # http_client.client() returns the pooled real client when httpx.AsyncClient is untouched,
        # so the pooled path must not construct anything through the module attribute.
        monkeypatch.setattr(quota_service.httpx, "AsyncClient", real_cls)
        run(quota_service._rpc, USER_TOKEN, "get_my_plan", {})
        assert created == []

    def test_no_backend_module_uses_http_client_without_importing_it(self):
        """The class of bug, not just this instance: any module that calls http_client.X must import it."""
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend")
        offenders = []
        for name in sorted(os.listdir(root)):
            if not name.endswith(".py"):
                continue
            tree = ast.parse(open(os.path.join(root, name), encoding="utf-8").read())
            used = any(isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "http_client"
                       for n in ast.walk(tree))
            bound = set()
            for n in ast.walk(tree):
                if isinstance(n, ast.ImportFrom):
                    bound.update(a.asname or a.name for a in n.names)
                elif isinstance(n, ast.Import):
                    bound.update((a.asname or a.name).split(".")[0] for a in n.names)
            if used and "http_client" not in bound and name != "http_client.py":
                offenders.append(name)
        assert offenders == []


# ================================================================= B. successful RPCs
class TestSuccessfulQuotaRpc:
    def test_consume_allowed_is_decoded_and_sent_with_the_students_own_token(self, supabase):
        ticket = quota_service.new_refund_ticket("chat", "req-aaa111")
        decision = run(quota_service.consume_quota, USER_TOKEN, "chat", "req-aaa111", ticket)
        assert (decision.allowed, decision.plan, decision.limit, decision.used, decision.remaining) == (True, "free", 3, 1, 2)
        assert decision.resets_at == ALLOWED["resets_at"] and decision.replay is False

        (req,) = supabase.calls("consume_quota")
        assert req["method"] == "POST" and req["path"] == "/rest/v1/rpc/consume_quota"
        assert req["headers"]["authorization"] == f"Bearer {USER_TOKEN}"          # never a service-role key
        assert req["headers"]["apikey"] == ANON_KEY
        assert req["headers"]["content-type"].startswith("application/json")
        assert req["body"] == {"p_kind": "chat", "p_request_id": "req-aaa111",
                               "p_refund_token_hash": hashlib.sha256(ticket.token.encode()).hexdigest()}
        assert ticket.token not in json.dumps(req["body"])                          # the secret itself is never sent here

    def test_the_decision_is_identical_to_parsing_the_same_payload_directly(self, supabase):
        supabase.rules["/rest/v1/rpc/consume_quota"] = (200, DENIED, 0)
        over_the_wire = run(quota_service.consume_quota, USER_TOKEN, "chat", "req-bbb222", None)
        assert over_the_wire == quota_service.parse_decision("chat", DENIED)

    def test_a_denial_is_a_429_quota_exceeded_not_an_error(self, supabase):
        supabase.rules["/rest/v1/rpc/consume_quota"] = (200, DENIED, 0)
        with pytest.raises(HTTPException) as err:
            run(quota_service.enforce_quota, USER_TOKEN, "chat", "req-ccc333")
        assert err.value.status_code == 429
        assert err.value.detail["code"] == "QUOTA_EXCEEDED" and err.value.detail["kind"] == "chat"
        assert err.value.detail["limit"] == 1 and err.value.detail["used"] == 1
        assert supabase.calls("refund_quota") == []                                 # a denial counted nothing -> nothing to refund

    def test_plan_not_configured_is_still_a_403(self, supabase):
        supabase.rules["/rest/v1/rpc/consume_quota"] = (200, dict(DENIED, plan="pro", reason="plan_not_configured"), 0)
        with pytest.raises(HTTPException) as err:
            run(quota_service.enforce_quota, USER_TOKEN, "chat", "req-ddd444")
        assert err.value.status_code == 403 and err.value.detail["code"] == "PLAN_NOT_CONFIGURED"

    def test_a_replay_of_the_same_request_id_is_reported_not_double_counted(self, supabase):
        supabase.rules["/rest/v1/rpc/consume_quota"] = (200, dict(ALLOWED, replay=True), 0)
        decision = run(quota_service.consume_quota, USER_TOKEN, "chat", "req-eee555", None)
        assert decision.replay is True and decision.allowed is True

    @pytest.mark.parametrize("plan", ["free", "pro"])
    def test_get_plan_returns_the_database_answer(self, supabase, plan):
        supabase.rules["/rest/v1/rpc/get_my_plan"] = (200, plan, 0)
        assert run(quota_service.get_plan, USER_TOKEN) == plan

    def test_pro_decisions_keep_their_plan(self, supabase):
        supabase.rules["/rest/v1/rpc/consume_quota"] = (200, dict(ALLOWED, plan="pro", limit=20, used=2, remaining=18), 0)
        decision, ticket = run(quota_service.enforce_quota, USER_TOKEN, "chat", "req-fff666")
        assert decision.plan == "pro" and decision.remaining == 18 and ticket is not None

    def test_refund_sends_the_raw_ticket_and_reports_the_result(self, supabase):
        ticket = quota_service.new_refund_ticket("chat", "req-ggg777")
        assert run(quota_service.refund_quota, USER_TOKEN, ticket) is True
        (req,) = supabase.calls("refund_quota")
        assert req["body"] == {"p_kind": "chat", "p_request_id": "req-ggg777", "p_refund_token": ticket.token}
        supabase.rules["/rest/v1/rpc/refund_quota"] = (200, False, 0)
        assert run(quota_service.refund_quota, USER_TOKEN, ticket) is False

    def test_quota_mode_off_makes_no_network_call_at_all(self, supabase, monkeypatch):
        monkeypatch.setenv("QUOTA_MODE", "off")
        decision, ticket = run(quota_service.enforce_quota, USER_TOKEN, "chat", "req-hhh888")
        assert decision.allowed and decision.plan == "unmetered" and ticket is None
        assert supabase.requests == []


# ================================================================= C. failures fail CLOSED
class TestRpcFailuresNeverBypassQuota:
    @pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 502, 503])
    def test_any_non_200_is_unavailable(self, supabase, status):
        supabase.rules["/rest/v1/rpc/consume_quota"] = (status, {"message": "boom"}, 0)
        with pytest.raises(quota_service.QuotaUnavailable):
            run(quota_service.consume_quota, USER_TOKEN, "chat", "req-1", None)

    @pytest.mark.parametrize("body", [b"<html>gateway</html>", b"", b"{not json"])
    def test_a_200_that_is_not_json_is_unavailable(self, supabase, body):
        supabase.rules["/rest/v1/rpc/consume_quota"] = (200, body, 0)
        with pytest.raises(quota_service.QuotaUnavailable):
            run(quota_service.consume_quota, USER_TOKEN, "chat", "req-2", None)

    @pytest.mark.parametrize("payload", [[], {}, None, 123, "allowed", {"allowed": "yes"}, {"allowed": 1}, {"plan": "free"}])
    def test_an_unexpected_json_shape_is_unavailable_never_allowed(self, supabase, payload):
        supabase.rules["/rest/v1/rpc/consume_quota"] = (200, payload, 0)
        with pytest.raises(quota_service.QuotaUnavailable):
            run(quota_service.consume_quota, USER_TOKEN, "chat", "req-3", None)

    def test_an_unexpected_plan_value_is_unavailable(self, supabase):
        supabase.rules["/rest/v1/rpc/get_my_plan"] = (200, "enterprise", 0)
        with pytest.raises(quota_service.QuotaUnavailable):
            run(quota_service.get_plan, USER_TOKEN)

    def test_a_timeout_is_unavailable_and_enforce_refuses_with_503(self, supabase, monkeypatch):
        monkeypatch.setattr(database, "DB_REQUEST_TIMEOUT_SECONDS", 0.3)
        supabase.rules["/rest/v1/rpc/consume_quota"] = (200, ALLOWED, 1.5)       # slower than the client timeout
        supabase.rules["/rest/v1/rpc/refund_quota"] = (200, True, 1.5)
        with pytest.raises(quota_service.QuotaUnavailable):
            run(quota_service.consume_quota, USER_TOKEN, "chat", "req-4", None)
        with pytest.raises(HTTPException) as err:
            run(quota_service.enforce_quota, USER_TOKEN, "chat", "req-5")
        assert err.value.status_code == 503 and err.value.detail["code"] == "QUOTA_UNAVAILABLE"

    def test_a_lost_response_triggers_one_compensating_refund_with_the_requests_own_ticket(self, supabase, monkeypatch):
        monkeypatch.setattr(database, "DB_REQUEST_TIMEOUT_SECONDS", 0.3)
        supabase.rules["/rest/v1/rpc/consume_quota"] = (200, ALLOWED, 1.5)
        with pytest.raises(HTTPException) as err:
            run(quota_service.enforce_quota, USER_TOKEN, "chat", "req-6")
        assert err.value.status_code == 503
        (consume,) = supabase.calls("consume_quota")
        (refund,) = supabase.calls("refund_quota")
        assert refund["body"]["p_request_id"] == "req-6"
        # the compensating refund proves ownership with the raw token whose hash the consume call carried
        assert hashlib.sha256(refund["body"]["p_refund_token"].encode()).hexdigest() == consume["body"]["p_refund_token_hash"]

    def test_connection_refused_is_unavailable_and_enforce_refuses_with_503(self, supabase, monkeypatch):
        for module in (database, main):
            monkeypatch.setattr(module, "SUPABASE_URL", unused_port_url())
        with pytest.raises(quota_service.QuotaUnavailable):
            run(quota_service.consume_quota, USER_TOKEN, "chat", "req-7", None)
        with pytest.raises(HTTPException) as err:
            run(quota_service.enforce_quota, USER_TOKEN, "chat", "req-8")
        assert err.value.status_code == 503

    @pytest.mark.parametrize("bad_url", ["http://[::1", "htp://nope", "://", "http://exa mple.com"])
    def test_a_malformed_supabase_url_is_a_safe_503_not_a_crash(self, supabase, monkeypatch, bad_url):
        monkeypatch.setattr(database, "SUPABASE_URL", bad_url)
        with pytest.raises(quota_service.QuotaUnavailable):
            run(quota_service.consume_quota, USER_TOKEN, "chat", "req-url", None)
        with pytest.raises(HTTPException) as err:
            run(quota_service.enforce_quota, USER_TOKEN, "chat", "req-url2")
        assert err.value.status_code == 503

    def test_unconfigured_supabase_is_unavailable_and_sends_nothing(self, supabase, monkeypatch):
        monkeypatch.setattr(database, "SUPABASE_URL", "")
        with pytest.raises(quota_service.QuotaUnavailable):
            run(quota_service.consume_quota, USER_TOKEN, "chat", "req-9", None)
        assert supabase.requests == []

    def test_enforce_never_returns_an_allowed_decision_when_the_backend_fails(self, supabase):
        supabase.rules["/rest/v1/rpc/consume_quota"] = (500, {"message": "db down"}, 0)
        with pytest.raises(HTTPException) as err:
            run(quota_service.enforce_quota, USER_TOKEN, "chat", "req-10")
        assert err.value.status_code == 503

    def test_the_students_token_is_never_written_to_the_logs(self, supabase, caplog):
        supabase.rules["/rest/v1/rpc/consume_quota"] = (500, {"message": "db down"}, 0)
        caplog.set_level(logging.DEBUG)
        with pytest.raises(quota_service.QuotaUnavailable):
            run(quota_service.consume_quota, USER_TOKEN, "chat", "req-11", None)
        assert USER_TOKEN not in caplog.text


# ================================================================= D. the real chat-stream endpoint
@pytest.fixture
def chat(supabase, monkeypatch):
    """The REAL auth dependency (GET /auth/v1/user) and the REAL quota RPC, both served by the fake
    Supabase. Only the unrelated profile lookups and the AI provider are stubbed."""
    supabase.user_id = "user-" + uuid.uuid4().hex[:8]          # a fresh in-memory rate-limit bucket per test

    async def _ctx(**kwargs):
        return ("Class 9-10 (SSC)", "Science")

    async def _name(**kwargs):
        return None

    async def _curriculum(*args, **kwargs):
        return None

    monkeypatch.setattr(main, "_get_academic_context", _ctx)
    monkeypatch.setattr(main, "_get_student_display_name", _name)
    monkeypatch.setattr(main, "_get_curriculum", _curriculum)
    monkeypatch.setattr(main, "active_pdf_contexts", {})
    monkeypatch.setattr(main, "_pdf_contexts", state_limits.BoundedPdfContexts())
    yield TestClient(main.app)
    set_provider(None)


def post_stream(client):
    return client.post("/api/chat/stream", data={"prompt": "What is force?", "chat_id": "chat-1"},
                       headers={"Authorization": f"Bearer {USER_TOKEN}"})


def events(resp):
    return [json.loads(line) for line in resp.text.strip().split("\n") if line.strip()]


USAGE = ProviderUsage(prompt_tokens=11, output_tokens=5, thoughts_tokens=3, total_tokens=19)


class TestChatStreamPassesQuotaOverTheRealWire:
    def test_an_authenticated_student_gets_an_answer_and_no_500(self, chat, supabase):
        mock = MockProvider([text_attempt("Hello ", "world", usage=USAGE)])
        set_provider(mock)
        resp = post_stream(chat)
        assert resp.status_code == 200                      # the incident returned 500 here
        assert events(resp)[-1]["type"] == "done" and mock.call_count == 1

        paths = [r["path"] for r in supabase.requests]
        assert "/auth/v1/user" in paths                     # authentication ran for real
        (consume,) = supabase.calls("consume_quota")        # counted exactly once
        assert consume["body"]["p_kind"] == "chat"
        assert consume["body"]["p_request_id"] == resp.headers["x-request-id"]
        assert consume["headers"]["authorization"] == f"Bearer {USER_TOKEN}"
        assert paths.index("/auth/v1/user") < paths.index("/rest/v1/rpc/consume_quota")
        assert supabase.calls("refund_quota") == []         # an answered request is never refunded

    def test_a_pro_student_passes_the_same_flow(self, chat, supabase):
        supabase.rules["/rest/v1/rpc/get_my_plan"] = (200, "pro", 0)
        supabase.rules["/rest/v1/rpc/consume_quota"] = (200, dict(ALLOWED, plan="pro", limit=20, used=1, remaining=19), 0)
        set_provider(MockProvider([text_attempt("Pro answer", usage=USAGE)]))
        resp = post_stream(chat)
        assert resp.status_code == 200 and events(resp)[-1]["type"] == "done"

    def test_an_over_quota_student_gets_a_real_429_and_the_provider_is_never_called(self, chat, supabase):
        supabase.rules["/rest/v1/rpc/consume_quota"] = (200, DENIED, 0)
        mock = MockProvider([text_attempt("must not be generated")])
        set_provider(mock)
        resp = post_stream(chat)
        assert resp.status_code == 429 and resp.json()["detail"]["code"] == "QUOTA_EXCEEDED"
        assert mock.call_count == 0 and supabase.calls("refund_quota") == []

    @pytest.mark.parametrize("rule", [(500, {"message": "db down"}, 0), (200, b"<html>", 0), (200, [], 0)])
    def test_a_broken_quota_backend_refuses_with_503_and_never_runs_the_chat_unmetered(self, chat, supabase, rule):
        supabase.rules["/rest/v1/rpc/consume_quota"] = rule
        mock = MockProvider([text_attempt("must not be generated")])
        set_provider(mock)
        resp = post_stream(chat)
        assert resp.status_code == 503 and resp.json()["detail"]["code"] == "QUOTA_UNAVAILABLE"
        assert mock.call_count == 0

    def test_an_unreachable_quota_backend_after_auth_is_503_not_500(self, chat, supabase, monkeypatch):
        # Auth succeeds (fake server), then the quota host disappears.
        monkeypatch.setattr(database, "SUPABASE_URL", unused_port_url())
        mock = MockProvider([text_attempt("must not be generated")])
        set_provider(mock)
        resp = post_stream(chat)
        assert resp.status_code == 503 and mock.call_count == 0

    def test_a_failure_before_any_text_refunds_the_unit_with_the_same_ticket(self, chat, supabase):
        set_provider(MockProvider([failing_attempt(ProviderErrorKind.AUTH, http_status=401)]))
        resp = post_stream(chat)
        assert resp.status_code == 200 and events(resp)[-1]["type"] == "error"
        (consume,) = supabase.calls("consume_quota")
        (refund,) = supabase.calls("refund_quota")
        assert refund["body"]["p_request_id"] == consume["body"]["p_request_id"]
        assert hashlib.sha256(refund["body"]["p_refund_token"].encode()).hexdigest() == consume["body"]["p_refund_token_hash"]
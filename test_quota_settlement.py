"""
Pre-Phase 10 finalization - regression tests for Q-01, Q-03, Q-04, Q-10, Q-11.

CEO DECISION A (the policy these tests encode):
  If even ONE piece of generated text has been delivered to the student, the
  request is final and is NEVER refunded - whatever happens afterwards
  (provider failure, MAX_TOKENS, failed recovery, client disconnect). If NO text
  was delivered, a refund is allowed through the existing secret-token refund.

No network: the provider is the scripted MockProvider and the quota backend is
`LedgerQuota`, an in-memory stand-in for public.consume_quota / refund_quota that
keeps a REAL ledger (rows, secret-token hashes, refunded flags) so the tests
assert the balance and ledger - not merely "an RPC was called". The SQL
semantics themselves (atomicity, token gating, idempotent refunds, no credit for
a missing record) are proven separately against PostgreSQL by
supabase/tests/*.sql and *.sh.
"""
import asyncio
import hashlib
import json
import threading
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

import backend.main as main
from backend import quota_service, usage_telemetry
from backend.providers import set_provider
from backend.providers.base import ProviderErrorKind, ProviderEvent, ProviderUsage
from backend.providers.mock import (
    MockProvider, failing_attempt, max_tokens_attempt, text_attempt,
)

# Re-use the established endpoint harness (auth override, profile stubs, bounded
# state resets). Importing an autouse fixture by name registers it here.
from test_pre_phase10_endpoints import _app_state, client, events, post_stream  # noqa: F401


class LedgerQuota:
    """In-memory model of the quota database functions, with a real ledger."""

    def __init__(self, limits=None, fail_consume=False, lose_consume_response=False, fail_refund=False):
        self.limits = {"chat": 1, "quiz": 3, "notes_ai": 6}
        self.limits.update(limits or {})
        self.fail_consume = fail_consume
        self.lose_consume_response = lose_consume_response
        self.fail_refund = fail_refund
        self.rows = {}        # (kind, request_id) -> {"hash": str, "refunded": bool}
        self.calls = []
        self._lock = threading.Lock()

    def live(self, kind):
        return sum(1 for (k, _), r in self.rows.items() if k == kind and not r["refunded"])

    def named(self, function):
        return [c for c in self.calls if c["function"] == function]

    async def rpc(self, user_token, function, params):
        self.calls.append({"function": function, "params": dict(params), "token": user_token})
        if function == "consume_quota":
            if self.fail_consume:
                raise quota_service.QuotaUnavailable("down before any record exists")
            kind, rid = params["p_kind"], params["p_request_id"]
            with self._lock:
                if (kind, rid) in self.rows:
                    return {"allowed": True, "replay": True, "reason": "ok", "plan": "free", "kind": kind}
                used = self.live(kind)
                limit = self.limits[kind]
                if used >= limit:
                    return {"allowed": False, "reason": "quota_exceeded", "plan": "free", "kind": kind,
                            "limit": limit, "used": used, "remaining": 0,
                            "resets_at": "2026-10-26T00:00:00+06:00"}
                self.rows[(kind, rid)] = {"hash": params["p_refund_token_hash"], "refunded": False}
            if self.lose_consume_response:
                # The database COMMITTED the unit but the answer never reached the app.
                raise quota_service.QuotaUnavailable("response lost after commit")
            return {"allowed": True, "reason": "ok", "plan": "free", "kind": kind, "limit": limit,
                    "used": used + 1, "remaining": limit - used - 1,
                    "resets_at": "2026-10-26T00:00:00+06:00"}
        if function == "refund_quota":
            if self.fail_refund:
                raise quota_service.QuotaUnavailable("refund backend down")
            with self._lock:
                row = self.rows.get((params["p_kind"], params["p_request_id"]))
                token_hash = hashlib.sha256(params["p_refund_token"].encode()).hexdigest()
                if row is None or row["refunded"] or row["hash"] != token_hash:
                    return False        # no record / already refunded / wrong token: no credit
                row["refunded"] = True
                return True
        raise AssertionError(f"unexpected rpc {function}")


def use_ledger(monkeypatch, **kw):
    monkeypatch.setenv("QUOTA_MODE", "enforce")
    ledger = LedgerQuota(**kw)
    monkeypatch.setattr(quota_service, "_rpc", ledger.rpc)
    return ledger


def provider(*attempts):
    mock = MockProvider(list(attempts))
    set_provider(mock)
    return mock


def types(resp):
    return [e["type"] for e in events(resp)]


# ===========================================================================
# Q-01 - no refund once text has been delivered
# ===========================================================================

class TestNoRefundAfterTextDelivery:
    def test_max_tokens_after_text_is_not_refunded(self, client, monkeypatch):
        ledger = use_ledger(monkeypatch)
        provider(max_tokens_attempt("Some partial answer"))
        resp = post_stream(client)
        assert types(resp)[-1] == "interrupted" and "delta" in types(resp)
        assert ledger.named("refund_quota") == []
        assert ledger.live("chat") == 1

    def test_provider_failure_after_text_is_not_refunded(self, client, monkeypatch):
        ledger = use_ledger(monkeypatch)
        provider(failing_attempt(ProviderErrorKind.UNAVAILABLE, http_status=503, after_text=["Partial "]))
        resp = post_stream(client)
        assert types(resp)[-1] == "interrupted" and "delta" in types(resp)
        assert ledger.named("refund_quota") == []
        assert ledger.live("chat") == 1

    def test_failed_recovery_after_text_was_delivered_is_not_refunded(self, client, monkeypatch):
        """First attempt streams text then 503s; the single recovery generation
        fails before producing text. Text WAS delivered on the first attempt, so
        the request is final (CEO decision A, applied literally)."""
        ledger = use_ledger(monkeypatch)
        provider(
            failing_attempt(ProviderErrorKind.UNAVAILABLE, http_status=503, after_text=["Part "]),
            failing_attempt(ProviderErrorKind.UNAVAILABLE, http_status=503),
        )
        resp = post_stream(client)
        assert types(resp)[:3] == ["start", "delta", "retry"] and types(resp)[-1] == "error"
        assert ledger.named("refund_quota") == []
        assert ledger.live("chat") == 1

    def test_provider_failure_before_any_text_is_refunded_safely(self, client, monkeypatch):
        ledger = use_ledger(monkeypatch)
        provider(failing_attempt(ProviderErrorKind.UNAVAILABLE, http_status=503))
        resp = post_stream(client)
        assert types(resp)[-1] == "error" and "delta" not in types(resp)
        assert len(ledger.named("refund_quota")) == 1
        assert ledger.live("chat") == 0, "the unit went back to the student"

    def test_a_retry_that_succeeds_never_refunds(self, client, monkeypatch):
        ledger = use_ledger(monkeypatch)
        provider(
            failing_attempt(ProviderErrorKind.UNAVAILABLE, http_status=503, after_text=["Part "]),
            text_attempt("A full answer."),
        )
        resp = post_stream(client)
        assert types(resp)[-1] == "done"
        assert ledger.named("refund_quota") == [], "no refund, so no duplicate refund either"
        assert ledger.live("chat") == 1

    def test_pre_token_retry_then_success_never_refunds(self, client, monkeypatch):
        ledger = use_ledger(monkeypatch)
        provider(failing_attempt(ProviderErrorKind.UNAVAILABLE, http_status=503), text_attempt("Fine."))
        resp = post_stream(client)
        assert types(resp)[-1] == "done"
        assert ledger.named("refund_quota") == []
        assert ledger.live("chat") == 1

    def test_limit_stays_enforced_after_a_delivered_then_failed_response(self, client, monkeypatch):
        ledger = use_ledger(monkeypatch, limits={"chat": 1})
        mock = provider(failing_attempt(ProviderErrorKind.UNAVAILABLE, http_status=503, after_text=["Partial "]))
        first = post_stream(client)
        assert types(first)[-1] == "interrupted"
        calls_before = mock.call_count
        second = post_stream(client)
        assert second.status_code == 429 and second.json()["detail"]["code"] == "QUOTA_EXCEEDED"
        assert mock.call_count == calls_before, "an over-quota request never reaches the provider"

    def test_after_a_pre_text_failure_the_student_can_try_again(self, client, monkeypatch):
        ledger = use_ledger(monkeypatch, limits={"chat": 1})
        provider(failing_attempt(ProviderErrorKind.UNAVAILABLE, http_status=503))
        assert types(post_stream(client))[-1] == "error"
        provider(text_attempt("Now it works."))
        again = post_stream(client)
        assert again.status_code == 200 and types(again)[-1] == "done"
        assert ledger.live("chat") == 1

    def test_success_is_unchanged_and_records_usage(self, client, monkeypatch, usage_writes):
        ledger = use_ledger(monkeypatch)
        provider(text_attempt("Hello ", "world", usage=ProviderUsage(prompt_tokens=11, output_tokens=5, total_tokens=16)))
        resp = post_stream(client)
        ev = events(resp)
        assert [e["type"] for e in ev] == ["start", "delta", "delta", "done"] and ev[-1]["reply"] == "Hello world"
        assert ledger.named("refund_quota") == [] and ledger.live("chat") == 1
        assert usage_writes and usage_writes[0]["rows"][0]["status"] == "success"

    def test_the_client_cannot_influence_the_refund_decision(self, client, monkeypatch):
        """Delivery/refund state is server-side only: extra form fields that look
        like flags are ignored."""
        ledger = use_ledger(monkeypatch)
        provider(failing_attempt(ProviderErrorKind.UNAVAILABLE, http_status=503, after_text=["Partial "]))
        resp = post_stream(client, text_delivered="false", refund="true", force_refund="1", plan="pro")
        assert types(resp)[-1] == "interrupted"
        assert ledger.named("refund_quota") == []
        assert set(ledger.named("consume_quota")[0]["params"]) == {"p_kind", "p_request_id", "p_refund_token_hash"}

    def test_refund_token_and_ticket_never_reach_the_client_or_logs(self, client, monkeypatch, caplog):
        ledger = use_ledger(monkeypatch)
        provider(failing_attempt(ProviderErrorKind.UNAVAILABLE, http_status=503))
        with caplog.at_level("DEBUG"):
            resp = post_stream(client)
        token = ledger.named("refund_quota")[0]["params"]["p_refund_token"]
        assert token not in resp.text and token not in str(dict(resp.headers))
        assert token not in caplog.text and "token-1" not in caplog.text


# ---------------------------------------------------------------------------
# Client disconnects, exercised through the real ASGI application
# ---------------------------------------------------------------------------

class GatedProvider(MockProvider):
    """Yields `first` immediately (if given), then blocks until `release` is set,
    then yields the rest. Lets a test disconnect at an exact point."""

    def __init__(self, first, rest, release):
        super().__init__([text_attempt("unused")])
        self._first, self._rest, self._release = first, rest, release

    def stream_chat(self, request):
        self.calls.append("stream_chat")
        self.requests.append(request)

        def gen():
            if self._first:
                yield ProviderEvent(text=self._first)
            self._release.wait(timeout=10)
            for piece in self._rest:
                yield ProviderEvent(text=piece)
        return gen()


def _asgi_stream_then_disconnect(disconnect_on):
    """POSTs to /api/chat/stream through the ASGI interface and disconnects as
    soon as a body chunk containing `disconnect_on` is sent. Returns the chunks."""
    async def run():
        body = urlencode({"prompt": "What is force?", "chat_id": "chat-1"}).encode()
        sent_request = False
        disconnect = asyncio.Event()
        chunks = []

        async def receive():
            nonlocal sent_request
            if not sent_request:
                sent_request = True
                return {"type": "http.request", "body": body, "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                chunks.append(message["body"].decode())
                if disconnect_on in chunks[-1]:
                    disconnect.set()

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
            "path": "/api/chat/stream", "raw_path": b"/api/chat/stream", "query_string": b"", "root_path": "",
            "scheme": "http", "server": ("testserver", 80), "client": ("testclient", 1234),
            "headers": [(b"content-type", b"application/x-www-form-urlencoded"),
                        (b"content-length", str(len(body)).encode()), (b"host", b"testserver")],
        }
        await asyncio.wait_for(main.app(scope, receive, send), timeout=15)
        return chunks
    return asyncio.run(run())


class TestClientDisconnect:
    def test_cancel_after_text_delivery_is_not_refunded(self, monkeypatch, usage_writes):
        ledger = use_ledger(monkeypatch)
        release = threading.Event()
        set_provider(GatedProvider("Hello", ["world"], release))
        try:
            chunks = _asgi_stream_then_disconnect(disconnect_on='"delta"')
        finally:
            release.set()
        assert any('"delta"' in c for c in chunks)
        assert ledger.named("refund_quota") == []
        assert ledger.live("chat") == 1

    def test_cancel_before_any_text_is_not_refunded_by_design(self, monkeypatch):
        """The student walked away (their choice, not a service failure). Refunding
        would let anyone cancel for free credits and still cost us a provider call.
        Documented policy: abandoned streams are never refunded."""
        ledger = use_ledger(monkeypatch)
        release = threading.Event()
        set_provider(GatedProvider(None, ["late text"], release))
        try:
            chunks = _asgi_stream_then_disconnect(disconnect_on='"start"')
        finally:
            release.set()
        assert not any('"delta"' in c for c in chunks)
        assert ledger.named("refund_quota") == []
        assert ledger.live("chat") == 1


# ===========================================================================
# Q-03 - exceptions after quota was consumed
# ===========================================================================

@pytest.fixture
def crash_client():
    # Unhandled exceptions become real 500 responses, as in production.
    return TestClient(main.app, raise_server_exceptions=False)


class TestExceptionsAfterConsumption:
    def test_stream_profile_lookup_failure_refunds_and_stays_an_error(self, crash_client, monkeypatch):
        ledger = use_ledger(monkeypatch)

        async def boom(**kw):
            raise RuntimeError("profile lookup exploded")
        monkeypatch.setattr(main, "_get_academic_context", boom)
        mock = provider(text_attempt("never reached"))
        resp = crash_client.post("/api/chat/stream", data={"prompt": "x", "chat_id": "c1"})
        assert resp.status_code == 500, "the exception is not swallowed"
        assert "exploded" not in resp.text, "no internals leak"
        assert len(ledger.named("consume_quota")) == 1 and len(ledger.named("refund_quota")) == 1
        assert ledger.live("chat") == 0
        assert mock.call_count == 0

    def test_stream_display_name_failure_refunds(self, crash_client, monkeypatch):
        ledger = use_ledger(monkeypatch)

        async def boom(**kw):
            raise RuntimeError("name lookup exploded")
        monkeypatch.setattr(main, "_get_student_display_name", boom)
        provider(text_attempt("never reached"))
        resp = crash_client.post("/api/chat/stream", data={"prompt": "x", "chat_id": "c1"})
        assert resp.status_code == 500
        assert ledger.live("chat") == 0 and len(ledger.named("refund_quota")) == 1

    def test_quiz_generation_exception_refunds(self, crash_client, monkeypatch):
        ledger = use_ledger(monkeypatch)

        def boom(**kw):
            raise RuntimeError("quiz generation exploded")
        monkeypatch.setattr(main, "generate_quiz_questions", boom)
        resp = crash_client.post("/api/quiz/generate", data={"subject": "Physics", "topic": "Force", "count": 5})
        assert resp.status_code == 500
        assert ledger.live("quiz") == 0 and len(ledger.named("refund_quota")) == 1

    def test_quiz_profile_lookup_exception_refunds(self, crash_client, monkeypatch):
        ledger = use_ledger(monkeypatch)

        async def boom(**kw):
            raise RuntimeError("profile exploded")
        monkeypatch.setattr(main, "_get_academic_context", boom)
        resp = crash_client.post("/api/quiz/generate", data={"subject": "Physics", "topic": "Force", "count": 5})
        assert resp.status_code == 500
        assert ledger.live("quiz") == 0

    def test_quiz_storage_exception_refunds(self, crash_client, monkeypatch):
        ledger = use_ledger(monkeypatch)
        good = [{"question": "q?", "options": ["a", "b", "c", "d"], "correct_index": 1, "explanation": "e"}]
        monkeypatch.setattr(main, "generate_quiz_questions", lambda **kw: good)

        def boom(*a, **kw):
            raise RuntimeError("store exploded")
        monkeypatch.setattr(main._quiz_definitions, "put", boom)
        resp = crash_client.post("/api/quiz/generate", data={"subject": "Physics", "topic": "Force", "count": 1})
        assert resp.status_code == 500
        assert ledger.live("quiz") == 0

    def test_a_successful_quiz_is_not_refunded(self, client, monkeypatch):
        ledger = use_ledger(monkeypatch)
        good = [{"question": "q?", "options": ["a", "b", "c", "d"], "correct_index": 1, "explanation": "e"}]
        monkeypatch.setattr(main, "generate_quiz_questions", lambda **kw: good)
        resp = client.post("/api/quiz/generate", data={"subject": "Physics", "topic": "Force", "count": 1})
        assert resp.status_code == 200 and resp.json()["questions"]
        assert ledger.named("refund_quota") == [] and ledger.live("quiz") == 1

    def test_the_refund_is_made_once_even_if_settlement_runs_twice(self, monkeypatch):
        ledger = use_ledger(monkeypatch)

        async def run():
            ticket = quota_service.new_refund_ticket("chat", "req-settle-0001")
            await quota_service.consume_quota("tok", "chat", "req-settle-0001", ticket)
            c = usage_telemetry.start_collection("req-settle-0001", "")
            c.bind("user-1", "tok")
            c.refund_ticket = ticket
            c.force_refund = True
            await usage_telemetry.settle_request(c)
            await usage_telemetry.settle_request(c)       # e.g. guard + background task
            await usage_telemetry.finalize_ai_request(c)
        asyncio.run(run())
        assert len(ledger.named("refund_quota")) == 1
        assert ledger.live("chat") == 0

    def test_nothing_is_refunded_when_text_was_delivered_even_if_forced(self, monkeypatch):
        ledger = use_ledger(monkeypatch)

        async def run():
            ticket = quota_service.new_refund_ticket("chat", "req-settle-0002")
            await quota_service.consume_quota("tok", "chat", "req-settle-0002", ticket)
            c = usage_telemetry.start_collection("req-settle-0002", "")
            c.bind("user-1", "tok")
            c.refund_ticket = ticket
            c.mark_text_delivered()
            c.force_refund = True
            await usage_telemetry.settle_request(c)
        asyncio.run(run())
        assert ledger.named("refund_quota") == [] and ledger.live("chat") == 1


# ===========================================================================
# Q-04 - quota service failures and lost responses
# ===========================================================================

class TestQuotaServiceFailures:
    def test_consume_fails_before_any_record_exists_fails_closed_without_credit(self, client, monkeypatch):
        ledger = use_ledger(monkeypatch, fail_consume=True)
        mock = provider(text_attempt("never"))
        resp = post_stream(client)
        assert resp.status_code == 503 and resp.json()["detail"]["code"] == "QUOTA_UNAVAILABLE"
        assert mock.call_count == 0, "fail closed: never an unmetered AI request"
        assert ledger.live("chat") == 0 and ledger.rows == {}, "balance untouched, no phantom credit"

    def test_lost_consume_response_is_compensated_so_the_student_is_not_charged(self, client, monkeypatch):
        """The database COMMITTED the unit, but the answer was lost. The request
        is refused (503) and the compensating refund gives the unit back."""
        ledger = use_ledger(monkeypatch, lose_consume_response=True)
        mock = provider(text_attempt("never"))
        resp = post_stream(client)
        assert resp.status_code == 503
        assert mock.call_count == 0
        assert len(ledger.rows) == 1 and ledger.live("chat") == 0
        assert len(ledger.named("refund_quota")) == 1

    def test_lost_response_and_failed_refund_still_fails_closed(self, client, monkeypatch):
        """Residual risk, stated plainly: if the database is unreachable for BOTH
        calls, one unit may stay counted. The request itself is still refused."""
        ledger = use_ledger(monkeypatch, lose_consume_response=True, fail_refund=True)
        mock = provider(text_attempt("never"))
        resp = post_stream(client)
        assert resp.status_code == 503 and mock.call_count == 0
        assert ledger.live("chat") == 1

    def test_a_database_failure_is_never_masked_as_an_ai_answer(self, client, monkeypatch):
        use_ledger(monkeypatch, fail_consume=True)
        provider(text_attempt("An answer that must not appear"))
        resp = post_stream(client)
        assert "An answer" not in resp.text and resp.status_code == 503

    def test_refund_is_idempotent_at_the_ledger(self, monkeypatch):
        ledger = use_ledger(monkeypatch)

        async def run():
            ticket = quota_service.new_refund_ticket("chat", "req-idem-00001")
            await quota_service.consume_quota("tok", "chat", "req-idem-00001", ticket)
            first = await quota_service.refund_quota("tok", ticket)
            second = await quota_service.refund_quota("tok", ticket)
            return first, second
        first, second = asyncio.run(run())
        assert (first, second) == (True, False)
        assert ledger.live("chat") == 0

    def test_refunding_a_request_that_was_never_recorded_creates_no_credit(self, monkeypatch):
        ledger = use_ledger(monkeypatch, limits={"chat": 1})

        async def run():
            ghost = quota_service.new_refund_ticket("chat", "req-ghost-0001")
            refunded = await quota_service.refund_quota("tok", ghost)
            real = quota_service.new_refund_ticket("chat", "req-real-00001")
            first = await quota_service.consume_quota("tok", "chat", "req-real-00001", real)
            extra = quota_service.new_refund_ticket("chat", "req-real-00002")
            second = await quota_service.consume_quota("tok", "chat", "req-real-00002", extra)
            return refunded, first.allowed, second.allowed
        refunded, first_ok, second_ok = asyncio.run(run())
        assert refunded is False and first_ok is True and second_ok is False, "limit 1 still holds"

    def test_a_failing_refund_never_breaks_the_response_and_is_retried_once(self, client, monkeypatch):
        ledger = use_ledger(monkeypatch, fail_refund=True)
        provider(failing_attempt(ProviderErrorKind.UNAVAILABLE, http_status=503))
        resp = post_stream(client)
        assert resp.status_code == 200 and types(resp)[-1] == "error"
        assert len(ledger.named("refund_quota")) == 1
        assert ledger.live("chat") == 1, "unit stays counted: reported, not hidden"

    def test_an_over_quota_request_is_not_counted_or_refunded(self, client, monkeypatch):
        ledger = use_ledger(monkeypatch, limits={"chat": 0})
        mock = provider(text_attempt("never"))
        resp = post_stream(client)
        assert resp.status_code == 429 and mock.call_count == 0
        assert ledger.rows == {} and ledger.named("refund_quota") == []


# ===========================================================================
# Q-10 - invalid image input leaves a record and an eligible refund
# ===========================================================================

class TestInvalidImage:
    def test_undecodable_image_on_the_stream_is_recorded_and_refunded(self, client, monkeypatch, usage_writes):
        ledger = use_ledger(monkeypatch)
        mock = provider(text_attempt("never"))
        resp = client.post(
            "/api/chat/stream",
            data={"prompt": "What is this?", "chat_id": "c1"},
            files={"image": ("bad.png", b"this is not an image", "image/png")},
        )
        ev = events(resp)
        assert ev[-1]["type"] == "error" and ev[-1]["code"] == "REQUEST_INVALID"
        assert mock.call_count == 0, "no provider call was made"
        assert ledger.live("chat") == 0, "the student produced nothing usable: refunded"
        rows = [r for w in usage_writes for r in w["rows"]]
        assert rows and rows[0]["status"] == "error" and rows[0]["error_code"] == "REQUEST_INVALID"


# ===========================================================================
# Q-11 - non-streaming truncation is labelled, not presented as complete
# ===========================================================================

class TestNonStreamingTruncation:
    def test_max_tokens_is_returned_but_labelled_and_not_refunded(self, client, monkeypatch, usage_writes):
        ledger = use_ledger(monkeypatch)
        provider(max_tokens_attempt("A long answer that got cut"))
        resp = client.post("/api/chat", data={"prompt": "Explain", "chat_id": "c1"})
        body = resp.json()
        assert resp.status_code == 200 and body["reply"] == "A long answer that got cut"
        assert body["interrupted"] is True and body["code"] == "PROVIDER_MAX_TOKENS"
        assert ledger.named("refund_quota") == [] and ledger.live("chat") == 1, "text was delivered"
        rows = [r for w in usage_writes for r in w["rows"]]
        assert rows[0]["status"] == "interrupted"

    def test_a_clean_stop_is_not_labelled(self, client, monkeypatch):
        use_ledger(monkeypatch)
        provider(text_attempt("Complete answer."))
        body = client.post("/api/chat", data={"prompt": "Explain", "chat_id": "c1"}).json()
        assert body["reply"] == "Complete answer." and "interrupted" not in body

    def test_a_finish_reason_that_never_arrives_is_treated_as_complete(self, client, monkeypatch):
        use_ledger(monkeypatch)
        provider(text_attempt("Answer.", finish=None))
        body = client.post("/api/chat", data={"prompt": "Explain", "chat_id": "c1"}).json()
        assert "interrupted" not in body

    def test_provider_failure_without_text_is_refunded(self, client, monkeypatch):
        ledger = use_ledger(monkeypatch)
        provider(failing_attempt(ProviderErrorKind.UNKNOWN))
        body = client.post("/api/chat", data={"prompt": "Explain", "chat_id": "c1"}).json()
        assert "interrupted" not in body
        assert ledger.live("chat") == 0 and len(ledger.named("refund_quota")) == 1

    def test_a_successful_non_streaming_answer_is_not_refunded(self, client, monkeypatch):
        ledger = use_ledger(monkeypatch)
        provider(text_attempt("Good answer."))
        client.post("/api/chat", data={"prompt": "Explain", "chat_id": "c1"})
        assert ledger.named("refund_quota") == [] and ledger.live("chat") == 1
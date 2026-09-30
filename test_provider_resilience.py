"""
P0 STABILIZATION - tests for provider-failure resilience.

Covers, with behavior (not "the function exists") assertions:
  * bounded, jittered backoff between streaming retries (and no real waiting)
  * which failures are retried (5xx, 409, unknown pre-token errors) and which
    are not (429, 400/401/403/404, safety, max-tokens, timeouts get no extra wait)
  * the Bug 3 mid-stream recovery is intact: partial output discarded, one
    "retry" event, fresh attempt, no duplicate text, exactly one terminal event
  * stable error codes on the stream events, with student-safe text
  * one request id per request, consistent across logs, and no student content
    or provider internals in logs / event bodies
  * model configuration through GEMINI_MODEL, default unchanged

No network: the Gemini SDK client is replaced with fakes throughout.
`_sleep` is replaced with a recorder, so no test waits for a real backoff.
"""
import json
import logging
import os
import re
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from google.genai import errors as genai_errors
from google.genai import types
import httpx

import backend.ai_engine as ai_engine
import backend.main as main


# ---------------------------------------------------------------------------
# Fakes (self-contained on purpose - this module does not import from other
# test modules)
# ---------------------------------------------------------------------------

class _FakeChunk:
    def __init__(self, text=None, finish_reason=None):
        self.text = text
        self.usage_metadata = None
        if finish_reason is not None:
            cand = type("C", (), {"grounding_metadata": None, "finish_reason": finish_reason,
                                  "finish_message": None})()
            self.candidates = [cand]
        else:
            self.candidates = []


class _FakeChatSession:
    def __init__(self, chunks, raise_after=None, exc=None):
        self._chunks = chunks
        self._raise_after = raise_after
        self._exc = exc or RuntimeError("stream died")

    def send_message_stream(self, contents):
        for i, c in enumerate(self._chunks):
            if self._raise_after is not None and i == self._raise_after:
                raise self._exc
            yield c
        if self._raise_after == len(self._chunks):
            raise self._exc


def _install_sequence(monkeypatch, sessions):
    """Each chats.create() call returns the next (chunks, raise_after, exc);
    the last entry repeats. Returns a dict with the live call count and the
    kwargs of every create() call."""
    state = {"n": 0, "kwargs": []}

    class _Chats:
        def create(self, **kwargs):
            i = min(state["n"], len(sessions) - 1)
            state["n"] += 1
            state["kwargs"].append(kwargs)
            chunks, raise_after, exc = sessions[i]
            return _FakeChatSession(chunks, raise_after=raise_after, exc=exc)

    monkeypatch.setattr(ai_engine, "_client", type("C", (), {"chats": _Chats()})())
    return state


def _server_error(code=503, status="UNAVAILABLE"):
    return genai_errors.ServerError(
        code, {"error": {"code": code, "message": "high demand", "status": status}}, None
    )


def _client_error(code, status="ERROR"):
    return genai_errors.ClientError(
        code, {"error": {"code": code, "message": "provider text", "status": status}}, None
    )


_OK = ([_FakeChunk("full answer", finish_reason=types.FinishReason.STOP)], None, None)
_PRE_TOKEN_503 = ([], 0, _server_error(503))


@pytest.fixture
def sleeps(monkeypatch):
    """Records every backoff wait instead of performing it."""
    recorded = []
    monkeypatch.setattr(ai_engine, "_sleep", lambda seconds: recorded.append(seconds))
    return recorded


def _run(**kwargs):
    return list(ai_engine.stream_ai_response(prompt="hi", **kwargs))


def _bounds(n):
    """The documented [lo, hi] window for the delay before retry number n."""
    base = min(ai_engine.RETRY_DELAY_BASE_SECONDS * ai_engine.RETRY_BACKOFF_MULTIPLIER ** (n - 1),
               ai_engine.RETRY_BACKOFF_MAX_SECONDS)
    jitter = min(ai_engine.RETRY_DELAY_JITTER_SECONDS, base)
    return max(0.0, base - jitter), min(base + jitter, ai_engine.RETRY_BACKOFF_MAX_SECONDS)


# ---------------------------------------------------------------------------
# The backoff helper itself
# ---------------------------------------------------------------------------

class TestBackoffHelper:

    def test_delay_grows_between_retries(self):
        lo = lambda a, b: a
        d1 = ai_engine._compute_retry_backoff(1, rand=lo)
        d2 = ai_engine._compute_retry_backoff(2, rand=lo)
        d3 = ai_engine._compute_retry_backoff(3, rand=lo)
        assert d1 == pytest.approx(1.0)   # 1.5 - 0.5
        assert d2 == pytest.approx(2.5)   # 3.0 - 0.5
        assert d1 < d2 < d3

    def test_jitter_window_is_respected_at_both_ends(self):
        assert ai_engine._compute_retry_backoff(1, rand=lambda a, b: a) == pytest.approx(1.0)
        assert ai_engine._compute_retry_backoff(1, rand=lambda a, b: b) == pytest.approx(2.0)

    def test_real_random_stays_inside_the_window(self):
        lo, hi = _bounds(1)
        for _ in range(200):
            assert lo <= ai_engine._compute_retry_backoff(1) <= hi

    def test_delay_is_capped(self):
        for n in (4, 5, 10, 50):
            assert ai_engine._compute_retry_backoff(n, rand=lambda a, b: b) <= ai_engine.RETRY_BACKOFF_MAX_SECONDS

    def test_never_negative_even_with_oversized_jitter(self, monkeypatch):
        monkeypatch.setattr(ai_engine, "RETRY_DELAY_BASE_SECONDS", 0.1)
        monkeypatch.setattr(ai_engine, "RETRY_DELAY_JITTER_SECONDS", 5.0)
        assert ai_engine._compute_retry_backoff(1, rand=lambda a, b: a) >= 0.0

    @pytest.mark.parametrize("bad", [0, -3])
    def test_non_positive_retry_number_is_treated_as_first_retry(self, bad):
        lo = lambda a, b: a
        assert ai_engine._compute_retry_backoff(bad, rand=lo) == ai_engine._compute_retry_backoff(1, rand=lo)

    def test_constants_are_read_at_call_time_so_delays_are_configurable(self, monkeypatch):
        monkeypatch.setattr(ai_engine, "RETRY_DELAY_BASE_SECONDS", 4.0)
        monkeypatch.setattr(ai_engine, "RETRY_DELAY_JITTER_SECONDS", 0.0)
        assert ai_engine._compute_retry_backoff(1) == pytest.approx(4.0)

    def test_total_worst_case_added_wait_stays_small(self):
        worst = sum(_bounds(n)[1] for n in range(1, ai_engine.MAX_ATTEMPTS))
        assert worst < 10.0, "backoff must not turn a provider outage into a long student wait"


# ---------------------------------------------------------------------------
# Backoff in the streaming retry path
# ---------------------------------------------------------------------------

class TestStreamBackoff:

    def test_503_before_first_token_backs_off_between_every_attempt_then_fails_cleanly(self, monkeypatch, sleeps):
        state = _install_sequence(monkeypatch, [_PRE_TOKEN_503])
        out = _run()
        assert state["n"] == ai_engine.MAX_ATTEMPTS_BUCKET_B == 3, "existing attempt limit is unchanged"
        assert len(sleeps) == 2, "one wait between each pair of attempts, none after the last"
        for n, delay in enumerate(sleeps, start=1):
            lo, hi = _bounds(n)
            assert lo <= delay <= hi
        assert sleeps[0] < sleeps[1] or _bounds(1)[1] >= _bounds(2)[0]
        terminal = [c for c in out if c.kind in ("done", "interrupted", "error")]
        assert len(terminal) == 1 and terminal[0].kind == "error"
        assert terminal[0].error_code == ai_engine.ERROR_PROVIDER_UNAVAILABLE
        assert terminal[0].text == ai_engine.PROVIDER_UNAVAILABLE_ERROR

    @pytest.mark.parametrize("code", [500, 502, 503, 504])
    def test_every_5xx_is_retried_with_backoff(self, monkeypatch, sleeps, code):
        state = _install_sequence(monkeypatch, [([], 0, _server_error(code)), _OK])
        out = _run()
        assert state["n"] == 2
        assert len(sleeps) == 1
        assert out[-1].kind == "done" and out[-1].text == "full answer"

    def test_provider_recovering_on_a_later_attempt_yields_a_normal_answer(self, monkeypatch, sleeps):
        state = _install_sequence(monkeypatch, [_PRE_TOKEN_503, _PRE_TOKEN_503, _OK])
        out = _run()
        assert state["n"] == 3 and len(sleeps) == 2
        assert [c.kind for c in out] == ["text", "done"]
        assert out[-1].error_code is None

    def test_retries_are_bounded_no_infinite_loop(self, monkeypatch, sleeps):
        state = _install_sequence(monkeypatch, [_PRE_TOKEN_503])
        _run()
        assert state["n"] <= ai_engine.MAX_ATTEMPTS
        assert len(sleeps) <= ai_engine.MAX_ATTEMPTS - 1

    def test_409_is_retried_like_the_non_streaming_path(self, monkeypatch, sleeps):
        state = _install_sequence(monkeypatch, [([], 0, _client_error(409, "ABORTED"))])
        out = _run()
        assert state["n"] == 3 and len(sleeps) == 2
        assert out[-1].kind == "error" and out[-1].error_code == ai_engine.ERROR_PROVIDER_UNAVAILABLE

    def test_unknown_error_before_first_token_keeps_existing_retry_and_now_backs_off(self, monkeypatch, sleeps):
        state = _install_sequence(monkeypatch, [([], 0, RuntimeError("boom"))])
        out = _run()
        assert state["n"] == ai_engine.MAX_ATTEMPTS
        assert len(sleeps) == ai_engine.MAX_ATTEMPTS - 1
        assert out[-1].kind == "error" and out[-1].error_code == ai_engine.ERROR_INTERNAL
        assert out[-1].text == ai_engine.GENERIC_CHAT_ERROR

    def test_client_timeout_is_retried_without_stacking_a_backoff_on_the_wait_already_spent(self, monkeypatch, sleeps):
        state = _install_sequence(monkeypatch, [([], 0, httpx.ReadTimeout("deadline"))])
        out = _run()
        assert state["n"] == ai_engine.MAX_ATTEMPTS, "existing attempt limit for this path is unchanged"
        assert sleeps == []
        assert out[-1].error_code == ai_engine.ERROR_PROVIDER_TIMEOUT


class TestNonRetryableFailures:

    def test_429_is_not_retried_and_never_sleeps(self, monkeypatch, sleeps):
        state = _install_sequence(monkeypatch, [([], 0, _client_error(429, "RESOURCE_EXHAUSTED"))])
        out = _run()
        assert state["n"] == 1 and sleeps == []
        assert out[-1].kind == "error"
        assert out[-1].text == ai_engine.QUOTA_EXHAUSTED_ERROR
        assert out[-1].error_code == ai_engine.ERROR_PROVIDER_RATE_LIMITED

    @pytest.mark.parametrize("code,expected", [
        (400, ai_engine.ERROR_PROVIDER_INVALID_REQUEST),
        (404, ai_engine.ERROR_PROVIDER_INVALID_REQUEST),
        (401, ai_engine.ERROR_PROVIDER_AUTH_FAILED),
        (403, ai_engine.ERROR_PROVIDER_AUTH_FAILED),
    ])
    def test_non_retryable_client_errors_fail_immediately_with_their_own_code(self, monkeypatch, sleeps, code, expected):
        state = _install_sequence(monkeypatch, [([], 0, _client_error(code, "INVALID_ARGUMENT"))])
        out = _run()
        assert state["n"] == 1, "a request/config error cannot be fixed by retrying"
        assert sleeps == []
        assert out[-1].kind == "error" and out[-1].error_code == expected
        assert out[-1].text == ai_engine.GENERIC_CHAT_ERROR

    def test_safety_block_is_not_retried(self, monkeypatch, sleeps):
        state = _install_sequence(monkeypatch, [([_FakeChunk(None, finish_reason=types.FinishReason.SAFETY)], None, None)])
        out = _run()
        assert state["n"] == 1 and sleeps == []
        assert out[-1].kind == "error"
        assert out[-1].text == ai_engine.BLOCKED_RESPONSE_ERROR
        assert out[-1].error_code == ai_engine.ERROR_PROVIDER_SAFETY

    def test_empty_stream_without_a_reason_gets_the_empty_response_code(self, monkeypatch, sleeps):
        _install_sequence(monkeypatch, [([_FakeChunk(None)], None, None)])
        out = _run()
        assert out[-1].error_code == ai_engine.ERROR_PROVIDER_EMPTY_RESPONSE

    def test_max_tokens_is_interrupted_not_retried_and_has_its_own_code(self, monkeypatch, sleeps):
        state = _install_sequence(monkeypatch, [
            ([_FakeChunk("cut off", finish_reason=types.FinishReason.MAX_TOKENS)], None, None)
        ])
        out = _run()
        assert state["n"] == 1 and sleeps == []
        assert out[-1].kind == "interrupted" and out[-1].text == "cut off"
        assert out[-1].error_code == ai_engine.ERROR_PROVIDER_MAX_TOKENS

    def test_bad_image_fails_before_any_provider_call(self, monkeypatch, sleeps):
        state = _install_sequence(monkeypatch, [_OK])
        out = _run(image_bytes=b"not-an-image")
        assert state["n"] == 0 and sleeps == []
        assert len(out) == 1 and out[0].kind == "error"
        assert out[0].error_code == ai_engine.ERROR_REQUEST_INVALID


# ---------------------------------------------------------------------------
# Bug 3 mid-stream recovery must be intact
# ---------------------------------------------------------------------------

class TestRecoveryPreserved:

    def test_partial_output_is_discarded_and_only_the_fresh_answer_is_final(self, monkeypatch, sleeps):
        state = _install_sequence(monkeypatch, [
            ([_FakeChunk("attempt one partial")], 1, _server_error()),
            _OK,
        ])
        out = _run()
        kinds = [c.kind for c in out]
        assert kinds.count("retry") == 1
        assert kinds[-1] == "done"
        assert out[-1].text == "full answer"
        assert "attempt one partial" not in out[-1].text
        assert len([c for c in out if c.kind in ("done", "interrupted", "error")]) == 1
        assert state["n"] == 2

    def test_retry_event_is_sent_BEFORE_the_backoff_wait(self, monkeypatch):
        timeline = []
        monkeypatch.setattr(ai_engine, "_sleep", lambda s: timeline.append("sleep"))
        _install_sequence(monkeypatch, [
            ([_FakeChunk("attempt one partial")], 1, _server_error()),
            _OK,
        ])
        for chunk in ai_engine.stream_ai_response(prompt="hi"):
            timeline.append(chunk.kind)
        assert timeline == ["text", "retry", "sleep", "text", "done"], (
            "the client must be told to reset its bubble before the wait, not after it"
        )

    def test_recovery_waits_a_backoff_and_only_once(self, monkeypatch, sleeps):
        _install_sequence(monkeypatch, [([_FakeChunk("p")], 1, _server_error()), _OK])
        _run()
        assert len(sleeps) == 1
        lo, hi = _bounds(1)
        assert lo <= sleeps[0] <= hi

    def test_recovery_that_also_fails_is_interrupted_with_only_its_own_text(self, monkeypatch, sleeps):
        state = _install_sequence(monkeypatch, [
            ([_FakeChunk("first partial")], 1, _server_error()),
            ([_FakeChunk("second partial")], 1, _server_error()),
        ])
        out = _run()
        assert state["n"] == 2, "exactly one recovery, never more"
        assert [c.kind for c in out].count("retry") == 1
        assert out[-1].kind == "interrupted"
        assert out[-1].text == "second partial"
        assert "first partial" not in out[-1].text
        assert out[-1].error_code == ai_engine.ERROR_PROVIDER_UNAVAILABLE
        assert len(sleeps) == 1

    def test_recovery_is_not_granted_when_no_attempt_is_left_to_run_it(self, monkeypatch, sleeps):
        # Two pre-token 503s use up attempts 1 and 2; attempt 3 streams text
        # and then 503s. There is no attempt 4, so a "retry" event followed by
        # a generic error (discarding the partial for nothing) would be wrong:
        # the student keeps what was generated, marked interrupted.
        state = _install_sequence(monkeypatch, [
            _PRE_TOKEN_503, _PRE_TOKEN_503,
            ([_FakeChunk("late partial")], 1, _server_error()),
        ])
        out = _run()
        assert state["n"] == 3
        assert "retry" not in [c.kind for c in out]
        assert out[-1].kind == "interrupted" and out[-1].text == "late partial"
        assert len(sleeps) == 2

    def test_429_after_partial_output_still_never_recovers(self, monkeypatch, sleeps):
        state = _install_sequence(monkeypatch, [([_FakeChunk("partial")], 1, _client_error(429, "RESOURCE_EXHAUSTED"))])
        out = _run()
        assert state["n"] == 1 and sleeps == []
        assert "retry" not in [c.kind for c in out]
        assert out[-1].kind == "interrupted"

    def test_network_drop_after_partial_output_is_interrupted_with_its_own_code(self, monkeypatch, sleeps):
        state = _install_sequence(monkeypatch, [([_FakeChunk("partial")], 1, RuntimeError("connection reset"))])
        out = _run()
        assert state["n"] == 1 and sleeps == []
        assert out[-1].kind == "interrupted"
        assert out[-1].error_code == ai_engine.ERROR_STREAM_INTERRUPTED

    def test_normal_success_makes_one_call_and_never_waits(self, monkeypatch, sleeps):
        state = _install_sequence(monkeypatch, [_OK])
        out = _run()
        assert state["n"] == 1 and sleeps == []
        assert [c.kind for c in out] == ["text", "done"]


# ---------------------------------------------------------------------------
# Error codes and student-safe text
# ---------------------------------------------------------------------------

class TestErrorCodes:

    def test_provider_503_is_distinguishable_from_an_internal_failure(self, monkeypatch, sleeps):
        _install_sequence(monkeypatch, [_PRE_TOKEN_503])
        unavailable = _run()[-1]
        _install_sequence(monkeypatch, [([], 0, RuntimeError("bug"))])
        internal = _run()[-1]
        assert unavailable.error_code != internal.error_code
        assert unavailable.text != internal.text, "a student can tell 'busy, wait a minute' from 'something broke'"

    def test_student_messages_never_contain_provider_internals(self):
        for msg in (ai_engine.PROVIDER_UNAVAILABLE_ERROR, ai_engine.GENERIC_CHAT_ERROR):
            low = msg.lower()
            for leak in ("503", "unavailable", "high demand", "traceback", "gemini", "api key", "status"):
                assert leak not in low

    def test_codes_are_stable_strings(self):
        assert ai_engine.ERROR_PROVIDER_UNAVAILABLE == "PROVIDER_UNAVAILABLE"
        assert ai_engine.ERROR_PROVIDER_RATE_LIMITED == "PROVIDER_RATE_LIMITED"
        assert ai_engine.ERROR_STREAM_INTERRUPTED == "STREAM_INTERRUPTED"
        assert ai_engine.ERROR_INTERNAL == "INTERNAL_ERROR"

    def test_successful_answer_carries_no_error_code(self, monkeypatch, sleeps):
        _install_sequence(monkeypatch, [_OK])
        assert all(c.error_code is None for c in _run())


# ---------------------------------------------------------------------------
# Request id and logging
# ---------------------------------------------------------------------------

def _ai_records(caplog):
    return [r for r in caplog.records if r.name == "kognit.ai_engine"]


class TestRequestIdAndLogging:

    def test_supplied_request_id_appears_in_every_attempt_and_telemetry_line(self, monkeypatch, sleeps, caplog):
        caplog.set_level(logging.INFO)
        _install_sequence(monkeypatch, [_PRE_TOKEN_503])
        _run(request_id="rid-abc-123")
        msgs = [r.getMessage() for r in _ai_records(caplog)]
        attempts = [m for m in msgs if "provider attempt=" in m]
        backoffs = [m for m in msgs if "retry backoff" in m]
        telemetry = [m for m in msgs if "stream_telemetry" in m]
        assert len(attempts) == 3 and len(backoffs) == 2 and len(telemetry) == 1
        for m in attempts + backoffs + telemetry:
            assert "request_id=rid-abc-123" in m or "request_id=rid-abc-123" in m.split("error_code=")[-1]
        assert "error_code=PROVIDER_UNAVAILABLE" in telemetry[0]
        assert "outcome=error_server" in telemetry[0]

    def test_a_request_id_is_generated_once_when_none_is_supplied(self, monkeypatch, sleeps, caplog):
        caplog.set_level(logging.INFO)
        _install_sequence(monkeypatch, [_PRE_TOKEN_503])
        _run()
        ids = set()
        for r in _ai_records(caplog):
            ids.update(re.findall(r"request_id=([0-9a-f]{12})", r.getMessage()))
        assert len(ids) == 1, "one request must have exactly one id across all of its log lines"

    def test_two_requests_get_different_ids(self):
        assert ai_engine.new_request_id() != ai_engine.new_request_id()
        assert re.fullmatch(r"[0-9a-f]{12}", ai_engine.new_request_id())

    def test_transient_5xx_does_not_dump_a_traceback_per_attempt(self, monkeypatch, sleeps, caplog):
        caplog.set_level(logging.INFO)
        _install_sequence(monkeypatch, [_PRE_TOKEN_503])
        _run()
        with_tb = [r for r in _ai_records(caplog) if r.exc_info]
        assert with_tb == [], "an expected provider 503 must not produce repeated stack traces"

    def test_unexpected_exception_still_gets_one_traceback_on_final_failure(self, monkeypatch, sleeps, caplog):
        caplog.set_level(logging.INFO)
        _install_sequence(monkeypatch, [([], 0, RuntimeError("real bug"))])
        _run()
        with_tb = [r for r in _ai_records(caplog) if r.exc_info]
        assert len(with_tb) == 1, "unexpected failures keep their debugging detail - once"

    def test_logs_never_contain_the_prompt_or_provider_message_text(self, monkeypatch, sleeps, caplog):
        caplog.set_level(logging.DEBUG)
        _install_sequence(monkeypatch, [_PRE_TOKEN_503])
        list(ai_engine.stream_ai_response(prompt="UNIQUE-SECRET-PROMPT-991", history=[
            {"role": "user", "text": "UNIQUE-SECRET-HISTORY-992"}]))
        blob = "\n".join(r.getMessage() for r in caplog.records)
        assert "UNIQUE-SECRET-PROMPT-991" not in blob
        assert "UNIQUE-SECRET-HISTORY-992" not in blob
        assert "high demand" not in blob, "provider message text stays out of the logs"


# ---------------------------------------------------------------------------
# HTTP level: event codes, request id header, no leakage
# ---------------------------------------------------------------------------

def _events(body):
    return [json.loads(line) for line in body.strip().split("\n") if line.strip()]


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def authed(monkeypatch):
    async def _fake_user():
        return ("user-1", "token-1")
    main.app.dependency_overrides[main._rate_limited_chat] = _fake_user

    async def _ctx(**kwargs):
        return ("Class 9-10 (SSC)", "Science")
    monkeypatch.setattr(main, "_get_academic_context", _ctx)
    yield
    main.app.dependency_overrides.clear()


class TestHttpContract:

    def _post(self, client, **extra):
        return client.post("/api/chat/stream", data={"prompt": "q", "chat_id": "c1"}, **extra)

    def test_503_exhausted_emits_error_with_code_and_safe_text(self, client, authed, monkeypatch, sleeps):
        _install_sequence(monkeypatch, [_PRE_TOKEN_503])
        r = self._post(client)
        assert r.status_code == 200
        last = _events(r.text)[-1]
        assert last == {"type": "error", "reply": ai_engine.PROVIDER_UNAVAILABLE_ERROR,
                        "code": "PROVIDER_UNAVAILABLE"}
        assert "503" not in r.text and "high demand" not in r.text.lower()

    def test_429_emits_rate_limited_code(self, client, authed, monkeypatch, sleeps):
        _install_sequence(monkeypatch, [([], 0, _client_error(429, "RESOURCE_EXHAUSTED"))])
        last = _events(self._post(client).text)[-1]
        assert last["type"] == "error" and last["code"] == "PROVIDER_RATE_LIMITED"
        assert last["reply"] == ai_engine.QUOTA_EXHAUSTED_ERROR

    def test_max_tokens_interrupted_event_carries_its_code(self, client, authed, monkeypatch, sleeps):
        _install_sequence(monkeypatch, [([_FakeChunk("cut", finish_reason=types.FinishReason.MAX_TOKENS)], None, None)])
        last = _events(self._post(client).text)[-1]
        assert last["type"] == "interrupted" and last["code"] == "PROVIDER_MAX_TOKENS"
        assert last["reply"] == "cut"

    def test_network_drop_after_partial_is_interrupted_stream_interrupted(self, client, authed, monkeypatch, sleeps):
        _install_sequence(monkeypatch, [([_FakeChunk("part")], 1, RuntimeError("reset"))])
        last = _events(self._post(client).text)[-1]
        assert last["type"] == "interrupted" and last["code"] == "STREAM_INTERRUPTED"

    def test_unexpected_internal_failure_emits_internal_error(self, client, authed, monkeypatch, sleeps):
        def _boom(**kwargs):
            raise RuntimeError("bug in our code with secret detail")
        monkeypatch.setattr(main, "stream_ai_response", _boom)
        r = self._post(client)
        last = _events(r.text)[-1]
        assert last["type"] == "error" and last["code"] == "INTERNAL_ERROR"
        assert last["reply"] == ai_engine.GENERIC_CHAT_ERROR
        assert "secret detail" not in r.text

    def test_bad_image_emits_request_invalid_code(self, client, authed, monkeypatch, sleeps):
        _install_sequence(monkeypatch, [_OK])
        r = self._post(client, files={"image": ("x.png", b"not-an-image", "image/png")})
        last = _events(r.text)[-1]
        assert last["type"] == "error" and last["code"] == "REQUEST_INVALID"

    def test_success_events_are_unchanged_and_have_no_code(self, client, authed, monkeypatch, sleeps):
        _install_sequence(monkeypatch, [([_FakeChunk("Force "), _FakeChunk("equals ma", finish_reason=types.FinishReason.STOP)], None, None)])
        events = _events(self._post(client).text)
        assert [e["type"] for e in events] == ["start", "delta", "delta", "done"]
        assert events[-1]["reply"] == "Force equals ma"
        assert all("code" not in e for e in events)

    def test_recovery_over_http_still_emits_retry_then_done_with_no_duplicate_text(self, client, authed, monkeypatch, sleeps):
        _install_sequence(monkeypatch, [([_FakeChunk("one partial")], 1, _server_error()), _OK])
        events = _events(self._post(client).text)
        types_seen = [e["type"] for e in events]
        assert types_seen.count("retry") == 1
        assert types_seen[-1] == "done"
        assert events[-1]["reply"] == "full answer"
        assert len([e for e in events if e["type"] in ("done", "interrupted", "error")]) == 1
        after_retry = events[types_seen.index("retry") + 1:]
        assert "one partial" not in "".join(e.get("text", "") for e in after_retry)

    def test_request_id_header_matches_the_id_in_every_log_line_of_that_request(self, client, authed, monkeypatch, sleeps, caplog):
        caplog.set_level(logging.INFO)
        _install_sequence(monkeypatch, [_PRE_TOKEN_503])
        r = client.post("/api/chat/stream", data={"prompt": "UNIQUE-HTTP-PROMPT-771", "chat_id": "c1"})
        rid = r.headers["x-request-id"]
        assert re.fullmatch(r"[0-9a-f]{12}", rid)
        msgs = [(rec.name, rec.getMessage()) for rec in caplog.records]
        main_lines = [m for n, m in msgs if n == "kognit.main" and "received" in m]
        assert len(main_lines) == 1 and f"request_id={rid}" in main_lines[0]
        ai_lines = [m for n, m in msgs if n == "kognit.ai_engine"
                    and ("provider attempt=" in m or "retry backoff" in m or "stream_telemetry" in m)]
        assert len(ai_lines) == 3 + 2 + 1
        assert all(f"request_id={rid}" in m for m in ai_lines)
        assert rid not in r.text, "the id is a header and a log key, not part of the student-visible body"
        assert "UNIQUE-HTTP-PROMPT-771" not in "\n".join(m for _, m in msgs)

    def test_each_request_gets_its_own_id(self, client, authed, monkeypatch, sleeps):
        _install_sequence(monkeypatch, [_OK])
        a = self._post(client).headers["x-request-id"]
        b = self._post(client).headers["x-request-id"]
        assert a != b


# ---------------------------------------------------------------------------
# Model configuration
# ---------------------------------------------------------------------------

class TestModelConfiguration:

    def test_default_is_unchanged_when_env_is_absent(self):
        assert ai_engine.DEFAULT_MODEL_NAME == "gemini-3.6-flash"
        assert ai_engine._resolve_model_name({}) == "gemini-3.6-flash"

    def test_env_value_overrides_the_default(self):
        assert ai_engine._resolve_model_name({"GEMINI_MODEL": "gemini-custom-flash"}) == "gemini-custom-flash"

    def test_env_value_is_trimmed(self):
        assert ai_engine._resolve_model_name({"GEMINI_MODEL": "  gemini-custom-flash \n"}) == "gemini-custom-flash"

    @pytest.mark.parametrize("value", ["", "   ", None])
    def test_empty_env_value_falls_back_to_the_default(self, value):
        assert ai_engine._resolve_model_name({"GEMINI_MODEL": value}) == ai_engine.DEFAULT_MODEL_NAME

    @pytest.mark.parametrize("value", ["two words", "model;rm -rf", "x" * 200, "-leading-dash", "bad\nname"])
    def test_implausible_value_is_ignored_with_a_warning(self, value, caplog):
        caplog.set_level(logging.WARNING)
        assert ai_engine._resolve_model_name({"GEMINI_MODEL": value}) == ai_engine.DEFAULT_MODEL_NAME
        assert any("not a valid model identifier" in r.getMessage() for r in caplog.records)

    def test_reads_the_real_environment_by_default(self, monkeypatch):
        monkeypatch.setenv("GEMINI_MODEL", "gemini-from-real-env")
        assert ai_engine._resolve_model_name() == "gemini-from-real-env"
        monkeypatch.delenv("GEMINI_MODEL")
        assert ai_engine._resolve_model_name() == ai_engine.DEFAULT_MODEL_NAME

    def test_the_configured_model_is_what_is_sent_to_the_provider(self, monkeypatch, sleeps):
        monkeypatch.setattr(ai_engine, "MODEL_NAME", "custom-model-x")
        state = _install_sequence(monkeypatch, [_OK])
        _run()
        assert [k["model"] for k in state["kwargs"]] == ["custom-model-x"]

    def test_module_level_wiring_honours_the_environment_at_import(self):
        root = os.path.dirname(os.path.abspath(__file__))
        code = "import backend.ai_engine as a; print(a.MODEL_NAME)"
        base = dict(os.environ, GEMINI_API_KEY="dummy")
        out = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True,
                             env=dict(base, GEMINI_MODEL="gemini-import-override"), timeout=120)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip().splitlines()[-1] == "gemini-import-override"
        out = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True,
                             env=dict(base, GEMINI_MODEL="bad value with spaces"), timeout=120)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip().splitlines()[-1] == "gemini-3.6-flash"
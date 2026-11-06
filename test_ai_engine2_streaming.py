"""
AI Engine 2.0 - integration tests that drive the REAL attempt loop
(backend.ai_engine.stream_ai_response) with the deterministic mock provider.

They prove: model routing reaches the provider request; every provider attempt
(including a discarded partial) is recorded with its model, kind and cost;
deadlines, the retry budget and the circuit breaker actually stop retries;
a 429 is never retried on the same model; and the Bug-3 contract (exactly one
terminal event, no concatenation of failed partial output) still holds.
"""
import io

import pytest
from PIL import Image

from backend import ai_engine, usage_collector
from backend.ai import config, gateway, health, registry
from backend.providers import ProviderErrorKind as K
from backend.providers import ProviderEvent, ProviderUsage, FinishReason, set_provider
from backend.providers.mock import MockProvider, failing_attempt, raising_attempt, text_attempt

GENERAL = "gemini-3.1-flash-lite"
ADVANCED = "gemini-3.5-flash-lite"
TERMINALS = ("done", "interrupted", "error")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(ai_engine, "_sleep", lambda s: None)
    yield
    set_provider(None)
    usage_collector._current.set(None)      # start_collection() binds a ContextVar; never leak it into other tests


def stream(attempts, *, collect=True, provider=None, **kw):
    mock = provider or MockProvider(attempts)
    set_provider(mock)
    collector = usage_collector.start_collection("rid-e2", "") if collect else None
    kw.setdefault("request_id", "rid-e2")
    kw.setdefault("user_class", "Class 9-10 (SSC)")
    kw.setdefault("stream", "Science")
    prompt = kw.pop("prompt", "Who wrote Amar Shonar Bangla?")
    chunks = list(ai_engine.stream_ai_response(prompt, **kw))
    return mock, collector, chunks


def kinds(chunks):
    return [c.kind for c in chunks]


def terminals(chunks):
    return [c for c in chunks if c.kind in TERMINALS]


def png():
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


def U(p=1000, o=200, t=0):
    return ProviderUsage(prompt_tokens=p, output_tokens=o, thoughts_tokens=t, total_tokens=p + o + t)


# ============================ routing reaches the provider ===================
class TestModelSelection:
    def test_static_mode_is_the_legacy_behaviour(self):
        mock, _c, chunks = stream([text_attempt("ok")], plan="pro")
        req = mock.requests[0]
        assert req.model == ai_engine.MODEL_NAME and req.reasoning_effort == ai_engine.CHAT_THINKING_LEVEL
        assert kinds(chunks) == ["text", "done"]

    def test_policy_mode_sends_the_routed_model_and_only_documented_thinking(self, monkeypatch):
        monkeypatch.setenv("AI_ROUTER_MODE", "policy")
        mock, _c, _ = stream([text_attempt("ok")], plan="free")
        assert mock.requests[0].model == GENERAL and mock.requests[0].reasoning_effort is None
        mock, _c, _ = stream([text_attempt("ok")], plan="free", prompt="Solve the equation x^2 - 5x + 6 = 0")
        assert mock.requests[0].model == ADVANCED and mock.requests[0].reasoning_effort == "medium"

    def test_images_and_documents_route_to_the_advanced_model(self, monkeypatch):
        monkeypatch.setenv("AI_ROUTER_MODE", "policy")
        mock, _c, _ = stream([text_attempt("ok")], image_bytes=png(), prompt="what is this?")
        assert mock.requests[0].model == ADVANCED
        mock, _c, _ = stream([text_attempt("ok")], pdf_context="Chapter 1 text", prompt="summarise")
        assert mock.requests[0].model == ADVANCED

    def test_shadow_mode_logs_the_decision_but_keeps_the_legacy_model(self, monkeypatch, caplog):
        monkeypatch.setenv("AI_ROUTER_MODE", "shadow")
        with caplog.at_level("INFO"):
            mock, _c, _ = stream([text_attempt("ok")], prompt="Solve x^2 = 4")
        assert mock.requests[0].model == ai_engine.MODEL_NAME
        assert "route_model=gemini-3.5-flash-lite" in caplog.text and "route_reason=advanced_mathematics" in caplog.text

    def test_pro_students_are_split_across_both_models_for_general_text(self, monkeypatch):
        monkeypatch.setenv("AI_ROUTER_MODE", "policy")
        used = set()
        for i in range(40):
            mock, _c, _ = stream([text_attempt("ok")], plan="pro", request_id=f"pro-{i}", collect=False)
            used.add(mock.requests[0].model)
        assert used == {GENERAL, ADVANCED}


# ============================ per-attempt usage ==============================
class TestPerAttemptUsage:
    def test_a_discarded_partial_attempt_is_recorded_as_its_own_row(self, monkeypatch):
        monkeypatch.setenv("AI_ROUTER_MODE", "policy")
        mock, col, chunks = stream([
            failing_attempt(K.UNAVAILABLE, http_status=503, after_text=["partial"]),
            text_attempt("full answer", usage=U(1000, 200)),
        ], prompt="Solve x^2 = 4")
        assert kinds(chunks) == ["text", "retry", "text", "done"] and len(terminals(chunks)) == 1
        assert chunks[-1].text == "full answer"                     # never concatenated with "partial"
        rows = col.snapshot()
        assert [r["attempt"] for r in rows] == [1, 2]
        assert [r["status"] for r in rows] == ["interrupted", "success"]
        assert [r["attempt_kind"] for r in rows] == ["primary", "recovery"]
        assert rows[1]["input_tokens"] == 1000 and rows[1]["output_tokens"] == 200
        assert rows[0]["model"] == rows[1]["model"] == ADVANCED

    def test_cost_is_estimated_from_reported_usage_and_the_registry_price(self, monkeypatch):
        monkeypatch.setenv("AI_ROUTER_MODE", "policy")
        _m, col, _ = stream([text_attempt("ok", usage=U(1000, 500, 100))])
        row = col.snapshot()[0]
        assert row["model"] == GENERAL and row["estimated_cost_usd"] == pytest.approx(0.00115)
        assert row["pricing_version"] == registry.PRICE_VERSION

    def test_cost_is_null_when_the_provider_reported_no_usage_or_the_model_is_unpriced(self):
        _m, col, _ = stream([text_attempt("ok")])                    # static mode -> legacy gemini-3.6-flash, no price
        row = col.snapshot()[0]
        assert row["estimated_cost_usd"] is None

    def test_rows_carry_the_route_and_never_any_student_text(self, monkeypatch):
        monkeypatch.setenv("AI_ROUTER_MODE", "policy")
        secret = "MY-SECRET-QUESTION-ABOUT-X-12345"
        _m, col, _ = stream([text_attempt("MY-SECRET-ANSWER", usage=U())], prompt=secret + " solve 2x = 4")
        row = col.snapshot()[0]
        assert row["route_reason"] and row["route_policy_version"] and row["task_category"]
        flat = repr(row)
        assert "MY-SECRET" not in flat and secret not in flat

    def test_a_plain_success_makes_exactly_one_provider_call_and_one_row(self):
        mock, col, chunks = stream([text_attempt("hello", usage=U())])
        assert mock.call_count == 1 and len(col.snapshot()) == 1 and kinds(chunks) == ["text", "done"]


# ============================ retry budget & deadlines =======================
class TestRetryBudgetAndDeadlines:
    def test_an_empty_retry_budget_fails_fast_with_one_provider_call(self, monkeypatch):
        monkeypatch.setattr(gateway, "retry_budget", gateway.RetryBudget(burst=0, refill_per_second=0))
        mock, _c, chunks = stream([failing_attempt(K.UNAVAILABLE, http_status=503)])
        assert mock.call_count == 1
        assert kinds(chunks) == ["error"] and chunks[-1].error_code == ai_engine.ERROR_PROVIDER_UNAVAILABLE

    def test_the_budget_also_gates_recovery_after_partial_output(self, monkeypatch):
        monkeypatch.setattr(gateway, "retry_budget", gateway.RetryBudget(burst=0, refill_per_second=0))
        mock, _c, chunks = stream([failing_attempt(K.UNAVAILABLE, http_status=503, after_text=["abc"]), text_attempt("x")])
        assert mock.call_count == 1 and kinds(chunks) == ["text", "interrupted"] and len(terminals(chunks)) == 1

    def test_one_outage_cannot_spend_more_retries_than_the_budget_allows(self, monkeypatch):
        budget = gateway.RetryBudget(burst=4, refill_per_second=0)
        monkeypatch.setattr(gateway, "retry_budget", budget)
        calls = 0
        for i in range(20):                                        # 20 students hit the same outage
            mock, _c, _ = stream([failing_attempt(K.UNAVAILABLE, http_status=503)], request_id=f"o-{i}", collect=False)
            calls += mock.call_count
        assert calls == 20 + 4                                      # one try each + exactly 4 budgeted retries

    def test_legacy_gateway_flag_restores_unbudgeted_retries(self, monkeypatch):
        monkeypatch.setenv("AI_GATEWAY", "legacy")
        monkeypatch.setattr(gateway, "retry_budget", gateway.RetryBudget(burst=0, refill_per_second=0))
        mock, _c, _ = stream([failing_attempt(K.UNAVAILABLE, http_status=503)])
        assert mock.call_count == ai_engine.MAX_ATTEMPTS_BUCKET_B

    def test_the_first_token_deadline_stops_retries(self, monkeypatch):
        clock = _Clock()
        _patch_governor_clock(monkeypatch, clock, first_token=10.0)
        provider = _ClockedProvider(clock, [("fail503", 30.0)])      # the attempt "takes" 30 s then 503s
        provider_calls = []
        provider.on_call = provider_calls.append
        _m, _c, chunks = stream(None, provider=provider)
        assert len(provider_calls) == 1                              # no second attempt: the 10 s deadline is gone
        assert kinds(chunks) == ["error"]

    def test_the_stream_deadline_ends_a_slow_answer_as_interrupted_not_complete(self, monkeypatch):
        clock = _Clock()
        _patch_governor_clock(monkeypatch, clock, first_token=100.0, stream_deadline=50.0)
        provider = _ClockedProvider(clock, [("text_then_stall", 1000.0)])
        _m, _c, chunks = stream(None, provider=provider)
        assert kinds(chunks)[-1] == "interrupted" and len(terminals(chunks)) == 1
        assert "done" not in kinds(chunks)


# ============================ breaker, fallback, 429 =========================
class TestHealthFallbackAndRateLimits:
    def test_a_429_is_never_retried_on_the_same_model(self):
        mock, _c, chunks = stream([failing_attempt(K.RATE_LIMITED, http_status=429), text_attempt("x")])
        assert mock.call_count == 1 and kinds(chunks) == ["error"]
        assert chunks[-1].error_code == ai_engine.ERROR_PROVIDER_RATE_LIMITED

    def test_with_fallback_enabled_a_429_gets_one_attempt_on_the_other_approved_model(self, monkeypatch):
        monkeypatch.setenv("AI_ROUTER_MODE", "policy")
        monkeypatch.setenv("AI_MODEL_FALLBACK", "on")
        mock, col, chunks = stream([failing_attempt(K.RATE_LIMITED, http_status=429), text_attempt("recovered")])
        assert mock.call_count == 2 and kinds(chunks) == ["text", "done"] and chunks[-1].text == "recovered"
        assert [r.model for r in mock.requests] == [GENERAL, ADVANCED]
        rows = col.snapshot()
        assert rows[-1]["attempt_kind"] == "fallback" and rows[-1]["fallback_from"] == GENERAL

    def test_a_429_on_both_models_stops_after_exactly_two_attempts(self, monkeypatch):
        monkeypatch.setenv("AI_ROUTER_MODE", "policy")
        monkeypatch.setenv("AI_MODEL_FALLBACK", "on")
        mock, _c, chunks = stream([failing_attempt(K.RATE_LIMITED, http_status=429)])
        assert mock.call_count == 2 and kinds(chunks) == ["error"]

    def test_an_open_breaker_switches_the_retry_to_the_other_model(self, monkeypatch):
        monkeypatch.setenv("AI_ROUTER_MODE", "policy")
        monkeypatch.setenv("AI_MODEL_FALLBACK", "on")
        monkeypatch.setenv("AI_BREAKER_FAILURES", "1")
        mock, col, chunks = stream([failing_attempt(K.UNAVAILABLE, http_status=503), text_attempt("ok")])
        assert [r.model for r in mock.requests] == [GENERAL, ADVANCED] and kinds(chunks) == ["text", "done"]
        assert health.tracker.state(GENERAL) == "open"
        assert col.snapshot()[-1]["fallback_from"] == GENERAL

    def test_without_the_fallback_flag_a_retry_stays_on_the_same_model(self, monkeypatch):
        monkeypatch.setenv("AI_ROUTER_MODE", "policy")
        monkeypatch.setenv("AI_BREAKER_FAILURES", "1")
        mock, _c, _ = stream([failing_attempt(K.UNAVAILABLE, http_status=503), text_attempt("ok")])
        assert [r.model for r in mock.requests] == [GENERAL, GENERAL]

    def test_success_and_failures_are_reported_to_model_health(self, monkeypatch):
        monkeypatch.setenv("AI_ROUTER_MODE", "policy")
        stream([failing_attempt(K.UNAVAILABLE, http_status=503), text_attempt("ok")], collect=False)
        snap = health.tracker.snapshot(GENERAL)
        assert snap.window_requests == 2 and snap.window_failures == 1

    def test_bad_requests_and_auth_errors_never_count_against_model_health(self, monkeypatch):
        monkeypatch.setenv("AI_ROUTER_MODE", "policy")
        stream([failing_attempt(K.AUTH, http_status=401)], collect=False)
        stream([failing_attempt(K.INVALID_REQUEST, http_status=400)], collect=False)
        assert health.tracker.snapshot(GENERAL).window_failures == 0


# ============================ the Bug-3 contract still holds =================
SCENARIOS = {
    "success": [text_attempt("a")],
    "pre_token_503_then_ok": [failing_attempt(K.UNAVAILABLE, http_status=503), text_attempt("a")],
    "partial_503_recovered": [failing_attempt(K.UNAVAILABLE, http_status=503, after_text=["x"]), text_attempt("a")],
    "partial_503_twice": [failing_attempt(K.UNAVAILABLE, http_status=503, after_text=["x"])],
    "429": [failing_attempt(K.RATE_LIMITED, http_status=429)],
    "auth": [failing_attempt(K.AUTH, http_status=401)],
    "timeout": [failing_attempt(K.TIMEOUT)],
    "unknown": [raising_attempt(RuntimeError("bug"))],
    "unknown_mid_stream": [raising_attempt(RuntimeError("bug"), after_text=["p"])],
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
@pytest.mark.parametrize("mode", ["static", "policy"])
@pytest.mark.parametrize("fallback", ["off", "on"])
def test_exactly_one_terminal_event_in_every_scenario(monkeypatch, name, mode, fallback):
    monkeypatch.setenv("AI_ROUTER_MODE", mode)
    monkeypatch.setenv("AI_MODEL_FALLBACK", fallback)
    mock, _c, chunks = stream(SCENARIOS[name])
    assert len(terminals(chunks)) == 1 and chunks[-1].kind in TERMINALS
    if chunks[-1].kind == "done":
        assert chunks[-1].text == "a"                         # a recovered answer never contains failed partial text
    assert mock.call_count <= ai_engine.MAX_ATTEMPTS + 1


# ============================ helpers: controllable clock ====================
class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _patch_governor_clock(monkeypatch, clock, first_token=75.0, stream_deadline=240.0):
    original = ai_engine._engine2_start

    def patched(**kw):
        st = original(**kw)
        g = st.governor
        g._clock, g._t0 = clock, clock()
        g._first_token_deadline, g._stream_deadline = first_token, stream_deadline
        return st

    monkeypatch.setattr(ai_engine, "_engine2_start", patched)


class _ClockedProvider:
    """Scripted provider whose attempts advance a fake clock (to model slowness)."""
    name = "clocked"

    def __init__(self, clock, script):
        self.clock, self.script, self.n = clock, script, 0
        self.on_call = lambda *_: None

    def stream_chat(self, request):
        from backend.providers import ProviderError
        step = self.script[min(self.n, len(self.script) - 1)]
        self.n += 1
        self.on_call(request)

        def gen():
            action, seconds = step
            if action == "fail503":
                self.clock.t += seconds
                raise ProviderError(K.UNAVAILABLE, http_status=503)
            if action == "text_then_stall":
                yield ProviderEvent(text="first ")
                self.clock.t += seconds
                yield ProviderEvent(text="second ")
                yield ProviderEvent(finish_reason=FinishReason.STOP)
        return gen()
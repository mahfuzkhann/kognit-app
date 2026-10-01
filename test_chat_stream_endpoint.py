"""
PHASE 8E — tests for /api/chat/stream and stream_ai_response.

Covers the failure modes that actually matter for streaming: auth before any
bytes ship, partial-answer salvage after mid-stream failure, blocked/empty
responses, research normalization through the existing Phase 7C models, and
the guarantee that exactly one terminal event is always emitted.

No network: the Gemini SDK client is monkeypatched throughout.
"""
import json
import pytest
from fastapi.testclient import TestClient
from google.genai import types
from google.genai import errors as genai_errors

import backend.ai_engine as ai_engine
from backend.providers import get_provider
from backend.providers.gemini import _seconds_to_ms
import backend.main as main


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeChunk:
    def __init__(self, text=None, grounding=None, usage=None, finish_reason=None):
        self.text = text
        self.usage_metadata = usage
        if grounding is not None or finish_reason is not None:
            cand = type(
                "C", (),
                {"grounding_metadata": grounding, "finish_reason": finish_reason,
                 "finish_message": None},
            )()
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


def _install_fake_stream(monkeypatch, chunks, raise_after=None, exc=None):
    class _Chats:
        def create(self, **kwargs):
            return _FakeChatSession(chunks, raise_after=raise_after, exc=exc)

    class _Client:
        chats = _Chats()

    monkeypatch.setattr(get_provider(), "client", _Client())


def _server_error(code=503, message="high demand"):
    # Real genai_errors.ServerError, built the way the SDK itself does -
    # not a generic RuntimeError - so `type(e).__name__`/`e.code` in
    # stream_ai_response's telemetry and recovery classification are
    # exercised against the actual exception class, not a stand-in.
    return genai_errors.ServerError(
        code, {"error": {"code": code, "message": message, "status": "UNAVAILABLE"}}, None
    )


def _client_error(code=429, message="quota exceeded", status="RESOURCE_EXHAUSTED"):
    return genai_errors.ClientError(
        code, {"error": {"code": code, "message": message, "status": status}}, None
    )


def _install_fake_stream_sequence(monkeypatch, sessions):
    """
    BUG 3 PHASE 2 - like _install_fake_stream, but each successive call to
    chats.create() returns the NEXT (chunks, raise_after, exc) tuple in
    `sessions`. Needed because a recovery generation is a SECOND, SEPARATE
    create() call that must behave differently from the first (e.g. attempt
    1 streams partial text then 503s, attempt 2/recovery streams a clean
    complete answer). The last entry repeats if create() is called more
    times than len(sessions). Returns a dict with the live call count so
    tests can assert exactly how many generations were attempted.
    """
    calls = {"n": 0}

    class _Chats:
        def create(self, **kwargs):
            i = min(calls["n"], len(sessions) - 1)
            calls["n"] += 1
            chunks, raise_after, exc = sessions[i]
            return _FakeChatSession(chunks, raise_after=raise_after, exc=exc)

    class _Client:
        chats = _Chats()

    monkeypatch.setattr(get_provider(), "client", _Client())
    return calls


# ---------------------------------------------------------------------------
# stream_ai_response (adapter level)
# ---------------------------------------------------------------------------

class TestStreamAdapter:

    def test_yields_deltas_then_single_done(self, monkeypatch):
        _install_fake_stream(monkeypatch, [_FakeChunk("Hello "), _FakeChunk("world")])
        out = list(ai_engine.stream_ai_response(prompt="hi"))
        kinds = [c.kind for c in out]
        assert kinds == ["text", "text", "done"]
        assert out[-1].text == "Hello world", "done must carry the FULL answer"

    def test_exactly_one_terminal_event(self, monkeypatch):
        _install_fake_stream(monkeypatch, [_FakeChunk("a")])
        out = list(ai_engine.stream_ai_response(prompt="hi"))
        terminal = [c for c in out if c.kind in ("done", "error")]
        assert len(terminal) == 1, "a consumer must never be left waiting"

    def test_empty_stream_is_treated_as_blocked_not_success(self, monkeypatch):
        _install_fake_stream(monkeypatch, [_FakeChunk(None), _FakeChunk("")])
        out = list(ai_engine.stream_ai_response(prompt="hi"))
        assert out[-1].kind == "error"
        assert out[-1].text == ai_engine.BLOCKED_RESPONSE_ERROR

    def test_metadata_only_chunks_do_not_end_the_stream(self, monkeypatch):
        # A chunk with no text is legitimate (tool-use / metadata) and must
        # not be mistaken for the end of the answer.
        _install_fake_stream(monkeypatch, [_FakeChunk(None), _FakeChunk("real text")])
        out = list(ai_engine.stream_ai_response(prompt="hi"))
        assert out[-1].kind == "done"
        assert out[-1].text == "real text"

    def test_failure_AFTER_first_output_finalizes_as_interrupted_not_done(self, monkeypatch):
        # BUG 3 FIX: never discard text the student can already see, and
        # never retry once bytes have shipped - but a mid-stream exception
        # is NOT a successful completion, so it must be "interrupted", never
        # "done". (Before the Bug 3 fix this asserted kind == "done", which
        # was the exact defect: a genuine failure and a real success were
        # wire-identical.)
        _install_fake_stream(
            monkeypatch,
            [_FakeChunk("partial answer so far")],
            raise_after=1,
        )
        out = list(ai_engine.stream_ai_response(prompt="hi"))
        assert out[-1].kind == "interrupted", "a mid-stream exception must never be reported as done"
        assert out[-1].text == "partial answer so far", "the genuinely-produced text must still be preserved"

    def test_exactly_one_terminal_event_when_interrupted(self, monkeypatch):
        _install_fake_stream(monkeypatch, [_FakeChunk("partial")], raise_after=1)
        out = list(ai_engine.stream_ai_response(prompt="hi"))
        terminal = [c for c in out if c.kind in ("done", "interrupted", "error")]
        assert len(terminal) == 1, "a consumer must never be left waiting, even when interrupted"

    def test_failure_BEFORE_first_output_retries_then_errors(self, monkeypatch):
        attempts = {"n": 0}

        class _Chats:
            def create(self, **kwargs):
                attempts["n"] += 1
                return _FakeChatSession([], raise_after=0, exc=RuntimeError("boom"))

        monkeypatch.setattr(get_provider(), "client", type("C", (), {"chats": _Chats()})())
        out = list(ai_engine.stream_ai_response(prompt="hi"))
        assert out[-1].kind == "error"
        assert attempts["n"] > 1, "retry is safe before any byte is sent"

    def test_image_decode_failure_yields_error_not_exception(self, monkeypatch):
        out = list(ai_engine.stream_ai_response(prompt="hi", image_bytes=b"not-an-image"))
        assert len(out) == 1 and out[0].kind == "error"
        assert out[0].text == ai_engine.IMAGE_DECODE_ERROR

    def test_grounding_captured_and_normalized_when_research_enabled(self, monkeypatch):
        # PHASE 10: the adapter normalizes the SDK's GroundingMetadata, so the
        # stream carries a provider-neutral GroundingResult, never the SDK object.
        raw = types.GroundingMetadata(web_search_queries=["q1"], grounding_chunks=[], grounding_supports=[])
        _install_fake_stream(
            monkeypatch,
            [_FakeChunk("answer"), _FakeChunk(" more", grounding=raw)],
        )
        out = list(ai_engine.stream_ai_response(prompt="hi", enable_research=True))
        assert not isinstance(out[-1].grounding, types.GroundingMetadata)
        assert out[-1].grounding.search_queries == ("q1",)

    def test_no_grounding_captured_when_research_disabled(self, monkeypatch):
        raw = types.GroundingMetadata(web_search_queries=["q1"], grounding_chunks=[], grounding_supports=[])
        _install_fake_stream(monkeypatch, [_FakeChunk("answer", grounding=raw)])
        out = list(ai_engine.stream_ai_response(prompt="hi", enable_research=False))
        assert out[-1].grounding is None


class TestStreamCompletionIntegrity:
    """
    BUG 3 FIX - the explicit completion rule. A stream with non-empty text
    and NO exception must still be classified as interrupted, not done,
    whenever the last observed finish_reason is not STOP. These enum
    members were read directly off the installed google-genai==2.20.0
    package (see FinishReason in google.genai.types) - not guessed.
    """

    def test_clean_stop_finish_reason_is_success(self, monkeypatch):
        _install_fake_stream(
            monkeypatch,
            [_FakeChunk("A complete answer.", finish_reason=types.FinishReason.STOP)],
        )
        out = list(ai_engine.stream_ai_response(prompt="hi"))
        assert out[-1].kind == "done"
        assert out[-1].text == "A complete answer."

    def test_max_tokens_finish_reason_is_interrupted_not_done(self, monkeypatch):
        # The core Bug 3 scenario: the SDK iterator exits with NO exception
        # at all, but the model's own output cap truncated the answer.
        # Before this fix, non-empty text always meant "done" - finish_reason
        # was never even read - so this exact case was silently invisible.
        _install_fake_stream(
            monkeypatch,
            [_FakeChunk("An answer that got cut off ha", finish_reason=types.FinishReason.MAX_TOKENS)],
        )
        out = list(ai_engine.stream_ai_response(prompt="hi"))
        assert out[-1].kind == "interrupted", "MAX_TOKENS must never be reported as a clean done"
        assert out[-1].text == "An answer that got cut off ha", "the partial text must still be preserved"

    def test_safety_finish_reason_is_interrupted_not_done(self, monkeypatch):
        _install_fake_stream(
            monkeypatch,
            [_FakeChunk("Some text before a safety stop", finish_reason=types.FinishReason.SAFETY)],
        )
        out = list(ai_engine.stream_ai_response(prompt="hi"))
        assert out[-1].kind == "interrupted"

    def test_recitation_finish_reason_is_interrupted_not_done(self, monkeypatch):
        _install_fake_stream(
            monkeypatch,
            [_FakeChunk("Some quoted text", finish_reason=types.FinishReason.RECITATION)],
        )
        out = list(ai_engine.stream_ai_response(prompt="hi"))
        assert out[-1].kind == "interrupted"

    def test_unrecognized_other_finish_reason_is_interrupted_not_done(self, monkeypatch):
        _install_fake_stream(
            monkeypatch,
            [_FakeChunk("Some text", finish_reason=types.FinishReason.OTHER)],
        )
        out = list(ai_engine.stream_ai_response(prompt="hi"))
        assert out[-1].kind == "interrupted", "any non-STOP reason must default to interrupted, not done"

    def test_finish_reason_never_observed_on_any_chunk_defaults_to_success(self, monkeypatch):
        # Deliberate, documented conservative default (see
        # _is_successful_finish()'s docstring in ai_engine.py): when the
        # iterator completes cleanly and NO chunk ever carried a
        # finish_reason at all, this is treated as success rather than
        # interrupted, to avoid mislabeling ordinary answers as broken in
        # an SDK/model combination this sandbox could not verify against
        # live Gemini. REQUIRES LIVE GEMINI VALIDATION - see the Bug 3
        # report.
        _install_fake_stream(monkeypatch, [_FakeChunk("A normal answer with no finish_reason at all")])
        out = list(ai_engine.stream_ai_response(prompt="hi"))
        assert out[-1].kind == "done"

    def test_intermediate_none_finish_reason_does_not_erase_a_later_real_value(self, monkeypatch):
        # Matches the installed SDK's own internal pattern (google.genai
        # chats.py keeps the most recent non-None finish_reason across
        # chunks) - an early chunk with no finish_reason must not hide a
        # later, real one.
        _install_fake_stream(
            monkeypatch,
            [
                _FakeChunk("first "),
                _FakeChunk("part", finish_reason=None),
                _FakeChunk("", finish_reason=types.FinishReason.MAX_TOKENS),
            ],
        )
        out = list(ai_engine.stream_ai_response(prompt="hi"))
        assert out[-1].kind == "interrupted"


# ---------------------------------------------------------------------------
# BUG 3 PHASE 2 - bounded recovery after a confirmed transient failure
# ---------------------------------------------------------------------------

class TestStreamRecovery:
    """
    Scoped deliberately narrow: recovery triggers ONLY for a real
    genai_errors.ServerError occurring AFTER partial output, matched to the
    real production telemetry (HTTP 200, N chunks, then 503 UNAVAILABLE)
    that motivated this phase. Every other after-first-byte failure
    (ClientError incl. 429, a non-STOP finish_reason, a bare exception)
    still finalizes as "interrupted" immediately - see
    TestStreamCompletionIntegrity above, all still passing unchanged.
    """

    def test_normal_success_never_touches_create_more_than_once(self, monkeypatch):
        calls = _install_fake_stream_sequence(
            monkeypatch, [([_FakeChunk("A complete answer.", finish_reason=types.FinishReason.STOP)], None, None)]
        )
        out = list(ai_engine.stream_ai_response(prompt="hi"))
        assert [c.kind for c in out] == ["text", "done"]
        assert calls["n"] == 1, "a normal successful stream must never call chats.create() a second time"

    def test_503_after_partial_output_triggers_one_retry_event(self, monkeypatch):
        calls = _install_fake_stream_sequence(
            monkeypatch,
            [
                ([_FakeChunk("Newton's first law states")], 1, _server_error()),
                ([_FakeChunk("that an object remains at rest.", finish_reason=types.FinishReason.STOP)], None, None),
            ],
        )
        out = list(ai_engine.stream_ai_response(prompt="hi"))
        kinds = [c.kind for c in out]
        assert "retry" in kinds, "a confirmed transient failure after output must trigger a retry event"
        assert kinds[-1] == "done"
        assert calls["n"] == 2, "exactly one recovery generation (2 total create() calls)"

    def test_recovery_success_final_answer_comes_only_from_attempt_2(self, monkeypatch):
        # CRITICAL QUALITY REQUIREMENT from the spec: no concatenation. The
        # final "done" text must be attempt 2's answer alone - attempt 1's
        # discarded partial must not appear anywhere in it.
        _install_fake_stream_sequence(
            monkeypatch,
            [
                ([_FakeChunk("Newton's first law states")], 1, _server_error()),
                ([_FakeChunk("that an object remains at rest.", finish_reason=types.FinishReason.STOP)], None, None),
            ],
        )
        out = list(ai_engine.stream_ai_response(prompt="hi"))
        final = out[-1]
        assert final.kind == "done"
        assert final.text == "that an object remains at rest."
        assert "Newton's first law states" not in final.text, "attempt 1's discarded text must never be concatenated"

    def test_retry_event_cleanly_separates_discarded_text_from_the_fresh_answer(self, monkeypatch):
        # IMPORTANT: true native streaming cannot retroactively un-send bytes
        # that already went out in real time - attempt 1's delta(s) WILL be
        # yielded before Kognit even knows a 503 is coming. That is exactly
        # why "retry" exists: it is the explicit signal telling the consumer
        # to discard everything received so far (see main.py's event
        # contract and stream-render.js's applyStreamEvent). This test
        # verifies the boundary is clean - attempt 1's text appears only
        # BEFORE "retry", attempt 2's only AFTER - not that attempt 1's text
        # is magically never sent (see the spec's own "the browser may
        # already have rendered partial content" acknowledgment). The
        # guarantee that actually matters - the FINAL answer never contains
        # attempt 1's text - is covered by
        # test_recovery_success_final_answer_comes_only_from_attempt_2 above.
        _install_fake_stream_sequence(
            monkeypatch,
            [
                ([_FakeChunk("DISCARDED")], 1, _server_error()),
                ([_FakeChunk("fresh answer", finish_reason=types.FinishReason.STOP)], None, None),
            ],
        )
        out = list(ai_engine.stream_ai_response(prompt="hi"))
        retry_index = next(i for i, c in enumerate(out) if c.kind == "retry")
        before = [c.text for c in out[:retry_index] if c.kind == "text"]
        after = [c.text for c in out[retry_index + 1:] if c.kind == "text"]
        assert before == ["DISCARDED"]
        assert after == ["fresh answer"]

    def test_recovery_uses_same_model_and_request(self, monkeypatch):
        # STEP 5 of the spec: same model, same request, no fallback.
        seen_models = []
        seen_history = []

        class _Chats:
            def create(self, **kwargs):
                seen_models.append(kwargs.get("model"))
                seen_history.append(kwargs.get("history"))
                sessions = [
                    ([_FakeChunk("partial")], 0, _server_error()),
                    ([_FakeChunk("full", finish_reason=types.FinishReason.STOP)], None, None),
                ]
                i = min(len(seen_models) - 1, len(sessions) - 1)
                chunks, raise_after, exc = sessions[i]
                return _FakeChatSession(chunks, raise_after=raise_after, exc=exc)

        class _Client:
            chats = _Chats()

        monkeypatch.setattr(get_provider(), "client", _Client())
        list(ai_engine.stream_ai_response(prompt="hi", history=[{"role": "user", "parts": [{"text": "earlier"}]}]))
        assert len(seen_models) == 2
        assert seen_models[0] == seen_models[1] == ai_engine.MODEL_NAME, "recovery must use the SAME model"
        assert seen_history[0] == seen_history[1], "recovery must use the SAME conversation context"

    def test_recovery_attempt_gets_full_timeout_not_short_retry_timeout(self, monkeypatch):
        # A recovery is a complete fresh generation, not a quick incremental
        # retry - it must get the full AI_REQUEST_TIMEOUT_SECONDS budget.
        seen_timeouts = []

        class _Chats:
            def create(self, **kwargs):
                seen_timeouts.append(kwargs["config"].http_options.timeout)
                sessions = [
                    ([_FakeChunk("partial")], 0, _server_error()),
                    ([_FakeChunk("full", finish_reason=types.FinishReason.STOP)], None, None),
                ]
                i = min(len(seen_timeouts) - 1, len(sessions) - 1)
                chunks, raise_after, exc = sessions[i]
                return _FakeChatSession(chunks, raise_after=raise_after, exc=exc)

        class _Client:
            chats = _Chats()

        monkeypatch.setattr(get_provider(), "client", _Client())
        list(ai_engine.stream_ai_response(prompt="hi"))
        expected_full_ms = _seconds_to_ms(ai_engine.AI_REQUEST_TIMEOUT_SECONDS)
        assert seen_timeouts[0] == expected_full_ms
        assert seen_timeouts[1] == expected_full_ms, "the recovery attempt must get the FULL timeout, not the short retry timeout"

    def test_recovery_also_fails_yields_interrupted_with_only_its_own_text(self, monkeypatch):
        # spec test 7: attempt 2 also fails -> interrupted final state, and
        # bounded means bounded - no third generation.
        calls = _install_fake_stream_sequence(
            monkeypatch,
            [
                ([_FakeChunk("attempt one partial")], 1, _server_error()),
                ([_FakeChunk("attempt two partial")], 1, _server_error()),
            ],
        )
        out = list(ai_engine.stream_ai_response(prompt="hi"))
        assert out[-1].kind == "interrupted"
        assert out[-1].text == "attempt two partial"
        assert "attempt one partial" not in out[-1].text
        assert calls["n"] == 2, "no infinite retry - bounded to exactly one recovery generation"

    def test_no_infinite_retry_even_if_every_attempt_fails_after_output(self, monkeypatch):
        # spec test 8/9: retry count exactly matches policy, never unbounded.
        calls = _install_fake_stream_sequence(
            monkeypatch, [([_FakeChunk("x")], 1, _server_error())]  # same failure every time
        )
        out = list(ai_engine.stream_ai_response(prompt="hi"))
        terminal = [c for c in out if c.kind in ("done", "interrupted", "error")]
        assert len(terminal) == 1
        assert calls["n"] == 2, "exactly original + one recovery, never more"

    def test_normal_successful_request_does_not_trigger_recovery(self, monkeypatch):
        # spec test 10.
        calls = _install_fake_stream_sequence(
            monkeypatch, [([_FakeChunk("all good", finish_reason=types.FinishReason.STOP)], None, None)]
        )
        out = list(ai_engine.stream_ai_response(prompt="hi"))
        assert "retry" not in [c.kind for c in out]
        assert calls["n"] == 1

    def test_429_before_output_returns_immediately_no_recovery(self, monkeypatch):
        calls = _install_fake_stream_sequence(monkeypatch, [([], 0, _client_error(429))])
        out = list(ai_engine.stream_ai_response(prompt="hi"))
        assert out[-1].kind == "error"
        assert out[-1].text == ai_engine.QUOTA_EXHAUSTED_ERROR
        assert calls["n"] == 1, "429 must return immediately, never loop or recover"

    def test_429_after_partial_output_is_interrupted_not_recovered(self, monkeypatch):
        # A 429 is a ClientError, not a ServerError - even if it somehow
        # occurs after partial output (e.g. token-based quota exhausted
        # mid-generation), it must NOT trigger the new recovery mechanism:
        # retrying an exhausted quota cannot succeed and only burns latency.
        calls = _install_fake_stream_sequence(
            monkeypatch, [([_FakeChunk("partial")], 1, _client_error(429))]
        )
        out = list(ai_engine.stream_ai_response(prompt="hi"))
        assert out[-1].kind == "interrupted"
        assert "retry" not in [c.kind for c in out]
        assert calls["n"] == 1, "429 after output must never trigger a recovery generation"

    def test_max_tokens_does_not_trigger_recovery(self, monkeypatch):
        calls = _install_fake_stream_sequence(
            monkeypatch, [([_FakeChunk("truncated", finish_reason=types.FinishReason.MAX_TOKENS)], None, None)]
        )
        out = list(ai_engine.stream_ai_response(prompt="hi"))
        assert out[-1].kind == "interrupted"
        assert "retry" not in [c.kind for c in out]
        assert calls["n"] == 1, "MAX_TOKENS is a model-side outcome, not a confirmed transient provider failure - no recovery"

    def test_safety_does_not_trigger_recovery(self, monkeypatch):
        calls = _install_fake_stream_sequence(
            monkeypatch, [([_FakeChunk("blocked text", finish_reason=types.FinishReason.SAFETY)], None, None)]
        )
        out = list(ai_engine.stream_ai_response(prompt="hi"))
        assert out[-1].kind == "interrupted"
        assert calls["n"] == 1, "SAFETY must never be retried - an identical request would be blocked again"

    def test_recitation_does_not_trigger_recovery(self, monkeypatch):
        calls = _install_fake_stream_sequence(
            monkeypatch, [([_FakeChunk("quoted text", finish_reason=types.FinishReason.RECITATION)], None, None)]
        )
        out = list(ai_engine.stream_ai_response(prompt="hi"))
        assert out[-1].kind == "interrupted"
        assert calls["n"] == 1

    def test_recovery_telemetry_marks_recovery_used_on_final_outcome(self, monkeypatch, caplog):
        import logging
        caplog.set_level(logging.INFO, logger="kognit.ai_engine")
        _install_fake_stream_sequence(
            monkeypatch,
            [
                ([_FakeChunk("partial")], 1, _server_error()),
                ([_FakeChunk("full", finish_reason=types.FinishReason.STOP)], None, None),
            ],
        )
        list(ai_engine.stream_ai_response(prompt="hi"))
        telemetry_lines = [r.message for r in caplog.records if "stream_telemetry" in r.message]
        assert any("outcome=recovery_triggered" in l for l in telemetry_lines)
        assert any("outcome=success" in l and "recovery_used=True" in l for l in telemetry_lines)


# ---------------------------------------------------------------------------
# Shared generation core
# ---------------------------------------------------------------------------

class TestSharedCore:

    def test_streaming_and_non_streaming_build_identical_requests(self):
        kwargs = dict(
            prompt="Explain Newton's second law",
            mode="socratic",
            board="NCTB",
            user_class="Class 9-10 (SSC)",
            stream="Science",
            image_bytes=None,
            pdf_context="some chapter text",
            history=[{"role": "user", "text": "hi"}, {"role": "bot", "text": "hello"}],
        )
        a = ai_engine._build_chat_request(**kwargs)
        b = ai_engine._build_chat_request(**kwargs)
        assert a.system_instruction == b.system_instruction
        assert a.user_parts == b.user_parts
        assert len(a.history) == len(b.history)
        # Socratic instruction must be present in the shared core, not bolted
        # on by one adapter only.
        assert "DO NOT give direct answers immediately" in a.system_instruction

    def test_pdf_context_is_truncated_in_the_shared_core(self):
        huge = "x" * (ai_engine.MAX_PDF_CONTEXT_CHARS + 5000)
        req = ai_engine._build_chat_request(
            prompt="q", mode="direct", board="NCTB", user_class=None, stream=None,
            image_bytes=None, pdf_context=huge, history=None,
        )
        assert len(req.system_instruction) < len(huge) + 5000

    def test_missing_profile_does_not_fabricate_a_class(self):
        req = ai_engine._build_chat_request(
            prompt="q", mode="direct", board="NCTB", user_class=None, stream=None,
            image_bytes=None, pdf_context="", history=None,
        )
        assert "secondary/higher-secondary level" in req.system_instruction


# ---------------------------------------------------------------------------
# /api/chat/stream (HTTP level)
# ---------------------------------------------------------------------------

def _parse_ndjson(body: str):
    return [json.loads(line) for line in body.strip().split("\n") if line.strip()]


@pytest.fixture
def client():
    return TestClient(main.app)


class TestStreamEndpointAuth:

    def test_requires_auth_before_streaming_starts(self, client):
        r = client.post("/api/chat/stream", data={"prompt": "hi", "chat_id": "c1"})
        assert r.status_code == 401, "must be a real HTTP status, not a 200 with an error in the body"


class TestStreamEndpoint:

    @pytest.fixture(autouse=True)
    def _auth(self, monkeypatch):
        async def _fake_user():
            return ("user-1", "token-1")
        main.app.dependency_overrides[main._rate_limited_chat] = _fake_user

        async def _ctx(**kwargs):
            return ("Class 9-10 (SSC)", "Science")
        monkeypatch.setattr(main, "_get_academic_context", _ctx)
        yield
        main.app.dependency_overrides.clear()

    def test_happy_path_emits_start_deltas_and_done(self, client, monkeypatch):
        _install_fake_stream(monkeypatch, [_FakeChunk("Force "), _FakeChunk("equals ma")])
        r = client.post("/api/chat/stream", data={"prompt": "f=ma?", "chat_id": "c1"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/x-ndjson")
        events = _parse_ndjson(r.text)
        assert events[0]["type"] == "start"
        deltas = [e["text"] for e in events if e["type"] == "delta"]
        assert "".join(deltas) == "Force equals ma"
        assert events[-1]["type"] == "done"
        assert events[-1]["reply"] == "Force equals ma"

    def test_final_reply_matches_concatenated_deltas(self, client, monkeypatch):
        parts = ["A", "B", "C", "D"]
        _install_fake_stream(monkeypatch, [_FakeChunk(p) for p in parts])
        r = client.post("/api/chat/stream", data={"prompt": "q", "chat_id": "c1"})
        events = _parse_ndjson(r.text)
        deltas = "".join(e["text"] for e in events if e["type"] == "delta")
        assert deltas == events[-1]["reply"] == "ABCD"

    def test_exactly_one_terminal_event_over_http(self, client, monkeypatch):
        _install_fake_stream(monkeypatch, [_FakeChunk("x")])
        r = client.post("/api/chat/stream", data={"prompt": "q", "chat_id": "c1"})
        events = _parse_ndjson(r.text)
        assert len([e for e in events if e["type"] in ("done", "error")]) == 1

    def test_bengali_and_latex_survive_the_transport(self, client, monkeypatch):
        # NDJSON must not corrupt Bangla text or LaTeX. Newlines inside the
        # answer must not be mistaken for event delimiters.
        payload = "বলের সূত্র:\n$$F = ma$$\nএখানে $m$ হলো ভর।"
        _install_fake_stream(monkeypatch, [_FakeChunk(payload)])
        r = client.post("/api/chat/stream", data={"prompt": "q", "chat_id": "c1"})
        events = _parse_ndjson(r.text)
        assert events[-1]["reply"] == payload

    def test_chunk_split_mid_latex_is_reassembled_exactly(self, client, monkeypatch):
        _install_fake_stream(monkeypatch, [_FakeChunk("$$F = "), _FakeChunk("ma$$")])
        r = client.post("/api/chat/stream", data={"prompt": "q", "chat_id": "c1"})
        events = _parse_ndjson(r.text)
        assert events[-1]["reply"] == "$$F = ma$$"

    def test_error_stream_returns_error_event(self, client, monkeypatch):
        _install_fake_stream(monkeypatch, [])
        r = client.post("/api/chat/stream", data={"prompt": "q", "chat_id": "c1"})
        events = _parse_ndjson(r.text)
        assert events[-1]["type"] == "error"
        assert isinstance(events[-1]["reply"], str) and events[-1]["reply"]

    def test_interrupted_stream_emits_interrupted_event_not_done(self, client, monkeypatch):
        # BUG 3 FIX, end-to-end through the real HTTP endpoint: a mid-stream
        # exception after partial output must reach the client as
        # "interrupted", never "done".
        _install_fake_stream(monkeypatch, [_FakeChunk("partial answer")], raise_after=1)
        r = client.post("/api/chat/stream", data={"prompt": "q", "chat_id": "c1"})
        events = _parse_ndjson(r.text)
        assert events[-1]["type"] == "interrupted"
        assert events[-1]["reply"] == "partial answer"
        assert not any(e["type"] == "done" for e in events), "must never also emit a done event"

    def test_max_tokens_over_http_emits_interrupted_not_done(self, client, monkeypatch):
        _install_fake_stream(
            monkeypatch,
            [_FakeChunk("truncated answer", finish_reason=types.FinishReason.MAX_TOKENS)],
        )
        r = client.post("/api/chat/stream", data={"prompt": "q", "chat_id": "c1"})
        events = _parse_ndjson(r.text)
        assert events[-1]["type"] == "interrupted"
        assert events[-1]["reply"] == "truncated answer"

    def test_exactly_one_terminal_event_when_interrupted_over_http(self, client, monkeypatch):
        _install_fake_stream(monkeypatch, [_FakeChunk("x")], raise_after=1)
        r = client.post("/api/chat/stream", data={"prompt": "q", "chat_id": "c1"})
        events = _parse_ndjson(r.text)
        terminal = [e for e in events if e["type"] in ("done", "interrupted", "error")]
        assert len(terminal) == 1

    # --- BUG 3 PHASE 2: bounded recovery, end-to-end over the real HTTP
    # endpoint (spec tests 4-10 at the transport layer) ---

    def test_503_recovery_emits_retry_then_done_over_http(self, client, monkeypatch):
        _install_fake_stream_sequence(
            monkeypatch,
            [
                ([_FakeChunk("DISCARDED")], 1, _server_error()),
                ([_FakeChunk("the real answer", finish_reason=types.FinishReason.STOP)], None, None),
            ],
        )
        r = client.post("/api/chat/stream", data={"prompt": "q", "chat_id": "c1"})
        assert r.status_code == 200
        events = _parse_ndjson(r.text)
        types_seen = [e["type"] for e in events]
        assert "retry" in types_seen
        assert events[-1]["type"] == "done"
        assert events[-1]["reply"] == "the real answer"
        assert "DISCARDED" not in events[-1]["reply"], "no concatenation across the retry boundary"

    def test_retry_event_has_no_extraneous_fields(self, client, monkeypatch):
        # Keep the wire contract minimal - "retry" carries no reply/text, so
        # a naive frontend that doesn't special-case it can't accidentally
        # render provider internals.
        _install_fake_stream_sequence(
            monkeypatch,
            [
                ([_FakeChunk("x")], 1, _server_error()),
                ([_FakeChunk("y", finish_reason=types.FinishReason.STOP)], None, None),
            ],
        )
        r = client.post("/api/chat/stream", data={"prompt": "q", "chat_id": "c1"})
        events = _parse_ndjson(r.text)
        retry_events = [e for e in events if e["type"] == "retry"]
        assert len(retry_events) == 1
        assert retry_events[0] == {"type": "retry"}

    def test_retry_is_never_the_final_event(self, client, monkeypatch):
        # "retry" must never be mistaken for a terminal - a terminal always
        # follows it in the same response.
        _install_fake_stream_sequence(
            monkeypatch,
            [
                ([_FakeChunk("x")], 1, _server_error()),
                ([_FakeChunk("y", finish_reason=types.FinishReason.STOP)], None, None),
            ],
        )
        r = client.post("/api/chat/stream", data={"prompt": "q", "chat_id": "c1"})
        events = _parse_ndjson(r.text)
        assert events[-1]["type"] != "retry"
        assert events[-1]["type"] in ("done", "interrupted", "error")

    def test_recovery_failure_over_http_is_interrupted_never_done(self, client, monkeypatch):
        _install_fake_stream_sequence(
            monkeypatch,
            [
                ([_FakeChunk("first partial")], 1, _server_error()),
                ([_FakeChunk("second partial")], 1, _server_error()),
            ],
        )
        r = client.post("/api/chat/stream", data={"prompt": "q", "chat_id": "c1"})
        events = _parse_ndjson(r.text)
        assert events[-1]["type"] == "interrupted"
        assert events[-1]["reply"] == "second partial"
        assert not any(e["type"] == "done" for e in events)

    def test_429_over_http_never_emits_retry(self, client, monkeypatch):
        _install_fake_stream_sequence(monkeypatch, [([], 0, _client_error(429))])
        r = client.post("/api/chat/stream", data={"prompt": "q", "chat_id": "c1"})
        events = _parse_ndjson(r.text)
        assert not any(e["type"] == "retry" for e in events)
        assert events[-1]["type"] == "error"

    def test_oversized_image_rejected_with_413(self, client, monkeypatch):
        big = b"0" * (main.MAX_IMAGE_UPLOAD_SIZE_BYTES + 1024)
        r = client.post(
            "/api/chat/stream",
            data={"prompt": "q", "chat_id": "c1"},
            files={"image": ("big.png", big, "image/png")},
        )
        assert r.status_code == 413


class TestStreamLoadingContext:
    """Loading copy must describe what is actually happening - never claim to
    read a document when no PDF is attached."""

    @pytest.fixture(autouse=True)
    def _auth(self, monkeypatch):
        async def _fake_user():
            return ("user-1", "token-1")
        main.app.dependency_overrides[main._rate_limited_chat] = _fake_user

        async def _ctx(**kwargs):
            return ("Class 9-10 (SSC)", "Science")
        monkeypatch.setattr(main, "_get_academic_context", _ctx)
        monkeypatch.setattr(main, "active_pdf_contexts", {})
        yield
        main.app.dependency_overrides.clear()

    def _context_for(self, client, monkeypatch, research, pdf):
        _install_fake_stream(monkeypatch, [_FakeChunk("answer")])
        monkeypatch.setattr(
            main, "decide_research",
            lambda p: type("D", (), {"research_requested": research, "reason": "r", "category": "c"})()
        )
        if pdf:
            monkeypatch.setattr(main, "active_pdf_contexts", {"user-1": {"c1": "chapter text"}})
        else:
            monkeypatch.setattr(main, "active_pdf_contexts", {})
        r = client.post("/api/chat/stream", data={"prompt": "q", "chat_id": "c1"})
        return _parse_ndjson(r.text)[0]["context"]

    def test_plain_question_says_thinking(self, client, monkeypatch):
        assert self._context_for(client, monkeypatch, research=False, pdf=False) == "thinking"

    def test_research_question_says_research(self, client, monkeypatch):
        assert self._context_for(client, monkeypatch, research=True, pdf=False) == "research"

    def test_pdf_question_says_document(self, client, monkeypatch):
        assert self._context_for(client, monkeypatch, research=False, pdf=True) == "document"

    def test_pdf_plus_research_says_both(self, client, monkeypatch):
        assert self._context_for(client, monkeypatch, research=True, pdf=True) == "document_web"

    def test_never_claims_document_without_pdf(self, client, monkeypatch):
        ctx = self._context_for(client, monkeypatch, research=False, pdf=False)
        assert "document" not in ctx


class TestStreamResearchPayload:

    @pytest.fixture(autouse=True)
    def _auth(self, monkeypatch):
        async def _fake_user():
            return ("user-1", "token-1")
        main.app.dependency_overrides[main._rate_limited_chat] = _fake_user

        async def _ctx(**kwargs):
            return (None, None)
        monkeypatch.setattr(main, "_get_academic_context", _ctx)
        yield
        main.app.dependency_overrides.clear()

    def test_no_research_key_is_null_for_plain_answer(self, client, monkeypatch):
        _install_fake_stream(monkeypatch, [_FakeChunk("answer")])
        monkeypatch.setattr(
            main, "decide_research",
            lambda p: type("D", (), {"research_requested": False, "reason": "r", "category": "c"})()
        )
        r = client.post("/api/chat/stream", data={"prompt": "q", "chat_id": "c1"})
        assert _parse_ndjson(r.text)[-1]["research"] is None

    def test_research_requested_but_no_grounding_does_not_fabricate_sources(self, client, monkeypatch):
        # Gemini may decline to search. That must normalize to "not used" with
        # no sources - never invented citations.
        _install_fake_stream(monkeypatch, [_FakeChunk("answer with no grounding")])
        monkeypatch.setattr(
            main, "decide_research",
            lambda p: type("D", (), {"research_requested": True, "reason": "r", "category": "c"})()
        )
        r = client.post("/api/chat/stream", data={"prompt": "q", "chat_id": "c1"})
        done = _parse_ndjson(r.text)[-1]
        assert done["type"] == "done"
        research = done["research"]
        if research is not None:
            assert research.get("grounding_status") != "used"
            assert not research.get("sources")

    def test_research_normalization_failure_does_not_break_the_answer(self, client, monkeypatch):
        _install_fake_stream(monkeypatch, [_FakeChunk("the answer")])
        monkeypatch.setattr(
            main, "decide_research",
            lambda p: type("D", (), {"research_requested": True, "reason": "r", "category": "c"})()
        )

        def _boom(*a, **k):
            raise RuntimeError("normalization exploded")
        monkeypatch.setattr(main, "normalize_grounding_metadata", _boom)

        r = client.post("/api/chat/stream", data={"prompt": "q", "chat_id": "c1"})
        done = _parse_ndjson(r.text)[-1]
        assert done["type"] == "done"
        assert done["reply"] == "the answer", "answer must survive a research failure"
        assert done["research"] is None


class TestNonStreamingEndpointUnaffected:
    """The existing endpoint is the fallback and must keep working."""

    def test_api_chat_still_registered(self):
        paths = [r.path for r in main.app.routes]
        assert "/api/chat" in paths
        assert "/api/chat/stream" in paths

    def test_api_chat_still_requires_auth(self, client):
        r = client.post("/api/chat", data={"prompt": "hi", "chat_id": "c1"})
        assert r.status_code == 401
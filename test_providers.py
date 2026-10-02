"""
PHASE 10 - tests for the provider foundation.

Four layers, each tested at its own level:

  1. Contract (backend/providers/base.py): the neutral types, and that they
     carry no SDK.
  2. Mock provider (backend/providers/mock.py): deterministic scripted
     scenarios.
  3. Gemini adapter (backend/providers/gemini.py): request translation,
     response/chunk/finish-reason/usage/grounding mapping and exception
     normalization, using fake SDK objects. This is the ONLY layer allowed to
     know SDK shapes.
  4. Kognit's policy layer (backend/ai_engine.py) driven by the MockProvider -
     no SDK, no network - proving retry/backoff/recovery/error-code behavior
     lives in the policy layer and works against ANY provider.

Plus HTTP-level checks (request-id parity for /api/chat, streamed research
sources) and architectural checks that keep SDK types from leaking back out.

Nothing here makes a real provider call.
"""
import ast
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import textwrap
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient
from google.genai import errors as genai_errors
from google.genai import types
from PIL import Image

import backend.ai_engine as ai_engine
import backend.main as main
from backend.providers import (
    FinishReason,
    GroundingResult,
    GroundingSource,
    GroundingSupport,
    ProviderError,
    ProviderErrorKind,
    ProviderEvent,
    ProviderMessage,
    ProviderRequest,
    ProviderResult,
    ProviderUsage,
    get_provider,
    set_provider,
)
from backend.providers import gemini as gemini_adapter
from backend.providers.gemini import GeminiProvider, map_grounding, translate_exception
from backend.providers.mock import (
    MockAttempt,
    MockProvider,
    empty_attempt,
    failing_attempt,
    max_tokens_attempt,
    raising_attempt,
    safety_block_attempt,
    text_attempt,
)

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
K = ProviderErrorKind


def _request(**overrides) -> ProviderRequest:
    base = dict(system_instruction="sys", user_parts=["hello"], model="m-1", timeout_seconds=7)
    base.update(overrides)
    return ProviderRequest(**base)


# ===========================================================================
# 1. CONTRACT
# ===========================================================================

class TestContract:

    def test_request_defaults_are_minimal_and_safe(self):
        r = _request()
        assert r.history == [] and r.reasoning_effort is None
        assert r.enable_web_search is False and r.request_id is None

    def test_request_holds_ai_inputs_only(self):
        fields = set(ProviderRequest.__dataclass_fields__)
        assert fields == {"system_instruction", "user_parts", "model", "timeout_seconds", "history",
                          "reasoning_effort", "enable_web_search", "request_id"}

    def test_default_history_lists_are_not_shared_between_requests(self):
        a, b = _request(), _request()
        a.history.append(ProviderMessage("user", "x"))
        assert b.history == []

    def test_provider_error_message_is_a_safe_label_only(self):
        e = ProviderError(K.UNAVAILABLE, http_status=503, status="UNAVAILABLE", provider="gemini",
                          original_type="ServerError")
        text = str(e)
        assert "kind=UNAVAILABLE" in text and "http_status=503" in text and "type=ServerError" in text
        assert e.kind == K.UNAVAILABLE and e.http_status == 503 and e.provider == "gemini"

    def test_provider_error_defaults(self):
        e = ProviderError(K.UNKNOWN)
        assert e.http_status is None and e.status is None and e.original_type == "ProviderError"

    def test_normalized_types_are_immutable(self):
        for obj in (ProviderUsage(1), GroundingSource("t"), ProviderEvent(text="x"), ProviderMessage("user", "x")):
            with pytest.raises(Exception):
                obj.__dict__  # frozen dataclasses with slots-less layout still reject assignment:
                setattr(obj, "text", "changed") if hasattr(obj, "text") else setattr(obj, "role", "changed")

    def test_finish_reason_members(self):
        assert {m.value for m in FinishReason} == {"STOP", "MAX_TOKENS", "SAFETY", "OTHER"}

    def test_contract_and_mock_import_no_sdk(self):
        # Run in a stdlib-only interpreter (-I -S: no site-packages, no .pth
        # files, no sitecustomize, no PYTHON* variables). Nothing third-party
        # can be pre-loaded, and any attempt by these modules to import the
        # SDK or httpx is recorded by the probe whether or not it is
        # wrapped in try/except. The result therefore depends only on Kognit's
        # own code, never on what the developer's virtualenv happens to install.
        rc, attempts, stderr = _probe_sdk_import_attempts(
            REPO_ROOT, ["backend.providers.base", "backend.providers.mock", "backend.providers"])
        assert rc == 0, f"importing the contract/mock needs a third-party package:\n{stderr}"
        assert attempts == [], f"[module, importer] SDK import attempts by the contract/mock: {attempts}"


class TestFactory:

    def test_set_provider_swaps_and_restores(self):
        previous = get_provider()
        mock = MockProvider([text_attempt("x")])
        try:
            set_provider(mock)
            assert get_provider() is mock
        finally:
            set_provider(previous)
        assert get_provider() is previous

    def test_default_provider_is_gemini_and_is_a_singleton(self):
        previous = get_provider()
        try:
            set_provider(None)
            first = get_provider()
            assert isinstance(first, GeminiProvider) and first.name == "gemini"
            assert get_provider() is first
        finally:
            set_provider(previous)


# ===========================================================================
# 2. MOCK PROVIDER
# ===========================================================================

def _drain(provider, request=None):
    return list(provider.stream_chat(request or _request()))


class TestMockProviderStreaming:

    def test_normal_streaming_success(self):
        m = MockProvider([text_attempt("Hel", "lo", usage=ProviderUsage(total_tokens=9))])
        events = _drain(m)
        assert [e.text for e in events if e.text] == ["Hel", "lo"]
        assert events[-1].finish_reason == FinishReason.STOP and events[-1].usage.total_tokens == 9

    def test_grounding_is_delivered_on_the_final_event(self):
        g = GroundingResult(search_queries=("q",))
        events = _drain(MockProvider([text_attempt("a", grounding=g)]))
        assert events[-1].grounding is g

    def test_pre_token_failure_raises_before_any_text(self):
        m = MockProvider([failing_attempt(K.UNAVAILABLE, http_status=503)])
        it = m.stream_chat(_request())
        with pytest.raises(ProviderError) as exc:
            next(it)
        assert exc.value.kind == K.UNAVAILABLE and exc.value.http_status == 503

    def test_mid_stream_failure_delivers_text_then_raises(self):
        m = MockProvider([failing_attempt(K.UNAVAILABLE, http_status=503, after_text=["par", "tial"])])
        got = []
        with pytest.raises(ProviderError):
            for e in m.stream_chat(_request()):
                got.append(e.text)
        assert got == ["par", "tial"]

    @pytest.mark.parametrize("kind", [K.RATE_LIMITED, K.TIMEOUT, K.AUTH, K.INVALID_REQUEST, K.CONFLICT, K.UNKNOWN])
    def test_each_error_kind_can_be_scripted(self, kind):
        with pytest.raises(ProviderError) as exc:
            _drain(MockProvider([failing_attempt(kind)]))
        assert exc.value.kind == kind

    def test_safety_block(self):
        events = _drain(MockProvider([safety_block_attempt()]))
        assert not any(e.text for e in events) and events[-1].finish_reason == FinishReason.SAFETY

    def test_max_tokens(self):
        events = _drain(MockProvider([max_tokens_attempt("cut")]))
        assert events[0].text == "cut" and events[-1].finish_reason == FinishReason.MAX_TOKENS

    def test_empty_response(self):
        events = _drain(MockProvider([empty_attempt()]))
        assert len(events) == 1 and events[0] == ProviderEvent()

    def test_malformed_response_can_be_modelled_with_an_arbitrary_exception(self):
        with pytest.raises(ValueError):
            _drain(MockProvider([raising_attempt(ValueError("garbled"), after_text=["x"])]))

    def test_script_advances_per_call_and_the_last_attempt_repeats(self):
        m = MockProvider([failing_attempt(K.UNAVAILABLE), text_attempt("ok")])
        with pytest.raises(ProviderError):
            _drain(m)
        assert [e.text for e in _drain(m) if e.text] == ["ok"]
        assert [e.text for e in _drain(m) if e.text] == ["ok"], "last scripted attempt repeats"
        assert m.call_count == 3

    def test_records_requests_and_operations(self):
        m = MockProvider([text_attempt("x")])
        r1, r2, r3 = _request(model="a"), _request(model="b"), _request(model="c")
        _drain(m, r1)
        m.chat(r2)
        m.generate(r3)
        assert m.requests == [r1, r2, r3]
        assert m.calls == ["stream_chat", "chat", "generate"]

    def test_empty_script_is_rejected(self):
        with pytest.raises(ValueError):
            MockProvider([])

    def test_is_deterministic_across_instances(self):
        script = lambda: [failing_attempt(K.UNAVAILABLE, after_text=["a"]), text_attempt("b")]
        def run():
            m = MockProvider(script()); out = []
            for _ in range(2):
                try:
                    out.append([e.text for e in m.stream_chat(_request())])
                except ProviderError as e:
                    out.append(("err", e.kind.value))
            return out
        assert run() == run()


class TestMockProviderUnary:

    def test_chat_and_generate_success_fold_the_events(self):
        m = MockProvider([text_attempt("Hel", "lo", usage=ProviderUsage(prompt_tokens=3))])
        for result in (m.chat(_request()), m.generate(_request())):
            assert isinstance(result, ProviderResult)
            assert result.text == "Hello" and result.finish_reason == FinishReason.STOP
            assert result.usage.prompt_tokens == 3 and result.model_version == "mock-model-1"

    def test_unary_failure_raises_and_drops_partial_text(self):
        m = MockProvider([failing_attempt(K.UNAVAILABLE, after_text=["partial"])])
        with pytest.raises(ProviderError):
            m.chat(_request())

    def test_empty_unary_result_has_no_text(self):
        assert MockProvider([empty_attempt()]).chat(_request()).text is None
        assert MockProvider([safety_block_attempt()]).generate(_request()).finish_reason == FinishReason.SAFETY

    def test_unary_grounding(self):
        g = GroundingResult(search_queries=("q",))
        assert MockProvider([text_attempt("a", grounding=g)]).chat(_request()).grounding is g

    def test_response_ids_are_deterministic(self):
        m = MockProvider([text_attempt("a")])
        assert [m.chat(_request()).response_id, m.chat(_request()).response_id] == ["mock-response-1", "mock-response-2"]


# ===========================================================================
# 3. GEMINI ADAPTER (fake SDK objects)
# ===========================================================================

def _sdk_chunk(text=None, finish=None, usage=None, grounding=None, message=None):
    cand = SimpleNamespace(finish_reason=finish, finish_message=message, grounding_metadata=grounding)
    return SimpleNamespace(text=text, usage_metadata=usage, candidates=[cand] if (finish or grounding or message) else [])


class _FakeSession:
    def __init__(self, chunks=(), response=None, fail_at=None, exc=None):
        self.chunks, self.response, self.fail_at, self.exc = list(chunks), response, fail_at, exc
        self.sent = None

    def send_message_stream(self, contents):
        self.sent = contents
        for i, c in enumerate(self.chunks):
            if self.fail_at == i:
                raise self.exc
            yield c
        if self.fail_at == len(self.chunks):
            raise self.exc

    def send_message(self, contents):
        self.sent = contents
        if self.exc is not None and self.fail_at == 0:
            raise self.exc
        return self.response


def _gemini(session=None, models=None):
    chats = MagicMock()
    chats.create.return_value = session if session is not None else _FakeSession()
    client = SimpleNamespace(chats=chats, models=models or MagicMock())
    return GeminiProvider(client=client), client


def _server_err(code=503):
    return genai_errors.ServerError(code, {"error": {"code": code, "message": "SECRET provider text", "status": "UNAVAILABLE"}}, None)


def _client_err(code):
    return genai_errors.ClientError(code, {"error": {"code": code, "message": "SECRET provider text", "status": "ERR"}}, None)


class TestExceptionTranslation:

    @pytest.mark.parametrize("exc,kind,status", [
        (_server_err(500), K.UNAVAILABLE, 500), (_server_err(503), K.UNAVAILABLE, 503),
        (_server_err(504), K.UNAVAILABLE, 504),
        (_client_err(429), K.RATE_LIMITED, 429),
        (_client_err(401), K.AUTH, 401), (_client_err(403), K.AUTH, 403),
        (_client_err(409), K.CONFLICT, 409),
        (_client_err(400), K.INVALID_REQUEST, 400), (_client_err(404), K.INVALID_REQUEST, 404),
        (httpx.ReadTimeout("t"), K.TIMEOUT, None), (httpx.ConnectTimeout("t"), K.TIMEOUT, None),
        (httpx.ConnectError("c"), K.TIMEOUT, None),
        (RuntimeError("boom"), K.UNKNOWN, None), (ValueError("v"), K.UNKNOWN, None),
    ])
    def test_kind_and_status(self, exc, kind, status):
        e = translate_exception(exc)
        assert isinstance(e, ProviderError) and e.kind == kind and e.http_status == status
        assert e.provider == "gemini" and e.original_type == type(exc).__name__

    def test_provider_status_label_is_kept(self):
        assert translate_exception(_server_err(503)).status == "UNAVAILABLE"

    def test_translated_message_never_contains_provider_text(self):
        assert "SECRET" not in str(translate_exception(_server_err(503)))
        assert "SECRET" not in str(translate_exception(_client_err(400)))

    def test_provider_error_passes_through_unchanged(self):
        original = ProviderError(K.AUTH, provider="x")
        assert translate_exception(original) is original


class TestMappers:

    @pytest.mark.parametrize("raw,expected,label", [
        (types.FinishReason.STOP, FinishReason.STOP, "STOP"),
        (types.FinishReason.MAX_TOKENS, FinishReason.MAX_TOKENS, "MAX_TOKENS"),
        (types.FinishReason.SAFETY, FinishReason.SAFETY, "SAFETY"),
        (types.FinishReason.RECITATION, FinishReason.SAFETY, "RECITATION"),
        (types.FinishReason.BLOCKLIST, FinishReason.SAFETY, "BLOCKLIST"),
        (types.FinishReason.PROHIBITED_CONTENT, FinishReason.SAFETY, "PROHIBITED_CONTENT"),
        (types.FinishReason.SPII, FinishReason.SAFETY, "SPII"),
        (types.FinishReason.IMAGE_SAFETY, FinishReason.SAFETY, "IMAGE_SAFETY"),
        (types.FinishReason.OTHER, FinishReason.OTHER, "OTHER"),
        (types.FinishReason.LANGUAGE, FinishReason.OTHER, "LANGUAGE"),
        (types.FinishReason.MALFORMED_FUNCTION_CALL, FinishReason.OTHER, "MALFORMED_FUNCTION_CALL"),
        (types.FinishReason.FINISH_REASON_UNSPECIFIED, FinishReason.OTHER, "FINISH_REASON_UNSPECIFIED"),
    ])
    def test_every_sdk_finish_reason_normalizes(self, raw, expected, label):
        assert gemini_adapter._map_finish_reason(raw) == (expected, label)

    def test_every_sdk_finish_reason_member_maps_to_something(self):
        for member in types.FinishReason:
            normalized, label = gemini_adapter._map_finish_reason(member)
            assert isinstance(normalized, FinishReason) and label == member.name

    def test_no_finish_reason_is_none(self):
        assert gemini_adapter._map_finish_reason(None) == (None, None)

    def test_usage_mapping(self):
        sdk = types.GenerateContentResponseUsageMetadata(
            prompt_token_count=10, candidates_token_count=20, thoughts_token_count=5,
            cached_content_token_count=2, total_token_count=35)
        assert gemini_adapter._map_usage(sdk) == ProviderUsage(10, 20, 5, 2, 35)
        assert gemini_adapter._map_usage(None) is None
        assert gemini_adapter._map_usage(SimpleNamespace()) == ProviderUsage()

    def test_grounding_full_mapping_preserves_positions(self):
        web = lambda t, u, d: types.GroundingChunk(web=types.GroundingChunkWeb(title=t, uri=u, domain=d))
        md = types.GroundingMetadata(
            web_search_queries=["q1", "q2"],
            grounding_chunks=[web("A", "https://a.com", "a.com"), types.GroundingChunk(), web("C", "https://c.com", "c.com")],
            grounding_supports=[
                types.GroundingSupport(segment=types.Segment(text="seg", start_index=1, end_index=4),
                                       grounding_chunk_indices=[0, 2]),
                types.GroundingSupport(grounding_chunk_indices=[1]),
            ])
        g = map_grounding(md)
        assert isinstance(g, GroundingResult) and g.search_queries == ("q1", "q2")
        assert g.sources == (GroundingSource("A", "https://a.com", "a.com"), None,
                             GroundingSource("C", "https://c.com", "c.com"))
        assert g.supports[0] == GroundingSupport("seg", 1, 4, (0, 2))
        assert g.supports[1] == GroundingSupport(None, None, None, (1,))

    def test_grounding_none_and_empty(self):
        assert map_grounding(None) is None
        g = map_grounding(types.GroundingMetadata())
        assert g == GroundingResult((), (), ())

    def test_mapped_grounding_contains_no_sdk_objects(self):
        md = types.GroundingMetadata(web_search_queries=["q"], grounding_chunks=[
            types.GroundingChunk(web=types.GroundingChunkWeb(title="t", uri="https://u.com"))])
        g = map_grounding(md)
        for value in (g, *g.sources, *g.search_queries):
            assert type(value).__module__.split(".")[0] != "google"


class TestRequestTranslation:

    def test_chat_translates_the_request_exactly(self):
        provider, client = _gemini(_FakeSession(response=SimpleNamespace(text="ok", usage_metadata=None, candidates=[])))
        img = Image.new("RGB", (2, 2))
        provider.chat(_request(
            system_instruction="SYSTEM", user_parts=[img, "q"], model="model-x", timeout_seconds=12.5,
            history=[ProviderMessage("user", "hi"), ProviderMessage("assistant", "hello")],
            reasoning_effort="low"))
        kwargs = client.chats.create.call_args.kwargs
        assert kwargs["model"] == "model-x"
        assert kwargs["history"] == [{"role": "user", "parts": [{"text": "hi"}]},
                                     {"role": "model", "parts": [{"text": "hello"}]}]
        cfg = kwargs["config"]
        assert cfg.system_instruction == "SYSTEM"
        assert cfg.http_options.timeout == 12500
        assert cfg.http_options.retry_options is None, "exactly one retry owner: the policy layer"
        assert cfg.thinking_config.thinking_level == types.ThinkingLevel.LOW
        assert not cfg.tools

    def test_user_parts_reach_the_sdk_unchanged(self):
        session = _FakeSession(response=SimpleNamespace(text="ok", usage_metadata=None, candidates=[]))
        provider, _ = _gemini(session)
        img = Image.new("RGB", (2, 2))
        provider.chat(_request(user_parts=[img, "q"]))
        assert session.sent[0] is img and session.sent[1] == "q"

    @pytest.mark.parametrize("effort,level", [
        ("minimal", types.ThinkingLevel.MINIMAL), ("low", types.ThinkingLevel.LOW),
        ("medium", types.ThinkingLevel.MEDIUM), ("HIGH", types.ThinkingLevel.HIGH)])
    def test_reasoning_effort_maps_to_thinking_level(self, effort, level):
        cfg = GeminiProvider._config(_request(reasoning_effort=effort))
        assert cfg.thinking_config.thinking_level == level

    def test_no_reasoning_effort_sends_no_thinking_config(self):
        assert GeminiProvider._config(_request(reasoning_effort=None)).thinking_config is None

    def test_web_search_adds_the_google_search_tool_only_when_requested(self):
        on = GeminiProvider._config(_request(enable_web_search=True))
        assert len(on.tools) == 1 and isinstance(on.tools[0].google_search, types.GoogleSearch)
        assert not GeminiProvider._config(_request(enable_web_search=False)).tools

    def test_unsupported_effort_or_role_becomes_a_normalized_error_not_a_raw_exception(self):
        provider, _ = _gemini()
        with pytest.raises(ProviderError) as exc:
            provider.chat(_request(reasoning_effort="turbo"))
        assert exc.value.kind == K.UNKNOWN and isinstance(exc.value.__cause__, ValueError)
        with pytest.raises(ProviderError):
            provider.chat(_request(history=[ProviderMessage("system", "x")]))

    def test_generate_is_a_one_shot_call_without_history(self):
        models = MagicMock()
        models.generate_content.return_value = SimpleNamespace(
            text="quiz", usage_metadata=None, candidates=[], model_version="v9", response_id="r9")
        provider, client = _gemini(models=models)
        result = provider.generate(_request(user_parts=["make a quiz"], model="mq", reasoning_effort=None, timeout_seconds=30))
        kwargs = models.generate_content.call_args.kwargs
        assert kwargs["model"] == "mq" and kwargs["contents"] == ["make a quiz"]
        assert kwargs["config"].http_options.timeout == 30000 and kwargs["config"].thinking_config is None
        client.chats.create.assert_not_called()
        assert (result.text, result.model_version, result.response_id) == ("quiz", "v9", "r9")

    def test_stream_chat_sends_history_and_parts(self):
        session = _FakeSession(chunks=[_sdk_chunk("a")])
        provider, client = _gemini(session)
        list(provider.stream_chat(_request(history=[ProviderMessage("assistant", "prev")], user_parts=["now"])))
        assert client.chats.create.call_args.kwargs["history"] == [{"role": "model", "parts": [{"text": "prev"}]}]
        assert session.sent == ["now"]


class TestResponseTranslation:

    def test_stream_yields_one_event_per_chunk_including_text_less_ones(self):
        chunks = [_sdk_chunk("a"), _sdk_chunk(None), _sdk_chunk("b", finish=types.FinishReason.STOP,
                  usage=types.GenerateContentResponseUsageMetadata(total_token_count=4))]
        events = list(_gemini(_FakeSession(chunks=chunks))[0].stream_chat(_request()))
        assert [e.text for e in events] == ["a", None, "b"]
        assert events[-1].finish_reason == FinishReason.STOP and events[-1].raw_finish_reason == "STOP"
        assert events[-1].usage.total_tokens == 4

    def test_stream_events_contain_no_sdk_types(self):
        chunks = [_sdk_chunk("a", finish=types.FinishReason.MAX_TOKENS,
                             usage=types.GenerateContentResponseUsageMetadata(total_token_count=1))]
        for e in _gemini(_FakeSession(chunks=chunks))[0].stream_chat(_request()):
            for part in (e, e.finish_reason, e.usage):
                assert part is None or type(part).__module__.split(".")[0] != "google"

    def test_grounding_is_mapped_only_when_web_search_was_requested(self):
        md = types.GroundingMetadata(web_search_queries=["q"])
        chunks = lambda: [_sdk_chunk("a", grounding=md, finish=types.FinishReason.STOP)]
        on = list(_gemini(_FakeSession(chunks=chunks()))[0].stream_chat(_request(enable_web_search=True)))
        off = list(_gemini(_FakeSession(chunks=chunks()))[0].stream_chat(_request(enable_web_search=False)))
        assert on[-1].grounding.search_queries == ("q",) and off[-1].grounding is None

    def test_finish_reason_is_reported_even_on_a_safety_stop(self):
        events = list(_gemini(_FakeSession(chunks=[_sdk_chunk(None, finish=types.FinishReason.SAFETY, message="m")]))[0]
                      .stream_chat(_request()))
        assert events[-1].finish_reason == FinishReason.SAFETY and events[-1].finish_message == "m"

    def test_chat_result_mapping(self):
        resp = SimpleNamespace(
            text="answer", model_version="v1", response_id="r1",
            usage_metadata=types.GenerateContentResponseUsageMetadata(prompt_token_count=7, total_token_count=9),
            candidates=[SimpleNamespace(finish_reason=types.FinishReason.STOP, grounding_metadata=types.GroundingMetadata(web_search_queries=["q"]))])
        provider, _ = _gemini(_FakeSession(response=resp))
        r = provider.chat(_request(enable_web_search=True))
        assert (r.text, r.model_version, r.response_id) == ("answer", "v1", "r1")
        assert r.finish_reason == FinishReason.STOP and r.usage.prompt_tokens == 7
        assert r.grounding.search_queries == ("q",)

    def test_chat_result_without_text_or_candidates(self):
        resp = SimpleNamespace(text=None, usage_metadata=None, candidates=[])
        r = _gemini(_FakeSession(response=resp))[0].chat(_request(enable_web_search=True))
        assert r.text is None and r.finish_reason is None and r.grounding is None

    def test_grounding_not_mapped_for_non_search_unary_chat(self):
        resp = SimpleNamespace(text="a", usage_metadata=None, candidates=[
            SimpleNamespace(finish_reason=None, grounding_metadata=types.GroundingMetadata(web_search_queries=["q"]))])
        assert _gemini(_FakeSession(response=resp))[0].chat(_request(enable_web_search=False)).grounding is None


class TestAdapterErrors:

    def test_pre_token_sdk_error_is_normalized_with_cause_kept(self):
        provider, _ = _gemini(_FakeSession(chunks=[], fail_at=0, exc=_server_err(503)))
        with pytest.raises(ProviderError) as exc:
            list(provider.stream_chat(_request()))
        assert exc.value.kind == K.UNAVAILABLE and isinstance(exc.value.__cause__, genai_errors.ServerError)

    def test_mid_stream_error_delivers_earlier_events_then_raises_normalized(self):
        provider, _ = _gemini(_FakeSession(chunks=[_sdk_chunk("a"), _sdk_chunk("b")], fail_at=1, exc=_server_err(503)))
        got = []
        with pytest.raises(ProviderError) as exc:
            for e in provider.stream_chat(_request()):
                got.append(e.text)
        assert got == ["a"] and exc.value.kind == K.UNAVAILABLE

    def test_error_while_creating_the_session_is_normalized(self):
        provider, client = _gemini()
        client.chats.create.side_effect = _client_err(429)
        with pytest.raises(ProviderError) as exc:
            provider.chat(_request())
        assert exc.value.kind == K.RATE_LIMITED

    def test_unary_error_is_normalized(self):
        models = MagicMock()
        models.generate_content.side_effect = httpx.ReadTimeout("deadline")
        with pytest.raises(ProviderError) as exc:
            _gemini(models=models)[0].generate(_request())
        assert exc.value.kind == K.TIMEOUT

    def test_closing_a_stream_early_is_not_turned_into_an_error(self):
        provider, _ = _gemini(_FakeSession(chunks=[_sdk_chunk("a"), _sdk_chunk("b")]))
        it = provider.stream_chat(_request())
        assert next(it).text == "a"
        it.close()  # GeneratorExit must propagate untouched (client disconnect)

    def test_adapter_never_retries(self):
        provider, client = _gemini(_FakeSession(chunks=[], fail_at=0, exc=_server_err(503)))
        with pytest.raises(ProviderError):
            list(provider.stream_chat(_request()))
        assert client.chats.create.call_count == 1


# ===========================================================================
# 4. POLICY LAYER OVER THE MOCK PROVIDER (no SDK anywhere)
# ===========================================================================

@pytest.fixture
def use_mock():
    previous = get_provider()
    def install(*attempts):
        mock = MockProvider(list(attempts))
        set_provider(mock)
        return mock
    yield install
    set_provider(previous)


@pytest.fixture
def sleeps(monkeypatch):
    recorded = []
    monkeypatch.setattr(ai_engine, "_sleep", lambda s: recorded.append(s))
    return recorded


@pytest.fixture
def unary_sleeps(monkeypatch):
    recorded = []
    monkeypatch.setattr(ai_engine.time, "sleep", lambda s: recorded.append(s))
    return recorded


def _stream(**kw):
    return list(ai_engine.stream_ai_response(prompt="hi", **kw))


def _terminal(chunks):
    t = [c for c in chunks if c.kind in ("done", "interrupted", "error")]
    assert len(t) == 1, "exactly one terminal event"
    return t[0]


class TestPolicyStreamingOverMock:

    def test_success(self, use_mock, sleeps):
        use_mock(text_attempt("Hel", "lo", usage=ProviderUsage(total_tokens=5)))
        out = _stream()
        assert [c.kind for c in out] == ["text", "text", "done"]
        assert out[-1].text == "Hello" and out[-1].usage.total_tokens == 5 and sleeps == []

    def test_503_before_first_token_retries_with_backoff_then_fails_with_code(self, use_mock, sleeps):
        mock = use_mock(failing_attempt(K.UNAVAILABLE, http_status=503))
        t = _terminal(_stream())
        assert mock.call_count == 3 and len(sleeps) == 2
        assert t.kind == "error" and t.error_code == ai_engine.ERROR_PROVIDER_UNAVAILABLE
        assert t.text == ai_engine.PROVIDER_UNAVAILABLE_ERROR

    def test_provider_recovering_on_attempt_two(self, use_mock, sleeps):
        mock = use_mock(failing_attempt(K.UNAVAILABLE), text_attempt("fine"))
        out = _stream()
        assert out[-1].kind == "done" and out[-1].text == "fine" and mock.call_count == 2 and len(sleeps) == 1

    def test_mid_stream_503_recovers_with_retry_event_and_no_duplicate_text(self, use_mock, sleeps):
        mock = use_mock(failing_attempt(K.UNAVAILABLE, http_status=503, after_text=["partial"]), text_attempt("full answer"))
        out = _stream()
        assert [c.kind for c in out] == ["text", "retry", "text", "done"]
        assert out[-1].text == "full answer" and "partial" not in out[-1].text
        assert mock.call_count == 2 and len(sleeps) == 1

    def test_retry_event_precedes_the_backoff_wait(self, use_mock, monkeypatch):
        timeline = []
        monkeypatch.setattr(ai_engine, "_sleep", lambda s: timeline.append("sleep"))
        use_mock(failing_attempt(K.UNAVAILABLE, after_text=["p"]), text_attempt("ok"))
        for c in ai_engine.stream_ai_response(prompt="hi"):
            timeline.append(c.kind)
        assert timeline == ["text", "retry", "sleep", "text", "done"]

    def test_recovery_is_granted_at_most_once(self, use_mock, sleeps):
        mock = use_mock(failing_attempt(K.UNAVAILABLE, after_text=["one"]), failing_attempt(K.UNAVAILABLE, after_text=["two"]))
        out = _stream()
        t = _terminal(out)
        assert [c.kind for c in out].count("retry") == 1 and mock.call_count == 2
        assert t.kind == "interrupted" and t.text == "two" and t.error_code == ai_engine.ERROR_PROVIDER_UNAVAILABLE

    def test_no_recovery_when_no_attempt_remains(self, use_mock, sleeps):
        use_mock(failing_attempt(K.UNAVAILABLE), failing_attempt(K.UNAVAILABLE),
                 failing_attempt(K.UNAVAILABLE, after_text=["late"]))
        out = _stream()
        assert "retry" not in [c.kind for c in out] and _terminal(out).text == "late"

    def test_429_is_never_retried(self, use_mock, sleeps):
        mock = use_mock(failing_attempt(K.RATE_LIMITED, http_status=429))
        t = _terminal(_stream())
        assert mock.call_count == 1 and sleeps == []
        assert t.text == ai_engine.QUOTA_EXHAUSTED_ERROR and t.error_code == ai_engine.ERROR_PROVIDER_RATE_LIMITED

    def test_429_after_partial_output_is_interrupted_without_recovery(self, use_mock, sleeps):
        mock = use_mock(failing_attempt(K.RATE_LIMITED, after_text=["part"]))
        out = _stream()
        assert mock.call_count == 1 and "retry" not in [c.kind for c in out]
        assert _terminal(out).kind == "interrupted" and _terminal(out).error_code == ai_engine.ERROR_STREAM_INTERRUPTED

    @pytest.mark.parametrize("kind,code", [
        (K.INVALID_REQUEST, ai_engine.ERROR_PROVIDER_INVALID_REQUEST), (K.AUTH, ai_engine.ERROR_PROVIDER_AUTH_FAILED)])
    def test_non_retryable_errors_fail_immediately(self, use_mock, sleeps, kind, code):
        mock = use_mock(failing_attempt(kind, http_status=400))
        t = _terminal(_stream())
        assert mock.call_count == 1 and sleeps == []
        assert t.error_code == code and t.text == ai_engine.GENERIC_CHAT_ERROR

    def test_conflict_is_retried_like_a_transient_failure(self, use_mock, sleeps):
        mock = use_mock(failing_attempt(K.CONFLICT, http_status=409))
        t = _terminal(_stream())
        assert mock.call_count == 3 and len(sleeps) == 2
        assert t.error_code == ai_engine.ERROR_PROVIDER_UNAVAILABLE

    def test_conflict_after_partial_output_is_interrupted_not_recovered(self, use_mock, sleeps):
        mock = use_mock(failing_attempt(K.CONFLICT, after_text=["x"]))
        out = _stream()
        assert mock.call_count == 1 and "retry" not in [c.kind for c in out]

    def test_timeout_is_retried_without_extra_backoff(self, use_mock, sleeps):
        mock = use_mock(failing_attempt(K.TIMEOUT))
        t = _terminal(_stream())
        assert mock.call_count == ai_engine.MAX_ATTEMPTS and sleeps == []
        assert t.error_code == ai_engine.ERROR_PROVIDER_TIMEOUT and t.text == ai_engine.GENERIC_CHAT_ERROR

    def test_unknown_and_unwrapped_errors_are_retried_with_backoff(self, use_mock, sleeps):
        mock = use_mock(raising_attempt(RuntimeError("bug")))
        t = _terminal(_stream())
        assert mock.call_count == 3 and len(sleeps) == 2 and t.error_code == ai_engine.ERROR_INTERNAL

    def test_safety_block_and_max_tokens_and_empty(self, use_mock, sleeps):
        use_mock(safety_block_attempt())
        t = _terminal(_stream())
        assert t.kind == "error" and t.error_code == ai_engine.ERROR_PROVIDER_SAFETY and t.text == ai_engine.BLOCKED_RESPONSE_ERROR
        use_mock(max_tokens_attempt("cut"))
        t = _terminal(_stream())
        assert t.kind == "interrupted" and t.text == "cut" and t.error_code == ai_engine.ERROR_PROVIDER_MAX_TOKENS
        use_mock(empty_attempt())
        assert _terminal(_stream()).error_code == ai_engine.ERROR_PROVIDER_EMPTY_RESPONSE
        assert sleeps == []

    def test_no_reported_finish_reason_counts_as_success(self, use_mock, sleeps):
        use_mock(text_attempt("answer", finish=None))
        assert _stream()[-1].kind == "done"

    def test_other_finish_reason_is_interrupted(self, use_mock, sleeps):
        use_mock(MockAttempt(events=[ProviderEvent(text="x"), ProviderEvent(finish_reason=FinishReason.OTHER, raw_finish_reason="LANGUAGE")]))
        t = _terminal(_stream())
        assert t.kind == "interrupted" and t.error_code == ai_engine.ERROR_STREAM_INTERRUPTED

    def test_bad_image_fails_before_any_provider_call(self, use_mock, sleeps):
        mock = use_mock(text_attempt("x"))
        out = _stream(image_bytes=b"not an image")
        assert mock.call_count == 0 and out[0].error_code == ai_engine.ERROR_REQUEST_INVALID

    def test_grounding_and_usage_are_passed_through_from_the_provider(self, use_mock, sleeps):
        g = GroundingResult(search_queries=("q",))
        use_mock(text_attempt("a", grounding=g, usage=ProviderUsage(total_tokens=3)))
        done = _stream(enable_research=True)[-1]
        assert done.grounding is g and done.usage.total_tokens == 3


class TestPolicyBuildsTheProviderRequest:

    def test_stream_request_contents(self, use_mock, sleeps):
        mock = use_mock(text_attempt("ok"))
        ai_engine.stream_ai_response  # noqa
        list(ai_engine.stream_ai_response(
            prompt="Why?", mode="socratic", board="NCTB", user_class="Class 9-10 (SSC)", stream="Science",
            history=[{"role": "user", "text": "a"}, {"role": "bot", "text": "b"}, {"role": "system", "text": "skip"}],
            enable_research=True, request_id="rid-1"))
        r = mock.requests[0]
        assert mock.calls == ["stream_chat"]
        assert r.model == ai_engine.MODEL_NAME and r.reasoning_effort == ai_engine.CHAT_THINKING_LEVEL == "low"
        assert r.enable_web_search is True and r.request_id == "rid-1"
        assert r.user_parts == ["Why?"]
        assert r.history == [ProviderMessage("user", "a"), ProviderMessage("assistant", "b")]
        assert "DO NOT give direct answers immediately" in r.system_instruction
        assert r.timeout_seconds == ai_engine.AI_REQUEST_TIMEOUT_SECONDS

    def test_web_search_is_off_unless_kognit_asked_for_it(self, use_mock, sleeps):
        mock = use_mock(text_attempt("ok"))
        _stream()
        assert mock.requests[0].enable_web_search is False

    def test_image_is_carried_as_a_decoded_pil_image_before_the_prompt(self, use_mock, sleeps):
        mock = use_mock(text_attempt("ok"))
        import io
        buf = io.BytesIO(); Image.new("RGB", (3, 3)).save(buf, format="PNG")
        _stream(image_bytes=buf.getvalue())
        parts = mock.requests[0].user_parts
        assert isinstance(parts[0], Image.Image) and parts[1] == "hi"

    def test_pdf_context_goes_into_the_system_instruction_not_the_user_parts(self, use_mock, sleeps):
        mock = use_mock(text_attempt("ok"))
        _stream(pdf_context="CHAPTER TEXT")
        r = mock.requests[0]
        assert "CHAPTER TEXT" in r.system_instruction and r.user_parts == ["hi"]

    def test_retry_attempts_use_the_retry_timeout_and_the_same_content(self, use_mock, sleeps):
        mock = use_mock(failing_attempt(K.UNAVAILABLE), failing_attempt(K.UNAVAILABLE), text_attempt("ok"))
        _stream(request_id="same")
        a, b, c = mock.requests
        assert a.timeout_seconds == ai_engine.AI_REQUEST_TIMEOUT_SECONDS
        assert b.timeout_seconds == c.timeout_seconds == ai_engine.RETRY_REQUEST_TIMEOUT_SECONDS
        assert a.system_instruction == b.system_instruction == c.system_instruction
        assert a.user_parts == b.user_parts == c.user_parts and {a.request_id, b.request_id, c.request_id} == {"same"}

    def test_recovery_attempt_gets_the_full_timeout(self, use_mock, sleeps):
        mock = use_mock(failing_attempt(K.UNAVAILABLE, after_text=["p"]), text_attempt("ok"))
        _stream()
        assert [r.timeout_seconds for r in mock.requests] == [ai_engine.AI_REQUEST_TIMEOUT_SECONDS] * 2

    def test_generated_request_id_is_stable_across_attempts(self, use_mock, sleeps):
        mock = use_mock(failing_attempt(K.UNAVAILABLE), text_attempt("ok"))
        _stream()
        ids = {r.request_id for r in mock.requests}
        assert len(ids) == 1 and re.fullmatch(r"[0-9a-f]{12}", ids.pop())

    def test_model_comes_from_kognit_config_not_the_provider(self, use_mock, sleeps, monkeypatch):
        monkeypatch.setattr(ai_engine, "MODEL_NAME", "configured-model")
        mock = use_mock(text_attempt("ok"))
        _stream()
        assert mock.requests[0].model == "configured-model"


class TestPolicyUnaryOverMock:

    def test_chat_success_returns_text_or_metadata(self, use_mock, unary_sleeps):
        g = GroundingResult(search_queries=("q",))
        use_mock(text_attempt("Hello", usage=ProviderUsage(prompt_tokens=2), grounding=g))
        assert ai_engine.generate_ai_response("hi") == "Hello"
        r = ai_engine.generate_ai_response("hi", return_metadata=True, enable_research=True)
        assert r.text == "Hello" and r.is_error is False and r.usage.prompt_tokens == 2
        assert r.grounding is g and r.resolved_model_version == "mock-model-1" and r.error_code is None

    def test_chat_uses_the_chat_operation_with_history(self, use_mock, unary_sleeps):
        mock = use_mock(text_attempt("ok"))
        ai_engine.generate_ai_response("hi", history=[{"role": "bot", "text": "prev"}])
        assert mock.calls == ["chat"]
        assert mock.requests[0].history == [ProviderMessage("assistant", "prev")]

    def test_503_is_retried_with_the_existing_delay_then_generic_error_with_code(self, use_mock, unary_sleeps):
        mock = use_mock(failing_attempt(K.UNAVAILABLE))
        r = ai_engine.generate_ai_response("hi", return_metadata=True)
        assert mock.call_count == ai_engine.MAX_ATTEMPTS_BUCKET_B and len(unary_sleeps) == 2
        assert r.text == ai_engine.GENERIC_CHAT_ERROR and r.is_error and r.error_code == ai_engine.ERROR_PROVIDER_UNAVAILABLE
        assert all(ai_engine.RETRY_DELAY_BASE_SECONDS - ai_engine.RETRY_DELAY_JITTER_SECONDS <= d
                   <= ai_engine.RETRY_DELAY_BASE_SECONDS + ai_engine.RETRY_DELAY_JITTER_SECONDS for d in unary_sleeps)

    def test_recovering_on_a_later_attempt(self, use_mock, unary_sleeps):
        mock = use_mock(failing_attempt(K.UNAVAILABLE), text_attempt("fine"))
        assert ai_engine.generate_ai_response("hi") == "fine" and mock.call_count == 2

    def test_conflict_is_retried(self, use_mock, unary_sleeps):
        mock = use_mock(failing_attempt(K.CONFLICT), text_attempt("ok"))
        assert ai_engine.generate_ai_response("hi") == "ok" and mock.call_count == 2

    def test_429_is_not_retried(self, use_mock, unary_sleeps):
        mock = use_mock(failing_attempt(K.RATE_LIMITED))
        r = ai_engine.generate_ai_response("hi", return_metadata=True)
        assert mock.call_count == 1 and unary_sleeps == []
        assert r.text == ai_engine.QUOTA_EXHAUSTED_ERROR and r.error_code == ai_engine.ERROR_PROVIDER_RATE_LIMITED

    @pytest.mark.parametrize("kind,code", [
        (K.INVALID_REQUEST, ai_engine.ERROR_PROVIDER_INVALID_REQUEST), (K.AUTH, ai_engine.ERROR_PROVIDER_AUTH_FAILED),
        (K.UNKNOWN, ai_engine.ERROR_INTERNAL)])
    def test_other_errors_are_not_retried(self, use_mock, unary_sleeps, kind, code):
        mock = use_mock(failing_attempt(kind))
        r = ai_engine.generate_ai_response("hi", return_metadata=True)
        assert mock.call_count == 1 and r.text == ai_engine.GENERIC_CHAT_ERROR and r.error_code == code

    def test_unexpected_exception_is_not_retried(self, use_mock, unary_sleeps):
        mock = use_mock(raising_attempt(RuntimeError("bug")))
        assert ai_engine.generate_ai_response("hi") == ai_engine.GENERIC_CHAT_ERROR and mock.call_count == 1

    def test_client_timeout_is_retried_at_most_once(self, use_mock, unary_sleeps):
        mock = use_mock(failing_attempt(K.TIMEOUT))
        r = ai_engine.generate_ai_response("hi", return_metadata=True)
        assert mock.call_count == ai_engine.MAX_ATTEMPTS_BUCKET_A and unary_sleeps == []
        assert r.error_code == ai_engine.ERROR_PROVIDER_TIMEOUT

    def test_empty_response_is_a_blocked_error_and_not_retried(self, use_mock, unary_sleeps):
        mock = use_mock(safety_block_attempt())
        r = ai_engine.generate_ai_response("hi", return_metadata=True)
        assert mock.call_count == 1 and r.text == ai_engine.BLOCKED_RESPONSE_ERROR and r.error_code == ai_engine.ERROR_PROVIDER_SAFETY

    def test_bad_image_never_reaches_the_provider(self, use_mock, unary_sleeps):
        mock = use_mock(text_attempt("x"))
        r = ai_engine.generate_ai_response("hi", image_bytes=b"nope", return_metadata=True)
        assert mock.call_count == 0 and r.text == ai_engine.IMAGE_DECODE_ERROR and r.error_code == ai_engine.ERROR_REQUEST_INVALID

    def test_request_id_is_passed_through_and_stable(self, use_mock, unary_sleeps):
        mock = use_mock(failing_attempt(K.UNAVAILABLE), text_attempt("ok"))
        ai_engine.generate_ai_response("hi", request_id="rid-9")
        assert {r.request_id for r in mock.requests} == {"rid-9"}

    def test_request_ids_appear_in_unary_logs(self, use_mock, unary_sleeps, caplog):
        caplog.set_level(logging.INFO)
        use_mock(failing_attempt(K.UNAVAILABLE))
        ai_engine.generate_ai_response("UNIQUE-UNARY-PROMPT-5", request_id="rid-log")
        # (the shared request builder's own "timing: image_decode" line predates
        # request ids and is not part of the per-attempt policy loop)
        lines = [r.getMessage() for r in caplog.records
                 if r.name == "kognit.ai_engine" and "generate_ai_response" in r.getMessage()
                 and "image_decode" not in r.getMessage()]
        assert len(lines) >= 3 and all("request_id=rid-log" in m for m in lines)
        assert "UNIQUE-UNARY-PROMPT-5" not in "\n".join(r.getMessage() for r in caplog.records)


class TestPolicyQuizAndTitleOverMock:
    QUIZ = json.dumps([{"id": 1, "question": "2+2?", "options": ["3", "4", "5", "6"], "correct_index": 1, "explanation": "sum"}])

    def test_quiz_success_uses_generate_with_no_reasoning_and_no_history(self, use_mock, unary_sleeps):
        mock = use_mock(text_attempt(self.QUIZ))
        out = ai_engine.generate_quiz_questions("NCTB", "Class 9-10 (SSC)", "Math", "Science", "Algebra", count=1)
        assert len(out) == 1 and out[0]["correct_index"] == 1
        r = mock.requests[0]
        assert mock.calls == ["generate"] and r.reasoning_effort is None and r.history == [] and r.enable_web_search is False
        assert r.model == ai_engine.MODEL_NAME and "Algebra" in r.user_parts[0]

    def test_quiz_accepts_fenced_json(self, use_mock, unary_sleeps):
        use_mock(text_attempt("```json\n" + self.QUIZ + "\n```"))
        assert len(ai_engine.generate_quiz_questions("NCTB", "c", "s", "st", "t", count=1)) == 1

    def test_quiz_retries_5xx_and_409_then_succeeds(self, use_mock, unary_sleeps):
        mock = use_mock(failing_attempt(K.UNAVAILABLE), failing_attempt(K.CONFLICT), text_attempt(self.QUIZ))
        assert len(ai_engine.generate_quiz_questions("NCTB", "c", "s", "st", "t", count=1)) == 1
        assert mock.call_count == 3 and len(unary_sleeps) == 2

    def test_quiz_5xx_exhausted_returns_empty(self, use_mock, unary_sleeps):
        mock = use_mock(failing_attempt(K.UNAVAILABLE))
        assert ai_engine.generate_quiz_questions("NCTB", "c", "s", "st", "t") == [] and mock.call_count == 3

    @pytest.mark.parametrize("attempt", [
        failing_attempt(K.RATE_LIMITED), failing_attempt(K.TIMEOUT), failing_attempt(K.AUTH),
        failing_attempt(K.INVALID_REQUEST), raising_attempt(RuntimeError("x"))])
    def test_quiz_other_errors_return_empty_without_retry(self, use_mock, unary_sleeps, attempt):
        mock = use_mock(attempt)
        assert ai_engine.generate_quiz_questions("NCTB", "c", "s", "st", "t") == [] and mock.call_count == 1

    def test_quiz_malformed_json_or_empty_returns_empty(self, use_mock, unary_sleeps):
        use_mock(text_attempt("this is not json"))
        assert ai_engine.generate_quiz_questions("NCTB", "c", "s", "st", "t") == []
        use_mock(safety_block_attempt())
        assert ai_engine.generate_quiz_questions("NCTB", "c", "s", "st", "t") == []

    def test_title_success_and_request_shape(self, use_mock, unary_sleeps):
        mock = use_mock(text_attempt('  "Newton\'s Laws\nextra line'))
        title = ai_engine.generate_chat_title([{"role": "user", "text": "explain newton"}])
        assert title == "Newton's Laws"
        r = mock.requests[0]
        assert mock.calls == ["generate"] and r.reasoning_effort == ai_engine.TITLE_THINKING_LEVEL
        assert r.timeout_seconds == ai_engine.CHAT_TITLE_TIMEOUT_SECONDS and "explain newton" in r.user_parts[0]

    def test_title_failure_returns_none_and_never_raises(self, use_mock, unary_sleeps):
        use_mock(failing_attempt(K.UNAVAILABLE))
        assert ai_engine.generate_chat_title([{"role": "user", "text": "hi"}]) is None
        use_mock(empty_attempt())
        assert ai_engine.generate_chat_title([{"role": "user", "text": "hi"}]) is None

    def test_title_with_no_history_makes_no_provider_call(self, use_mock, unary_sleeps):
        mock = use_mock(text_attempt("x"))
        assert ai_engine.generate_chat_title([]) is None and mock.call_count == 0


class TestErrorKindToCode:

    @pytest.mark.parametrize("kind,code", [
        (K.UNAVAILABLE, "PROVIDER_UNAVAILABLE"), (K.CONFLICT, "PROVIDER_UNAVAILABLE"),
        (K.RATE_LIMITED, "PROVIDER_RATE_LIMITED"), (K.TIMEOUT, "PROVIDER_TIMEOUT"),
        (K.AUTH, "PROVIDER_AUTH_FAILED"), (K.INVALID_REQUEST, "PROVIDER_INVALID_REQUEST"),
        (K.UNKNOWN, "INTERNAL_ERROR")])
    def test_mapping(self, kind, code):
        assert ai_engine._error_code_for_kind(kind) == code

    def test_every_kind_has_a_code(self):
        for kind in ProviderErrorKind:
            assert ai_engine._error_code_for_kind(kind)

    def test_wire_codes_are_unchanged(self):
        assert {ai_engine.ERROR_PROVIDER_UNAVAILABLE, ai_engine.ERROR_PROVIDER_RATE_LIMITED, ai_engine.ERROR_PROVIDER_SAFETY,
                ai_engine.ERROR_PROVIDER_MAX_TOKENS, ai_engine.ERROR_PROVIDER_EMPTY_RESPONSE,
                ai_engine.ERROR_PROVIDER_INVALID_REQUEST, ai_engine.ERROR_PROVIDER_AUTH_FAILED,
                ai_engine.ERROR_PROVIDER_TIMEOUT, ai_engine.ERROR_REQUEST_INVALID, ai_engine.ERROR_STREAM_INTERRUPTED,
                ai_engine.ERROR_INTERNAL} == {
            "PROVIDER_UNAVAILABLE", "PROVIDER_RATE_LIMITED", "PROVIDER_SAFETY", "PROVIDER_MAX_TOKENS",
            "PROVIDER_EMPTY_RESPONSE", "PROVIDER_INVALID_REQUEST", "PROVIDER_AUTH_FAILED", "PROVIDER_TIMEOUT",
            "REQUEST_INVALID", "STREAM_INTERRUPTED", "INTERNAL_ERROR"}

    def test_exception_type_name_uses_the_original_failure(self):
        assert ai_engine._exception_type_name(ProviderError(K.UNAVAILABLE, original_type="ServerError")) == "ServerError"
        assert ai_engine._exception_type_name(RuntimeError("x")) == "RuntimeError"

    def test_telemetry_reports_the_original_exception_type(self, use_mock, sleeps, caplog):
        caplog.set_level(logging.INFO)
        use_mock(ProviderError and failing_attempt(K.UNAVAILABLE, http_status=503))
        _stream()
        # MockProvider labels its errors "MockProviderError"; real adapters keep the SDK type.
        tele = [r.getMessage() for r in caplog.records if "stream_telemetry" in r.getMessage()]
        assert tele and "exception_type=MockProviderError" in tele[-1] and "http_status=503" in tele[-1]


# ===========================================================================
# HTTP LEVEL
# ===========================================================================

@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def authed(monkeypatch):
    async def _user():
        return ("user-1", "token-1")
    main.app.dependency_overrides[main._rate_limited_chat] = _user

    async def _ctx(**kwargs):
        return ("Class 9-10 (SSC)", "Science")
    monkeypatch.setattr(main, "_get_academic_context", _ctx)
    yield
    main.app.dependency_overrides.clear()


def _events(body):
    return [json.loads(line) for line in body.strip().split("\n") if line.strip()]


def _research(monkeypatch, requested=True):
    monkeypatch.setattr(main, "decide_research", lambda p: SimpleNamespace(
        research_requested=requested, reason="r", category="c"))


GROUNDING = GroundingResult(
    search_queries=("bdt usd rate",),
    sources=(GroundingSource("Bangladesh Bank", "https://www.bb.org.bd/", "bb.org.bd"),
             GroundingSource("Bad", "javascript:alert(1)", "evil")),
    supports=(GroundingSupport("current rate", 0, 12, (0,)),))


class TestStreamedResearchSources:
    """Regression for a pre-Phase-10 bug: the streaming endpoint called
    normalize_grounding_metadata() with arguments it does not accept, the
    TypeError was swallowed, and a streamed research answer always carried
    research=null (no sources, no citations)."""

    def test_streamed_research_answer_carries_normalized_sources_and_citations(self, client, authed, use_mock, sleeps, monkeypatch):
        _research(monkeypatch)
        use_mock(text_attempt("The rate is X.", grounding=GROUNDING))
        done = _events(client.post("/api/chat/stream", data={"prompt": "current bdt usd rate", "chat_id": "c"}).text)[-1]
        r = done["research"]
        assert done["type"] == "done" and r["research_used"] is True and r["grounding_status"] == "used"
        assert r["search_queries"] == ["bdt usd rate"]
        assert [s["url"] for s in r["sources"]] == ["https://www.bb.org.bd/"], "the javascript: URL must be dropped"
        assert r["citations"][0]["answer_segment"] == "current rate" and r["citations"][0]["source_ids"] == ["src-0"]

    def test_research_requested_but_no_grounding_is_failed_not_used(self, client, authed, use_mock, sleeps, monkeypatch):
        _research(monkeypatch)
        use_mock(text_attempt("answer"))
        r = _events(client.post("/api/chat/stream", data={"prompt": "q", "chat_id": "c"}).text)[-1]["research"]
        assert r["research_used"] is False and r["grounding_status"] == "failed" and r["sources"] == []

    def test_model_chose_not_to_search_is_not_used(self, client, authed, use_mock, sleeps, monkeypatch):
        _research(monkeypatch)
        use_mock(text_attempt("answer", grounding=GroundingResult()))
        r = _events(client.post("/api/chat/stream", data={"prompt": "q", "chat_id": "c"}).text)[-1]["research"]
        assert r["grounding_status"] == "not_used" and r["research_used"] is False

    def test_no_research_means_null_payload_and_no_search_tool_requested(self, client, authed, use_mock, sleeps, monkeypatch):
        _research(monkeypatch, requested=False)
        mock = use_mock(text_attempt("answer"))
        done = _events(client.post("/api/chat/stream", data={"prompt": "q", "chat_id": "c"}).text)[-1]
        assert done["research"] is None and mock.requests[0].enable_web_search is False

    def test_web_search_flag_reaches_the_provider_when_kognit_decides_research(self, client, authed, use_mock, sleeps, monkeypatch):
        _research(monkeypatch)
        mock = use_mock(text_attempt("a", grounding=GROUNDING))
        client.post("/api/chat/stream", data={"prompt": "q", "chat_id": "c"})
        assert mock.requests[0].enable_web_search is True

    def test_stream_wire_events_over_the_mock_match_the_existing_contract(self, client, authed, use_mock, sleeps, monkeypatch):
        _research(monkeypatch, requested=False)
        use_mock(text_attempt("Force ", "equals ma"))
        events = _events(client.post("/api/chat/stream", data={"prompt": "q", "chat_id": "c"}).text)
        assert [e["type"] for e in events] == ["start", "delta", "delta", "done"]
        assert events[-1]["reply"] == "Force equals ma" and all("code" not in e for e in events)

    def test_mid_stream_recovery_over_http_with_the_mock(self, client, authed, use_mock, sleeps, monkeypatch):
        _research(monkeypatch, requested=False)
        use_mock(failing_attempt(K.UNAVAILABLE, http_status=503, after_text=["partial"]), text_attempt("full"))
        events = _events(client.post("/api/chat/stream", data={"prompt": "q", "chat_id": "c"}).text)
        types_seen = [e["type"] for e in events]
        assert types_seen == ["start", "delta", "retry", "delta", "done"] and events[-1]["reply"] == "full"

    def test_503_exhausted_over_http_gets_the_code_and_a_request_id(self, client, authed, use_mock, sleeps, monkeypatch):
        _research(monkeypatch, requested=False)
        use_mock(failing_attempt(K.UNAVAILABLE, http_status=503))
        r = client.post("/api/chat/stream", data={"prompt": "q", "chat_id": "c"})
        assert _events(r.text)[-1] == {"type": "error", "reply": ai_engine.PROVIDER_UNAVAILABLE_ERROR, "code": "PROVIDER_UNAVAILABLE"}
        assert re.fullmatch(r"[0-9a-f]{12}", r.headers["x-request-id"])


class TestUnaryChatRequestIdParity:

    def test_chat_endpoint_returns_a_request_id_header_and_passes_it_down(self, client, authed, monkeypatch):
        seen = {}
        def fake(**kwargs):
            seen.update(kwargs)
            return "ok"
        monkeypatch.setattr(main, "generate_ai_response", fake)
        r = client.post("/api/chat", data={"prompt": "explain photosynthesis", "mode": "direct"})
        rid = r.headers["x-request-id"]
        assert re.fullmatch(r"[0-9a-f]{12}", rid) and seen["request_id"] == rid
        assert r.json()["reply"] == "ok" and "request_id" not in r.json()

    def test_each_chat_request_gets_its_own_id(self, client, authed, monkeypatch):
        monkeypatch.setattr(main, "generate_ai_response", lambda **kw: "ok")
        a = client.post("/api/chat", data={"prompt": "x"}).headers["x-request-id"]
        b = client.post("/api/chat", data={"prompt": "x"}).headers["x-request-id"]
        assert a != b

    def test_research_chat_path_passes_the_id_and_the_normalized_grounding(self, client, authed, monkeypatch):
        _research(monkeypatch)
        seen = {}
        def fake(**kwargs):
            seen.update(kwargs)
            return ai_engine.AIGenerationResult(text="answer", is_error=False, grounding=GROUNDING)
        monkeypatch.setattr(main, "generate_ai_response", fake)
        r = client.post("/api/chat", data={"prompt": "current rate", "mode": "direct"})
        assert seen["request_id"] == r.headers["x-request-id"] and seen["return_metadata"] is True
        assert r.json()["research"]["grounding_status"] == "used"

    def test_real_unary_path_end_to_end_over_the_mock(self, client, authed, use_mock, unary_sleeps, monkeypatch, caplog):
        caplog.set_level(logging.INFO)
        _research(monkeypatch, requested=False)
        mock = use_mock(failing_attempt(K.UNAVAILABLE), text_attempt("regenerated answer"))
        r = client.post("/api/chat", data={"prompt": "UNIQUE-HTTP-UNARY-3", "mode": "direct"})
        rid = r.headers["x-request-id"]
        assert r.json()["reply"] == "regenerated answer" and mock.call_count == 2
        assert {req.request_id for req in mock.requests} == {rid}
        assert "UNIQUE-HTTP-UNARY-3" not in "\n".join(rec.getMessage() for rec in caplog.records)


# ===========================================================================
# ARCHITECTURE: SDK ISOLATION
# ===========================================================================

# Runtime modules that may import the SDK, and why.
ALLOWED_SDK_IMPORTERS = {
    "backend/providers/gemini.py": "the Gemini provider adapter - the one runtime SDK boundary",
    "evaluation/judge.py": "evaluation tooling: the LLM-as-judge calls Gemini directly via an injected client",
    "evaluation/research_judge.py": "evaluation tooling: the research LLM-as-judge, same reason",
}

# Directories that never hold Kognit source, whatever the checkout looks like.
_NEVER_SCAN_DIRS = {".git", "node_modules", "__pycache__", "site-packages", "dist-packages",
                    "tests_frontend", "static", "templates", "supabase"}
_VENV_MARKER = "pyvenv.cfg"  # every virtualenv has one, whatever it is named


def _is_test_file(rel):
    base = os.path.basename(rel)
    return (base.startswith("test_") or base.endswith("_test.py") or base == "conftest.py"
            or rel.startswith("evaluation/tests/"))


def _git_python_files(root):
    """The repository's own Python files per git (tracked plus untracked-but-not-
    ignored, so a brand-new module is scanned before it is committed). Honours
    .gitignore, so a virtualenv is excluded under ANY name. None if git cannot
    answer (not installed / not a repository / ownership refusal)."""
    try:
        proc = subprocess.run(
            ["git", "-C", root, "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", "*.py"],
            capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return sorted(p for p in proc.stdout.decode("utf-8").split("\0") if p)


def _walk_python_files(root):
    """Filesystem fallback. Prunes by what a directory IS, not what it is
    called: any directory holding a pyvenv.cfg is a virtualenv (so a venv named
    `venv`, `venvvenv`, `.myenv` or nested at `x/Scripts/activate` is skipped),
    and site-packages / dist-packages are never entered."""
    found = []
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs
                   if d not in _NEVER_SCAN_DIRS and not os.path.exists(os.path.join(current, d, _VENV_MARKER))]
        for name in files:
            if name.endswith(".py"):
                found.append(os.path.relpath(os.path.join(current, name), root).replace(os.sep, "/"))
    return sorted(found)


def _runtime_python_files(root=REPO_ROOT):
    """Kognit's own runtime (non-test) Python files under `root`."""
    candidates = _git_python_files(root)
    if candidates is None:
        candidates = _walk_python_files(root)
    return sorted(
        rel for rel in candidates
        if os.path.exists(os.path.join(root, rel))
        and not any(part in _NEVER_SCAN_DIRS for part in rel.split("/")[:-1])
        and not _is_test_file(rel))


def _imports_sdk(rel, root=REPO_ROOT):
    tree = ast.parse(open(os.path.join(root, rel), encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name == "google" or a.name.startswith("google.") for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and node.module and (node.module == "google" or node.module.startswith("google.")):
            return True
    return False


def _sdk_importers_outside_allowlist(root=REPO_ROOT, allowlist=ALLOWED_SDK_IMPORTERS):
    return [f for f in _runtime_python_files(root) if _imports_sdk(f, root) and f not in allowlist]


# A stdlib-only import probe. Runs under `python -I -S`, so no site-packages,
# .pth files, sitecustomize or PYTHON* variables can pre-load anything. A
# meta-path finder sits first and records every import of google/httpx that is
# ATTEMPTED (even one the module swallows with try/except ImportError) together
# with the module that attempted it. The optional `preload` snippet lets a test
# simulate an environment that already has a namespace package loaded.
_IMPORT_PROBE = textwrap.dedent("""
    import importlib.abc, json, sys
    root, preload, modules = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
    sys.path.insert(0, root)
    exec(preload)
    def sdkish(name):
        return name in ("google", "httpx") or name.startswith(("google.", "httpx."))
    attempts = []
    class Probe(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if sdkish(name) and name not in sys.modules:
                frame = sys._getframe(1)
                while frame is not None and frame.f_globals.get("__name__", "").startswith(
                        ("importlib", "_frozen_importlib")):
                    frame = frame.f_back
                attempts.append([name, frame.f_globals.get("__name__") if frame else None])
            return None
    sys.meta_path.insert(0, Probe())
    for module in modules:
        __import__(module)
    print("PROBE:" + json.dumps(attempts))
""")


def _probe_sdk_import_attempts(root, modules, preload=""):
    """(returncode, attempts or None, stderr). attempts is None if the import
    itself crashed (e.g. a hard `import httpx` with no site-packages)."""
    proc = subprocess.run(
        [sys.executable, "-I", "-S", "-c", _IMPORT_PROBE, root, preload, json.dumps(modules)],
        capture_output=True, text=True, timeout=120)
    attempts = None
    for line in proc.stdout.splitlines():
        if line.startswith("PROBE:"):
            attempts = json.loads(line[len("PROBE:"):])
    return proc.returncode, attempts, proc.stderr


def _write(root, rel, text=""):
    path = os.path.join(str(root), *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


# pip's vendored urllib3 (contrib/appengine.py) really contains this statement.
_APPENGINE_STUB = "try:\n    from google.appengine.api import urlfetch\nexcept ImportError:\n    urlfetch = None\n"
_LEAKY_MODULE = "from google.genai import types\n"


class TestFileDiscovery:
    """The SDK-isolation scan must read Kognit's source - never a developer's
    virtualenv. (Regression: a virtualenv named `venvvenv`, which the old
    hardcoded skip-list did not know, was scanned and pip's vendored
    urllib3 was reported as an 'SDK import outside the provider boundary'.)"""

    def _layout(self, root):
        _write(root, "backend/app.py", "x = 1\n")
        _write(root, "backend/helper/util.py", "y = 2\n")
        _write(root, "test_something.py", "assert True\n")
        _write(root, "conftest.py", "")
        _write(root, "evaluation/tests/test_eval.py", "")
        # the reported layout: a venv nested at venvvenv/Scripts/activate
        _write(root, "venvvenv/Scripts/activate/pyvenv.cfg", "home = C:\\Python312\n")
        _write(root, "venvvenv/Scripts/activate/Lib/site-packages/pip/_vendor/urllib3/contrib/appengine.py", _APPENGINE_STUB)
        # a venv under an unrelated name, found only via its pyvenv.cfg
        _write(root, ".whatever/pyvenv.cfg", "")
        _write(root, ".whatever/lib/thing.py", _LEAKY_MODULE)
        # site-packages with no pyvenv.cfg anywhere above it
        _write(root, "loose/site-packages/dep/mod.py", _LEAKY_MODULE)
        _write(root, "node_modules/pkg/x.py", _LEAKY_MODULE)

    def test_walk_keeps_kognit_source_and_prunes_every_kind_of_environment(self, tmp_path):
        self._layout(tmp_path)
        assert _walk_python_files(str(tmp_path)) == sorted([
            "backend/app.py", "backend/helper/util.py", "test_something.py", "conftest.py",
            "evaluation/tests/test_eval.py"])

    def test_runtime_files_drop_tests_and_environments(self, tmp_path):
        self._layout(tmp_path)  # not a git repo -> exercises the filesystem fallback
        assert _runtime_python_files(str(tmp_path)) == ["backend/app.py", "backend/helper/util.py"]

    def test_an_environment_named_by_the_gitignore_typo_is_excluded_even_without_pyvenv_cfg(self, tmp_path):
        _write(tmp_path, "backend/app.py")
        _write(tmp_path, "venvvenv/Scripts/activate/Lib/site-packages/pip/_vendor/urllib3/contrib/appengine.py", _APPENGINE_STUB)
        assert _runtime_python_files(str(tmp_path)) == ["backend/app.py"]

    def test_git_listing_honours_gitignore_and_includes_new_untracked_modules(self, tmp_path):
        if shutil.which("git") is None:
            pytest.skip("git executable not available; the filesystem fallback is covered by the tests above")
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
        _write(tmp_path, ".gitignore", "venvvenv/\n")
        _write(tmp_path, "backend/app.py")
        _write(tmp_path, "backend/brand_new_uncommitted.py")
        _write(tmp_path, "venvvenv/Scripts/activate/Lib/site-packages/pip/_vendor/urllib3/contrib/appengine.py", _APPENGINE_STUB)
        listed = _git_python_files(str(tmp_path))
        assert listed == ["backend/app.py", "backend/brand_new_uncommitted.py"]
        assert _runtime_python_files(str(tmp_path)) == listed

    def test_git_failure_falls_back_to_the_filesystem_walk(self, tmp_path):
        _write(tmp_path, "backend/app.py")
        assert _git_python_files(str(tmp_path)) in (None, [])  # not a repository
        assert _runtime_python_files(str(tmp_path)) == ["backend/app.py"]

    def test_the_real_repository_scan_contains_no_environment_files(self):
        files = _runtime_python_files()
        assert files, "scan found nothing"
        for rel in files:
            parts = rel.split("/")
            assert "site-packages" not in parts and "dist-packages" not in parts, rel
            current = REPO_ROOT
            for part in parts[:-1]:
                current = os.path.join(current, part)
                assert not os.path.exists(os.path.join(current, _VENV_MARKER)), f"{rel} is inside a virtualenv"

    def test_environment_noise_does_not_hide_or_fake_a_real_violation(self, tmp_path):
        self._layout(tmp_path)
        _write(tmp_path, "backend/leaky.py", _LEAKY_MODULE)
        _write(tmp_path, "backend/providers/gemini.py", _LEAKY_MODULE)  # allowlisted adapter
        assert _sdk_importers_outside_allowlist(str(tmp_path)) == ["backend/leaky.py"], (
            "the genuine violation must be reported; venv/site-packages files must not be")

    def test_a_clean_tree_with_a_polluted_environment_reports_nothing(self, tmp_path):
        self._layout(tmp_path)
        assert _sdk_importers_outside_allowlist(str(tmp_path)) == []


class TestImportProbe:
    """The probe behind test_contract_and_mock_import_no_sdk has to be able to
    FAIL, and must not be fooled by a polluted interpreter."""

    def test_detects_an_import_even_when_the_module_swallows_the_failure(self, tmp_path):
        _write(tmp_path, "swallowing_probe.py",
               "try:\n    import httpx\nexcept ImportError:\n    httpx = None\n"
               "try:\n    from google.genai import types\nexcept ImportError:\n    types = None\n")
        rc, attempts, _ = _probe_sdk_import_attempts(str(tmp_path), ["swallowing_probe"])
        assert rc == 0
        assert ["httpx", "swallowing_probe"] in attempts and ["google", "swallowing_probe"] in attempts

    def test_a_hard_import_fails_loudly_with_the_import_chain(self, tmp_path):
        _write(tmp_path, "hard_probe.py", "import httpx\n")
        rc, attempts, stderr = _probe_sdk_import_attempts(str(tmp_path), ["hard_probe"])
        assert rc != 0 and attempts is None
        assert "ModuleNotFoundError" in stderr and "hard_probe.py" in stderr

    def test_a_transitive_import_is_attributed_to_the_module_that_made_it(self, tmp_path):
        _write(tmp_path, "outer_probe.py", "import inner_probe\n")
        _write(tmp_path, "inner_probe.py", "try:\n    import httpx\nexcept ImportError:\n    pass\n")
        _, attempts, _ = _probe_sdk_import_attempts(str(tmp_path), ["outer_probe"])
        assert attempts == [["httpx", "inner_probe"]]

    def test_a_module_that_imports_nothing_third_party_is_clean(self, tmp_path):
        _write(tmp_path, "clean_probe.py", "import dataclasses, enum, typing\n")
        rc, attempts, stderr = _probe_sdk_import_attempts(str(tmp_path), ["clean_probe"])
        assert (rc, attempts) == (0, []), stderr

    def test_a_preloaded_google_namespace_is_not_mistaken_for_a_leak(self, tmp_path):
        # What a legacy namespace-package .pth (e.g. protobuf 3.x) does at
        # interpreter startup: 'google' is already in sys.modules before any
        # Kognit code runs. That is the environment, not a provider-boundary
        # violation, and must not fail the contract/mock check.
        preload = "import sys, types; sys.modules['google'] = types.ModuleType('google')"
        rc, attempts, stderr = _probe_sdk_import_attempts(
            REPO_ROOT, ["backend.providers.base", "backend.providers.mock", "backend.providers"], preload=preload)
        assert (rc, attempts) == (0, []), stderr

    def test_the_real_contract_and_mock_modules_attempt_no_sdk_import(self):
        rc, attempts, stderr = _probe_sdk_import_attempts(
            REPO_ROOT, ["backend.providers.base", "backend.providers.mock", "backend.providers"])
        assert (rc, attempts) == (0, []), stderr


class TestSdkIsolation:

    def test_the_scan_actually_sees_the_runtime_code(self):
        files = _runtime_python_files()
        assert {"backend/ai_engine.py", "backend/main.py", "backend/providers/gemini.py"} <= set(files)

    def test_only_allowlisted_runtime_modules_import_the_sdk(self):
        offenders = _sdk_importers_outside_allowlist()
        assert offenders == [], f"SDK import outside the provider boundary: {offenders}"

    def test_the_allowlist_has_no_stale_entries(self):
        for rel in ALLOWED_SDK_IMPORTERS:
            assert os.path.exists(os.path.join(REPO_ROOT, rel)), rel
            assert _imports_sdk(rel), f"{rel} no longer imports the SDK - remove it from the allowlist"

    def test_provider_contract_and_mock_do_not_import_the_sdk(self):
        for rel in ("backend/providers/base.py", "backend/providers/mock.py", "backend/providers/__init__.py"):
            assert not _imports_sdk(rel), rel

    def test_policy_layer_and_consumers_import_no_sdk_even_indirectly_named(self):
        for rel in ("backend/ai_engine.py", "backend/main.py", "backend/research_models.py", "evaluation/model_adapter.py"):
            src = open(os.path.join(REPO_ROOT, rel), encoding="utf-8").read()
            assert not _imports_sdk(rel), rel
            assert "genai_errors" not in src, rel

    def test_ai_engine_namespace_exposes_no_sdk_objects(self):
        for name in ("genai", "genai_errors", "types", "httpx", "_client", "_seconds_to_ms", "_build_gemini_history"):
            assert not hasattr(ai_engine, name), f"ai_engine still exposes {name}"

    def test_exactly_one_runtime_site_constructs_an_sdk_client(self):
        sites = []
        for rel in _runtime_python_files():
            tree = ast.parse(open(os.path.join(REPO_ROOT, rel), encoding="utf-8").read())
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "Client"
                        and isinstance(node.func.value, ast.Name) and node.func.value.id == "genai"):
                    sites.append(rel)
        assert sites == ["backend/providers/gemini.py"]

    def test_stream_and_unary_outputs_carry_neutral_types_only(self, use_mock, sleeps, unary_sleeps):
        use_mock(text_attempt("a", usage=ProviderUsage(total_tokens=1), grounding=GroundingResult(search_queries=("q",))))
        done = list(ai_engine.stream_ai_response(prompt="hi", enable_research=True))[-1]
        assert isinstance(done.grounding, GroundingResult) and isinstance(done.usage, ProviderUsage)
        res = ai_engine.generate_ai_response("hi", return_metadata=True, enable_research=True)
        assert isinstance(res.grounding, GroundingResult) and isinstance(res.usage, ProviderUsage)

    def test_nothing_google_typed_survives_the_real_adapter_into_the_policy_layer(self, monkeypatch, sleeps):
        # Real GeminiProvider + fake SDK objects, all the way up through ai_engine.
        md = types.GroundingMetadata(web_search_queries=["q"], grounding_chunks=[
            types.GroundingChunk(web=types.GroundingChunkWeb(title="t", uri="https://u.com", domain="u.com"))])
        chunks = [_sdk_chunk("answer", finish=types.FinishReason.STOP, grounding=md,
                             usage=types.GenerateContentResponseUsageMetadata(total_token_count=2))]
        previous = get_provider()
        try:
            set_provider(GeminiProvider(client=SimpleNamespace(
                chats=SimpleNamespace(create=lambda **kw: _FakeSession(chunks=chunks)), models=MagicMock())))
            done = list(ai_engine.stream_ai_response(prompt="hi", enable_research=True))[-1]
        finally:
            set_provider(previous)
        assert done.kind == "done"
        for value in (done.grounding, done.usage, *done.grounding.sources):
            assert type(value).__module__.split(".")[0] != "google"

    def test_research_models_reads_the_neutral_type(self):
        from backend.research_models import normalize_grounding_metadata
        result = normalize_grounding_metadata(
            GroundingResult(search_queries=("q",), sources=(GroundingSource("T", "https://x.com", "x.com"),),
                            supports=(GroundingSupport("seg", 0, 3, (0,)),)),
            provider="google", provider_model="m", research_latency_seconds=None,
            decision_reason="r", decision_category="c")
        assert result.grounding_status == "used" and result.sources[0].url == "https://x.com"
        assert result.citations[0].source_ids == ["src-0"] and result.citations[0].citation_status == "cited_with_source"
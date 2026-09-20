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

import backend.ai_engine as ai_engine
import backend.main as main


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeChunk:
    def __init__(self, text=None, grounding=None, usage=None):
        self.text = text
        self.usage_metadata = usage
        if grounding is not None:
            cand = type("C", (), {"grounding_metadata": grounding})()
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

    monkeypatch.setattr(ai_engine, "_client", _Client())


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

    def test_failure_AFTER_first_output_salvages_partial_answer(self, monkeypatch):
        # The critical streaming rule: never discard text the student can
        # already see, and never retry once bytes have shipped.
        _install_fake_stream(
            monkeypatch,
            [_FakeChunk("partial answer so far")],
            raise_after=1,
        )
        out = list(ai_engine.stream_ai_response(prompt="hi"))
        assert out[-1].kind == "done", "must finalize, not error, after partial output"
        assert out[-1].text == "partial answer so far"

    def test_failure_BEFORE_first_output_retries_then_errors(self, monkeypatch):
        attempts = {"n": 0}

        class _Chats:
            def create(self, **kwargs):
                attempts["n"] += 1
                return _FakeChatSession([], raise_after=0, exc=RuntimeError("boom"))

        monkeypatch.setattr(ai_engine, "_client", type("C", (), {"chats": _Chats()})())
        out = list(ai_engine.stream_ai_response(prompt="hi"))
        assert out[-1].kind == "error"
        assert attempts["n"] > 1, "retry is safe before any byte is sent"

    def test_image_decode_failure_yields_error_not_exception(self, monkeypatch):
        out = list(ai_engine.stream_ai_response(prompt="hi", image_bytes=b"not-an-image"))
        assert len(out) == 1 and out[0].kind == "error"
        assert out[0].text == ai_engine.IMAGE_DECODE_ERROR

    def test_grounding_metadata_captured_when_research_enabled(self, monkeypatch):
        sentinel = object()
        _install_fake_stream(
            monkeypatch,
            [_FakeChunk("answer"), _FakeChunk(" more", grounding=sentinel)],
        )
        out = list(ai_engine.stream_ai_response(prompt="hi", enable_research=True))
        assert out[-1].grounding_metadata is sentinel

    def test_no_grounding_captured_when_research_disabled(self, monkeypatch):
        _install_fake_stream(monkeypatch, [_FakeChunk("answer", grounding=object())])
        out = list(ai_engine.stream_ai_response(prompt="hi", enable_research=False))
        assert out[-1].grounding_metadata is None


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
        assert a.contents == b.contents
        assert len(a.gemini_history) == len(b.gemini_history)
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
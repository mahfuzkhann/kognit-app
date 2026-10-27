"""
Real-HTTP regression tests: the application is served by an actual uvicorn server
(on 127.0.0.1, ephemeral port, in a background thread) and exercised with httpx, so the
multipart parser, upload limits, streaming and the quota settlement run through the real
network stack instead of the in-process TestClient.

Added with the FastAPI/Starlette security upgrade (audit finding F-03). On the previous
pins (fastapi 0.110.1 / starlette 0.37.2) this exact probe ACCEPTED a 2 MB text form field
and a 2,500-field form (HTTP 200); the pinned versions reject both with HTTP 400
(CVE-2024-47874 and CVE-2026-54283: unbounded memory for fields without a filename).

Nothing external is contacted: the AI provider is the scripted MockProvider, Supabase calls
are replaced, and quotas use the in-memory ledger from test_quota_settlement.
"""
import io
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from PIL import Image

import backend.main as main
from backend.providers import set_provider
from backend.providers.base import ProviderErrorKind
from backend.providers.mock import MockProvider, failing_attempt, text_attempt

from test_p1_hardening import make_pdf
from test_quota_settlement import GatedProvider, use_ledger


@pytest.fixture(scope="module")
def server():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    srv = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(base + "/healthz", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        pytest.fail("test server did not start")
    yield base
    srv.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(autouse=True)
def student(monkeypatch):
    async def user_and_token():
        return ("user-1", "token-1")

    async def user_id():
        return "user-1"

    async def ctx(**kwargs):
        return ("Class 9-10 (SSC)", "Science")

    async def name(**kwargs):
        return None

    overrides = {
        main._rate_limited_chat: user_and_token,
        main._rate_limited_quiz_generation: user_and_token,
        main._rate_limited_pdf_upload: user_id,
        main.get_current_user_id: user_id,
        main.get_current_user_and_token: user_and_token,
    }
    main.app.dependency_overrides.update(overrides)
    monkeypatch.setattr(main, "_get_academic_context", ctx)
    monkeypatch.setattr(main, "_get_student_display_name", name)
    main.active_pdf_contexts.clear()
    yield
    for key in overrides:
        main.app.dependency_overrides.pop(key, None)
    set_provider(None)


@pytest.fixture
def http(server):
    with httpx.Client(base_url=server, timeout=60) as client:
        yield client


def png_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 30, 30)).save(buf, "PNG")
    return buf.getvalue()


def stream_events(http, **kwargs):
    with http.stream("POST", "/api/chat/stream", **kwargs) as resp:
        lines = [json.loads(line) for line in resp.iter_lines() if line]
    return resp.status_code, lines


class TestUploadsOverRealHttp:
    def test_a_valid_pdf_is_accepted_and_its_text_extracted(self, http):
        pdf = make_pdf(["Newton first law of motion", "Second law is F equals ma"])
        resp = http.post("/api/pdf/upload", data={"chat_id": "c1"}, files={"file": ("chapter.pdf", pdf, "application/pdf")})
        assert resp.status_code == 200, resp.text

    def test_an_oversized_pdf_is_refused_with_413(self, http):
        big = b"%PDF-" + b"0" * (main.MAX_PDF_UPLOAD_SIZE_BYTES + 1)
        resp = http.post("/api/pdf/upload", data={"chat_id": "c1"}, files={"file": ("big.pdf", big, "application/pdf")})
        assert resp.status_code == 413

    def test_a_file_that_is_not_a_pdf_is_refused_without_internals(self, http):
        resp = http.post("/api/pdf/upload", data={"chat_id": "c1"}, files={"file": ("a.pdf", b"hello", "application/pdf")})
        assert resp.status_code == 400
        assert "Traceback" not in resp.text

    def test_a_valid_image_is_streamed_back_as_separate_events(self, http):
        mock = MockProvider([text_attempt("Hello ", "there ", "student.")])
        set_provider(mock)
        status, events = stream_events(
            http, data={"prompt": "What is this?", "chat_id": "c1"}, files={"image": ("p.png", png_bytes(), "image/png")}
        )
        assert status == 200
        kinds = [e["type"] for e in events]
        assert kinds[0] == "start" and kinds[-1] == "done" and kinds.count("delta") == 3
        assert events[-1]["reply"] == "Hello there student."
        images = [part for part in mock.requests[0].user_parts if isinstance(part, Image.Image)]
        assert images and images[0].size == (64, 64), "the uploaded image reached the provider decoded and intact"

    def test_an_oversized_image_is_refused_with_413(self, http):
        set_provider(MockProvider([text_attempt("never")]))
        resp = http.post(
            "/api/chat/stream", data={"prompt": "x", "chat_id": "c1"},
            files={"image": ("p.png", b"0" * (main.MAX_IMAGE_UPLOAD_SIZE_BYTES + 1), "image/png")},
        )
        assert resp.status_code == 413

    def test_a_corrupt_image_is_a_clean_error_event(self, http):
        mock = MockProvider([text_attempt("never")])
        set_provider(mock)
        status, events = stream_events(
            http, data={"prompt": "x", "chat_id": "c1"}, files={"image": ("p.png", b"not really an image", "image/png")}
        )
        assert status == 200 and events[-1]["type"] == "error" and events[-1]["code"] == "REQUEST_INVALID"
        assert mock.call_count == 0


class TestFormParserLimits:
    """CVE-2024-47874 / CVE-2026-54283: before the upgrade these were all accepted (HTTP 200)."""

    def test_a_huge_text_field_in_a_multipart_form_is_rejected(self, http):
        set_provider(MockProvider([text_attempt("never")]))
        resp = http.post(
            "/api/chat/stream", data={"prompt": "a" * (2 * 1024 * 1024), "chat_id": "c1"},
            files={"x": ("n.txt", b"1", "text/plain")},
        )
        assert resp.status_code == 400

    def test_a_huge_urlencoded_field_is_rejected(self, http):
        set_provider(MockProvider([text_attempt("never")]))
        resp = http.post("/api/chat/stream", data={"prompt": "a" * (2 * 1024 * 1024), "chat_id": "c1"})
        assert resp.status_code == 400

    def test_a_form_with_thousands_of_fields_is_rejected(self, http):
        set_provider(MockProvider([text_attempt("never")]))
        fields = {f"f{i}": "v" for i in range(2500)}
        fields.update(prompt="hi", chat_id="c1")
        assert http.post("/api/chat/stream", data=fields).status_code == 400


class TestAuthOverRealHttp:
    def test_a_request_without_a_token_is_unauthorised(self, http):
        for dep in (main._rate_limited_chat, main.get_current_user_and_token):
            main.app.dependency_overrides.pop(dep, None)
        resp = http.post("/api/chat/stream", data={"prompt": "x", "chat_id": "c1"})
        assert resp.status_code == 401 and "Traceback" not in resp.text


class TestQuotaSettlementOverRealHttp:
    def test_failure_before_any_text_is_refunded(self, http, monkeypatch):
        ledger = use_ledger(monkeypatch)
        set_provider(MockProvider([failing_attempt(ProviderErrorKind.UNAVAILABLE, http_status=503)]))
        status, events = stream_events(http, data={"prompt": "x", "chat_id": "c1"})
        assert status == 200 and events[-1]["type"] == "error"
        deadline = time.time() + 5
        while ledger.live("chat") and time.time() < deadline:
            time.sleep(0.05)
        assert ledger.live("chat") == 0 and len(ledger.named("refund_quota")) == 1

    def test_failure_after_text_is_not_refunded(self, http, monkeypatch):
        ledger = use_ledger(monkeypatch)
        set_provider(MockProvider([failing_attempt(ProviderErrorKind.UNAVAILABLE, http_status=503, after_text=["Partial "])]))
        status, events = stream_events(http, data={"prompt": "x", "chat_id": "c1"})
        assert events[-1]["type"] == "interrupted"
        time.sleep(0.5)   # give the background settlement time to (wrongly) refund
        assert ledger.named("refund_quota") == [] and ledger.live("chat") == 1

    def test_a_real_client_disconnect_after_text_is_not_refunded(self, http, monkeypatch):
        ledger = use_ledger(monkeypatch)
        release = threading.Event()
        set_provider(GatedProvider("Hello", ["world"], release))
        try:
            with http.stream("POST", "/api/chat/stream", data={"prompt": "x", "chat_id": "c1"}) as resp:
                for line in resp.iter_lines():
                    if line and json.loads(line)["type"] == "delta":
                        break                       # leaving the block closes the connection mid-stream
        finally:
            release.set()
        time.sleep(1.0)
        assert ledger.named("refund_quota") == [] and ledger.live("chat") == 1

    def test_an_exception_after_consumption_is_a_generic_500_and_refunds(self, server, monkeypatch):
        ledger = use_ledger(monkeypatch)

        async def boom(**kwargs):
            raise RuntimeError("profile lookup exploded: secret internals")
        monkeypatch.setattr(main, "_get_academic_context", boom)
        set_provider(MockProvider([text_attempt("never")]))
        resp = httpx.post(server + "/api/chat/stream", data={"prompt": "x", "chat_id": "c1"}, timeout=30)
        assert resp.status_code == 500
        assert "exploded" not in resp.text and "Traceback" not in resp.text
        assert ledger.live("chat") == 0 and len(ledger.named("refund_quota")) == 1
"""
Final Free / Pro plan (migration 0016) - tested THROUGH the real FastAPI routes.

What is proven here (the database half - lifetime trial, Dhaka day/week boundaries, atomic concurrency,
refunds, RLS on Pocket Notes - is proven against a real PostgreSQL by supabase/tests/plan_final_0016_test.sql
and supabase/tests/concurrency_photo_pdf_test.sh):

  * a request carrying a photo spends the PHOTO SOLVE meter, never a chat - on BOTH chat endpoints;
  * a photo of 3 MiB or more is refused (413) BEFORE any quota is spent; one byte less is accepted;
  * PDF upload is Pro-only (403 PLAN_REQUIRED, body never read), judged on the REAL page count (<= 5),
    spends the Pro PDF allowance only for a valid upload, and gives it back if the upload then fails;
  * Pocket Notes are Pro-only server-side (covered in test_engine2_entitlements.py), existing notes stay;
  * the server - never the client - decides the model: Free never reaches the advanced model, not on the
    streaming path, not on the non-streaming path, not for quizzes, not through a fallback;
  * a client-supplied plan / model / kind field is ignored;
  * an expired Pro subscription is Free again, everywhere.
"""
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import backend.main as main
from backend import ai_engine, entitlements, quota_service, usage_collector
from backend.ai import router
from backend.providers import set_provider
from backend.providers.mock import MockProvider, text_attempt

from test_pre_phase10_endpoints import _app_state, client, events, post_stream  # noqa: F401
from test_quota_settlement import LedgerQuota

GENERAL = "gemini-3.1-flash-lite"
ADVANCED = "gemini-3.5-flash-lite"


# ------------------------------------------------------------------ harness
class PlanLedger(LedgerQuota):
    """LedgerQuota whose consume decisions report the ledger's CURRENT plan (the database's answer), and
    which models the lifetime Photo Solve trial (Free: 1 for ever; Pro: 5 a day)."""

    def __init__(self, plan="free", **kw):
        super().__init__(plan=plan, **kw)
        self.limits.update({"chat": 1 if plan == "free" else 20, "photo_solve": 1 if plan == "free" else 5,
                            "quiz": 1 if plan == "free" else 5, "pdf_upload": 2})
        self.limits.update(kw.get("limits") or {})

    async def rpc(self, user_token, function, params):
        if function == "consume_quota" and params["p_kind"] == "pdf_upload" and self.plan != "pro":
            return {"allowed": False, "reason": "plan_not_configured", "plan": self.plan, "kind": "pdf_upload"}
        result = await super().rpc(user_token, function, params)
        if function == "consume_quota" and isinstance(result, dict):
            result = dict(result, plan=self.plan)
            if params["p_kind"] == "photo_solve" and self.plan == "free":
                result["resets_at"] = None                       # a lifetime allowance has no reset time
        return result


@pytest.fixture
def ledger(monkeypatch):
    monkeypatch.setenv("QUOTA_MODE", "enforce")
    led = PlanLedger()
    monkeypatch.setattr(quota_service, "_rpc", led.rpc)
    monkeypatch.setenv("AI_ROUTER_MODE", "policy")
    yield led
    set_provider(None)
    usage_collector._current.set(None)


def png(extra_bytes=0):
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue() + b"\x00" * extra_bytes


def kinds(led):
    return [c["params"]["p_kind"] for c in led.named("consume_quota")]


def mock_provider(*attempts):
    m = MockProvider(list(attempts) or [text_attempt("ok")] * 5)
    set_provider(m)
    return m


def make_pdf(pages: int) -> bytes:
    """A real, minimal text PDF with `pages` pages (so the REAL page counter and extractor run)."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [%s] /Count %d >>" % (" ".join(f"{3 + 2 * i} 0 R" for i in range(pages)), pages)]
    for i in range(pages):
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] "
                    f"/Resources << /Font << /F1 {3 + 2 * pages} 0 R >> >> /Contents {4 + 2 * i} 0 R >>")
        stream = f"BT /F1 14 Tf 20 100 Td (Page {i + 1} force and motion) Tj ET"
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    objs.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out, offsets = b"%PDF-1.4\n", []
    for n, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n{obj}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def upload(client, pdf, name="notes.pdf", chat_id="chat-1"):
    return client.post("/api/pdf/upload", data={"chat_id": chat_id}, files={"file": (name, pdf, "application/pdf")})


# ======================================================== Photo Solve meter
class TestPhotoSolveMeter:
    def test_a_streamed_photo_spends_the_photo_solve_meter_not_a_chat(self, client, ledger):
        mock_provider(text_attempt("It is a diagram."))
        resp = post_stream(client, prompt="what is this?") if False else client.post(
            "/api/chat/stream", data={"prompt": "what is this?", "chat_id": "c1"},
            files={"image": ("a.png", png(), "image/png")})
        assert resp.status_code == 200 and events(resp)[-1]["type"] == "done"
        assert kinds(ledger) == ["photo_solve"] and ledger.live("chat") == 0 and ledger.live("photo_solve") == 1

    def test_a_non_streamed_photo_spends_the_same_meter(self, client, ledger):
        mock_provider(text_attempt("It is a diagram."))
        resp = client.post("/api/chat", data={"prompt": "what is this?", "chat_id": "c1"},
                           files={"image": ("a.png", png(), "image/png")})
        assert resp.status_code == 200 and kinds(ledger) == ["photo_solve"]

    def test_a_text_request_still_spends_a_chat(self, client, ledger):
        mock_provider(text_attempt("Force is a push or a pull."))
        assert post_stream(client).status_code == 200
        assert kinds(ledger) == ["chat"]

    @pytest.mark.parametrize("path", ["/api/chat/stream", "/api/chat"])
    def test_the_second_free_photo_is_refused_on_both_endpoints_and_never_reaches_the_provider(self, client, ledger, path):
        mock = mock_provider()
        files = {"image": ("a.png", png(), "image/png")}
        first = client.post(path, data={"prompt": "q", "chat_id": "c1"}, files=files)
        assert first.status_code == 200
        calls = mock.call_count
        second = client.post(path, data={"prompt": "q again", "chat_id": "c1"}, files={"image": ("a.png", png(), "image/png")})
        assert second.status_code == 429
        detail = second.json()["detail"]
        assert detail["code"] == "QUOTA_EXCEEDED" and detail["kind"] == "photo_solve" and detail["plan"] == "free"
        assert detail["limit"] == 1 and detail["resets_at"] is None                 # a lifetime allowance never resets
        assert "one free Photo Solve" in detail["message"]
        assert mock.call_count == calls

    def test_the_trial_does_not_touch_the_daily_chat_allowance(self, client, ledger):
        mock_provider()
        assert client.post("/api/chat/stream", data={"prompt": "q", "chat_id": "c1"},
                           files={"image": ("a.png", png(), "image/png")}).status_code == 200
        assert post_stream(client).status_code == 200                                # today's one chat is still there
        assert kinds(ledger) == ["photo_solve", "chat"]

    def test_a_pro_student_gets_five_a_day_and_the_sixth_is_refused(self, client, monkeypatch):
        monkeypatch.setenv("QUOTA_MODE", "enforce")
        led = PlanLedger(plan="pro")
        monkeypatch.setattr(quota_service, "_rpc", led.rpc)
        mock_provider()
        for i in range(5):
            r = client.post("/api/chat/stream", data={"prompt": f"q{i}", "chat_id": "c1"}, files={"image": ("a.png", png(), "image/png")})
            assert r.status_code == 200, i
        sixth = client.post("/api/chat/stream", data={"prompt": "q6", "chat_id": "c1"}, files={"image": ("a.png", png(), "image/png")})
        assert sixth.status_code == 429 and sixth.json()["detail"]["kind"] == "photo_solve" and sixth.json()["detail"]["limit"] == 5
        assert led.live("photo_solve") == 5 and led.live("chat") == 0

    def test_a_photo_that_cannot_be_read_gives_the_trial_back(self, client, ledger):
        mock = mock_provider()
        r = client.post("/api/chat/stream", data={"prompt": "q", "chat_id": "c1"},
                        files={"image": ("bad.png", b"definitely not an image", "image/png")})
        assert events(r)[-1]["code"] == "REQUEST_INVALID" and mock.call_count == 0
        assert ledger.live("photo_solve") == 0, "the one-time trial must not be burned by a corrupt file"


# ============================================================= photo size
class TestPhotoSizeLimit:
    LIMIT = 3 * 1024 * 1024

    def test_the_limit_is_exactly_three_mib_exclusive(self):
        assert entitlements.PHOTO_MAX_BYTES == self.LIMIT - 1 == main.MAX_IMAGE_UPLOAD_SIZE_BYTES

    @pytest.mark.parametrize("path", ["/api/chat/stream", "/api/chat"])
    @pytest.mark.parametrize("size", [LIMIT, LIMIT + 1, 8 * 1024 * 1024])
    def test_a_photo_at_or_above_3mb_is_refused_before_any_quota_is_spent(self, client, ledger, path, size):
        mock = mock_provider()
        r = client.post(path, data={"prompt": "q", "chat_id": "c1"}, files={"image": ("big.png", png(size - len(png())), "image/png")})
        assert r.status_code == 413 and r.json()["detail"] == "Image must be smaller than 3MB."
        assert ledger.calls == [] and mock.call_count == 0, "a refused upload costs nothing"

    @pytest.mark.parametrize("path", ["/api/chat/stream", "/api/chat"])
    def test_a_photo_just_under_3mb_is_accepted(self, client, ledger, path):
        mock_provider()
        data = png(self.LIMIT - 1 - len(png()))
        assert len(data) == self.LIMIT - 1
        r = client.post(path, data={"prompt": "q", "chat_id": "c1"}, files={"image": ("ok.png", data, "image/png")})
        assert r.status_code == 200
        assert kinds(ledger) == ["photo_solve"]


# ================================================================ PDF upload
class TestPdfUpload:
    def test_a_free_student_is_refused_before_the_body_is_read_or_any_quota_is_touched(self, client, ledger, monkeypatch):
        called = []
        monkeypatch.setattr(main, "count_pdf_pages", lambda b: called.append(1) or 1)
        r = upload(client, make_pdf(1))
        assert r.status_code == 403
        d = r.json()["detail"]
        assert d["code"] == "PLAN_REQUIRED" and d["feature"] == "pdf_upload" and d["required_plan"] == "pro"
        assert called == [] and ledger.named("consume_quota") == []
        assert main.active_pdf_contexts == {}, "nothing was stored"

    def test_the_client_cannot_claim_a_plan_to_upload(self, client, ledger):
        r = client.post("/api/pdf/upload", data={"chat_id": "c1", "plan": "pro", "tier": "pro"},
                        headers={"X-Plan": "pro", "X-User-Plan": "pro"}, params={"plan": "pro"},
                        files={"file": ("a.pdf", make_pdf(1), "application/pdf")})
        assert r.status_code == 403 and r.json()["detail"]["code"] == "PLAN_REQUIRED"

    @pytest.fixture
    def pro(self, monkeypatch):
        monkeypatch.setenv("QUOTA_MODE", "enforce")
        led = PlanLedger(plan="pro")
        monkeypatch.setattr(quota_service, "_rpc", led.rpc)
        return led

    @pytest.mark.parametrize("pages", [1, 5])
    def test_a_pro_pdf_of_up_to_five_real_pages_is_accepted_and_counted_once(self, client, pro, pages):
        r = upload(client, make_pdf(pages))
        assert r.status_code == 200 and r.json()["status"] == "success" and r.json()["length"] > 0
        assert kinds(pro) == ["pdf_upload"] and pro.live("pdf_upload") == 1
        assert main.active_pdf_contexts["user-1"]["chat-1"], "the text is available for this chat"

    def test_a_six_page_pdf_is_refused_with_a_stable_413_and_costs_nothing(self, client, pro):
        r = upload(client, make_pdf(6))
        assert r.status_code == 413
        d = r.json()["detail"]
        assert d["code"] == "PDF_PAGE_LIMIT_EXCEEDED" and d["max_pages"] == 5 and d["pages"] == 6
        assert pro.named("consume_quota") == [] and main.active_pdf_contexts == {}

    def test_the_page_count_is_read_from_the_file_not_from_the_client(self, client, pro):
        # the filename and every form field say "5 pages"; the file has 6
        r = client.post("/api/pdf/upload", data={"chat_id": "c1", "pages": "5", "page_count": "5"},
                        files={"file": ("five-pages.pdf", make_pdf(6), "application/pdf")})
        assert r.status_code == 413

    def test_the_third_upload_of_the_day_is_refused(self, client, pro):
        assert upload(client, make_pdf(1), chat_id="c1").status_code == 200
        assert upload(client, make_pdf(2), chat_id="c2").status_code == 200
        third = upload(client, make_pdf(1), chat_id="c3")
        assert third.status_code == 429
        d = third.json()["detail"]
        assert d["code"] == "QUOTA_EXCEEDED" and d["kind"] == "pdf_upload" and d["plan"] == "pro" and d["limit"] == 2
        assert pro.live("pdf_upload") == 2 and "c3" not in main.active_pdf_contexts.get("user-1", {})

    def test_a_filename_is_not_proof_of_a_pdf(self, client, pro):
        r = upload(client, b"MZ\x90\x00 this is an executable, not a PDF", name="report.pdf")
        assert r.status_code == 400 and pro.named("consume_quota") == []

    def test_a_corrupt_pdf_that_looks_like_one_costs_nothing(self, client, pro):
        r = upload(client, b"%PDF-1.4\n" + b"\xff" * 4000)
        assert 400 <= r.status_code < 500 and pro.named("consume_quota") == []

    def test_an_upload_that_fails_after_being_counted_gives_the_unit_back(self, client, pro, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("parser exploded")
        monkeypatch.setattr(main, "extract_text_from_pdf", boom)
        r = upload(client, make_pdf(1))
        assert r.status_code == 500 and "parser exploded" not in r.text
        assert kinds(pro) == ["pdf_upload"] and len(pro.named("refund_quota")) == 1 and pro.live("pdf_upload") == 0

    def test_a_pdf_with_no_readable_text_gives_the_unit_back(self, client, pro, monkeypatch):
        def empty(*a, **k):
            raise main.PDFExtractionError("This PDF has no readable text.")
        monkeypatch.setattr(main, "extract_text_from_pdf", empty)
        r = upload(client, make_pdf(1))
        assert r.status_code == 400 and pro.live("pdf_upload") == 0

    def test_a_quota_backend_outage_fails_closed_and_stores_nothing(self, client, monkeypatch):
        monkeypatch.setenv("QUOTA_MODE", "enforce")
        led = PlanLedger(plan="pro", fail_consume=True)
        monkeypatch.setattr(quota_service, "_rpc", led.rpc)
        r = upload(client, make_pdf(1))
        assert r.status_code == 503 and r.json()["detail"]["code"] == "QUOTA_UNAVAILABLE"
        assert main.active_pdf_contexts == {}

    def test_an_expired_pro_subscription_loses_pdf_upload(self, client, pro):
        assert upload(client, make_pdf(1)).status_code == 200
        pro.plan = "free"                                                       # the subscription lapsed
        assert upload(client, make_pdf(1), chat_id="c2").status_code == 403


# ========================================================== model access
class TestModelAccessIsDecidedByTheServer:
    def _stream(self, client, **extra):
        data = {"prompt": "Solve the equation x^2 - 5x + 6 = 0", "chat_id": "c1"}
        data.update(extra)
        return client.post("/api/chat/stream", data=data)

    def test_a_free_maths_question_runs_on_the_general_model(self, client, ledger):
        mock = mock_provider()
        assert self._stream(client).status_code == 200
        assert [r.model for r in mock.requests] == [GENERAL]

    def test_a_pro_maths_question_runs_on_the_advanced_model(self, client, monkeypatch):
        monkeypatch.setenv("QUOTA_MODE", "enforce")
        led = PlanLedger(plan="pro")
        monkeypatch.setattr(quota_service, "_rpc", led.rpc)
        monkeypatch.setenv("AI_ROUTER_MODE", "policy")
        mock = mock_provider()
        assert self._stream(client).status_code == 200
        assert [r.model for r in mock.requests] == [ADVANCED]

    @pytest.mark.parametrize("field", ["model", "model_id", "model_name", "plan", "tier", "kind", "advanced"])
    def test_a_client_supplied_model_or_plan_is_ignored(self, client, ledger, field):
        mock = mock_provider()
        value = ADVANCED if "model" in field else ("pro" if field in ("plan", "tier", "kind") else "true")
        r = self._stream(client, **{field: value})
        assert r.status_code == 200 and [m.model for m in mock.requests] == [GENERAL]
        assert kinds(ledger) == ["chat"], "the client cannot choose the meter either"

    def test_the_non_streaming_endpoint_follows_the_same_model_policy(self, client, ledger):
        mock = mock_provider()
        r = client.post("/api/chat", data={"prompt": "Solve the equation x^2 - 5x + 6 = 0", "chat_id": "c1", "model": ADVANCED})
        assert r.status_code == 200 and [m.model for m in mock.requests] == [GENERAL]

    def test_a_free_photo_solve_uses_the_general_model(self, client, ledger):
        mock = mock_provider()
        client.post("/api/chat/stream", data={"prompt": "what is this?", "chat_id": "c1"},
                    files={"image": ("a.png", png(), "image/png")})
        assert [m.model for m in mock.requests] == [GENERAL]

    def test_a_free_quiz_uses_the_general_model(self, client, ledger):
        quiz = ('[{"id":1,"question":"Which law relates force and acceleration?","options":["Newton 1","Newton 2","Newton 3","Ohm"],'
                '"correct_index":1,"explanation":"F = ma"}]')
        mock = mock_provider(text_attempt(quiz))
        r = client.post("/api/quiz/generate", data={"subject": "Physics", "topic": "Newton's laws of motion", "count": 1})
        assert r.status_code == 200 and [m.model for m in mock.requests] == [GENERAL]

    def test_a_free_students_429_is_not_retried_on_the_advanced_model(self, client, ledger, monkeypatch):
        from backend.providers.base import ProviderErrorKind
        from backend.providers.mock import failing_attempt
        monkeypatch.setenv("AI_MODEL_FALLBACK", "on")
        mock = mock_provider(failing_attempt(ProviderErrorKind.RATE_LIMITED, http_status=429), text_attempt("x"))
        self._stream(client)
        assert [m.model for m in mock.requests] == [GENERAL]

    def test_an_expired_pro_student_is_back_on_the_general_model(self, client, monkeypatch):
        monkeypatch.setenv("QUOTA_MODE", "enforce")
        led = PlanLedger(plan="pro")
        monkeypatch.setattr(quota_service, "_rpc", led.rpc)
        monkeypatch.setenv("AI_ROUTER_MODE", "policy")
        mock = mock_provider()
        self._stream(client)
        led.plan = "free"
        led.limits["chat"] = 5
        self._stream(client)
        assert [m.model for m in mock.requests] == [ADVANCED, GENERAL]


class TestThePlanModelGateItself:
    """plan_model_for_current_request() is what the NON-routed calls (non-streaming chat, quiz, title, notes AI)
    use. The plan it reads is the one the endpoint recorded from the database on the request's collector."""

    @pytest.fixture(autouse=True)
    def _reset(self):
        usage_collector._current.set(None)
        yield
        usage_collector._current.set(None)

    def test_with_no_request_context_the_configured_model_is_unchanged(self):
        assert ai_engine.plan_model_for_current_request() == ai_engine.MODEL_NAME
        assert ai_engine.plan_model_for_current_request(ADVANCED) == ADVANCED       # offline evaluation keeps working

    @pytest.mark.parametrize("plan,expected", [("pro", ADVANCED), ("free", GENERAL), ("unmetered", GENERAL), (None, GENERAL)])
    def test_inside_a_request_only_pro_may_use_the_advanced_model(self, plan, expected):
        collector = usage_collector.start_collection("rid-plan", "")
        collector.plan = plan
        assert ai_engine.plan_model_for_current_request(ADVANCED) == expected
        assert ai_engine.plan_model_for_current_request(GENERAL) == GENERAL

    def test_the_gate_is_the_router_gate(self):
        assert router.plan_safe_model_id("free", ADVANCED) == GENERAL and router.plan_safe_model_id("pro", ADVANCED) == ADVANCED


# ===================================================== policy single source
class TestPolicyIsOneSourceOfTruth:
    def test_every_new_kind_is_known_to_the_application(self):
        for kind in ("photo_solve", "pdf_upload"):
            assert kind in quota_service.VALID_KINDS
            assert kind in quota_service._KIND_NOUN and kind in quota_service._EXCEEDED_MESSAGES

    def test_the_two_new_pro_features_are_gated_and_have_labels(self):
        for feature in (entitlements.POCKET_NOTES, entitlements.PDF_UPLOAD):
            assert feature in entitlements.PRO_FEATURES
            assert not entitlements.is_allowed("free", feature) and entitlements.is_allowed("pro", feature)
            assert entitlements.plan_required_exception(feature).detail["message"].endswith("is available on the Pro plan.")

    def test_the_advertised_multipliers_still_match_the_numbers(self):
        free, pro = entitlements.QUOTA_POLICY["free"], entitlements.QUOTA_POLICY["pro"]
        assert pro["chat"][1] // free["chat"][1] == 20                       # "20x more usage limit"
        assert pro["quiz"][1] * 7 // free["quiz"][1] == 35                   # "35x more quizzes" (5 a day x 7 vs 1 a week)

    def test_unknown_plans_fail_closed_for_the_new_features(self):
        for plan in (None, "", "admin", "PRO", "enterprise"):
            assert not entitlements.is_allowed(plan, entitlements.PDF_UPLOAD)
            assert not entitlements.is_allowed(plan, entitlements.POCKET_NOTES)


class TestPriceMirror:
    def test_the_application_price_matches_the_database_after_migration_0016(self):
        import pathlib
        import re
        from backend import payments
        root = pathlib.Path(__file__).parent / "supabase/migrations"
        executable = "\n".join(l for l in (root / "0016_free_pro_plan_final.sql").read_text().splitlines()
                               if not l.lstrip().startswith("--"))          # the ROLLBACK note mentions the old 299
        m = re.search(r"update public\.plan_prices set amount_bdt = (\d+) where plan = 'pro' and billing_period = 'monthly'", executable)
        assert m and int(m.group(1)) == payments.PRO_PRICES_BDT[payments.BILLING_MONTHLY] == 399
        seed = (root / "0008_plans_quota_subscriptions.sql").read_text()
        assert re.search(r"'pro',\s*'yearly',\s*" + str(payments.PRO_PRICES_BDT[payments.BILLING_YEARLY]) + r"\b", seed)
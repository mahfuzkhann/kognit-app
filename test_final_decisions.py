"""
AI Engine 2.0 - the CEO's FINAL decisions, proved through the real routes.

  A. Free quiz: 3 per week, structured refusal, one policy for every quiz-generation entry point
  B. Pro flashcards: own meter, internal guard, "Unlimited flashcards" in everything the student sees
  C. Learning Intelligence: the five exact Pro-only endpoints
  D. PDF document budget: 300,000 characters, never silent, never assumed safe
  E. The UI copy and the backend policy cannot drift apart

Database-level proofs (weekly Asia/Dhaka boundary, 24-way concurrency, the refund rule on the real
SQL) live in supabase/tests/quota_weekly_and_flashcards_test.sql and concurrency_quiz_flashcards_test.sh.
"""
import inspect
import json
import logging
import pathlib
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import backend.main as main
from backend import entitlements, quota_service
from backend.ai import prompts
from backend import ai_engine

from test_notes_endpoints import (  # noqa: F401  (fixtures + helpers of the notes harness)
    ALICE, FLASH, QUIZ, FakeNotesDB, as_user, clean_state, db, save, stub_ai,
)
from test_quota_settlement import LedgerQuota, use_ledger

ROOT = pathlib.Path(__file__).parent
PROFILE = {"user_id": "user-1", "name": "T", "user_class": "Class 11-12 (HSC)",
           "stream": "Science (বিজ্ঞান)", "created_at": "x", "updated_at": "x"}
SAMPLE_QUESTIONS = [{"question": "2+2?", "options": ["3", "4"], "correct_index": 1, "explanation": "math"}]


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture(autouse=True)
def _clean_overrides():
    yield
    main.app.dependency_overrides.clear()


def auth(user_id="user-1", token="tok"):
    async def _both():
        return (user_id, token)

    async def _uid():
        return user_id
    main.app.dependency_overrides[main.get_current_user_and_token] = _both
    main.app.dependency_overrides[main.get_current_user_id] = _uid


def provider_text(text):
    return stub_ai(generate=MagicMock(return_value=MagicMock(text=text)))


def consumed(ledger, kind=None):
    rows = [c["params"]["p_kind"] for c in ledger.named("consume_quota")]
    return rows if kind is None else [r for r in rows if r == kind]


@pytest.fixture
def quiz_env():
    auth()
    with patch.object(main, "generate_quiz_questions", return_value=SAMPLE_QUESTIONS) as gen, \
         patch.object(main, "get_student_profile", new=AsyncMock(return_value=PROFILE)):
        yield gen


def gen_quiz(client, **extra):
    return client.post("/api/quiz/generate", data={"subject": "Physics", "topic": "Motion", "count": 5, **extra})


# ================================ A. Free weekly quiz ========================
class TestFreeQuiz:
    def test_three_in_a_week_then_a_structured_refusal(self, client, quiz_env, monkeypatch):
        ledger = use_ledger(monkeypatch, plan="free")           # model default: quiz 3
        for n in range(3):
            assert gen_quiz(client).status_code == 200, n
        fourth = gen_quiz(client)
        assert fourth.status_code == 429
        detail = fourth.json()["detail"]
        assert detail["code"] == "QUOTA_EXCEEDED" and detail["kind"] == "quiz"
        assert detail.get("message") and "reset" in detail["message"].lower()
        assert quiz_env.call_count == 3                          # the model is never called for the refused one
        assert consumed(ledger, "quiz") == ["quiz"] * 4          # the 4th was asked, counted nowhere, refused

    def test_a_failed_generation_is_refunded_so_the_allowance_is_not_wasted(self, client, quiz_env, monkeypatch):
        ledger = use_ledger(monkeypatch, plan="free")
        quiz_env.return_value = []                                # the model produced nothing usable
        failed = gen_quiz(client)
        assert failed.status_code >= 500 or failed.json().get("questions") in (None, [])
        assert len(ledger.named("refund_quota")) == 1 and ledger.live("quiz") == 0
        quiz_env.return_value = SAMPLE_QUESTIONS
        for n in range(3):                                        # all three weekly quizzes are still available
            assert gen_quiz(client).status_code == 200, n
        assert gen_quiz(client).status_code == 429

    def test_a_successful_quiz_is_never_refunded(self, client, quiz_env, monkeypatch):
        ledger = use_ledger(monkeypatch, plan="free")
        assert gen_quiz(client).status_code == 200
        assert ledger.named("refund_quota") == [] and ledger.live("quiz") == 1

    def test_the_client_cannot_claim_a_plan_or_a_quota(self, client, quiz_env, monkeypatch):
        use_ledger(monkeypatch, plan="free", limits={"quiz": 0})
        resp = client.post("/api/quiz/generate",
                           data={"subject": "Physics", "topic": "Motion", "count": 5, "plan": "pro", "kind": "chat", "limit": 999},
                           headers={"X-Plan": "pro", "X-User-Plan": "pro", "X-Quota-Limit": "999"}, params={"plan": "pro"})
        assert resp.status_code == 429 and resp.json()["detail"]["kind"] == "quiz"
        quiz_env.assert_not_called()

    def test_free_students_cannot_make_quizzes_from_notes(self, client, db, monkeypatch):
        ledger = use_ledger(monkeypatch, plan="free")
        as_user(ALICE)
        note = save(client)
        generate = MagicMock(return_value=MagicMock(text=json.dumps(QUIZ)))
        with stub_ai(generate=generate):
            resp = client.post(f"/api/notes/{note['id']}/mini-quiz", json={})
        assert resp.status_code == 403 and resp.json()["detail"]["code"] == "PLAN_REQUIRED"
        generate.assert_not_called()
        assert consumed(ledger, "quiz") == []                     # a refused request spends nothing

    def test_every_quiz_generation_entry_point_shares_one_pool_and_one_policy(self, client, db, monkeypatch):
        """Pro, allowance of 2: one general quiz + one note quiz use it up; a third attempt on EITHER door is refused."""
        ledger = use_ledger(monkeypatch, plan="pro", limits={"quiz": 2})
        as_user(ALICE)
        note = save(client)
        with patch.object(main, "generate_quiz_questions", return_value=SAMPLE_QUESTIONS), \
             patch.object(main, "get_student_profile", new=AsyncMock(return_value=PROFILE)):
            assert gen_quiz(client).status_code == 200
            with provider_text(json.dumps(QUIZ)):
                assert client.post(f"/api/notes/{note['id']}/mini-quiz", json={}).status_code == 200
                refused_note = client.post(f"/api/notes/{note['id']}/mini-quiz", json={"regenerate": True})
            refused_general = gen_quiz(client)
        assert refused_note.status_code == 429 and refused_note.json()["detail"]["kind"] == "quiz"
        assert refused_general.status_code == 429 and refused_general.json()["detail"]["kind"] == "quiz"
        assert set(consumed(ledger)) == {"quiz"}                  # neither door ever touched another meter

    def test_the_inventory_of_quiz_generating_routes_is_exactly_the_two_reviewed_ones(self):
        """A NEW route that generates quizzes must be reviewed for quota + entitlement: this test fails until it is."""
        generating = set()
        for route in main.app.routes:
            if "POST" not in getattr(route, "methods", set()):
                continue
            src = inspect.getsource(route.endpoint)
            if re.search(r"\bgenerate_quiz_questions\b", src) or "mini_quiz" in src:
                generating.add(route.path)
        assert generating == {"/api/quiz/generate", "/api/notes/{note_id}/mini-quiz"}, generating
        # both draw on the quiz meter: the general route by name, the note route via the entitlement table
        assert "KIND_QUIZ" in inspect.getsource(main.quiz_generate_endpoint)
        assert entitlements.quota_kind_for(entitlements.NOTE_QUIZ, "pro") == quota_service.KIND_QUIZ


# ================================ B. Pro flashcards ==========================
class TestProFlashcards:
    def test_the_guard_is_enforced_on_its_own_meter_and_refuses_with_the_structured_response(self, client, db, monkeypatch):
        ledger = use_ledger(monkeypatch, plan="pro", limits={"flashcards": 2})
        as_user(ALICE)
        note = save(client)
        with provider_text(json.dumps(FLASH)):
            assert client.post(f"/api/notes/{note['id']}/flashcards", json={"regenerate": True}).status_code == 200
            assert client.post(f"/api/notes/{note['id']}/flashcards", json={"regenerate": True}).status_code == 200
            third = client.post(f"/api/notes/{note['id']}/flashcards", json={"regenerate": True})
        assert third.status_code == 429
        detail = third.json()["detail"]
        assert detail["code"] == "QUOTA_EXCEEDED" and detail["kind"] == "flashcards"
        assert consumed(ledger) == ["flashcards"] * 3

    def test_the_refusal_never_quotes_the_internal_number_and_never_says_unlimited(self, client, db, monkeypatch):
        use_ledger(monkeypatch, plan="pro", limits={"flashcards": 0})
        as_user(ALICE)
        note = save(client)
        with provider_text(json.dumps(FLASH)):
            resp = client.post(f"/api/notes/{note['id']}/flashcards", json={})
        text = json.dumps(resp.json())
        assert resp.status_code == 429
        assert not re.search(r"\b200\b", text) and "unlimited" not in text.lower(), text
        assert "flashcard" in resp.json()["detail"]["message"].lower()

    def test_an_exhausted_guard_does_not_throttle_unrelated_features(self, client, db, monkeypatch):
        ledger = use_ledger(monkeypatch, plan="pro", limits={"flashcards": 0})
        as_user(ALICE)
        note = save(client)
        with provider_text(json.dumps(["Force", "Newton"])):
            assert client.post(f"/api/notes/{note['id']}/suggest-tags").status_code == 200      # notes_ai meter
        with provider_text(json.dumps(QUIZ)):
            assert client.post(f"/api/notes/{note['id']}/mini-quiz", json={}).status_code == 200  # quiz meter
        assert consumed(ledger) == ["notes_ai", "quiz"]

    def test_flashcards_never_consume_the_title_and_tag_meter(self, client, db, monkeypatch):
        ledger = use_ledger(monkeypatch, plan="pro", limits={"notes_ai": 0})                    # titles/tags exhausted
        as_user(ALICE)
        note = save(client)
        with provider_text(json.dumps(FLASH)):
            assert client.post(f"/api/notes/{note['id']}/flashcards", json={}).status_code == 200
        assert consumed(ledger) == ["flashcards"]

    def test_free_students_are_refused_before_any_meter_is_touched(self, client, db, monkeypatch):
        ledger = use_ledger(monkeypatch, plan="free")
        as_user(ALICE)
        note = save(client)
        with provider_text(json.dumps(FLASH)):
            resp = client.post(f"/api/notes/{note['id']}/flashcards", json={})
        assert resp.status_code == 403 and consumed(ledger) == []

    def test_the_guard_is_configuration_not_code(self):
        policy = entitlements.QUOTA_POLICY["pro"]["flashcards"]
        assert policy == ("day", entitlements.PRO_FLASHCARDS_PER_DAY, None) == ("day", 200, None)
        sql = (ROOT / "supabase/migrations/0015_flashcards_meter.sql").read_text()
        assert "TEMPORARY" in sql and "CONFIGURABLE" in sql
        assert "update public.plan_limits set limit_count = <N> where plan = 'pro' and kind = 'flashcards'" in sql
        assert "'free', 'flashcards'" not in sql                  # Free deliberately has no flashcards row

    def test_the_meter_is_a_valid_kind_everywhere_the_application_knows_kinds(self):
        assert quota_service.KIND_FLASHCARDS == "flashcards" and "flashcards" in quota_service.VALID_KINDS
        assert quota_service.KIND_FLASHCARDS in quota_service._KIND_NOUN
        assert quota_service.KIND_FLASHCARDS in quota_service._EXCEEDED_MESSAGES


# ================================ C. Learning Intelligence ===================
GATED = {
    "/api/profile/insights": entitlements.LEARNING_INSIGHTS,
    "/api/profile/snapshot": entitlements.LEARNING_SNAPSHOT,
    "/api/profile/mistakes": entitlements.LEARNING_MISTAKES,
    "/api/profile/next-steps": entitlements.LEARNING_NEXT_STEPS,
    "/api/profile/topics": entitlements.LEARNING_MATRIX,
}


class TestLearningIntelligence:
    def test_the_five_exact_endpoints_are_the_gated_ones_and_the_basic_profile_is_not(self):
        profile_get_routes = {}
        for route in main.app.routes:
            path = getattr(route, "path", "")
            if path.startswith("/api/profile") and "GET" in getattr(route, "methods", set()):
                profile_get_routes[path] = inspect.getsource(route.endpoint)
        gated_in_code = {p for p, src in profile_get_routes.items() if "_require_plan_feature" in src}
        assert gated_in_code == set(GATED), gated_in_code
        for path, feature in GATED.items():
            assert f"entitlements.{feature.upper()}" in profile_get_routes[path], path
        assert "/api/profile" in profile_get_routes and "/api/profile" not in gated_in_code
        history = next(r for r in main.app.routes if getattr(r, "path", "") == "/api/learning/history")
        assert "_require_plan_feature" not in inspect.getsource(history.endpoint)

    @pytest.mark.parametrize("route", sorted(GATED))
    def test_free_gets_plan_required_with_the_feature_named(self, client, monkeypatch, route):
        use_ledger(monkeypatch, plan="free")
        as_user("alice-id")
        resp = client.get(route)
        assert resp.status_code == 403
        assert resp.json()["detail"] == {
            "code": "PLAN_REQUIRED", "feature": GATED[route], "required_plan": "pro",
            "message": entitlements.plan_required_exception(GATED[route]).detail["message"],
        }

    @pytest.mark.parametrize("route", sorted(GATED))
    def test_client_side_plan_manipulation_grants_nothing(self, client, monkeypatch, route):
        use_ledger(monkeypatch, plan="free")
        as_user("alice-id")
        resp = client.get(route, params={"plan": "pro", "tier": "pro", "user_id": "bob"},
                          headers={"X-Plan": "pro", "X-User-Plan": "pro", "X-Entitlements": "learning_snapshot"})
        assert resp.status_code == 403

    @pytest.mark.parametrize("route", sorted(GATED))
    def test_pro_with_a_valid_entitlement_is_let_through(self, client, monkeypatch, route):
        use_ledger(monkeypatch, plan="pro")
        as_user("alice-id")
        with patch.object(main, "get_learning_insights", AsyncMock(return_value=[])), \
             patch.object(main, "get_user_topic_profile", AsyncMock(return_value=[])), \
             patch.object(main, "get_user_topic_mistakes", AsyncMock(return_value=[])):
            resp = client.get(route)
        assert resp.status_code == 200, (route, resp.status_code, resp.text[:200])

    @pytest.mark.parametrize("route", sorted(GATED))
    def test_a_pro_student_can_only_ever_read_their_own_data(self, client, monkeypatch, route):
        use_ledger(monkeypatch, plan="pro")
        as_user("alice-id")
        mocks = {name: AsyncMock(return_value=[]) for name in ("get_learning_insights", "get_user_topic_profile", "get_user_topic_mistakes")}
        with patch.object(main, "get_learning_insights", mocks["get_learning_insights"]), \
             patch.object(main, "get_user_topic_profile", mocks["get_user_topic_profile"]), \
             patch.object(main, "get_user_topic_mistakes", mocks["get_user_topic_mistakes"]):
            client.get(route, params={"user_id": "bob", "student_id": "bob"}, headers={"X-User-Id": "bob"})
        calls = [c for m in mocks.values() for c in m.call_args_list]
        assert calls, "the endpoint reached the data layer"
        for call in calls:
            seen = list(call.args) + list(call.kwargs.values())
            assert "alice-id" in seen and "bob" not in seen, seen

    @pytest.mark.parametrize("route", sorted(GATED))
    def test_the_identity_comes_from_the_verified_token_only(self, route):
        handler = next(r for r in main.app.routes if getattr(r, "path", "") == route).endpoint
        assert set(inspect.signature(handler).parameters) == {"user_and_token"}, "no client-supplied user_id/plan parameter exists"

    @pytest.mark.parametrize("route", sorted(GATED))
    def test_unauthenticated_requests_are_rejected_before_the_plan_is_even_looked_up(self, client, monkeypatch, route):
        ledger = use_ledger(monkeypatch, plan="pro")
        assert client.get(route).status_code == 401
        assert ledger.named("get_my_plan") == []

    def test_a_plan_lookup_failure_denies_access_with_a_safe_503(self, client, monkeypatch):
        use_ledger(monkeypatch, plan="pro", fail_plan=True)
        as_user("alice-id")
        resp = client.get("/api/profile/snapshot")
        assert resp.status_code == 503 and "traceback" not in resp.text.lower()


# ================================ D. PDF document budget =====================
def legacy_request(pdf, **kw):
    return ai_engine._build_chat_request(prompt="Summarise", mode="direct", board="b", user_class="Class 9-10 (SSC)",
                                         stream="Science", image_bytes=None, pdf_context=pdf, history=None, **kw)


class TestPdfBudget:
    def test_the_budget_is_300000_characters_everywhere(self):
        assert prompts.DEFAULT_DOC_MAX_CHARS == ai_engine.MAX_PDF_CONTEXT_CHARS == main.MAX_PDF_CONTEXT_CHARS == 300_000

    def test_a_document_exactly_at_the_budget_is_sent_whole_and_unannounced(self):
        block, used, total, truncated = prompts.build_document_block("a" * 300_000)
        assert (used, total, truncated) == (300_000, 300_000, False) and "too long" not in block
        assert "a" * 300_000 in legacy_request("a" * 300_000).system_instruction

    def test_one_character_over_is_truncated_and_the_model_is_told_explicitly(self):
        block, used, total, truncated = prompts.build_document_block("b" * 300_001)
        assert (used, total, truncated) == (300_000, 300_001, True)
        assert "too long to include in full" in block and "NOT visible" in block
        legacy = legacy_request("b" * 300_001).system_instruction
        assert "b" * 300_001 not in legacy and "b" * 300_000 in legacy
        assert re.search(r"truncat|too long|only the beginning|first 300", legacy, re.I), "legacy builder also announces it"

    def test_the_token_safeguard_shrinks_the_document_when_the_whole_request_would_be_too_big(self, monkeypatch):
        monkeypatch.setenv("AI_CONTEXT_MAX_INPUT_TOKENS", "50000")          # 50k tokens ~ 100k chars of everything
        monkeypatch.setenv("AI_CONTEXT_BUILDER", "v2")
        req = ai_engine._build_chat_request(prompt="Q" * 1000, mode="direct", board="b", user_class="Class 9-10 (SSC)",
                                            stream="Science", image_bytes=None, pdf_context="c" * 300_000,
                                            history=[{"role": "user", "text": "h" * 4000}] * 20)
        doc = next(p for p in req.user_parts if isinstance(p, str) and prompts.DOC_START in p)
        assert doc.count("c") < 100_000 and "too long to include in full" in doc
        total_chars = len(req.system_instruction) + sum(len(p) for p in req.user_parts if isinstance(p, str)) + 80_000
        assert total_chars <= 50_000 * prompts.CHARS_PER_TOKEN_ESTIMATE + 10_000

    def test_the_safeguard_cannot_be_disabled_by_nonsense_configuration(self, monkeypatch):
        for bad in ("0", "-1", "abc", "99999999999"):
            monkeypatch.setenv("AI_CONTEXT_MAX_INPUT_TOKENS", bad)
            assert prompts.max_input_tokens() == prompts.DEFAULT_MAX_INPUT_TOKENS
        assert prompts.DEFAULT_MAX_INPUT_TOKENS < 1_048_576          # far inside the approved models' window

    def test_a_default_budget_document_fits_the_safeguard_untouched(self):
        assert prompts.doc_char_budget(None, other_chars=100_000) == 300_000

    def test_truncation_is_logged_with_numbers_and_never_with_content(self, monkeypatch, caplog):
        monkeypatch.setenv("AI_CONTEXT_BUILDER", "v2")
        secret = "TOP-SECRET-STUDENT-DOCUMENT-TEXT"
        with caplog.at_level(logging.WARNING, logger="kognit.ai_engine"):
            ai_engine._build_chat_request(prompt="q", mode="direct", board="b", user_class="Class 9-10 (SSC)", stream="Science",
                                          image_bytes=None, pdf_context=secret + "x" * 310_000, history=None)
        warnings = [r.getMessage() for r in caplog.records if "truncated" in r.getMessage()]
        assert warnings and "total_chars=" in warnings[0] and "used_chars=300000" in warnings[0]
        assert secret not in caplog.text

    def test_the_upload_response_reports_truncation_explicitly(self, client, monkeypatch, caplog):
        auth()
        for size, expected in ((299_999, False), (300_000, False), (300_001, True), (450_000, True)):
            with patch.object(main, "extract_text_from_pdf", lambda data, budget, size=size: "p" * size), \
                 patch.object(main, "has_pdf_signature", lambda b: True):
                with caplog.at_level(logging.WARNING):
                    r = client.post("/api/pdf/upload", data={"chat_id": f"c{size}"}, files={"file": ("a.pdf", b"%PDF-1.4 x", "application/pdf")})
            body = r.json()
            assert r.status_code == 200 and body["length"] == size, (size, r.status_code, r.text[:120])
            assert body["will_be_truncated"] is expected and body["max_context_chars"] == 300_000
        assert any("longer than the AI context budget" in rec.getMessage() for rec in caplog.records)
        assert "ppppp" not in caplog.text                               # content never logged

    @pytest.mark.parametrize("blob", [b"", b"not a pdf at all", b"%PDF-1.4 truncated", b"\x00" * 2048, b"%PDF-" + b"\xff" * 5000])
    def test_malformed_extraction_failures_are_safe_client_errors_and_the_app_keeps_working(self, client, blob):
        auth()
        r = client.post("/api/pdf/upload", data={"chat_id": "c1"}, files={"file": ("bad.pdf", blob, "application/pdf")})
        assert 400 <= r.status_code < 500, (r.status_code, r.text[:160])
        assert "traceback" not in r.text.lower() and "pypdf" not in r.text.lower() and "Error(" not in r.text
        assert client.get("/healthz").status_code == 200                # still alive afterwards

    def test_an_oversized_upload_is_refused_with_413_and_does_not_reach_the_parser(self, client, monkeypatch):
        auth()
        monkeypatch.setattr(main, "MAX_PDF_UPLOAD_SIZE_BYTES", 1024)
        parser = MagicMock()
        with patch.object(main, "extract_text_from_pdf", parser):
            r = client.post("/api/pdf/upload", data={"chat_id": "c1"}, files={"file": ("big.pdf", b"%PDF-1.4 " + b"x" * 5000, "application/pdf")})
        assert r.status_code == 413
        parser.assert_not_called()

    def test_the_stored_document_is_bounded_so_an_extreme_pdf_cannot_exhaust_memory(self):
        assert main.PDF_EXTRACTION_TEXT_BUDGET_CHARS == 2 * main.MAX_PDF_CONTEXT_CHARS


# ================================ E. UI copy == backend policy ===============
class TestUiAndBackendAgree:
    def _js(self, name):
        return (ROOT / "static/js" / name).read_text(encoding="utf-8")

    def test_the_feature_list_in_the_upgrade_ui_equals_the_backend_list(self):
        js = self._js("feedback.js")
        block = re.search(r"var PRO_FEATURE_LIST = \[(.*?)\];", js, re.S).group(1)
        assert tuple(re.findall(r'"([^"]+)"', block)) == entitlements.PRO_FEATURE_LIST

    def test_the_plan_screen_lists_exactly_the_same_features_in_english(self):
        i18n = self._js("i18n.js")
        english = i18n[: i18n.index('"plan.free.f3"', i18n.index('"plan.free.f3"') + 1)] if False else i18n
        found = [re.search(r'"plan\.pro\.f%d": "([^"]+)"' % n, i18n).group(1) for n in range(1, 7)]   # first = English dictionary
        assert tuple(found) == entitlements.PRO_FEATURE_LIST

    def test_both_languages_say_unlimited_flashcards(self):
        values = re.findall(r'"plan\.pro\.f3": "([^"]+)"', self._js("i18n.js"))
        assert len(values) == 2 and values[0] == "Unlimited flashcards" and "Unlimited flashcards" in values[1]

    def test_the_internal_guard_number_appears_in_no_student_facing_text(self):
        for name in ("feedback.js", "i18n.js", "notes.js", "profile-menu.js", "nav-views.js"):
            text = self._js(name)
            for line in text.splitlines():
                if re.search(r"flashcard", line, re.I) and re.search(r"\b200\b", line):
                    pytest.fail(f"{name}: a flashcard line mentions the internal 200 guard: {line.strip()[:120]}")
        message = quota_service._EXCEEDED_MESSAGES[quota_service.KIND_FLASHCARDS]
        assert not re.search(r"\b200\b|unlimited", message, re.I)

    def test_every_pro_feature_the_backend_gates_has_upgrade_copy_in_the_frontend(self):
        js = self._js("feedback.js")
        copy_keys = set(re.findall(r"^\s{8}(\w+): \{\s*$", js[js.index("var UPGRADE_COPY"): js.index("var UPGRADE_FALLBACK")], re.M))
        assert copy_keys == set(entitlements.PRO_FEATURES), copy_keys ^ set(entitlements.PRO_FEATURES)

    def test_the_frontend_never_sends_or_trusts_a_plan(self):
        for name in ("app.js", "notes.js", "nav-views.js", "feedback.js"):
            text = self._js(name)
            assert not re.search(r"""(?:formData\.append|headers\[)\s*\(?\s*["'](?:plan|tier|entitlement)""", text, re.I), name
            assert not re.search(r"localStorage\.(?:get|set)Item\(\s*[\"'][^\"']*plan", text, re.I), name

    def test_there_is_no_checkout_route_and_the_cta_destination_is_the_existing_plan_screen(self):
        paths = {getattr(r, "path", "") for r in main.app.routes}
        assert not any(re.search(r"checkout|subscribe|pay(ment)?s?\b|billing|upgrade", p) for p in paths), "no payment flow exists"
        assert 'openAccount("plan")' in self._js("feedback.js")
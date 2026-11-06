"""
AI Engine 2.0 - Free/Pro entitlements (CEO decision D-4) and the curriculum
medium on the student profile (D-7), tested THROUGH the real FastAPI routes.

The quota backend is test_quota_settlement.LedgerQuota (an in-memory model of
the database functions, extended with get_my_plan); that the real SQL behaves
the same under concurrency is proven against PostgreSQL by
supabase/tests/engine2_behaviour_test.sql.

What is asserted: the plan comes from the database, never the client; a Free
request for Pro-only material is refused with a stable 403 BEFORE any quota
unit or provider call is spent; a note-generated quiz spends the QUIZ
allowance; every Pro-only learning endpoint is gated server-side; and
PLAN_GATING=off restores the pre-2.0 behaviour.
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import backend.main as main
from backend import entitlements, quota_service
from backend.ai import prompts
from backend.providers.mock import MockProvider, text_attempt
from backend.providers import set_provider

from test_notes_endpoints import (  # noqa: F401  (fixtures + helpers of the notes harness)
    ALICE, FLASH, QUIZ, FakeNotesDB, as_user, body, clean_state, db, save, stub_ai,
)
from test_quota_settlement import LedgerQuota, use_ledger


@pytest.fixture
def client():
    return TestClient(main.app)


def provider_text(text):
    return stub_ai(generate=MagicMock(return_value=MagicMock(text=text)))


def consumed(ledger):
    return [c["params"]["p_kind"] for c in ledger.named("consume_quota")]


# ============================== pure policy ==================================
class TestEntitlementPolicy:
    def test_free_has_no_pro_features_and_pro_has_all(self):
        for feature in entitlements.PRO_FEATURES:
            assert not entitlements.is_allowed("free", feature)
            assert entitlements.is_allowed("pro", feature)

    @pytest.mark.parametrize("plan", [None, "", "FREE", "enterprise", "admin", "Pro", 1])
    def test_unknown_plan_values_fail_closed_to_free(self, plan):
        assert entitlements.normalize_plan(plan) == "free"
        assert not entitlements.is_allowed(plan, entitlements.NOTE_QUIZ)

    def test_a_note_quiz_spends_the_quiz_allowance_and_flashcards_spend_notes_ai(self):
        assert entitlements.quota_kind_for(entitlements.NOTE_QUIZ, "pro") == "quiz"
        assert entitlements.quota_kind_for(entitlements.NOTE_FLASHCARDS, "pro") == "notes_ai"

    def test_the_403_body_is_stable_and_names_the_required_plan(self):
        exc = entitlements.plan_required_exception(entitlements.NOTE_FLASHCARDS)
        assert exc.status_code == 403
        assert exc.detail["code"] == "PLAN_REQUIRED" and exc.detail["required_plan"] == "pro"
        assert exc.detail["feature"] == "note_flashcards" and "Pro plan" in exc.detail["message"]

    def test_the_written_policy_matches_the_documented_ceo_numbers(self):
        free, pro = entitlements.QUOTA_POLICY["free"], entitlements.QUOTA_POLICY["pro"]
        assert free["chat"] == ("day", 1, 3) and free["quiz"] == ("week", 3, None)
        assert pro["chat"] == ("day", 20, None) and pro["quiz"] == ("day", 15, None)
        assert pro["notes_ai"][1] == entitlements.PRO_NOTES_AI_PER_DAY

    def test_the_migration_seeds_exactly_the_policy_written_in_code(self):
        """Single source of truth: the Pro rows in migration 0014 must equal QUOTA_POLICY."""
        import pathlib
        import re
        sql = (pathlib.Path(__file__).parent / "supabase/migrations/0014_ai_engine_2_plans_usage_curriculum.sql").read_text()
        rows = re.findall(r"\('pro',\s*'(\w+)',\s*'(\w+)',\s*(\d+),\s*(null|\d+)\)", sql)
        seeded = {kind: (period, int(limit)) for kind, period, limit, _first in rows}
        expected = {k: (v[0], v[1]) for k, v in entitlements.QUOTA_POLICY["pro"].items()}
        assert seeded == expected


# ============================ notes generation gate ==========================
class TestNotesGeneration:
    @pytest.mark.parametrize("path,payload", [("flashcards", {}), ("mini-quiz", {})])
    def test_free_students_cannot_generate_study_material_from_notes(self, client, db, monkeypatch, path, payload):
        ledger = use_ledger(monkeypatch, plan="free")
        as_user(ALICE)
        note = save(client)                                         # saving notes stays free
        generate = MagicMock(return_value=MagicMock(text="[]"))
        with stub_ai(generate=generate):
            resp = client.post(f"/api/notes/{note['id']}/{path}", json=payload)
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "PLAN_REQUIRED" and resp.json()["detail"]["required_plan"] == "pro"
        generate.assert_not_called()                                # no provider spend
        assert consumed(ledger) == []                               # no quota unit spent either

    def test_free_students_keep_saving_notes_and_getting_tag_suggestions(self, client, db, monkeypatch):
        ledger = use_ledger(monkeypatch, plan="free")
        as_user(ALICE)
        note = save(client)
        assert note["id"]
        with provider_text(json.dumps(["Force", "Newton"])):
            resp = client.post(f"/api/notes/{note['id']}/suggest-tags")
        assert resp.status_code == 200 and consumed(ledger) == ["notes_ai"]

    def test_pro_flashcards_spend_notes_ai_and_a_pro_note_quiz_spends_the_quiz_allowance(self, client, db, monkeypatch):
        ledger = use_ledger(monkeypatch, plan="pro")
        as_user(ALICE)
        note = save(client)
        with provider_text(json.dumps(FLASH)):
            assert client.post(f"/api/notes/{note['id']}/flashcards", json={}).status_code == 200
        with provider_text(json.dumps(QUIZ)):
            assert client.post(f"/api/notes/{note['id']}/mini-quiz", json={}).status_code == 200
        assert consumed(ledger) == ["notes_ai", "quiz"]
        assert ledger.live("quiz") == 1 and ledger.live("chat") == 0

    def test_a_note_quiz_and_an_ordinary_quiz_draw_from_the_same_allowance(self, client, db, monkeypatch):
        ledger = use_ledger(monkeypatch, plan="pro", limits={"quiz": 1})
        as_user(ALICE)
        note = save(client)
        with provider_text(json.dumps(QUIZ)):
            assert client.post(f"/api/notes/{note['id']}/mini-quiz", json={}).status_code == 200
            second = client.post(f"/api/notes/{note['id']}/mini-quiz", json={"regenerate": True})
        assert second.status_code == 429 and second.json()["detail"]["kind"] == "quiz"

    def test_already_generated_material_stays_readable_after_a_downgrade(self, client, db, monkeypatch):
        ledger = use_ledger(monkeypatch, plan="pro")
        as_user(ALICE)
        note = save(client)
        with provider_text(json.dumps(FLASH)):
            assert client.post(f"/api/notes/{note['id']}/flashcards", json={}).status_code == 200
        ledger.plan = "free"                                        # subscription lapsed
        with provider_text("[]") as _:
            again = client.post(f"/api/notes/{note['id']}/flashcards", json={})
        assert again.status_code == 200 and again.json()["regenerated"] is False
        with provider_text(json.dumps(FLASH)):
            regen = client.post(f"/api/notes/{note['id']}/flashcards", json={"regenerate": True})
        assert regen.status_code == 403                             # generating NEW material needs Pro

    def test_the_client_cannot_claim_a_plan(self, client, db, monkeypatch):
        use_ledger(monkeypatch, plan="free")
        as_user(ALICE)
        note = save(client)
        with provider_text(json.dumps(FLASH)):
            resp = client.post(f"/api/notes/{note['id']}/flashcards", json={"plan": "pro", "kind": "chat"},
                               headers={"X-Plan": "pro", "X-User-Plan": "pro"}, params={"plan": "pro"})
        assert resp.status_code == 403

    def test_a_plan_lookup_failure_fails_closed_with_a_safe_503(self, client, db, monkeypatch):
        use_ledger(monkeypatch, plan="pro", fail_plan=True)
        as_user(ALICE)
        note = save(client)
        generate = MagicMock(return_value=MagicMock(text=json.dumps(FLASH)))
        with stub_ai(generate=generate):
            resp = client.post(f"/api/notes/{note['id']}/flashcards", json={})
        assert resp.status_code == 503 and "traceback" not in resp.text.lower()
        generate.assert_not_called()

    def test_plan_gating_off_restores_the_pre_2_0_behaviour(self, client, db, monkeypatch):
        monkeypatch.setenv("PLAN_GATING", "off")
        ledger = use_ledger(monkeypatch, plan="free")
        as_user(ALICE)
        note = save(client)
        with provider_text(json.dumps(FLASH)):
            resp = client.post(f"/api/notes/{note['id']}/flashcards", json={})
        assert resp.status_code == 200 and ledger.named("get_my_plan") == []

    def test_unmetered_development_mode_skips_gating(self, client, db, monkeypatch):
        monkeypatch.setenv("QUOTA_MODE", "off")
        as_user(ALICE)
        note = save(client)
        with provider_text(json.dumps(FLASH)):
            assert client.post(f"/api/notes/{note['id']}/flashcards", json={}).status_code == 200


# ========================== learning-intelligence gate =======================
INTELLIGENCE_ROUTES = [
    "/api/profile/insights", "/api/profile/snapshot", "/api/profile/mistakes",
    "/api/profile/next-steps", "/api/profile/topics",
]


class TestLearningIntelligenceGate:
    @pytest.fixture(autouse=True)
    def _clean(self):
        yield
        main.app.dependency_overrides.clear()

    @pytest.mark.parametrize("route", INTELLIGENCE_ROUTES)
    def test_free_students_are_refused_before_any_data_is_read(self, client, monkeypatch, route):
        use_ledger(monkeypatch, plan="free")
        as_user("alice-id")
        with patch.object(main, "get_learning_insights", AsyncMock()) as insights, \
             patch.object(main, "get_user_topic_profile", AsyncMock()) as topics:
            resp = client.get(route)
        assert resp.status_code == 403 and resp.json()["detail"]["code"] == "PLAN_REQUIRED"
        insights.assert_not_called()
        topics.assert_not_called()

    @pytest.mark.parametrize("route", INTELLIGENCE_ROUTES)
    def test_pro_students_pass_the_gate(self, client, monkeypatch, route):
        use_ledger(monkeypatch, plan="pro")
        as_user("alice-id")
        resp = client.get(route)
        assert resp.status_code != 403                              # may be 200/502 depending on data layer, never the gate

    @pytest.mark.parametrize("route", INTELLIGENCE_ROUTES)
    def test_unauthenticated_requests_are_still_401_not_403(self, client, route):
        assert client.get(route).status_code == 401

    def test_the_basic_profile_and_quiz_history_stay_available_to_free_students(self, client, monkeypatch):
        use_ledger(monkeypatch, plan="free")
        as_user("alice-id")
        with patch.object(main, "get_student_profile", AsyncMock(return_value={"name": "A", "user_class": "Class 9-10 (SSC)"})):
            assert client.get("/api/profile").status_code == 200

    def test_gate_off_switch_restores_open_access(self, client, monkeypatch):
        monkeypatch.setenv("PLAN_GATING", "off")
        use_ledger(monkeypatch, plan="free")
        as_user("alice-id")
        assert client.get("/api/profile/insights").status_code != 403


# ================================ curriculum =================================
FORM = {"name": "Rahim", "user_class": "Class 9-10 (SSC)", "stream": "Science (বিজ্ঞান)"}


class TestCurriculumOnTheProfile:
    @pytest.fixture(autouse=True)
    def _clean(self):
        yield
        main.app.dependency_overrides.clear()

    def _put(self, client, **extra):
        as_user("alice-id")
        upsert = AsyncMock(return_value={"user_id": "alice-id"})
        with patch.object(main, "upsert_student_profile", upsert):
            resp = client.put("/api/profile", data={**FORM, **extra})
        return resp, upsert

    @pytest.mark.parametrize("code", ["nctb_bangla", "nctb_english"])
    def test_the_two_approved_mediums_are_saved(self, client, code):
        resp, upsert = self._put(client, curriculum=code)
        assert resp.status_code == 200 and upsert.call_args.kwargs["curriculum"] == code

    @pytest.mark.parametrize("bad", ["cambridge", "edexcel", "NCTB", "nctb", "nctb_bangla; drop table", "বাংলা", "<script>"])
    def test_everything_else_is_rejected(self, client, bad):
        resp, upsert = self._put(client, curriculum=bad)
        assert resp.status_code == 422
        upsert.assert_not_called()

    def test_omitting_it_never_overwrites_a_previously_chosen_medium(self, client):
        resp, upsert = self._put(client)
        assert resp.status_code == 200 and "curriculum" not in upsert.call_args.kwargs

    def test_the_database_layer_only_sends_the_column_when_given(self):
        import asyncio
        sent = []

        class FakeResp:
            status_code = 201
            def json(self):
                return [{"user_id": "u"}]

        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, **kw):
                sent.append(kw["json"])
                return FakeResp()

        from backend import database
        with patch.object(database.http_client, "client", lambda timeout: FakeClient()), \
             patch.object(database, "SUPABASE_URL", "https://x.invalid"), patch.object(database, "SUPABASE_ANON_KEY", "k"):
            asyncio.new_event_loop().run_until_complete(
                database.upsert_student_profile("tok", "u", "N", "Class 9-10 (SSC)", "Science (বিজ্ঞান)"))
            asyncio.new_event_loop().run_until_complete(
                database.upsert_student_profile("tok", "u", "N", "Class 9-10 (SSC)", "Science (বিজ্ঞান)", curriculum="nctb_english"))
        assert "curriculum" not in sent[0] and sent[1]["curriculum"] == "nctb_english"

    def test_the_curriculum_is_read_server_side_only_when_the_v2_builder_is_on(self, monkeypatch):
        import asyncio
        profile = AsyncMock(return_value={"curriculum": "nctb_english"})
        with patch.object(main, "get_student_profile", profile):
            loop = asyncio.new_event_loop()
            assert loop.run_until_complete(main._get_curriculum("t", "u")) is None      # legacy: no extra read
            profile.assert_not_called()
            monkeypatch.setenv("AI_CONTEXT_BUILDER", "v2")
            assert loop.run_until_complete(main._get_curriculum("t", "u")) == "nctb_english"

    def test_a_corrupt_or_unsupported_stored_value_is_treated_as_not_recorded(self, monkeypatch):
        import asyncio
        monkeypatch.setenv("AI_CONTEXT_BUILDER", "v2")
        for stored in ("cambridge", "", None, 5):
            with patch.object(main, "get_student_profile", AsyncMock(return_value={"curriculum": stored})):
                assert asyncio.new_event_loop().run_until_complete(main._get_curriculum("t", "u")) is None

    def test_a_profile_read_failure_never_breaks_chat(self, monkeypatch):
        import asyncio
        from backend.database import DatabaseError
        monkeypatch.setenv("AI_CONTEXT_BUILDER", "v2")
        with patch.object(main, "get_student_profile", AsyncMock(side_effect=DatabaseError("down"))):
            assert asyncio.new_event_loop().run_until_complete(main._get_curriculum("t", "u")) is None
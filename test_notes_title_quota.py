"""
Pre-Phase 10 finalization - Q-05: AI title generation and the Pocket Notes AI
tools are metered by a PERSISTENT, SERVER-ENFORCED, SEPARATE quota meter
(kind "notes_ai"), not just by the in-memory per-minute limiter.

Covered routes (the only AI operations outside chat and quiz):
    POST /api/chat/title
    POST /api/notes/{id}/suggest-tags
    POST /api/notes/{id}/flashcards   (only when it really generates)
    POST /api/notes/{id}/mini-quiz    (only when it really generates)

The quota backend is test_quota_settlement.LedgerQuota (an in-memory model of
the database functions with a real ledger). That the real SQL is atomic under
concurrency, user-isolated and refund-token gated is proven against PostgreSQL by
supabase/tests/hardening_behaviour_test.sql and concurrency_notes_test.sh.
"""
import ast
import json
import os
import pathlib
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import backend.main as main
from backend import notes_ai, quota_service
from backend.providers import set_provider
from backend.providers.base import ProviderErrorKind
from backend.providers.mock import MockProvider, failing_attempt, text_attempt

from test_notes_endpoints import (  # noqa: F401  (fixtures + helpers of the notes harness)
    ALICE, BOB, FLASH, QUIZ, FakeNotesDB, as_user, body, clean_state, db, save, stub_ai,
)
from test_quota_settlement import LedgerQuota, use_ledger

TAGS = json.dumps(["Newton's laws", "Force"])


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture(autouse=True)
def _title_auth_and_state(monkeypatch):
    """The title route has its own rate-limited auth dependency; give it the
    same stubbed identity the notes routes use via as_user()."""
    async def _chat_title_user():
        return (ALICE, "alice-token")
    main.app.dependency_overrides[main._rate_limited_chat_title] = _chat_title_user
    monkeypatch.setattr(main, "_get_academic_context", lambda **kw: None, raising=False)
    yield
    set_provider(None)


def provider_text(text):
    return stub_ai(generate=MagicMock(return_value=MagicMock(text=text)))


def title_post(client, **extra):
    history = json.dumps([{"role": "user", "text": "What is force?"}, {"role": "bot", "text": "Force is push or pull."}])
    return client.post("/api/chat/title", data={"history": history, "chat_id": "c1", **extra})


def kinds(ledger):
    return [c["params"]["p_kind"] for c in ledger.named("consume_quota")]


# ---------------------------------------------------------------------------
# Every AI route consumes the SEPARATE meter - and only that meter
# ---------------------------------------------------------------------------

class TestEachRouteUsesTheNotesMeter:
    def test_title_consumes_notes_ai_only(self, client, monkeypatch):
        ledger = use_ledger(monkeypatch)
        set_provider(MockProvider([text_attempt("Force and motion")]))
        resp = title_post(client)
        assert resp.status_code == 200 and resp.json()["title"] == "Force and motion"
        assert kinds(ledger) == ["notes_ai"]
        assert ledger.live("notes_ai") == 1 and ledger.live("chat") == 0 and ledger.live("quiz") == 0
        assert ledger.named("consume_quota")[0]["token"] == "alice-token", "student's own token, never a service key"

    def test_suggest_tags_consumes_notes_ai_only(self, client, db, monkeypatch):
        ledger = use_ledger(monkeypatch)
        as_user(ALICE)
        note = save(client)
        with provider_text(TAGS):
            resp = client.post(f"/api/notes/{note['id']}/suggest-tags")
        assert resp.status_code == 200 and resp.json()["suggestions"]
        assert kinds(ledger) == ["notes_ai"] and ledger.live("chat") == 0

    def test_flashcards_consume_notes_ai_only(self, client, db, monkeypatch):
        ledger = use_ledger(monkeypatch)
        as_user(ALICE)
        note = save(client)
        with provider_text(json.dumps(FLASH)):
            resp = client.post(f"/api/notes/{note['id']}/flashcards", json={})
        assert resp.status_code == 200 and resp.json()["regenerated"] is True
        assert kinds(ledger) == ["notes_ai"] and ledger.live("chat") == 0

    def test_mini_quiz_consumes_notes_ai_only(self, client, db, monkeypatch):
        ledger = use_ledger(monkeypatch)
        as_user(ALICE)
        note = save(client)
        with provider_text(json.dumps(QUIZ)):
            resp = client.post(f"/api/notes/{note['id']}/mini-quiz", json={})
        assert resp.status_code == 200
        assert kinds(ledger) == ["notes_ai"] and ledger.live("quiz") == 0

    def test_the_main_chat_meter_is_not_affected_by_notes_use(self, client, db, monkeypatch):
        ledger = use_ledger(monkeypatch, limits={"chat": 1, "notes_ai": 6})
        as_user(ALICE)
        note = save(client)
        with provider_text(json.dumps(FLASH)):
            client.post(f"/api/notes/{note['id']}/flashcards", json={})
        set_provider(MockProvider([text_attempt("Chat answer.")]))
        chat = client.post("/api/chat/stream", data={"prompt": "hi", "chat_id": "c1"})
        assert chat.status_code == 200, "the student's one daily chat is still available"


# ---------------------------------------------------------------------------
# Quota exhaustion: stable response, and the provider is never called
# ---------------------------------------------------------------------------

class TestExhaustionPreventsProviderCalls:
    def test_title_over_quota_never_calls_the_provider(self, client, monkeypatch):
        use_ledger(monkeypatch, limits={"notes_ai": 0})
        mock = MockProvider([text_attempt("never")])
        set_provider(mock)
        resp = title_post(client)
        assert resp.status_code == 429
        d = resp.json()["detail"]
        assert d["code"] == "QUOTA_EXCEEDED" and d["kind"] == "notes_ai" and d["resets_at"] and d["message"]
        assert mock.call_count == 0

    @pytest.mark.parametrize("path,payload", [
        ("suggest-tags", None), ("flashcards", {}), ("mini-quiz", {}),
    ])
    def test_notes_routes_over_quota_never_call_the_provider(self, client, db, monkeypatch, path, payload):
        use_ledger(monkeypatch, limits={"notes_ai": 0})
        as_user(ALICE)
        note = save(client)
        generate = MagicMock(return_value=MagicMock(text="[]"))
        with stub_ai(generate=generate):
            resp = client.post(f"/api/notes/{note['id']}/{path}", **({"json": payload} if payload is not None else {}))
        assert resp.status_code == 429 and resp.json()["detail"]["code"] == "QUOTA_EXCEEDED"
        assert resp.json()["detail"]["kind"] == "notes_ai"
        generate.assert_not_called()

    def test_the_limit_is_exhausted_after_exactly_n_successes(self, client, db, monkeypatch):
        ledger = use_ledger(monkeypatch, limits={"notes_ai": 2})
        as_user(ALICE)
        note = save(client)
        main._rate_limit_buckets.clear()
        generate = MagicMock(return_value=MagicMock(text=TAGS))
        with stub_ai(generate=generate):
            codes = [client.post(f"/api/notes/{note['id']}/suggest-tags").status_code for _ in range(3)]
        assert codes == [200, 200, 429] and generate.call_count == 2
        assert ledger.live("notes_ai") == 2

    def test_missing_plan_configuration_fails_safe(self, client, db, monkeypatch):
        use_ledger(monkeypatch)

        async def not_configured(user_token, function, params):
            return {"allowed": False, "reason": "plan_not_configured", "plan": "pro", "kind": params["p_kind"]}
        monkeypatch.setattr(quota_service, "_rpc", not_configured)
        as_user(ALICE)
        note = save(client)
        generate = MagicMock(return_value=MagicMock(text=TAGS))
        with stub_ai(generate=generate):
            resp = client.post(f"/api/notes/{note['id']}/suggest-tags")
        assert resp.status_code == 403 and resp.json()["detail"]["code"] == "PLAN_NOT_CONFIGURED"
        generate.assert_not_called()

    def test_quota_backend_unavailable_fails_closed(self, client, db, monkeypatch):
        use_ledger(monkeypatch, fail_consume=True)
        as_user(ALICE)
        note = save(client)
        generate = MagicMock(return_value=MagicMock(text=TAGS))
        with stub_ai(generate=generate):
            resp = client.post(f"/api/notes/{note['id']}/suggest-tags")
        assert resp.status_code == 503 and resp.json()["detail"]["code"] == "QUOTA_UNAVAILABLE"
        generate.assert_not_called()
        mock = MockProvider([text_attempt("never")])
        set_provider(mock)
        assert title_post(client).status_code == 503 and mock.call_count == 0


# ---------------------------------------------------------------------------
# Bypass attempts
# ---------------------------------------------------------------------------

class TestCannotBypassTheMeter:
    def test_client_supplied_plan_limit_or_kind_is_ignored(self, client, db, monkeypatch):
        ledger = use_ledger(monkeypatch, limits={"notes_ai": 6})
        as_user(ALICE)
        note = save(client)
        with provider_text(json.dumps(FLASH)):
            client.post(
                f"/api/notes/{note['id']}/flashcards",
                json={"plan": "pro", "limit": 9999, "kind": "chat", "used": 0, "refund": True},
                headers={"X-Plan": "pro", "X-Quota-Limit": "9999"},
            )
        params = ledger.named("consume_quota")[0]["params"]
        assert set(params) == {"p_kind", "p_request_id", "p_refund_token_hash"}
        assert params["p_kind"] == "notes_ai"

    def test_repeated_requests_are_each_counted_with_distinct_ledger_ids(self, client, db, monkeypatch):
        ledger = use_ledger(monkeypatch, limits={"notes_ai": 6})
        as_user(ALICE)
        note = save(client)
        main._rate_limit_buckets.clear()
        with stub_ai(generate=MagicMock(return_value=MagicMock(text=TAGS))):
            for _ in range(3):
                assert client.post(f"/api/notes/{note['id']}/suggest-tags").status_code == 200
        ids = [c["params"]["p_request_id"] for c in ledger.named("consume_quota")]
        assert len(ids) == 3 and len(set(ids)) == 3 and all(len(i) == 32 for i in ids), "full 128-bit ids, no collisions"

    def test_concurrent_requests_cannot_exceed_the_limit(self, db, monkeypatch):
        """10 simultaneous requests against a limit of 4: exactly 4 reach the
        provider. The in-memory per-minute limiter is switched off here to prove
        the PERSISTENT meter alone enforces the cap."""
        ledger = use_ledger(monkeypatch, limits={"notes_ai": 4})
        monkeypatch.setattr(main, "_check_rate_limit", lambda *a, **k: None)
        as_user(ALICE)
        note = save(TestClient(main.app))
        generate = MagicMock(return_value=MagicMock(text=TAGS))

        def hit(_):
            return TestClient(main.app).post(f"/api/notes/{note['id']}/suggest-tags").status_code

        with stub_ai(generate=generate), ThreadPoolExecutor(max_workers=10) as pool:
            codes = list(pool.map(hit, range(10)))
        assert codes.count(200) == 4 and codes.count(429) == 6
        assert generate.call_count == 4 and ledger.live("notes_ai") == 4

    def test_another_students_note_is_a_404_and_costs_nothing(self, client, db, monkeypatch):
        ledger = use_ledger(monkeypatch)
        as_user(ALICE)
        note = save(client)
        as_user(BOB)
        generate = MagicMock(return_value=MagicMock(text=TAGS))
        with stub_ai(generate=generate):
            resp = client.post(f"/api/notes/{note['id']}/suggest-tags")
        assert resp.status_code == 404
        assert ledger.named("consume_quota") == []
        generate.assert_not_called()

    def test_stored_material_costs_no_quota_and_no_provider_call(self, client, db, monkeypatch):
        ledger = use_ledger(monkeypatch)
        as_user(ALICE)
        note = save(client)
        with provider_text(json.dumps(FLASH)):
            client.post(f"/api/notes/{note['id']}/flashcards", json={})
        assert len(ledger.named("consume_quota")) == 1
        generate = MagicMock(return_value=MagicMock(text="[]"))
        with stub_ai(generate=generate):
            again = client.post(f"/api/notes/{note['id']}/flashcards", json={})
        assert again.json()["regenerated"] is False
        assert len(ledger.named("consume_quota")) == 1, "reading stored study material is free"
        generate.assert_not_called()

    def test_ordinary_notes_crud_is_not_metered(self, client, db, monkeypatch):
        ledger = use_ledger(monkeypatch, limits={"notes_ai": 0})
        as_user(ALICE)
        note = save(client)
        assert client.get("/api/notes").status_code == 200
        assert client.get(f"/api/notes/{note['id']}").status_code == 200
        assert client.patch(f"/api/notes/{note['id']}", json={"tags": [{"label": "Physics", "kind": "topic"}]}).status_code == 200
        assert client.delete(f"/api/notes/{note['id']}").status_code in (200, 204)
        assert ledger.named("consume_quota") == [], "no AI, no quota - even with a limit of 0"

    def test_empty_title_history_is_not_counted(self, client, monkeypatch):
        ledger = use_ledger(monkeypatch)
        set_provider(MockProvider([text_attempt("never")]))
        resp = client.post("/api/chat/title", data={"history": "[]"})
        assert resp.json() == {"title": None} and ledger.named("consume_quota") == []


# ---------------------------------------------------------------------------
# Refund policy on the notes meter (same approved policy as chat)
# ---------------------------------------------------------------------------

class TestRefundPolicy:
    def test_title_provider_failure_before_delivery_is_refunded(self, client, monkeypatch):
        ledger = use_ledger(monkeypatch)
        set_provider(MockProvider([failing_attempt(ProviderErrorKind.UNAVAILABLE, http_status=503)]))
        resp = title_post(client)
        assert resp.status_code == 200 and resp.json()["title"] is None
        assert ledger.live("notes_ai") == 0 and len(ledger.named("refund_quota")) == 1

    def test_delivered_title_is_not_refunded(self, client, monkeypatch):
        ledger = use_ledger(monkeypatch)
        set_provider(MockProvider([text_attempt("A good title")]))
        title_post(client)
        assert ledger.named("refund_quota") == [] and ledger.live("notes_ai") == 1

    @pytest.mark.parametrize("path,payload", [("suggest-tags", None), ("flashcards", {}), ("mini-quiz", {})])
    def test_provider_failure_before_delivery_is_refunded(self, client, db, monkeypatch, path, payload):
        ledger = use_ledger(monkeypatch)
        as_user(ALICE)
        note = save(client)
        boom = MagicMock(side_effect=failing_attempt(ProviderErrorKind.UNAVAILABLE, http_status=503).error)
        with stub_ai(generate=boom):
            resp = client.post(f"/api/notes/{note['id']}/{path}", **({"json": payload} if payload is not None else {}))
        assert resp.status_code >= 500
        assert ledger.live("notes_ai") == 0 and len(ledger.named("refund_quota")) == 1

    def test_malformed_model_output_is_refunded(self, client, db, monkeypatch):
        ledger = use_ledger(monkeypatch)
        as_user(ALICE)
        note = save(client)
        with provider_text("this is not json"):
            resp = client.post(f"/api/notes/{note['id']}/flashcards", json={})
        assert resp.status_code == 502
        assert ledger.live("notes_ai") == 0

    def test_database_failure_after_generation_is_refunded(self, client, db, monkeypatch):
        ledger = use_ledger(monkeypatch)
        as_user(ALICE)
        note = save(client)
        db.fail = "update"
        with provider_text(json.dumps(FLASH)):
            resp = client.post(f"/api/notes/{note['id']}/flashcards", json={})
        assert resp.status_code == 502
        assert ledger.live("notes_ai") == 0, "infrastructure fault: nothing delivered, unit returned"

    def test_deleting_the_note_mid_generation_does_not_earn_a_free_generation(self, client, db, monkeypatch):
        ledger = use_ledger(monkeypatch)
        as_user(ALICE)
        note = save(client)

        async def gone(*a, **k):
            return None
        monkeypatch.setattr(main, "update_pocket_note", gone)
        with provider_text(json.dumps(FLASH)):
            resp = client.post(f"/api/notes/{note['id']}/flashcards", json={})
        assert resp.status_code == 404
        assert ledger.live("notes_ai") == 1 and ledger.named("refund_quota") == []

    def test_successful_generation_is_not_refunded(self, client, db, monkeypatch):
        ledger = use_ledger(monkeypatch)
        as_user(ALICE)
        note = save(client)
        with provider_text(json.dumps(FLASH)):
            client.post(f"/api/notes/{note['id']}/flashcards", json={})
        assert ledger.named("refund_quota") == [] and ledger.live("notes_ai") == 1


# ---------------------------------------------------------------------------
# Structural guard: no AI route can be added or changed without the meter
# ---------------------------------------------------------------------------

AI_ENTRY_POINTS = {
    "generate_ai_response", "stream_ai_response", "generate_quiz_questions", "generate_chat_title",
    "suggest_topics", "generate_flashcards", "generate_mini_quiz",
}
ENFORCERS = {"enforce_quota", "_enforce_notes_ai_quota", "_generate_study_material"}


class TestRouteCoverage:
    def test_every_function_that_reaches_an_ai_provider_enforces_a_quota(self):
        tree = ast.parse(pathlib.Path(main.__file__).read_text(encoding="utf-8"))   # source is UTF-8 (Bangla text); never rely on the platform default
        offenders = []
        # Top-level functions only: a nested closure (e.g. the stream generator inside
        # chat_stream_endpoint) is covered by the enforcer its enclosing endpoint calls.
        for fn in [n for n in tree.body if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))]:
            referenced, enforced = set(), set()
            for node in ast.walk(fn):
                name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None
                if name in AI_ENTRY_POINTS:
                    referenced.add(name)
                if name in ENFORCERS:
                    enforced.add(name)
            if referenced and not enforced:
                offenders.append((fn.name, sorted(referenced)))
        assert offenders == [], f"AI entry points used without a quota enforcer: {offenders}"

    def test_backend_source_is_strict_utf_8_without_a_bom(self):
        """The coverage scan above reads backend/main.py with an explicit UTF-8
        decode; this pins that this is the file's real encoding."""
        raw = pathlib.Path(main.__file__).read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf")
        raw.decode("utf-8")   # raises if it is not valid UTF-8

    def test_the_coverage_scan_does_not_depend_on_the_platform_default_encoding(self):
        """Regression: on Windows the default text encoding is not UTF-8, and an
        implicit read_text() of the (Bangla-containing) source raised
        UnicodeDecodeError. Re-run the scan in a child process whose default
        encoding is NOT UTF-8 (PYTHONUTF8=0; on Linux also the C locale)."""
        env = dict(os.environ, PYTHONUTF8="0", PYTHONCOERCECLOCALE="0")
        if os.name != "nt":
            env.update(LC_ALL="C", LANG="C")
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "test_notes_title_quota.py::TestRouteCoverage::test_every_function_that_reaches_an_ai_provider_enforces_a_quota"],
            cwd=pathlib.Path(__file__).resolve().parent, env=env, capture_output=True, text=True, timeout=180,
        )
        assert proc.returncode == 0, proc.stdout[-1500:] + proc.stderr[-500:]
        assert "UnicodeDecodeError" not in proc.stdout

    def test_the_four_notes_title_routes_exist_and_are_covered(self):
        routes = {r.path for r in main.app.routes if "POST" in getattr(r, "methods", set())}
        for path in ("/api/chat/title", "/api/notes/{note_id}/suggest-tags",
                     "/api/notes/{note_id}/flashcards", "/api/notes/{note_id}/mini-quiz"):
            assert path in routes
"""
Tests for GET /api/learning/history in backend/main.py (Phase 5D).

Run with: GEMINI_API_KEY=dummy python3 -m pytest test_learning_history_endpoint.py -v

Auth is stubbed via dependency_overrides on get_current_user_and_token,
same convention as test_profile_topics_endpoint.py.
backend.database.get_learning_history is mocked at the backend.main import
site for endpoint-contract tests; its own real behavior is covered in
test_learning_evidence_database.py.
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

import pytest
from fastapi.testclient import TestClient

from backend import main as main_module
from backend.database import DatabaseError


@pytest.fixture
def client():
    return TestClient(main_module.app)


@pytest.fixture(autouse=True)
def clean_state():
    yield
    main_module.app.dependency_overrides.clear()


def _override_auth_as(user_id, token="test-token"):
    async def _fake_user_and_token():
        return (user_id, token)
    main_module.app.dependency_overrides[main_module.get_current_user_and_token] = _fake_user_and_token


SAMPLE_RESULT = {
    "conversations": [
        {"chat_id": "chat_1", "subject": None, "attribution_confidence": "unknown",
         "short_label": "Force and Motion chat", "occurred_at": "2026-08-15T10:00:00+00:00"},
    ],
    "evidence_notes": [
        {"topic": "Force & Motion", "topic_key": "force & motion", "notes": ["expressed confusion 1 time(s)"]},
    ],
}


class TestAuthentication:
    def test_unauthenticated_request_returns_401(self, client):
        resp = client.get("/api/learning/history")
        assert resp.status_code == 401

    def test_authenticated_request_succeeds(self, client):
        _override_auth_as("user-1")
        with patch.object(main_module, "get_learning_history", new=AsyncMock(return_value=SAMPLE_RESULT)):
            resp = client.get("/api/learning/history")
        assert resp.status_code == 200
        assert resp.json() == SAMPLE_RESULT


class TestUserIsolation:
    def test_client_cannot_supply_user_id(self, client):
        # There is no user_id query/body parameter at all on this
        # endpoint - identity comes exclusively from the verified token.
        _override_auth_as("user-1")
        captured = {}

        async def _fake_get_learning_history(user_token, user_id, since_iso, until_iso, limit=100):
            captured["user_id"] = user_id
            captured["user_token"] = user_token
            return SAMPLE_RESULT

        with patch.object(main_module, "get_learning_history", new=_fake_get_learning_history):
            resp = client.get("/api/learning/history", params={"user_id": "someone-elses-id"})
        assert resp.status_code == 200
        assert captured["user_id"] == "user-1"  # from the verified token, not the query param
        assert captured["user_token"] == "test-token"


class TestDateRangeValidation:
    def test_default_range_is_last_30_days(self, client):
        _override_auth_as("user-1")
        captured = {}

        async def _fake(user_token, user_id, since_iso, until_iso, limit=100):
            captured["since_iso"] = since_iso
            captured["until_iso"] = until_iso
            return SAMPLE_RESULT

        with patch.object(main_module, "get_learning_history", new=_fake):
            resp = client.get("/api/learning/history")
        assert resp.status_code == 200
        since_dt = datetime.fromisoformat(captured["since_iso"])
        until_dt = datetime.fromisoformat(captured["until_iso"])
        assert abs((until_dt - since_dt).days - 30) <= 1

    def test_explicit_since_until_are_forwarded(self, client):
        _override_auth_as("user-1")
        captured = {}

        async def _fake(user_token, user_id, since_iso, until_iso, limit=100):
            captured["since_iso"] = since_iso
            captured["until_iso"] = until_iso
            return SAMPLE_RESULT

        with patch.object(main_module, "get_learning_history", new=_fake):
            resp = client.get("/api/learning/history", params={
                "since": "2026-08-01T00:00:00Z", "until": "2026-08-31T00:00:00Z",
            })
        assert resp.status_code == 200
        assert captured["since_iso"].startswith("2026-08-01")
        assert captured["until_iso"].startswith("2026-08-31")

    def test_invalid_since_returns_400(self, client):
        _override_auth_as("user-1")
        resp = client.get("/api/learning/history", params={"since": "not-a-date"})
        assert resp.status_code == 400

    def test_invalid_until_returns_400(self, client):
        _override_auth_as("user-1")
        resp = client.get("/api/learning/history", params={"until": "not-a-date"})
        assert resp.status_code == 400

    def test_since_after_until_returns_400(self, client):
        _override_auth_as("user-1")
        resp = client.get("/api/learning/history", params={
            "since": "2026-08-31T00:00:00Z", "until": "2026-08-01T00:00:00Z",
        })
        assert resp.status_code == 400

    def test_range_exceeding_maximum_returns_400(self, client):
        _override_auth_as("user-1")
        resp = client.get("/api/learning/history", params={
            "since": "2020-01-01T00:00:00Z", "until": "2026-08-01T00:00:00Z",
        })
        assert resp.status_code == 400

    def test_future_until_returns_400(self, client):
        _override_auth_as("user-1")
        far_future = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()
        resp = client.get("/api/learning/history", params={"until": far_future})
        assert resp.status_code == 400

    def test_naive_datetime_is_treated_as_utc(self, client):
        _override_auth_as("user-1")
        captured = {}

        async def _fake(user_token, user_id, since_iso, until_iso, limit=100):
            captured["since_iso"] = since_iso
            return SAMPLE_RESULT

        with patch.object(main_module, "get_learning_history", new=_fake):
            resp = client.get("/api/learning/history", params={
                "since": "2026-08-01T00:00:00", "until": "2026-08-15T00:00:00",
            })
        assert resp.status_code == 200
        assert "+00:00" in captured["since_iso"]


class TestFailureHandling:
    def test_database_error_returns_502_not_500_with_stack_trace(self, client):
        _override_auth_as("user-1")
        with patch.object(main_module, "get_learning_history", new=AsyncMock(side_effect=DatabaseError("boom"))):
            resp = client.get("/api/learning/history")
        assert resp.status_code == 502
        assert "boom" not in resp.text  # internal error detail never leaked to the client
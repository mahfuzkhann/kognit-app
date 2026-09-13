"""
Tests for GET /api/profile/mistakes in backend/main.py (Phase 6C).

Run with: GEMINI_API_KEY=dummy python3 -m pytest test_profile_mistakes_endpoint.py -v

Auth is stubbed via dependency_overrides on get_current_user_and_token,
same convention as test_profile_topics_endpoint.py /
test_profile_snapshot_endpoint.py. backend.database.get_user_topic_mistakes
is mocked at the backend.main import site - this file tests the
ENDPOINT's contract (auth, wiring, error handling) only. The mistake
computation logic itself is covered in test_mistake_engine.py; the DB
read/translation is covered in test_mistake_evidence_database.py.
"""
import os
import sys
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
    async def _fake():
        return (user_id, token)
    main_module.app.dependency_overrides[main_module.get_current_user_and_token] = _fake


SAMPLE_MISTAKE = {
    "mistake_type": "repeated_topic_difficulty",
    "subject": "Physics", "topic": "Force & Motion", "topic_key": "force & motion",
    "affected_attempts": 3, "total_incorrect": 12,
    "first_observed_at": "2026-08-01T10:00:00+00:00",
    "last_observed_at": "2026-09-01T10:00:00+00:00",
    "recent_incorrect_count": 12, "earlier_incorrect_count": None,
}


class TestAuthentication:
    def test_unauthenticated_request_returns_401(self, client):
        resp = client.get("/api/profile/mistakes")
        assert resp.status_code == 401

    def test_authenticated_request_succeeds(self, client):
        _override_auth_as("user-1")
        with patch.object(main_module, "get_user_topic_mistakes", new=AsyncMock(return_value=[SAMPLE_MISTAKE])):
            resp = client.get("/api/profile/mistakes")
        assert resp.status_code == 200


class TestResponseShape:
    def test_response_has_mistakes_key(self, client):
        _override_auth_as("user-1")
        with patch.object(main_module, "get_user_topic_mistakes", new=AsyncMock(return_value=[SAMPLE_MISTAKE])):
            resp = client.get("/api/profile/mistakes")
        body = resp.json()
        assert set(body.keys()) == {"mistakes"}
        assert body["mistakes"] == [SAMPLE_MISTAKE]

    def test_no_evidence_returns_empty_list(self, client):
        _override_auth_as("user-1")
        with patch.object(main_module, "get_user_topic_mistakes", new=AsyncMock(return_value=[])):
            resp = client.get("/api/profile/mistakes")
        assert resp.status_code == 200
        assert resp.json() == {"mistakes": []}

    def test_response_never_exposes_raw_content_fields(self, client):
        _override_auth_as("user-1")
        with patch.object(main_module, "get_user_topic_mistakes", new=AsyncMock(return_value=[SAMPLE_MISTAKE])):
            resp = client.get("/api/profile/mistakes")
        raw_text = resp.text
        for forbidden in ("question_text", "selected_index", "correct_index", "severity", "confidence"):
            assert forbidden not in raw_text


class TestFailureHandling:
    def test_database_error_returns_502_without_leaking_exception_text(self, client):
        _override_auth_as("user-1")
        with patch.object(main_module, "get_user_topic_mistakes", new=AsyncMock(side_effect=DatabaseError("boom"))):
            resp = client.get("/api/profile/mistakes")
        assert resp.status_code == 502
        assert "boom" not in resp.text


class TestAdversarialUserIsolation:
    def test_user_identity_comes_only_from_verified_token(self, client):
        _override_auth_as("user-a", token="token-a")
        captured = {}

        async def _fake(user_token, user_id):
            captured["user_id"] = user_id
            captured["user_token"] = user_token
            return []

        with patch.object(main_module, "get_user_topic_mistakes", new=_fake):
            resp = client.get("/api/profile/mistakes")

        assert resp.status_code == 200
        assert captured["user_id"] == "user-a"
        assert captured["user_token"] == "token-a"

    def test_client_cannot_override_user_id_via_query_param(self, client):
        _override_auth_as("user-a", token="token-a")
        captured = {}

        async def _fake(user_token, user_id):
            captured["user_id"] = user_id
            return []

        with patch.object(main_module, "get_user_topic_mistakes", new=_fake):
            resp = client.get("/api/profile/mistakes?user_id=user-b")

        assert resp.status_code == 200
        # The endpoint has no user_id parameter at all - a client-supplied
        # query param is simply ignored, identity still comes from the
        # verified token only.
        assert captured["user_id"] == "user-a"


class TestNoRegressionOnExistingEndpoints:
    """Sanity check that adding this endpoint did not disturb 6B/5A/5E -
    full regression coverage lives in their own test files."""

    def test_profile_snapshot_endpoint_still_works_independently(self, client):
        _override_auth_as("user-1")
        with patch.object(main_module, "get_user_topic_profile", new=AsyncMock(return_value=[])), \
             patch.object(main_module, "get_learning_insights", new=AsyncMock(return_value=[])):
            resp = client.get("/api/profile/snapshot")
        assert resp.status_code == 200

    def test_profile_topics_endpoint_still_works_independently(self, client):
        _override_auth_as("user-1")
        with patch.object(main_module, "get_user_topic_profile", new=AsyncMock(return_value=[])):
            resp = client.get("/api/profile/topics")
        assert resp.status_code == 200
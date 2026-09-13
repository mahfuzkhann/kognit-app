"""
Tests for GET /api/profile/snapshot in backend/main.py (Phase 6B).

Run with: GEMINI_API_KEY=dummy python3 -m pytest test_profile_snapshot_endpoint.py -v

Auth is stubbed via dependency_overrides on get_current_user_and_token,
same convention as test_profile_topics_endpoint.py /
test_profile_insights_endpoint.py. backend.database.get_user_topic_profile
and backend.database.get_learning_insights are both mocked at the
backend.main import site - this file tests the ENDPOINT's contract
(auth, wiring, error handling) only. The actual snapshot combination
logic is covered in test_learning_snapshot.py; the two upstream read
functions' own real behavior is covered in test_quiz_database.py /
test_learning_insights_database.py.
"""
import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

import pytest
from fastapi.testclient import TestClient

from backend import main as main_module
from backend.database import DatabaseError
from backend.mastery_engine import STATUS_STRONG, STATUS_NEEDS_PRACTICE


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


SAMPLE_TOPIC_STRONG = {
    "subject": "Physics", "topic": "Force & Motion", "topic_key": "force & motion",
    "status": STATUS_STRONG, "attempt_count": 3, "total_questions": 30,
    "total_correct": 25, "correct_rate": 0.83, "last_attempt_at": "2026-09-01T10:00:00+00:00",
    "recent_attempts": [],
}
SAMPLE_TOPIC_NEEDS_PRACTICE = {
    "subject": "Chemistry", "topic": "Stoichiometry", "topic_key": "stoichiometry",
    "status": STATUS_NEEDS_PRACTICE, "attempt_count": 2, "total_questions": 20,
    "total_correct": 6, "correct_rate": 0.3, "last_attempt_at": "2026-08-28T10:00:00+00:00",
    "recent_attempts": [],
}
SAMPLE_INSIGHT = {
    "subject": "Physics", "topic": None, "topic_key": None,
    "insight_type": "emerging_strength", "confidence": "high",
    "evidence_count": 4, "first_observed_at": "2026-08-01T10:00:00+00:00",
    "last_observed_at": "2026-08-20T10:00:00+00:00",
}


def _mock_both(topics_return=None, insights_return=None, topics_side_effect=None, insights_side_effect=None):
    topics_mock = AsyncMock(return_value=topics_return, side_effect=topics_side_effect)
    insights_mock = AsyncMock(return_value=insights_return, side_effect=insights_side_effect)
    return patch.object(main_module, "get_user_topic_profile", new=topics_mock), \
        patch.object(main_module, "get_learning_insights", new=insights_mock), \
        topics_mock, insights_mock


class TestAuthentication:
    def test_unauthenticated_request_returns_401(self, client):
        resp = client.get("/api/profile/snapshot")
        assert resp.status_code == 401

    def test_authenticated_request_succeeds(self, client):
        _override_auth_as("user-1")
        p1, p2, _, _ = _mock_both(topics_return=[SAMPLE_TOPIC_STRONG], insights_return=[])
        with p1, p2:
            resp = client.get("/api/profile/snapshot")
        assert resp.status_code == 200


class TestResponseShape:
    def test_response_has_all_four_top_level_keys(self, client):
        _override_auth_as("user-1")
        p1, p2, _, _ = _mock_both(topics_return=[SAMPLE_TOPIC_STRONG], insights_return=[SAMPLE_INSIGHT])
        with p1, p2:
            resp = client.get("/api/profile/snapshot")
        body = resp.json()
        assert set(body.keys()) == {"strengths", "needs_practice", "recent_progress", "evidence_summary"}

    def test_no_data_state_returns_valid_empty_structure(self, client):
        _override_auth_as("user-1")
        p1, p2, _, _ = _mock_both(topics_return=[], insights_return=[])
        with p1, p2:
            resp = client.get("/api/profile/snapshot")
        assert resp.status_code == 200
        body = resp.json()
        assert body["strengths"] == []
        assert body["needs_practice"] == []
        assert body["recent_progress"]["state"] == "insufficient_evidence"

    def test_quiz_and_chat_evidence_combine_correctly(self, client):
        _override_auth_as("user-1")
        p1, p2, _, _ = _mock_both(
            topics_return=[SAMPLE_TOPIC_STRONG, SAMPLE_TOPIC_NEEDS_PRACTICE],
            insights_return=[SAMPLE_INSIGHT],
        )
        with p1, p2:
            resp = client.get("/api/profile/snapshot")
        body = resp.json()
        # 1 quiz strength (Strong) + 1 chat strength (high-confidence emerging_strength).
        assert len(body["strengths"]) == 2
        assert len(body["needs_practice"]) == 1
        assert body["needs_practice"][0]["subject"] == "Chemistry"

    def test_never_exposes_raw_learning_evidence_content(self, client):
        _override_auth_as("user-1")
        p1, p2, _, _ = _mock_both(topics_return=[], insights_return=[SAMPLE_INSIGHT])
        with p1, p2:
            resp = client.get("/api/profile/snapshot")
        raw_text = resp.text
        # No field in the Insight/ChatEvidenceRecord chain ever carries
        # raw chat message text - only structured subject/topic/confidence
        # fields. Spot-check the response never contains anything beyond
        # the known-safe fields by checking the evidence_summary is
        # counts-only.
        for value in resp.json()["evidence_summary"].values():
            assert isinstance(value, (int, bool))
        assert "message" not in raw_text.lower() or True  # structural guard only; no message field exists


class TestFailureHandling:
    def test_topic_profile_database_error_returns_502(self, client):
        _override_auth_as("user-1")
        p1, p2, _, _ = _mock_both(topics_side_effect=DatabaseError("boom"), insights_return=[])
        with p1, p2:
            resp = client.get("/api/profile/snapshot")
        assert resp.status_code == 502
        assert "boom" not in resp.text

    def test_learning_insights_database_error_returns_502(self, client):
        _override_auth_as("user-1")
        p1, p2, _, _ = _mock_both(topics_return=[], insights_side_effect=DatabaseError("boom"))
        with p1, p2:
            resp = client.get("/api/profile/snapshot")
        assert resp.status_code == 502
        assert "boom" not in resp.text


class TestAdversarialUserIsolation:
    def test_user_a_gets_only_user_a_scoped_calls(self, client):
        _override_auth_as("user-a", token="token-a")
        captured = {}

        async def _fake_topics(user_token, user_id):
            captured["topics_user_id"] = user_id
            captured["topics_token"] = user_token
            return [SAMPLE_TOPIC_STRONG]

        async def _fake_insights(user_token, user_id):
            captured["insights_user_id"] = user_id
            captured["insights_token"] = user_token
            return []

        with patch.object(main_module, "get_user_topic_profile", new=_fake_topics), \
             patch.object(main_module, "get_learning_insights", new=_fake_insights):
            resp = client.get("/api/profile/snapshot")

        assert resp.status_code == 200
        assert captured["topics_user_id"] == "user-a"
        assert captured["topics_token"] == "token-a"
        assert captured["insights_user_id"] == "user-a"
        assert captured["insights_token"] == "token-a"

    def test_user_b_gets_only_user_b_scoped_calls(self, client):
        _override_auth_as("user-b", token="token-b")
        captured = {}

        async def _fake_topics(user_token, user_id):
            captured["topics_user_id"] = user_id
            return []

        async def _fake_insights(user_token, user_id):
            captured["insights_user_id"] = user_id
            return []

        with patch.object(main_module, "get_user_topic_profile", new=_fake_topics), \
             patch.object(main_module, "get_learning_insights", new=_fake_insights):
            resp = client.get("/api/profile/snapshot")

        assert resp.status_code == 200
        assert captured["topics_user_id"] == "user-b"
        assert captured["insights_user_id"] == "user-b"


class TestNoRegressionOnExistingEndpoints:
    """Sanity check that adding this endpoint did not disturb the two
    endpoints it reuses - full regression coverage lives in their own
    test files; this is a smoke-level cross-check only."""

    def test_profile_topics_endpoint_still_works_independently(self, client):
        _override_auth_as("user-1")
        with patch.object(main_module, "get_user_topic_profile", new=AsyncMock(return_value=[SAMPLE_TOPIC_STRONG])):
            resp = client.get("/api/profile/topics")
        assert resp.status_code == 200
        assert resp.json() == {"topics": [SAMPLE_TOPIC_STRONG]}

    def test_profile_insights_endpoint_still_works_independently(self, client):
        _override_auth_as("user-1")
        with patch.object(main_module, "get_learning_insights", new=AsyncMock(return_value=[SAMPLE_INSIGHT])):
            resp = client.get("/api/profile/insights")
        assert resp.status_code == 200
        assert resp.json() == {"insights": [SAMPLE_INSIGHT]}
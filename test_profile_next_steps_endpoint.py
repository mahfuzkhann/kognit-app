"""
Tests for GET /api/profile/next-steps in backend/main.py (Phase 6D).

Run with: GEMINI_API_KEY=dummy python3 -m pytest test_profile_next_steps_endpoint.py -v

Auth is stubbed via dependency_overrides on get_current_user_and_token,
same convention as test_profile_mistakes_endpoint.py /
test_profile_snapshot_endpoint.py. All three upstream database functions
are mocked at the backend.main import site - this file tests the
ENDPOINT's contract (auth, wiring, error handling) only. The
recommendation logic itself is covered in test_next_step_engine.py.
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


SAMPLE_TOPIC_WEAK = {
    "subject": "Math", "topic": "Algebra", "topic_key": "algebra",
    "status": "Needs Practice", "attempt_count": 3, "total_questions": 30,
    "total_correct": 9, "correct_rate": 0.3, "last_attempt_at": "2026-09-01T10:00:00+00:00",
    "recent_attempts": [],
}


def _mock_all(topics=None, insights=None, mistakes=None,
              topics_err=None, insights_err=None, mistakes_err=None):
    p1 = patch.object(main_module, "get_user_topic_profile",
                       new=AsyncMock(return_value=topics or [], side_effect=topics_err))
    p2 = patch.object(main_module, "get_learning_insights",
                       new=AsyncMock(return_value=insights or [], side_effect=insights_err))
    p3 = patch.object(main_module, "get_user_topic_mistakes",
                       new=AsyncMock(return_value=mistakes or [], side_effect=mistakes_err))
    return p1, p2, p3


class TestAuthentication:
    def test_unauthenticated_request_returns_401(self, client):
        resp = client.get("/api/profile/next-steps")
        assert resp.status_code == 401

    def test_authenticated_request_succeeds(self, client):
        _override_auth_as("user-1")
        p1, p2, p3 = _mock_all(topics=[SAMPLE_TOPIC_WEAK])
        with p1, p2, p3:
            resp = client.get("/api/profile/next-steps")
        assert resp.status_code == 200


class TestResponseShape:
    def test_response_has_recommendations_key(self, client):
        _override_auth_as("user-1")
        p1, p2, p3 = _mock_all(topics=[SAMPLE_TOPIC_WEAK])
        with p1, p2, p3:
            resp = client.get("/api/profile/next-steps")
        body = resp.json()
        assert set(body.keys()) == {"recommendations"}
        assert body["recommendations"][0]["recommendation_type"] == "practice_topic"

    def test_no_evidence_returns_empty_list(self, client):
        _override_auth_as("user-1")
        p1, p2, p3 = _mock_all()
        with p1, p2, p3:
            resp = client.get("/api/profile/next-steps")
        assert resp.status_code == 200
        assert resp.json() == {"recommendations": []}

    def test_response_never_exposes_raw_content_fields(self, client):
        _override_auth_as("user-1")
        p1, p2, p3 = _mock_all(topics=[SAMPLE_TOPIC_WEAK])
        with p1, p2, p3:
            resp = client.get("/api/profile/next-steps")
        raw_text = resp.text
        for forbidden in ("question_text", "selected_index", "correct_index", "recent_attempts"):
            assert forbidden not in raw_text


class TestFailureHandling:
    def test_topic_profile_database_error_returns_502(self, client):
        _override_auth_as("user-1")
        p1, p2, p3 = _mock_all(topics_err=DatabaseError("boom"))
        with p1, p2, p3:
            resp = client.get("/api/profile/next-steps")
        assert resp.status_code == 502
        assert "boom" not in resp.text

    def test_insights_database_error_returns_502(self, client):
        _override_auth_as("user-1")
        p1, p2, p3 = _mock_all(insights_err=DatabaseError("boom"))
        with p1, p2, p3:
            resp = client.get("/api/profile/next-steps")
        assert resp.status_code == 502
        assert "boom" not in resp.text

    def test_mistakes_database_error_returns_502(self, client):
        _override_auth_as("user-1")
        p1, p2, p3 = _mock_all(mistakes_err=DatabaseError("boom"))
        with p1, p2, p3:
            resp = client.get("/api/profile/next-steps")
        assert resp.status_code == 502
        assert "boom" not in resp.text


class TestAdversarialUserIsolation:
    def test_user_identity_comes_only_from_verified_token(self, client):
        _override_auth_as("user-a", token="token-a")
        captured = {}

        async def _fake_topics(user_token, user_id):
            captured["topics"] = (user_id, user_token)
            return []

        async def _fake_insights(user_token, user_id):
            captured["insights"] = (user_id, user_token)
            return []

        async def _fake_mistakes(user_token, user_id):
            captured["mistakes"] = (user_id, user_token)
            return []

        with patch.object(main_module, "get_user_topic_profile", new=_fake_topics), \
             patch.object(main_module, "get_learning_insights", new=_fake_insights), \
             patch.object(main_module, "get_user_topic_mistakes", new=_fake_mistakes):
            resp = client.get("/api/profile/next-steps")

        assert resp.status_code == 200
        assert captured["topics"] == ("user-a", "token-a")
        assert captured["insights"] == ("user-a", "token-a")
        assert captured["mistakes"] == ("user-a", "token-a")

    def test_client_cannot_override_user_id_via_query_param(self, client):
        _override_auth_as("user-a", token="token-a")
        captured = {}

        async def _fake(user_token, user_id):
            captured["user_id"] = user_id
            return []

        with patch.object(main_module, "get_user_topic_profile", new=_fake), \
             patch.object(main_module, "get_learning_insights", new=_fake), \
             patch.object(main_module, "get_user_topic_mistakes", new=_fake):
            resp = client.get("/api/profile/next-steps?user_id=user-b")

        assert resp.status_code == 200
        assert captured["user_id"] == "user-a"


class TestNoRegressionOnExistingEndpoints:
    def test_profile_mistakes_endpoint_still_works_independently(self, client):
        _override_auth_as("user-1")
        with patch.object(main_module, "get_user_topic_mistakes", new=AsyncMock(return_value=[])):
            resp = client.get("/api/profile/mistakes")
        assert resp.status_code == 200

    def test_profile_snapshot_endpoint_still_works_independently(self, client):
        _override_auth_as("user-1")
        with patch.object(main_module, "get_user_topic_profile", new=AsyncMock(return_value=[])), \
             patch.object(main_module, "get_learning_insights", new=AsyncMock(return_value=[])):
            resp = client.get("/api/profile/snapshot")
        assert resp.status_code == 200
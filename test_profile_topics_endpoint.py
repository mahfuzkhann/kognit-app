"""
Tests for GET /api/profile/topics in backend/main.py (Phase 5A).

Run with: GEMINI_API_KEY=dummy python3 -m pytest test_profile_topics_endpoint.py -v

Auth is stubbed via dependency_overrides on get_current_user_and_token,
same convention as test_quiz_submit_endpoint.py.
backend.database.get_user_topic_profile is mocked at the backend.main
import site for endpoint-contract tests; its own real behavior (row
grouping, EvidenceEvent translation) is covered in test_quiz_database.py,
and the mastery algorithm itself in test_mastery_engine.py.
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
    async def _fake_user_and_token():
        return (user_id, token)
    main_module.app.dependency_overrides[main_module.get_current_user_and_token] = _fake_user_and_token


SAMPLE_TOPICS = [
    {
        "subject": "Physics", "topic": "Force & Motion", "topic_key": "force & motion",
        "status": "Developing", "attempt_count": 2, "total_questions": 20,
        "total_correct": 11, "correct_rate": 0.55, "last_attempt_at": "2026-08-27T10:00:00+00:00",
        "recent_attempts": [],
    },
]


class TestAuthentication:
    def test_unauthenticated_request_returns_401(self, client):
        # No dependency override applied - hits the real
        # get_current_user_and_token, which requires a bearer token.
        resp = client.get("/api/profile/topics")
        assert resp.status_code == 401

    def test_authenticated_request_succeeds(self, client):
        _override_auth_as("user-1")
        with patch.object(main_module, "get_user_topic_profile", new=AsyncMock(return_value=SAMPLE_TOPICS)):
            resp = client.get("/api/profile/topics")
        assert resp.status_code == 200


class TestResponseShape:
    def test_response_is_wrapped_in_topics_envelope(self, client):
        # Explicitly NOT a bare array - see backend/main.py docstring for
        # why (extensibility for a future sibling field).
        _override_auth_as("user-1")
        with patch.object(main_module, "get_user_topic_profile", new=AsyncMock(return_value=SAMPLE_TOPICS)):
            resp = client.get("/api/profile/topics")
        body = resp.json()
        assert isinstance(body, dict)
        assert "topics" in body
        assert body["topics"] == SAMPLE_TOPICS

    def test_empty_result_set_returns_empty_topics_list(self, client):
        _override_auth_as("user-1")
        with patch.object(main_module, "get_user_topic_profile", new=AsyncMock(return_value=[])):
            resp = client.get("/api/profile/topics")
        assert resp.status_code == 200
        assert resp.json() == {"topics": []}

    def test_never_returns_a_bare_status_label(self, client):
        _override_auth_as("user-1")
        with patch.object(main_module, "get_user_topic_profile", new=AsyncMock(return_value=SAMPLE_TOPICS)):
            resp = client.get("/api/profile/topics")
        for topic_entry in resp.json()["topics"]:
            for key in ("status", "attempt_count", "total_questions", "total_correct",
                        "correct_rate", "last_attempt_at"):
                assert key in topic_entry


class TestUserIsolationAndTokenForwarding:
    def test_uses_authenticated_users_own_identity_and_token(self, client):
        _override_auth_as("user-42", token="student-42-token")
        mock_fn = AsyncMock(return_value=[])
        with patch.object(main_module, "get_user_topic_profile", new=mock_fn):
            client.get("/api/profile/topics")
        _, kwargs = mock_fn.call_args
        # Never a client-supplied user_id - it comes only from the
        # verified dependency, and the SAME verified token is what's
        # forwarded for the RLS-scoped database read.
        assert kwargs["user_id"] == "user-42"
        assert kwargs["user_token"] == "student-42-token"

    def test_different_authenticated_users_get_independently_scoped_calls(self, client):
        mock_fn = AsyncMock(return_value=[])

        _override_auth_as("user-A", token="token-A")
        with patch.object(main_module, "get_user_topic_profile", new=mock_fn):
            client.get("/api/profile/topics")
        assert mock_fn.call_args.kwargs["user_id"] == "user-A"

        _override_auth_as("user-B", token="token-B")
        with patch.object(main_module, "get_user_topic_profile", new=mock_fn):
            client.get("/api/profile/topics")
        assert mock_fn.call_args.kwargs["user_id"] == "user-B"


class TestFailureHandling:
    def test_database_error_returns_502_not_a_stack_trace(self, client):
        _override_auth_as("user-1")
        with patch.object(main_module, "get_user_topic_profile", new=AsyncMock(side_effect=DatabaseError("boom"))):
            resp = client.get("/api/profile/topics")
        assert resp.status_code == 502
        # Generic, student-safe message - never str(exception) leaked.
        assert "boom" not in resp.text
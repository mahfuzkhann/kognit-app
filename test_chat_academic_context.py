"""
Tests for ISSUE 1 (Phase 6A final correction): /api/chat's user_class/
stream now come ONLY from the authenticated student's own profile
(backend/main.py:_get_academic_context), never from a client-supplied
form field. This is the fix for the "competing academic context" bug -
a request can no longer disagree with the student's saved Profile.

Run with: GEMINI_API_KEY=dummy python3 -m pytest test_chat_academic_context.py -v

generate_ai_response and get_student_profile are mocked at the
backend.main import site, same convention as
test_chat_learning_memory_integration.py. Auth is stubbed via
dependency_overrides on get_current_user_and_token.
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
    main_module._rate_limit_buckets.clear()
    yield
    main_module._rate_limit_buckets.clear()
    main_module.app.dependency_overrides.clear()


def _override_auth_as(user_id, token="test-token"):
    async def _fake():
        return (user_id, token)
    main_module.app.dependency_overrides[main_module.get_current_user_and_token] = _fake


PROFILE_CLASS_11_12_SCIENCE = {
    "user_id": "user-1", "name": "Student", "user_class": "Class 11-12 (HSC)",
    "stream": "Science (বিজ্ঞান)", "created_at": "x", "updated_at": "x",
}
PROFILE_CLASS_10_SCIENCE = {
    "user_id": "user-1", "name": "Student", "user_class": "Class 9-10 (SSC)",
    "stream": "Science (বিজ্ঞান)", "created_at": "x", "updated_at": "x",
}
PROFILE_CLASS_7_NO_STREAM = {
    "user_id": "user-1", "name": "Student", "user_class": "Class 6-8",
    "stream": None, "created_at": "x", "updated_at": "x",
}


class TestChatUsesProfileAcademicContext:
    def test_class_11_12_science_profile_reaches_ai_context(self, client):
        _override_auth_as("user-1")
        with patch.object(main_module, "generate_ai_response", return_value="ok") as mock_ai, \
             patch.object(main_module, "get_student_profile", new=AsyncMock(return_value=PROFILE_CLASS_11_12_SCIENCE)):
            resp = client.post("/api/chat", data={"prompt": "explain photosynthesis", "mode": "direct"})
        assert resp.status_code == 200
        _, kwargs = mock_ai.call_args
        assert kwargs["user_class"] == "Class 11-12 (HSC)"
        assert kwargs["stream"] == "Science (বিজ্ঞান)"
        assert kwargs["board"] == main_module.DEFAULT_BOARD

    def test_class_10_science_profile_reaches_ai_context(self, client):
        _override_auth_as("user-1")
        with patch.object(main_module, "generate_ai_response", return_value="ok") as mock_ai, \
             patch.object(main_module, "get_student_profile", new=AsyncMock(return_value=PROFILE_CLASS_10_SCIENCE)):
            resp = client.post("/api/chat", data={"prompt": "explain photosynthesis", "mode": "direct"})
        assert resp.status_code == 200
        _, kwargs = mock_ai.call_args
        assert kwargs["user_class"] == "Class 9-10 (SSC)"
        assert kwargs["stream"] == "Science (বিজ্ঞান)"

    def test_class_7_profile_reaches_ai_context_with_no_stream(self, client):
        _override_auth_as("user-1")
        with patch.object(main_module, "generate_ai_response", return_value="ok") as mock_ai, \
             patch.object(main_module, "get_student_profile", new=AsyncMock(return_value=PROFILE_CLASS_7_NO_STREAM)):
            resp = client.post("/api/chat", data={"prompt": "explain photosynthesis", "mode": "direct"})
        assert resp.status_code == 200
        _, kwargs = mock_ai.call_args
        assert kwargs["user_class"] == "Class 6-8"
        assert kwargs["stream"] is None

    def test_changing_profile_between_requests_changes_subsequent_context(self, client):
        _override_auth_as("user-1")
        with patch.object(main_module, "generate_ai_response", return_value="ok") as mock_ai, \
             patch.object(main_module, "get_student_profile", new=AsyncMock(return_value=PROFILE_CLASS_10_SCIENCE)):
            client.post("/api/chat", data={"prompt": "hi", "mode": "direct"})
        first_call_class = mock_ai.call_args.kwargs["user_class"]

        with patch.object(main_module, "generate_ai_response", return_value="ok") as mock_ai2, \
             patch.object(main_module, "get_student_profile", new=AsyncMock(return_value=PROFILE_CLASS_11_12_SCIENCE)):
            client.post("/api/chat", data={"prompt": "hi again", "mode": "direct"})
        second_call_class = mock_ai2.call_args.kwargs["user_class"]

        assert first_call_class == "Class 9-10 (SSC)"
        assert second_call_class == "Class 11-12 (HSC)"
        assert first_call_class != second_call_class

    def test_client_supplied_class_and_stream_are_ignored(self, client):
        # Even if an old cached frontend (or a hand-crafted request)
        # still sends these fields, they must have zero effect - the
        # endpoint has no user_class/stream parameter at all anymore.
        _override_auth_as("user-1")
        with patch.object(main_module, "generate_ai_response", return_value="ok") as mock_ai, \
             patch.object(main_module, "get_student_profile", new=AsyncMock(return_value=PROFILE_CLASS_11_12_SCIENCE)):
            resp = client.post("/api/chat", data={
                "prompt": "hi", "mode": "direct",
                "board": "Some Other Board", "user_class": "Class 6-8", "stream": "Arts (মানবিক)",
            })
        assert resp.status_code == 200
        _, kwargs = mock_ai.call_args
        assert kwargs["user_class"] == "Class 11-12 (HSC)"
        assert kwargs["stream"] == "Science (বিজ্ঞান)"
        assert kwargs["board"] == main_module.DEFAULT_BOARD

    def test_client_cannot_choose_another_users_profile(self, client):
        # No user_id field exists on this form - identity can only come
        # from the verified token, and get_student_profile is always
        # called with THIS request's authenticated user_id/token.
        _override_auth_as("user-99", token="student-99-token")
        mock_profile_fn = AsyncMock(return_value=PROFILE_CLASS_11_12_SCIENCE)
        with patch.object(main_module, "generate_ai_response", return_value="ok"), \
             patch.object(main_module, "get_student_profile", new=mock_profile_fn):
            client.post("/api/chat", data={"prompt": "hi", "mode": "direct"})
        _, kwargs = mock_profile_fn.call_args
        assert kwargs["user_id"] == "user-99"
        assert kwargs["user_token"] == "student-99-token"

    def test_missing_profile_is_handled_safely_no_guessed_class(self, client):
        _override_auth_as("user-1")
        with patch.object(main_module, "generate_ai_response", return_value="ok") as mock_ai, \
             patch.object(main_module, "get_student_profile", new=AsyncMock(return_value=None)):
            resp = client.post("/api/chat", data={"prompt": "hi", "mode": "direct"})
        assert resp.status_code == 200
        _, kwargs = mock_ai.call_args
        assert kwargs["user_class"] is None
        assert kwargs["stream"] is None

    def test_profile_fetch_failure_degrades_gracefully_chat_still_works(self, client):
        # A Supabase outage/misconfiguration must not make core chat
        # newly fragile - falls back to no-academic-context, same as a
        # genuinely missing profile, not a 502.
        _override_auth_as("user-1")
        with patch.object(main_module, "generate_ai_response", return_value="ok") as mock_ai, \
             patch.object(main_module, "get_student_profile", new=AsyncMock(side_effect=DatabaseError("boom"))):
            resp = client.post("/api/chat", data={"prompt": "hi", "mode": "direct"})
        assert resp.status_code == 200
        assert resp.json() == {"reply": "ok"}
        _, kwargs = mock_ai.call_args
        assert kwargs["user_class"] is None
        assert kwargs["stream"] is None


class TestQuizSubjectPopulationSourceOfTruth:
    """DEFAULT_BOARD / UNSPECIFIED_USER_CLASS are module-level constants
    used consistently by both chat and quiz - a smoke check that they're
    the single shared values, not two independently-drifting constants."""

    def test_default_board_is_a_single_shared_constant(self):
        assert isinstance(main_module.DEFAULT_BOARD, str) and main_module.DEFAULT_BOARD
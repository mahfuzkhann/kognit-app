"""
Tests for the Phase 5B/5C learning-memory wiring inside /api/chat and
/api/chat/title in backend/main.py.

Run with: GEMINI_API_KEY=dummy python3 -m pytest test_chat_learning_memory_integration.py -v

Covers the ONE non-negotiable rule from the implementation brief: a
failure anywhere in learning-memory extraction/persistence must NEVER
turn a successful chat (or chat-title) response into an error, and must
NEVER change the reply/title actually returned to the student.

Auth is stubbed via dependency_overrides on get_current_user_and_token,
same convention as test_rate_limiting.py. generate_ai_response /
generate_chat_title are mocked at the backend.main import site, same
convention as test_quiz_submit_endpoint.py. Each test uses its own
dedicated, namespaced user_id so this file's tests can never collide with
_rate_limit_buckets state from other test files/processes, matching
test_rate_limiting.py's own stated convention.
"""
import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

import pytest
from fastapi.testclient import TestClient

from backend import main as main_module
from backend.database import DatabaseError
from backend.learning_memory import ContextResolution, CONFIDENCE_KNOWN, CONFIDENCE_UNKNOWN


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


# ---------------------------------------------------------------------------
# /api/chat - learning-memory resilience
# ---------------------------------------------------------------------------

class TestChatResilienceToLearningMemoryFailures:
    def test_signal_detection_exception_does_not_break_chat_reply(self, client):
        _override_auth_as("lm-chat-user-1")
        with patch.object(main_module, "generate_ai_response", return_value="Here is your answer."), \
             patch.object(main_module, "detect_learning_signals", side_effect=RuntimeError("boom")):
            resp = client.post("/api/chat", data={"prompt": "What is Newton's second law?", "mode": "direct"})
        assert resp.status_code == 200
        assert resp.json() == {"reply": "Here is your answer."}

    def test_context_resolution_exception_does_not_break_chat_reply(self, client):
        _override_auth_as("lm-chat-user-2")
        with patch.object(main_module, "generate_ai_response", return_value="Here is your answer."), \
             patch.object(main_module, "resolve_academic_context", side_effect=RuntimeError("boom")):
            resp = client.post("/api/chat", data={"prompt": "I don't understand this.", "mode": "direct"})
        assert resp.status_code == 200
        assert resp.json() == {"reply": "Here is your answer."}

    def test_evidence_persistence_failure_does_not_break_chat_reply(self, client):
        """Even when context resolves to Known (forced via mock) and the
        actual database write fails, the chat response must be unaffected -
        this is the core BackgroundTasks resilience guarantee."""
        _override_auth_as("lm-chat-user-3")
        known = ContextResolution(confidence=CONFIDENCE_KNOWN, subject="Physics", topic="Force & Motion")
        with patch.object(main_module, "generate_ai_response", return_value="Here is your answer."), \
             patch.object(main_module, "resolve_academic_context", return_value=known), \
             patch.object(main_module, "save_chat_learning_evidence", new=AsyncMock(side_effect=DatabaseError("db down"))):
            resp = client.post("/api/chat", data={"prompt": "I don't understand this.", "mode": "direct"})
        assert resp.status_code == 200
        assert resp.json() == {"reply": "Here is your answer."}

    def test_no_signal_no_persistence_call(self, client):
        _override_auth_as("lm-chat-user-4")
        with patch.object(main_module, "generate_ai_response", return_value="Here is your answer."), \
             patch.object(main_module, "save_chat_learning_evidence", new=AsyncMock()) as mock_save:
            resp = client.post("/api/chat", data={"prompt": "What is Newton's second law?", "mode": "direct"})
        assert resp.status_code == 200
        mock_save.assert_not_called()

    def test_unknown_context_never_persists_even_with_a_real_signal(self, client):
        """Default, honest, current-architecture behavior: a genuine
        confusion signal is detected, but with no reliable subject/topic
        source, nothing is written to learning_evidence."""
        _override_auth_as("lm-chat-user-5")
        with patch.object(main_module, "generate_ai_response", return_value="Here is your answer."), \
             patch.object(main_module, "save_chat_learning_evidence", new=AsyncMock()) as mock_save:
            resp = client.post("/api/chat", data={"prompt": "I don't understand this at all.", "mode": "direct"})
        assert resp.status_code == 200
        mock_save.assert_not_called()

    def test_known_context_with_real_signal_schedules_persistence(self, client):
        """Confirms the write path itself (subject+topic both present, as
        the still-unused explicit-context path in resolve_academic_context
        would produce for a hypothetical future caller that has a real
        topic - see backend/learning_memory.py's resolution order). The
        genuine, unmocked, subject-only production path is proven
        separately below by the PRODUCTION_ACTIVATION tests."""
        _override_auth_as("lm-chat-user-6")
        known = ContextResolution(confidence=CONFIDENCE_KNOWN, subject="Physics", topic="Force & Motion")
        with patch.object(main_module, "generate_ai_response", return_value="Here is your answer."), \
             patch.object(main_module, "resolve_academic_context", return_value=known), \
             patch.object(main_module, "save_chat_learning_evidence", new=AsyncMock()) as mock_save:
            resp = client.post(
                "/api/chat",
                data={"prompt": "I don't understand this.", "mode": "direct", "chat_id": "chat_abc"},
            )
        assert resp.status_code == 200
        mock_save.assert_awaited_once()
        _, kwargs = mock_save.call_args
        assert kwargs["subject"] == "Physics"
        assert kwargs["topic"] == "Force & Motion"
        assert kwargs["signal_type"] == "confusion"
        assert kwargs["chat_id"] == "chat_abc"

    def test_PRODUCTION_ACTIVATION_real_message_no_mocked_resolver(self, client):
        """
        THE ACTUAL PROOF THIS TASK EXISTS FOR: a real student message,
        with NOTHING about resolve_academic_context mocked or forced -
        the genuine deterministic subject detector runs, genuinely
        resolves Known (from the message text itself), the genuine
        detector finds a genuine confusion signal, and the write is
        genuinely scheduled - end to end, exactly as a real deployment
        would behave. Only the actual network call (save_chat_learning_evidence)
        and the Gemini call are mocked - everything about context
        resolution and signal detection is the real, unmocked code path.
        """
        _override_auth_as("lm-chat-user-8")
        with patch.object(main_module, "generate_ai_response", return_value="Here is your answer."), \
             patch.object(main_module, "save_chat_learning_evidence", new=AsyncMock()) as mock_save:
            resp = client.post(
                "/api/chat",
                data={
                    "prompt": "Physics-e Newton's second law bujhte parchi na.",
                    "mode": "direct",
                    "chat_id": "chat_real_1",
                },
            )
        assert resp.status_code == 200
        assert resp.json() == {"reply": "Here is your answer."}
        mock_save.assert_awaited_once()
        _, kwargs = mock_save.call_args
        assert kwargs["subject"] == "Physics"
        assert kwargs["topic"] is None  # deliberate scope boundary - see backend/learning_memory.py
        assert kwargs["signal_type"] == "confusion"
        assert kwargs["chat_id"] == "chat_real_1"

    def test_PRODUCTION_ACTIVATION_inherited_context_across_two_real_messages(self, client):
        """Case 2 end-to-end, unmocked: an earlier message in the same
        chat's history establishes Physics; the current message has a
        confusion signal but no explicit subject of its own - the real
        resolver inherits Physics as Probable and evidence is still
        written."""
        _override_auth_as("lm-chat-user-9")
        history = '[{"role": "user", "text": "Explain Newton'"'"'s second law of physics."}, ' \
                  '{"role": "bot", "text": "It states that..."}]'
        with patch.object(main_module, "generate_ai_response", return_value="Sure, here's another way."), \
             patch.object(main_module, "save_chat_learning_evidence", new=AsyncMock()) as mock_save:
            resp = client.post(
                "/api/chat",
                data={
                    "prompt": "Aro easy kore bujhao.",
                    "mode": "direct",
                    "chat_id": "chat_real_2",
                    "history": history,
                },
            )
        assert resp.status_code == 200
        mock_save.assert_awaited_once()
        _, kwargs = mock_save.call_args
        assert kwargs["subject"] == "Physics"
        assert kwargs["topic"] is None

    def test_PRODUCTION_ACTIVATION_ambiguous_stream_never_fabricates_subject(self, client):
        """Case 5 end-to-end, unmocked: even though the request declares
        stream=Science, an ambiguous message with no explicit subject
        keyword and no history must NOT produce Physics (or any subject) -
        chat still succeeds, nothing is persisted."""
        _override_auth_as("lm-chat-user-10")
        with patch.object(main_module, "generate_ai_response", return_value="Sure."), \
             patch.object(main_module, "save_chat_learning_evidence", new=AsyncMock()) as mock_save:
            resp = client.post(
                "/api/chat",
                data={"prompt": "Eta bujhte parchi na.", "mode": "direct", "stream": "Science (বিজ্ঞান)"},
            )
        assert resp.status_code == 200
        mock_save.assert_not_called()

    def test_PRODUCTION_ACTIVATION_casual_chat_never_persists(self, client):
        """Case 6 end-to-end, unmocked."""
        _override_auth_as("lm-chat-user-11")
        with patch.object(main_module, "generate_ai_response", return_value="I'm doing well, thanks!"), \
             patch.object(main_module, "save_chat_learning_evidence", new=AsyncMock()) as mock_save:
            resp = client.post("/api/chat", data={"prompt": "How are you?", "mode": "direct"})
        assert resp.status_code == 200
        mock_save.assert_not_called()

    def test_PRODUCTION_ACTIVATION_inherited_probable_context_persists_as_probable_not_known(self, client):
        """
        BUGFIX REGRESSION (attribution-confidence propagation): Case 2's
        inherited context resolves to PROBABLE, not KNOWN (see
        backend.learning_memory.resolve_academic_context). Before the
        fix, backend/database.py:save_chat_learning_evidence hardcoded
        "known" regardless of what was actually resolved, so this exact
        scenario would have silently written "known" to the database.
        This test drives the real, unmocked resolver end-to-end through
        /api/chat and asserts the persisted row is genuinely "probable".
        """
        _override_auth_as("lm-chat-user-12")
        history = '[{"role": "user", "text": "Explain Newton'"'"'s second law of physics."}, ' \
                  '{"role": "bot", "text": "It states that..."}]'
        with patch.object(main_module, "generate_ai_response", return_value="Sure, here's another way."), \
             patch.object(main_module, "save_chat_learning_evidence", new=AsyncMock()) as mock_save:
            resp = client.post(
                "/api/chat",
                data={
                    "prompt": "Aro easy kore bujhao.",
                    "mode": "direct",
                    "chat_id": "chat_probable_1",
                    "history": history,
                },
            )
        assert resp.status_code == 200
        mock_save.assert_awaited_once()
        _, kwargs = mock_save.call_args
        assert kwargs["attribution_confidence"] == "probable"
        assert kwargs["attribution_confidence"] != "known"
        assert kwargs["subject"] == "Physics"

    def test_PRODUCTION_ACTIVATION_explicit_known_context_persists_as_known(self, client):
        """Companion to the probable-context regression test above -
        confirms the positive case (an explicit, current-message subject
        match) still correctly persists as "known", not just that
        "probable" no longer gets clobbered."""
        _override_auth_as("lm-chat-user-13")
        with patch.object(main_module, "generate_ai_response", return_value="Sure."), \
             patch.object(main_module, "save_chat_learning_evidence", new=AsyncMock()) as mock_save:
            resp = client.post(
                "/api/chat",
                data={
                    "prompt": "Physics-e Newton's second law bujhte parchi na.",
                    "mode": "direct",
                    "chat_id": "chat_known_1",
                },
            )
        assert resp.status_code == 200
        mock_save.assert_awaited_once()
        _, kwargs = mock_save.call_args
        assert kwargs["attribution_confidence"] == "known"

    def test_ai_engine_failure_is_unrelated_to_learning_memory_and_still_handled(self, client):
        """Pre-existing behavior (backend/main.py's own outer try/except)
        must survive this change untouched: a Gemini failure still returns
        the existing generic safe message, not a stack trace."""
        _override_auth_as("lm-chat-user-7")
        with patch.object(main_module, "generate_ai_response", side_effect=RuntimeError("gemini down")):
            resp = client.post("/api/chat", data={"prompt": "hello", "mode": "direct"})
        assert resp.status_code == 200
        assert "went wrong" in resp.json()["reply"].lower()


# ---------------------------------------------------------------------------
# /api/chat/title - conversation_index resilience
# ---------------------------------------------------------------------------

class TestChatTitleResilienceToLearningMemoryFailures:
    HISTORY = '[{"role": "user", "text": "What is Newton'"'"'s second law?"}, {"role": "bot", "text": "It states..."}]'

    def test_title_still_returned_when_persistence_fails(self, client):
        _override_auth_as("lm-title-user-1")
        with patch.object(main_module, "generate_chat_title", return_value="Newton's Laws"), \
             patch.object(main_module, "save_conversation_index_entry", new=AsyncMock(side_effect=DatabaseError("db down"))):
            resp = client.post("/api/chat/title", data={"history": self.HISTORY, "chat_id": "chat_1"})
        assert resp.status_code == 200
        assert resp.json() == {"title": "Newton's Laws"}

    def test_title_still_returned_when_context_resolution_raises(self, client):
        _override_auth_as("lm-title-user-2")
        with patch.object(main_module, "generate_chat_title", return_value="Newton's Laws"), \
             patch.object(main_module, "resolve_academic_context", side_effect=RuntimeError("boom")):
            resp = client.post("/api/chat/title", data={"history": self.HISTORY, "chat_id": "chat_1"})
        assert resp.status_code == 200
        assert resp.json() == {"title": "Newton's Laws"}

    def test_no_chat_id_skips_persistence_entirely(self, client):
        _override_auth_as("lm-title-user-3")
        with patch.object(main_module, "generate_chat_title", return_value="Newton's Laws"), \
             patch.object(main_module, "save_conversation_index_entry", new=AsyncMock()) as mock_save:
            resp = client.post("/api/chat/title", data={"history": self.HISTORY})  # no chat_id
        assert resp.status_code == 200
        assert resp.json() == {"title": "Newton's Laws"}
        mock_save.assert_not_called()

    def test_no_title_generated_skips_persistence(self, client):
        _override_auth_as("lm-title-user-4")
        with patch.object(main_module, "generate_chat_title", return_value=None), \
             patch.object(main_module, "save_conversation_index_entry", new=AsyncMock()) as mock_save:
            resp = client.post("/api/chat/title", data={"history": self.HISTORY, "chat_id": "chat_1"})
        assert resp.status_code == 200
        mock_save.assert_not_called()

    def test_successful_title_with_chat_id_schedules_conversation_index_write(self, client):
        _override_auth_as("lm-title-user-5")
        with patch.object(main_module, "generate_chat_title", return_value="Newton's Laws"), \
             patch.object(main_module, "save_conversation_index_entry", new=AsyncMock()) as mock_save:
            resp = client.post("/api/chat/title", data={"history": self.HISTORY, "chat_id": "chat_1"})
        assert resp.status_code == 200
        mock_save.assert_awaited_once()
        _, kwargs = mock_save.call_args
        assert kwargs["chat_id"] == "chat_1"
        assert kwargs["short_label"] == "Newton's Laws"
        assert kwargs["attribution_confidence"] == CONFIDENCE_UNKNOWN  # honest default today


# ---------------------------------------------------------------------------
# Regression: rate limiting and auth on these endpoints must be unaffected
# by the (user_id, token) tuple refactor of _rate_limited_chat /
# _rate_limited_chat_title.
# ---------------------------------------------------------------------------

class TestRateLimitDependencyRefactorRegression:
    def test_chat_endpoint_still_requires_auth(self, client):
        resp = client.post("/api/chat", data={"prompt": "hello"})
        assert resp.status_code == 401

    def test_chat_title_endpoint_still_requires_auth(self, client):
        resp = client.post("/api/chat/title", data={"history": "[]"})
        assert resp.status_code == 401
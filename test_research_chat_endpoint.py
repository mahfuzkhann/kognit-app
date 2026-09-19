"""
Phase 7C tests: /api/chat's research integration. Mocking convention
matches test_chat_academic_context.py exactly (generate_ai_response and
get_student_profile patched at the backend.main import site, auth via
dependency_overrides) - kept as a separate file, not merged into an
existing test file, per "do not remove or weaken existing tests."
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

import pytest
from fastapi.testclient import TestClient
from google.genai import types

from backend import main as main_module
from backend.ai_engine import AIGenerationResult


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


PROFILE = {
    "user_id": "user-1", "name": "Student", "user_class": "Class 9-10 (SSC)",
    "stream": "Science (বিজ্ঞান)", "created_at": "x", "updated_at": "x",
}


class TestNonResearchPathUnaffected:
    def test_stable_question_does_not_request_research_kwargs(self, client):
        _override_auth_as("user-1")
        with patch.object(main_module, "generate_ai_response", return_value="6 N") as mock_ai, \
             patch.object(main_module, "get_student_profile", new=AsyncMock(return_value=PROFILE)):
            resp = client.post("/api/chat", data={"prompt": "Solve x^2-5x+6=0", "mode": "direct"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["reply"] == "6 N"
        assert "research" not in body  # never present on a non-research answer

        _, kwargs = mock_ai.call_args
        assert "return_metadata" not in kwargs  # byte-identical call to pre-Phase-7C
        assert "enable_research" not in kwargs

    def test_reply_field_shape_is_unchanged_for_normal_questions(self, client):
        _override_auth_as("user-1")
        with patch.object(main_module, "generate_ai_response", return_value="An explanation.") as mock_ai, \
             patch.object(main_module, "get_student_profile", new=AsyncMock(return_value=PROFILE)):
            resp = client.post("/api/chat", data={"prompt": "Explain photosynthesis.", "mode": "direct"})
        assert resp.json() == {"reply": "An explanation."}


class TestResearchPath:
    def _grounded_result(self, text="The current rate is 110 BDT/USD."):
        metadata = types.GroundingMetadata(
            web_search_queries=["current BDT USD exchange rate"],
            grounding_chunks=[types.GroundingChunk(web=types.GroundingChunkWeb(
                title="Bangladesh Bank", uri="https://www.bb.org.bd/", domain="bb.org.bd",
            ))],
            grounding_supports=[types.GroundingSupport(
                segment=types.Segment(start_index=0, end_index=10, text="current rate"),
                grounding_chunk_indices=[0],
            )],
        )
        return AIGenerationResult(text=text, is_error=False, grounding_metadata=metadata)

    def test_research_triggering_question_calls_with_correct_kwargs(self, client):
        _override_auth_as("user-1")
        with patch.object(main_module, "generate_ai_response", return_value=self._grounded_result()) as mock_ai, \
             patch.object(main_module, "get_student_profile", new=AsyncMock(return_value=PROFILE)):
            client.post("/api/chat", data={"prompt": "What is the current BDT to USD exchange rate?", "mode": "direct"})

        _, kwargs = mock_ai.call_args
        assert kwargs["return_metadata"] is True
        assert kwargs["enable_research"] is True

    def test_research_response_includes_normalized_sources_and_citations(self, client):
        _override_auth_as("user-1")
        with patch.object(main_module, "generate_ai_response", return_value=self._grounded_result()), \
             patch.object(main_module, "get_student_profile", new=AsyncMock(return_value=PROFILE)):
            resp = client.post("/api/chat", data={"prompt": "What is the current BDT to USD exchange rate?", "mode": "direct"})

        body = resp.json()
        assert body["reply"] == "The current rate is 110 BDT/USD."
        assert "research" in body
        assert body["research"]["research_used"] is True
        assert body["research"]["grounding_status"] == "used"
        assert body["research"]["sources"][0]["domain"] == "bb.org.bd"
        assert body["research"]["citations"][0]["citation_status"] == "cited_with_source"

    def test_research_call_that_errors_gets_no_research_payload(self, client):
        # An error string (quota exhausted, blocked, etc.) must never be
        # decorated with a research payload - Kognit must not imply an
        # error reply was web-grounded.
        _override_auth_as("user-1")
        error_result = AIGenerationResult(text="Sorry, the AI service is temporarily busy...", is_error=True, grounding_metadata=None)
        with patch.object(main_module, "generate_ai_response", return_value=error_result), \
             patch.object(main_module, "get_student_profile", new=AsyncMock(return_value=PROFILE)):
            resp = client.post("/api/chat", data={"prompt": "What is the current BDT to USD exchange rate?", "mode": "direct"})

        body = resp.json()
        assert "research" not in body
        assert body["reply"] == error_result.text

    def test_research_requested_but_no_grounding_metadata_is_failed_not_used(self, client):
        _override_auth_as("user-1")
        no_grounding_result = AIGenerationResult(text="I don't have current data on that.", is_error=False, grounding_metadata=None)
        with patch.object(main_module, "generate_ai_response", return_value=no_grounding_result), \
             patch.object(main_module, "get_student_profile", new=AsyncMock(return_value=PROFILE)):
            resp = client.post("/api/chat", data={"prompt": "What is the current BDT to USD exchange rate?", "mode": "direct"})

        body = resp.json()
        assert body["research"]["research_used"] is False
        assert body["research"]["grounding_status"] == "failed"

    def test_model_chose_not_to_search_is_not_used_not_failed(self, client):
        # The tool was enabled but the model genuinely decided the
        # question didn't need a search after all - a real, correct
        # outcome, distinct from a failure.
        _override_auth_as("user-1")
        metadata = types.GroundingMetadata(web_search_queries=[], grounding_chunks=[], grounding_supports=[])
        result = AIGenerationResult(text="Some answer.", is_error=False, grounding_metadata=metadata)
        with patch.object(main_module, "generate_ai_response", return_value=result), \
             patch.object(main_module, "get_student_profile", new=AsyncMock(return_value=PROFILE)):
            resp = client.post("/api/chat", data={"prompt": "What is the current BDT to USD exchange rate?", "mode": "direct"})

        body = resp.json()
        assert body["research"]["grounding_status"] == "not_used"
        assert body["research"]["research_used"] is False

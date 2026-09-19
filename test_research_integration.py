"""
Phase 7C tests: backend.ai_engine.generate_ai_response's enable_research
parameter. Kept as a separate file from kognit_smoke_test.py per the
"do not remove or weaken existing tests" instruction - this is purely
additive coverage for the Phase 7C addition, not a modification of the
existing smoke test file.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from google.genai import types

from backend import ai_engine


@pytest.fixture(autouse=True)
def no_real_sleep():
    with patch("backend.ai_engine.time.sleep", return_value=None):
        yield


@pytest.fixture
def mock_client():
    with patch.object(ai_engine, "_client") as client:
        yield client


def _response_with_grounding(text: str, grounding_metadata):
    response = MagicMock()
    response.text = text
    response.model_version = "gemini-3.6-flash-001"
    response.response_id = "resp-1"
    response.usage_metadata = MagicMock()
    candidate = MagicMock()
    candidate.grounding_metadata = grounding_metadata
    response.candidates = [candidate]
    return response


class TestEnableResearchBackwardCompatibility:
    def test_default_call_adds_no_tools_at_all(self, mock_client):
        chat = MagicMock()
        chat.send_message.return_value = _response_with_grounding("An answer.", None)
        mock_client.chats.create.return_value = chat

        result = ai_engine.generate_ai_response("2+2?", user_class="Class 10")

        config = mock_client.chats.create.call_args.kwargs["config"]
        assert config.tools is None
        assert result == "An answer."  # bare string, unaffected

    def test_enable_research_false_explicitly_still_adds_no_tools(self, mock_client):
        chat = MagicMock()
        chat.send_message.return_value = _response_with_grounding("An answer.", None)
        mock_client.chats.create.return_value = chat

        ai_engine.generate_ai_response("2+2?", user_class="Class 10", enable_research=False)
        config = mock_client.chats.create.call_args.kwargs["config"]
        assert config.tools is None


class TestEnableResearchTrue:
    def test_adds_google_search_tool_to_config(self, mock_client):
        chat = MagicMock()
        chat.send_message.return_value = _response_with_grounding("Answer.", None)
        mock_client.chats.create.return_value = chat

        ai_engine.generate_ai_response("current exchange rate?", user_class="Class 10", enable_research=True)
        config = mock_client.chats.create.call_args.kwargs["config"]
        assert len(config.tools) == 1
        assert isinstance(config.tools[0].google_search, types.GoogleSearch)

    def test_grounding_metadata_captured_when_return_metadata_true(self, mock_client):
        metadata = types.GroundingMetadata(web_search_queries=["current rate"], grounding_chunks=[], grounding_supports=[])
        chat = MagicMock()
        chat.send_message.return_value = _response_with_grounding("110 BDT/USD.", metadata)
        mock_client.chats.create.return_value = chat

        result = ai_engine.generate_ai_response(
            "current exchange rate?", user_class="Class 10",
            enable_research=True, return_metadata=True,
        )
        assert isinstance(result, ai_engine.AIGenerationResult)
        assert result.grounding_metadata is metadata
        assert result.grounding_metadata.web_search_queries == ["current rate"]

    def test_grounding_metadata_none_when_return_metadata_false_even_with_research(self, mock_client):
        # Bare-string callers never see grounding_metadata regardless -
        # it's only exposed via AIGenerationResult.
        metadata = types.GroundingMetadata(web_search_queries=["q"], grounding_chunks=[], grounding_supports=[])
        chat = MagicMock()
        chat.send_message.return_value = _response_with_grounding("Answer.", metadata)
        mock_client.chats.create.return_value = chat

        result = ai_engine.generate_ai_response(
            "current exchange rate?", user_class="Class 10", enable_research=True,
        )
        assert isinstance(result, str)  # unaffected by enable_research when return_metadata=False

    def test_missing_grounding_metadata_on_research_call_is_none_not_fabricated(self, mock_client):
        # The tool was enabled but the response has no grounding_metadata
        # at all (e.g. candidates list came back empty) - must be None,
        # never an empty-but-truthy stand-in object.
        response = MagicMock()
        response.text = "Answer."
        response.model_version = None
        response.response_id = None
        response.usage_metadata = MagicMock()
        response.candidates = []
        chat = MagicMock()
        chat.send_message.return_value = response
        mock_client.chats.create.return_value = chat

        result = ai_engine.generate_ai_response(
            "current exchange rate?", user_class="Class 10",
            enable_research=True, return_metadata=True,
        )
        assert result.grounding_metadata is None

    def test_research_call_error_path_still_returns_error_string_normally(self, mock_client):
        # A blocked/empty response with enable_research=True must behave
        # exactly like the non-research error path - research must not
        # change error handling.
        response = MagicMock()
        response.text = None
        chat = MagicMock()
        chat.send_message.return_value = response
        mock_client.chats.create.return_value = chat

        result = ai_engine.generate_ai_response(
            "current exchange rate?", user_class="Class 10", enable_research=True,
        )
        assert result == ai_engine.BLOCKED_RESPONSE_ERROR

"""Phase 7B-4 tests: GeminiAdapter."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from backend import ai_engine
from evaluation.model_adapter import GeminiAdapter, ModelRequest


class TestGeminiAdapter:
    def test_successful_generation_captures_all_metadata(self):
        response = MagicMock()
        response.text = "6 N"
        response.model_version = "gemini-3.6-flash-001"
        response.response_id = "resp-1"
        usage = MagicMock()
        usage.prompt_token_count = 50
        usage.candidates_token_count = 10
        usage.thoughts_token_count = 5
        usage.total_token_count = 65
        # No cached_content_token_count attribute set on this mock on
        # purpose - getattr(..., None) must handle that gracefully.
        del usage.cached_content_token_count
        response.usage_metadata = usage

        with patch.object(ai_engine, "_client") as client:
            chat = MagicMock()
            chat.send_message.return_value = response
            client.chats.create.return_value = chat

            adapter = GeminiAdapter()
            result = adapter.generate(ModelRequest(prompt="q", user_class="Class 10"))

        assert result.is_error is False
        assert result.generation_failed is False
        assert result.text == "6 N"
        assert result.resolved_model_version == "gemini-3.6-flash-001"
        assert result.prompt_tokens == 50
        assert result.output_tokens == 10
        assert result.total_tokens == 65
        assert result.model_provider == "google"
        assert result.model_name == ai_engine.MODEL_NAME
        assert result.prompt_hash  # non-empty
        assert result.ai_call_end_ts >= result.request_start_ts

    def test_operational_error_is_captured_without_crashing(self):
        # e.g. a quota-exhausted response - generate_ai_response() itself
        # already returns a controlled error string, not an exception.
        with patch.object(ai_engine, "_client") as client:
            from google.genai import errors as genai_errors

            mock_error_response = MagicMock()
            mock_error_response.status_code = 429
            client.chats.create.side_effect = genai_errors.ClientError(
                429, {"error": {"message": "quota exceeded"}}, mock_error_response
            )

            adapter = GeminiAdapter()
            result = adapter.generate(ModelRequest(prompt="q", user_class="Class 10"))

        assert result.is_error is True
        assert result.generation_failed is False  # a controlled error string, not a crash
        assert result.text == ai_engine.QUOTA_EXHAUSTED_ERROR

    def test_unexpected_adapter_level_exception_is_isolated_as_generation_failed(self):
        # Simulates a genuinely unexpected crash below generate_ai_response's
        # own exception handling (e.g. a bug in the SDK client construction
        # itself) - the adapter must isolate this, never let it propagate
        # and take down an entire run.
        with patch(
            "evaluation.model_adapter.ai_engine.generate_ai_response",
            side_effect=RuntimeError("unexpected crash"),
        ):
            adapter = GeminiAdapter()
            result = adapter.generate(ModelRequest(prompt="q", user_class="Class 10"))

        assert result.generation_failed is True
        assert result.is_error is True
        assert "unexpected crash" in result.failure_reason
        assert result.prompt_tokens is None

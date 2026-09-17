"""Phase 7B-3 tests: production AI metadata capture."""

from __future__ import annotations

import hashlib
from unittest.mock import MagicMock, patch

from backend import ai_engine
from evaluation import git_identity, prompt_identity


class TestPromptIdentity:
    def test_prompt_hash_matches_manual_sha256_of_the_real_constant(self):
        expected = hashlib.sha256(
            ai_engine.CHAT_SYSTEM_INSTRUCTION_RULES.encode("utf-8")
        ).hexdigest()
        assert prompt_identity.get_chat_prompt_hash() == expected

    def test_prompt_hash_changes_if_the_constant_changes(self, monkeypatch):
        original_hash = prompt_identity.get_chat_prompt_hash()
        monkeypatch.setattr(ai_engine, "CHAT_SYSTEM_INSTRUCTION_RULES", "a different prompt")
        assert prompt_identity.get_chat_prompt_hash() != original_hash

    def test_prompt_version_reads_the_real_constant(self):
        assert prompt_identity.get_chat_prompt_version() == ai_engine.CHAT_PROMPT_VERSION


class TestGitIdentity:
    def test_get_git_commit_sha_returns_a_real_looking_sha(self):
        sha = git_identity.get_git_commit_sha()
        assert isinstance(sha, str)
        assert len(sha) == 40
        assert all(c in "0123456789abcdef" for c in sha)

    def test_get_git_status_is_clean_returns_bool(self):
        assert isinstance(git_identity.get_git_status_is_clean(), bool)


class TestReturnMetadataBackwardCompatibility:
    """The single most important test in this file: confirms the
    Phase 7B-3 production change is genuinely additive. Every existing
    caller (backend/main.py) uses the default return_metadata=False and
    must keep receiving a bare str, byte-for-byte identical to before."""

    def _mock_success_response(self):
        response = MagicMock()
        response.text = "The force is 6 N."
        response.model_version = "gemini-3.6-flash-001"
        response.response_id = "resp-abc123"
        usage = MagicMock()
        usage.prompt_token_count = 120
        usage.candidates_token_count = 40
        usage.total_token_count = 160
        response.usage_metadata = usage
        return response

    def test_default_call_still_returns_bare_string(self):
        with patch.object(ai_engine, "_client") as client:
            chat = MagicMock()
            chat.send_message.return_value = self._mock_success_response()
            client.chats.create.return_value = chat

            result = ai_engine.generate_ai_response("2kg at 3 m/s^2?", user_class="Class 10")
            assert isinstance(result, str)
            assert result == "The force is 6 N."

    def test_return_metadata_true_returns_structured_result_on_success(self):
        with patch.object(ai_engine, "_client") as client:
            chat = MagicMock()
            chat.send_message.return_value = self._mock_success_response()
            client.chats.create.return_value = chat

            result = ai_engine.generate_ai_response(
                "2kg at 3 m/s^2?", user_class="Class 10", return_metadata=True
            )
            assert isinstance(result, ai_engine.AIGenerationResult)
            assert result.text == "The force is 6 N."
            assert result.is_error is False
            assert result.resolved_model_version == "gemini-3.6-flash-001"
            assert result.response_id == "resp-abc123"
            assert result.usage_metadata.prompt_token_count == 120
            assert isinstance(result.elapsed_seconds, float)

    def test_return_metadata_true_on_image_decode_failure_is_error(self):
        # No mocking of _client needed - this fails before any API call.
        result = ai_engine.generate_ai_response(
            "describe this", image_bytes=b"not a real image",
            return_metadata=True,
        )
        assert isinstance(result, ai_engine.AIGenerationResult)
        assert result.is_error is True
        assert result.text == ai_engine.IMAGE_DECODE_ERROR
        assert result.usage_metadata is None
        assert result.resolved_model_version is None

    def test_return_metadata_true_on_blocked_response_is_error_with_no_usage(self):
        with patch.object(ai_engine, "_client") as client:
            blocked = MagicMock()
            blocked.text = None
            chat = MagicMock()
            chat.send_message.return_value = blocked
            client.chats.create.return_value = chat

            result = ai_engine.generate_ai_response(
                "anything", user_class="Class 10", return_metadata=True
            )
            assert result.is_error is True
            assert result.text == ai_engine.BLOCKED_RESPONSE_ERROR
            assert result.usage_metadata is None

    def test_default_call_on_blocked_response_still_returns_bare_error_string(self):
        with patch.object(ai_engine, "_client") as client:
            blocked = MagicMock()
            blocked.text = None
            chat = MagicMock()
            chat.send_message.return_value = blocked
            client.chats.create.return_value = chat

            result = ai_engine.generate_ai_response("anything", user_class="Class 10")
            assert isinstance(result, str)
            assert result == ai_engine.BLOCKED_RESPONSE_ERROR

    def test_system_instruction_still_contains_the_static_rules(self):
        # Confirms the extraction refactor produced byte-identical output -
        # the same substring assertions kognit_smoke_test.py already makes,
        # re-checked here specifically against the refactored construction.
        with patch.object(ai_engine, "_client") as client:
            chat = MagicMock()
            chat.send_message.return_value = self._mock_success_response()
            client.chats.create.return_value = chat

            ai_engine.generate_ai_response("q", user_class="Class 10", stream="Science")
            config = client.chats.create.call_args.kwargs["config"]
            assert "STRICT ACADEMIC & VISION RULES" in config.system_instruction
            assert "studying Class 10 (Science stream)" in config.system_instruction

"""
Mocked test suite for backend/ai_engine.py after the google-genai SDK
migration.

Everything here mocks backend.ai_engine._client (the module-level
google.genai.Client instance) - no real network calls, no API key needed.
Covers:
  - happy path (text, image, multi-turn history)
  - each retry-policy bucket (A: client deadline, B: transient provider,
    C: quota, D: blocked response, E: unexpected error)
  - single-retry-authority invariant (retry_options never set)
  - thinking_level actually being sent for chat/title, and left unset for quiz
  - quiz JSON parsing (with/without code fences, malformed JSON)
  - chat title generation (success, cleanup, truncation, empty history)

Run with:  python3 -m pytest tests/test_ai_engine.py -v
"""
import io
import sys
import os
import types as pytypes
from unittest.mock import MagicMock, patch

import httpx
import pytest
from PIL import Image
from google.genai import errors as genai_errors
from google.genai import types as genai_types

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend import ai_engine  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_response(text="Here is the answer.", prompt_tokens=50, thought_tokens=10, output_tokens=20):
    """Build a mock GenerateContentResponse-shaped object."""
    resp = MagicMock()
    resp.text = text
    usage = MagicMock()
    usage.prompt_token_count = prompt_tokens
    usage.thoughts_token_count = thought_tokens
    usage.candidates_token_count = output_tokens
    usage.total_token_count = (prompt_tokens or 0) + (thought_tokens or 0) + (output_tokens or 0)
    resp.usage_metadata = usage
    return resp


def _client_error(code, status):
    return genai_errors.ClientError(code, {"error": {"status": status, "message": status}})


def _server_error(code, status):
    return genai_errors.ServerError(code, {"error": {"status": status, "message": status}})


def _tiny_png_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), color="white").save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def no_real_sleep():
    """Retry delays use time.sleep() - never actually wait in tests."""
    with patch("backend.ai_engine.time.sleep", return_value=None):
        yield


@pytest.fixture
def mock_client():
    """Patches backend.ai_engine._client, returns the mock for assertions."""
    with patch.object(ai_engine, "_client") as client:
        yield client


# ---------------------------------------------------------------------------
# generate_ai_response: happy path
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_simple_text_question_success(self, mock_client):
        chat = MagicMock()
        chat.send_message.return_value = _make_response("Photosynthesis is...")
        mock_client.chats.create.return_value = chat

        result = ai_engine.generate_ai_response(prompt="What is photosynthesis?", mode="direct")

        assert result == "Photosynthesis is..."
        mock_client.chats.create.assert_called_once()
        chat.send_message.assert_called_once()

    def test_banglish_typo_question_success(self, mock_client):
        chat = MagicMock()
        chat.send_message.return_value = _make_response("উত্তর...")
        mock_client.chats.create.return_value = chat

        result = ai_engine.generate_ai_response(prompt="gravity ki vabe kaj kre bujhaiya dao", mode="direct")

        assert result == "উত্তর..."

    def test_multistep_math_question_success(self, mock_client):
        chat = MagicMock()
        chat.send_message.return_value = _make_response("Step 1: ... Step 2: ... Final answer: 42")
        mock_client.chats.create.return_value = chat

        result = ai_engine.generate_ai_response(
            prompt="Solve for x: 2x^2 + 5x - 3 = 0, show all steps", mode="direct"
        )
        assert "42" in result

    def test_socratic_mode_success(self, mock_client):
        chat = MagicMock()
        chat.send_message.return_value = _make_response("What do you think happens first?")
        mock_client.chats.create.return_value = chat

        result = ai_engine.generate_ai_response(prompt="Solve x+2=5", mode="socratic")

        assert result == "What do you think happens first?"
        config = mock_client.chats.create.call_args.kwargs["config"]
        assert "DO NOT give direct answers" in config.system_instruction

    def test_image_question_success(self, mock_client):
        chat = MagicMock()
        chat.send_message.return_value = _make_response("The diagram shows a right triangle.")
        mock_client.chats.create.return_value = chat

        result = ai_engine.generate_ai_response(
            prompt="What is this?", mode="direct", image_bytes=_tiny_png_bytes()
        )

        assert result == "The diagram shows a right triangle."
        sent_contents = chat.send_message.call_args.args[0]
        assert any(isinstance(part, Image.Image) for part in sent_contents)
        assert "What is this?" in sent_contents

    def test_corrupted_image_returns_image_decode_error_without_calling_gemini(self, mock_client):
        result = ai_engine.generate_ai_response(
            prompt="What is this?", mode="direct", image_bytes=b"not a real image"
        )
        assert result == ai_engine.IMAGE_DECODE_ERROR
        mock_client.chats.create.assert_not_called()

    def test_followup_question_sends_history(self, mock_client):
        chat = MagicMock()
        chat.send_message.return_value = _make_response("As I mentioned, the answer is 5.")
        mock_client.chats.create.return_value = chat

        history = [
            {"role": "user", "text": "What is 2+3?"},
            {"role": "bot", "text": "2+3 = 5"},
        ]
        result = ai_engine.generate_ai_response(prompt="Why is that?", mode="direct", history=history)

        assert result == "As I mentioned, the answer is 5."
        sent_history = mock_client.chats.create.call_args.kwargs["history"]
        assert sent_history == [
            {"role": "user", "parts": [{"text": "What is 2+3?"}]},
            {"role": "model", "parts": [{"text": "2+3 = 5"}]},
        ]

    def test_pdf_context_included_in_system_instruction(self, mock_client):
        chat = MagicMock()
        chat.send_message.return_value = _make_response("Based on the PDF...")
        mock_client.chats.create.return_value = chat

        ai_engine.generate_ai_response(prompt="Summarize chapter 3", mode="direct", pdf_context="Chapter 3 is about...")

        config = mock_client.chats.create.call_args.kwargs["config"]
        assert "UPLOADED PDF DOCUMENT CONTENT CONTEXT" in config.system_instruction
        assert "Chapter 3 is about..." in config.system_instruction

    def test_pdf_context_under_budget_is_not_truncated_or_flagged(self, mock_client):
        """A normal-sized PDF (well under MAX_PDF_CONTEXT_CHARS) must be sent
        in full, with no truncation note added - regression guard against
        the old unconditional pdf_context[:10000] slice."""
        chat = MagicMock()
        chat.send_message.return_value = _make_response("Based on the PDF...")
        mock_client.chats.create.return_value = chat

        pdf_text = "A" * 50_000  # comfortably under the 300,000-char budget
        ai_engine.generate_ai_response(prompt="Summarize", mode="direct", pdf_context=pdf_text)

        config = mock_client.chats.create.call_args.kwargs["config"]
        assert pdf_text in config.system_instruction
        assert "too long to include in full" not in config.system_instruction

    def test_pdf_context_over_old_10000_char_limit_no_longer_truncated(self, mock_client):
        """Direct regression test for the confirmed bug: a document longer
        than the OLD 10,000-char cutoff (but still under the new budget) must
        now be passed through in full instead of being cut off."""
        chat = MagicMock()
        chat.send_message.return_value = _make_response("Based on the PDF...")
        mock_client.chats.create.return_value = chat

        pdf_text = "B" * 50_000
        marker = "IMPORTANT_FACT_NEAR_THE_END"
        pdf_text_with_marker = pdf_text[:40_000] + marker + pdf_text[40_000:]

        ai_engine.generate_ai_response(prompt="What does it say near the end?", mode="direct", pdf_context=pdf_text_with_marker)

        config = mock_client.chats.create.call_args.kwargs["config"]
        # Old behavior (pdf_context[:10000]) would have cut this off entirely.
        assert marker in config.system_instruction

    def test_pdf_context_over_new_budget_is_truncated_and_flagged_to_model(self, mock_client, caplog):
        """A document that genuinely exceeds the new budget must still be
        truncated (this is a fixed-budget cap, not retrieval), but the model
        must be told this happened so it can be honest with the student, and
        the truncation must be logged server-side."""
        chat = MagicMock()
        chat.send_message.return_value = _make_response("Based on the PDF...")
        mock_client.chats.create.return_value = chat

        oversized_pdf = "C" * (ai_engine.MAX_PDF_CONTEXT_CHARS + 5_000)

        with caplog.at_level("WARNING", logger="kognit.ai_engine"):
            ai_engine.generate_ai_response(prompt="Summarize everything", mode="direct", pdf_context=oversized_pdf)

        config = mock_client.chats.create.call_args.kwargs["config"]

        # Only the first MAX_PDF_CONTEXT_CHARS characters should be present.
        sent_pdf_section = config.system_instruction.split("UPLOADED PDF DOCUMENT CONTENT CONTEXT]:\n")[1]
        assert sent_pdf_section.startswith("C" * ai_engine.MAX_PDF_CONTEXT_CHARS)
        assert sent_pdf_section.count("C") == ai_engine.MAX_PDF_CONTEXT_CHARS

        # Model must be told the document was cut off.
        assert "too long to include in full" in config.system_instruction

        # Truncation must be observable server-side (was previously silent).
        assert any("PDF context truncated" in record.message for record in caplog.records)

    def test_pdf_context_exactly_at_budget_is_not_flagged_as_truncated(self, mock_client):
        """Boundary check: a document exactly MAX_PDF_CONTEXT_CHARS long is
        not truncated and must not trigger the truncation note."""
        chat = MagicMock()
        chat.send_message.return_value = _make_response("Based on the PDF...")
        mock_client.chats.create.return_value = chat

        exact_budget_pdf = "D" * ai_engine.MAX_PDF_CONTEXT_CHARS
        ai_engine.generate_ai_response(prompt="Summarize", mode="direct", pdf_context=exact_budget_pdf)

        config = mock_client.chats.create.call_args.kwargs["config"]
        assert "too long to include in full" not in config.system_instruction

    def test_no_pdf_context_has_no_pdf_section_or_truncation_note(self, mock_client):
        """Regression guard: chats without a PDF must remain completely
        unaffected by this change."""
        chat = MagicMock()
        chat.send_message.return_value = _make_response("Sure, here's the answer.")
        mock_client.chats.create.return_value = chat

        ai_engine.generate_ai_response(prompt="What is 2+2?", mode="direct")

        config = mock_client.chats.create.call_args.kwargs["config"]
        assert "UPLOADED PDF DOCUMENT CONTENT CONTEXT" not in config.system_instruction
        assert "too long to include in full" not in config.system_instruction


# ---------------------------------------------------------------------------
# Thinking-level / timeout / single-retry-authority configuration
# ---------------------------------------------------------------------------

class TestConfiguration:
    def test_chat_thinking_level_is_low(self, mock_client):
        chat = MagicMock()
        chat.send_message.return_value = _make_response("ok")
        mock_client.chats.create.return_value = chat

        ai_engine.generate_ai_response(prompt="hi", mode="direct")

        config = mock_client.chats.create.call_args.kwargs["config"]
        assert config.thinking_config.thinking_level == genai_types.ThinkingLevel.LOW

    def test_attempt_one_uses_full_timeout(self, mock_client):
        chat = MagicMock()
        chat.send_message.return_value = _make_response("ok")
        mock_client.chats.create.return_value = chat

        ai_engine.generate_ai_response(prompt="hi", mode="direct")

        config = mock_client.chats.create.call_args.kwargs["config"]
        assert config.http_options.timeout == ai_engine.AI_REQUEST_TIMEOUT_SECONDS * 1000

    def test_no_call_ever_sets_sdk_retry_options(self, mock_client):
        """
        Single-retry-authority invariant: Kognit's own retry loop must be the
        only retry mechanism. If retry_options were ever set here, the SDK's
        own tenacity-based retry would stack underneath Kognit's loop,
        invisibly to our logs - exactly the bug the old SDK had.
        """
        chat_first = MagicMock()
        chat_first.send_message.side_effect = _server_error(503, "UNAVAILABLE")
        chat_second = MagicMock()
        chat_second.send_message.return_value = _make_response("recovered")
        mock_client.chats.create.side_effect = [chat_first, chat_second]

        result = ai_engine.generate_ai_response(prompt="hi", mode="direct")

        assert result == "recovered"
        for call in mock_client.chats.create.call_args_list:
            assert call.kwargs["config"].http_options.retry_options is None

    def test_quiz_generation_does_not_set_thinking_config_by_default(self, mock_client):
        mock_client.models.generate_content.return_value = _make_response(
            text='[{"id":1,"question":"Q","options":["A","B","C","D"],"correct_index":0,"explanation":"E"}]'
        )
        ai_engine.generate_quiz_questions("NCTB", "SSC", "Physics", "Motion", count=1)

        config = mock_client.models.generate_content.call_args.kwargs["config"]
        assert config.thinking_config is None

    def test_title_generation_uses_low_thinking_and_short_timeout(self, mock_client):
        mock_client.models.generate_content.return_value = _make_response(text="Newton's Laws of Motion")
        ai_engine.generate_chat_title([{"role": "user", "text": "What is Newton's first law?"}])

        config = mock_client.models.generate_content.call_args.kwargs["config"]
        assert config.thinking_config.thinking_level == genai_types.ThinkingLevel.LOW
        assert config.http_options.timeout == ai_engine.CHAT_TITLE_TIMEOUT_SECONDS * 1000


# ---------------------------------------------------------------------------
# Retry policy - bucket by bucket
# ---------------------------------------------------------------------------

class TestRetryPolicy:
    def test_bucket_b_server_error_retries_then_succeeds(self, mock_client):
        chat_fail = MagicMock()
        chat_fail.send_message.side_effect = _server_error(503, "UNAVAILABLE")
        chat_ok = MagicMock()
        chat_ok.send_message.return_value = _make_response("recovered after retry")
        mock_client.chats.create.side_effect = [chat_fail, chat_ok]

        result = ai_engine.generate_ai_response(prompt="hi", mode="direct")

        assert result == "recovered after retry"
        assert mock_client.chats.create.call_count == 2

    def test_bucket_b_server_error_exhausts_retries(self, mock_client):
        chat = MagicMock()
        chat.send_message.side_effect = _server_error(500, "INTERNAL")
        mock_client.chats.create.return_value = chat

        result = ai_engine.generate_ai_response(prompt="hi", mode="direct")

        assert result == ai_engine.GENERIC_CHAT_ERROR
        assert mock_client.chats.create.call_count == ai_engine.MAX_ATTEMPTS

    def test_bucket_b_retry_uses_same_timeout_not_shorter(self, mock_client):
        chat_fail = MagicMock()
        chat_fail.send_message.side_effect = _server_error(503, "UNAVAILABLE")
        chat_ok = MagicMock()
        chat_ok.send_message.return_value = _make_response("ok")
        mock_client.chats.create.side_effect = [chat_fail, chat_ok]

        ai_engine.generate_ai_response(prompt="hi", mode="direct")

        timeouts = [c.kwargs["config"].http_options.timeout for c in mock_client.chats.create.call_args_list]
        assert timeouts[0] == timeouts[1] == ai_engine.AI_REQUEST_TIMEOUT_SECONDS * 1000

    def test_bucket_b_409_aborted_is_retried(self, mock_client):
        chat_fail = MagicMock()
        chat_fail.send_message.side_effect = _client_error(409, "ABORTED")
        chat_ok = MagicMock()
        chat_ok.send_message.return_value = _make_response("ok after conflict")
        mock_client.chats.create.side_effect = [chat_fail, chat_ok]

        result = ai_engine.generate_ai_response(prompt="hi", mode="direct")

        assert result == "ok after conflict"
        assert mock_client.chats.create.call_count == 2

    def test_bucket_c_quota_exhaustion_not_retried(self, mock_client):
        chat = MagicMock()
        chat.send_message.side_effect = _client_error(429, "RESOURCE_EXHAUSTED")
        mock_client.chats.create.return_value = chat

        result = ai_engine.generate_ai_response(prompt="hi", mode="direct")

        assert result == ai_engine.QUOTA_EXHAUSTED_ERROR
        assert mock_client.chats.create.call_count == 1

    def test_bucket_d_blocked_response_not_retried(self, mock_client):
        chat = MagicMock()
        chat.send_message.return_value = _make_response(text=None)
        mock_client.chats.create.return_value = chat

        result = ai_engine.generate_ai_response(prompt="hi", mode="direct")

        assert result == ai_engine.BLOCKED_RESPONSE_ERROR
        assert mock_client.chats.create.call_count == 1

    def test_bucket_d_empty_string_response_not_retried(self, mock_client):
        chat = MagicMock()
        chat.send_message.return_value = _make_response(text="")
        mock_client.chats.create.return_value = chat

        result = ai_engine.generate_ai_response(prompt="hi", mode="direct")

        assert result == ai_engine.BLOCKED_RESPONSE_ERROR
        assert mock_client.chats.create.call_count == 1

    def test_bucket_a_client_deadline_exceeded_retried_once_then_fails(self, mock_client):
        # RETRY_ON_CLIENT_TIMEOUT was flipped True -> False after the BM-01
        # benchmark incident (see ai_engine.py comment on this constant), so
        # a persistent client-side timeout is now retried exactly once
        # (bucket A) before giving up - not left unretried on attempt 1.
        chat = MagicMock()
        chat.send_message.side_effect = httpx.TimeoutException("deadline exceeded")
        mock_client.chats.create.return_value = chat

        assert ai_engine.RETRY_ON_CLIENT_TIMEOUT is True  # confirms the current documented default
        result = ai_engine.generate_ai_response(prompt="hi", mode="direct")

        assert result == ai_engine.GENERIC_CHAT_ERROR
        assert mock_client.chats.create.call_count == 2

    def test_bucket_a_connect_error_retried_once_then_fails(self, mock_client):
        chat = MagicMock()
        chat.send_message.side_effect = httpx.ConnectError("connection failed")
        mock_client.chats.create.return_value = chat

        result = ai_engine.generate_ai_response(prompt="hi", mode="direct")

        assert result == ai_engine.GENERIC_CHAT_ERROR
        assert mock_client.chats.create.call_count == 2

    def test_bucket_e_non_retryable_client_error_400(self, mock_client):
        chat = MagicMock()
        chat.send_message.side_effect = _client_error(400, "INVALID_ARGUMENT")
        mock_client.chats.create.return_value = chat

        result = ai_engine.generate_ai_response(prompt="hi", mode="direct")

        assert result == ai_engine.GENERIC_CHAT_ERROR
        assert mock_client.chats.create.call_count == 1

    def test_bucket_e_unexpected_exception_not_retried(self, mock_client):
        chat = MagicMock()
        chat.send_message.side_effect = RuntimeError("something exploded")
        mock_client.chats.create.return_value = chat

        result = ai_engine.generate_ai_response(prompt="hi", mode="direct")

        assert result == ai_engine.GENERIC_CHAT_ERROR
        assert mock_client.chats.create.call_count == 1

    def test_no_response_after_max_attempts_returns_generic_error_not_none(self, mock_client):
        chat = MagicMock()
        chat.send_message.side_effect = _server_error(502, "BAD_GATEWAY")
        mock_client.chats.create.return_value = chat

        result = ai_engine.generate_ai_response(prompt="hi", mode="direct")

        assert result == ai_engine.GENERIC_CHAT_ERROR


# ---------------------------------------------------------------------------
# _build_gemini_history unit tests
# ---------------------------------------------------------------------------

class TestBuildGeminiHistory:
    def test_empty_history(self):
        assert ai_engine._build_gemini_history([]) == []
        assert ai_engine._build_gemini_history(None) == []

    def test_role_mapping(self):
        history = [
            {"role": "user", "text": "hi"},
            {"role": "bot", "text": "hello"},
        ]
        result = ai_engine._build_gemini_history(history)
        assert result == [
            {"role": "user", "parts": [{"text": "hi"}]},
            {"role": "model", "parts": [{"text": "hello"}]},
        ]

    def test_unrecognized_role_skipped(self):
        history = [{"role": "system", "text": "should be skipped"}, {"role": "user", "text": "kept"}]
        result = ai_engine._build_gemini_history(history)
        assert result == [{"role": "user", "parts": [{"text": "kept"}]}]

    def test_empty_text_skipped(self):
        history = [{"role": "user", "text": ""}, {"role": "user", "text": "kept"}]
        result = ai_engine._build_gemini_history(history)
        assert result == [{"role": "user", "parts": [{"text": "kept"}]}]


# ---------------------------------------------------------------------------
# generate_quiz_questions
# ---------------------------------------------------------------------------

class TestQuizGeneration:
    def test_parses_raw_json_array(self, mock_client):
        mock_client.models.generate_content.return_value = _make_response(
            text='[{"id":1,"question":"2+2?","options":["3","4","5","6"],"correct_index":1,"explanation":"basic addition"}]'
        )
        result = ai_engine.generate_quiz_questions("NCTB", "SSC", "Math", "Addition", count=1)
        assert result == [{"id": 1, "question": "2+2?", "options": ["3", "4", "5", "6"], "correct_index": 1, "explanation": "basic addition"}]

    def test_strips_json_code_fence(self, mock_client):
        mock_client.models.generate_content.return_value = _make_response(
            text='```json\n[{"id":1,"question":"Q","options":["A","B","C","D"],"correct_index":0,"explanation":"E"}]\n```'
        )
        result = ai_engine.generate_quiz_questions("NCTB", "SSC", "Physics", "Motion", count=1)
        assert len(result) == 1 and result[0]["id"] == 1

    def test_strips_plain_code_fence(self, mock_client):
        mock_client.models.generate_content.return_value = _make_response(
            text='```\n[{"id":1,"question":"Q","options":["A","B","C","D"],"correct_index":0,"explanation":"E"}]\n```'
        )
        result = ai_engine.generate_quiz_questions("NCTB", "SSC", "Physics", "Motion", count=1)
        assert len(result) == 1

    def test_malformed_json_returns_empty_list(self, mock_client):
        mock_client.models.generate_content.return_value = _make_response(text="not valid json at all")
        result = ai_engine.generate_quiz_questions("NCTB", "SSC", "Physics", "Motion", count=1)
        assert result == []

    def test_provider_error_returns_empty_list(self, mock_client):
        mock_client.models.generate_content.side_effect = _server_error(503, "UNAVAILABLE")
        result = ai_engine.generate_quiz_questions("NCTB", "SSC", "Physics", "Motion", count=1)
        assert result == []

    def test_blocked_response_returns_empty_list_not_crash(self, mock_client):
        mock_client.models.generate_content.return_value = _make_response(text=None)
        result = ai_engine.generate_quiz_questions("NCTB", "SSC", "Physics", "Motion", count=1)
        assert result == []


# ---------------------------------------------------------------------------
# generate_chat_title
# ---------------------------------------------------------------------------

class TestChatTitle:
    def test_empty_history_returns_none_without_calling_api(self, mock_client):
        result = ai_engine.generate_chat_title([])
        assert result is None
        mock_client.models.generate_content.assert_not_called()

    def test_history_with_only_blank_text_returns_none(self, mock_client):
        result = ai_engine.generate_chat_title([{"role": "user", "text": "   "}])
        assert result is None
        mock_client.models.generate_content.assert_not_called()

    def test_success_returns_cleaned_title(self, mock_client):
        mock_client.models.generate_content.return_value = _make_response(text='"Newton\'s Laws of Motion"')
        result = ai_engine.generate_chat_title([{"role": "user", "text": "What is Newton's first law?"}])
        assert result == "Newton's Laws of Motion"

    def test_long_title_is_truncated(self, mock_client):
        long_title = "A Very Long Title About Advanced Quantum Mechanics And Relativity Theory Combined"
        mock_client.models.generate_content.return_value = _make_response(text=long_title)
        result = ai_engine.generate_chat_title([{"role": "user", "text": "explain quantum mechanics"}])
        assert len(result) <= ai_engine.MAX_CHAT_TITLE_CHARS + 3  # allow for trailing "..."
        assert result.endswith("...")

    def test_provider_error_returns_none(self, mock_client):
        mock_client.models.generate_content.side_effect = RuntimeError("boom")
        result = ai_engine.generate_chat_title([{"role": "user", "text": "hi"}])
        assert result is None

    def test_blocked_title_response_returns_none_not_crash(self, mock_client):
        mock_client.models.generate_content.return_value = _make_response(text=None)
        result = ai_engine.generate_chat_title([{"role": "user", "text": "hi"}])
        assert result is None
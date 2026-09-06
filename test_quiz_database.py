"""
Unit tests for backend/database.py:save_quiz_attempt().

Run with: GEMINI_API_KEY=dummy python3 -m pytest tests/test_quiz_database.py -v

Uses httpx.MockTransport to simulate Supabase's PostgREST responses
without any real network call - backend/database.py's own httpx.AsyncClient
calls are intercepted by monkeypatching httpx.AsyncClient to always attach
our mock transport.
"""
import asyncio
import json as jsonlib
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import httpx
import pytest

from backend import database


def _make_transport(handler):
    return httpx.MockTransport(handler)


def _patch_async_client(monkeypatch, transport):
    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(database.httpx, "AsyncClient", factory)


@pytest.fixture(autouse=True)
def supabase_env(monkeypatch):
    monkeypatch.setattr(database, "SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setattr(database, "SUPABASE_ANON_KEY", "anon-key-123")


QUESTIONS = [
    {"question": "2+2?", "options": ["3", "4"], "correct_index": 1, "explanation": "math"},
    {"question": "Capital of BD?", "options": ["Dhaka", "Delhi"], "correct_index": 0, "explanation": "geo"},
]


class TestSaveQuizAttemptConfigAndInput:
    def test_not_configured_raises(self, monkeypatch):
        monkeypatch.setattr(database, "SUPABASE_URL", "")
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.save_quiz_attempt(
                user_token="t", user_id="u", board="B", user_class="C",
                subject="S", stream="Science", topic="T", questions=QUESTIONS, selected_answers=[1, 0],
            ))

    def test_length_mismatch_raises(self):
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.save_quiz_attempt(
                user_token="t", user_id="u", board="B", user_class="C",
                subject="S", stream="Science", topic="T", questions=QUESTIONS, selected_answers=[1],
            ))


class TestSaveQuizAttemptScoring:
    def test_successful_insert_computes_score_and_returns_ids(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/quiz_attempts") and request.method == "POST":
                body = jsonlib.loads(request.content)
                assert body["score"] == 1  # only Q1 answered correctly
                assert body["total_questions"] == 2
                assert body["user_id"] == "user-1"
                # PHASE 5A: subject/stream are now distinct, and topic_key
                # is computed here (never client-supplied).
                assert body["subject"] == "Math"
                assert body["stream"] == "Science"
                assert body["topic"] == "Algebra"
                assert body["topic_key"] == "algebra"
                return httpx.Response(201, json=[{"id": "attempt-123", **body}])
            if request.url.path.endswith("/quiz_answers") and request.method == "POST":
                body = jsonlib.loads(request.content)
                assert len(body) == 2
                assert all(row["attempt_id"] == "attempt-123" for row in body)
                assert body[0]["is_correct"] is True
                assert body[1]["is_correct"] is False
                return httpx.Response(201)
            raise AssertionError(f"unexpected request {request.method} {request.url}")

        _patch_async_client(monkeypatch, _make_transport(handler))

        result = asyncio.run(database.save_quiz_attempt(
            user_token="student-token", user_id="user-1", board="NCTB",
            user_class="SSC", subject="Math", stream="Science", topic="Algebra",
            questions=QUESTIONS, selected_answers=[1, 1],  # Q1 correct, Q2 wrong
        ))

        assert result == {"attempt_id": "attempt-123", "score": 1, "total_questions": 2}

    def test_unanswered_question_counts_as_incorrect_not_error(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/quiz_attempts"):
                body = jsonlib.loads(request.content)
                assert body["score"] == 0
                return httpx.Response(201, json=[{"id": "attempt-999"}])
            return httpx.Response(201)

        _patch_async_client(monkeypatch, _make_transport(handler))
        result = asyncio.run(database.save_quiz_attempt(
            user_token="t", user_id="u", board="B", user_class="C",
            subject="S", stream="Science", topic="T", questions=QUESTIONS, selected_answers=[None, None],
        ))
        assert result["score"] == 0

    def test_forwards_students_own_token_never_a_service_role_key(self, monkeypatch):
        """Security-critical: every REST call must carry the STUDENT's own
        bearer token. RLS is the only enforcement boundary here - there is
        no service-role key anywhere in this module."""
        seen_auth_headers = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_auth_headers.append(request.headers.get("authorization"))
            if request.url.path.endswith("/quiz_attempts"):
                return httpx.Response(201, json=[{"id": "a1"}])
            return httpx.Response(201)

        _patch_async_client(monkeypatch, _make_transport(handler))
        asyncio.run(database.save_quiz_attempt(
            user_token="THIS_STUDENTS_OWN_TOKEN", user_id="u", board="B",
            user_class="C", subject="S", stream="Science", topic="T",
            questions=QUESTIONS, selected_answers=[1, 0],
        ))
        assert len(seen_auth_headers) == 2
        assert all(h == "Bearer THIS_STUDENTS_OWN_TOKEN" for h in seen_auth_headers)


class TestSaveQuizAttemptFailureModes:
    def test_attempt_insert_non_2xx_raises(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"message": "bad request"})

        _patch_async_client(monkeypatch, _make_transport(handler))
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.save_quiz_attempt(
                user_token="t", user_id="u", board="B", user_class="C",
                subject="S", stream="Science", topic="T", questions=QUESTIONS, selected_answers=[1, 0],
            ))

    def test_attempt_insert_network_error_raises(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

        _patch_async_client(monkeypatch, _make_transport(handler))
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.save_quiz_attempt(
                user_token="t", user_id="u", board="B", user_class="C",
                subject="S", stream="Science", topic="T", questions=QUESTIONS, selected_answers=[1, 0],
            ))

    def test_unexpected_attempt_response_shape_raises(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(201, json={"not": "a list"})

        _patch_async_client(monkeypatch, _make_transport(handler))
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.save_quiz_attempt(
                user_token="t", user_id="u", board="B", user_class="C",
                subject="S", stream="Science", topic="T", questions=QUESTIONS, selected_answers=[1, 0],
            ))

    def test_answers_insert_failure_rolls_back_orphaned_attempt(self, monkeypatch):
        deleted = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path.endswith("/quiz_attempts"):
                return httpx.Response(201, json=[{"id": "attempt-to-rollback"}])
            if request.method == "POST" and request.url.path.endswith("/quiz_answers"):
                return httpx.Response(500, json={"message": "db exploded"})
            if request.method == "DELETE" and request.url.path.endswith("/quiz_attempts"):
                deleted["id_param"] = request.url.params.get("id")
                return httpx.Response(204)
            raise AssertionError(f"unexpected request {request.method} {request.url}")

        _patch_async_client(monkeypatch, _make_transport(handler))

        with pytest.raises(database.DatabaseError):
            asyncio.run(database.save_quiz_attempt(
                user_token="t", user_id="u", board="B", user_class="C",
                subject="S", stream="Science", topic="T", questions=QUESTIONS, selected_answers=[1, 0],
            ))

        assert deleted["id_param"] == "eq.attempt-to-rollback"

    def test_answers_insert_failure_still_raises_even_if_rollback_delete_fails(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path.endswith("/quiz_attempts"):
                return httpx.Response(201, json=[{"id": "attempt-orphan"}])
            if request.method == "POST" and request.url.path.endswith("/quiz_answers"):
                return httpx.Response(500)
            if request.method == "DELETE":
                return httpx.Response(500)  # rollback itself also fails
            raise AssertionError("unexpected request")

        _patch_async_client(monkeypatch, _make_transport(handler))

        # Must still raise DatabaseError even though the compensating
        # delete also failed - never silently report success on a
        # partially-failed write.
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.save_quiz_attempt(
                user_token="t", user_id="u", board="B", user_class="C",
                subject="S", stream="Science", topic="T", questions=QUESTIONS, selected_answers=[1, 0],
            ))

    def test_answers_insert_network_error_also_rolls_back(self, monkeypatch):
        deleted = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path.endswith("/quiz_attempts"):
                return httpx.Response(201, json=[{"id": "attempt-net-fail"}])
            if request.method == "POST" and request.url.path.endswith("/quiz_answers"):
                raise httpx.ConnectError("boom", request=request)
            if request.method == "DELETE":
                deleted["id_param"] = request.url.params.get("id")
                return httpx.Response(204)
            raise AssertionError("unexpected request")

        _patch_async_client(monkeypatch, _make_transport(handler))
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.save_quiz_attempt(
                user_token="t", user_id="u", board="B", user_class="C",
                subject="S", stream="Science", topic="T", questions=QUESTIONS, selected_answers=[1, 0],
            ))
        assert deleted["id_param"] == "eq.attempt-net-fail"


class TestNormalizeTopicKey:
    """Deterministic topic normalization: trim + collapse whitespace +
    casefold. Explicitly NOT expected to unify synonyms, spelling
    variants, or Bangla/English/Banglish equivalents - see
    supabase/migrations/0002_quiz_topic_identity.sql."""

    def test_trims_leading_and_trailing_whitespace(self):
        assert database.normalize_topic_key("  Force & Motion  ") == database.normalize_topic_key("Force & Motion")

    def test_collapses_repeated_internal_whitespace(self):
        assert database.normalize_topic_key("Force   &    Motion") == database.normalize_topic_key("Force & Motion")

    def test_collapses_tabs_and_newlines_too(self):
        assert database.normalize_topic_key("Force\t&\nMotion") == database.normalize_topic_key("Force & Motion")

    def test_case_insensitive(self):
        assert database.normalize_topic_key("FORCE & MOTION") == database.normalize_topic_key("force & motion")

    def test_equivalent_variants_produce_identical_key(self):
        variants = [" Force & Motion ", "force & motion", "FORCE & MOTION", "Force  &  Motion"]
        keys = {database.normalize_topic_key(v) for v in variants}
        assert len(keys) == 1

    def test_genuinely_different_topics_remain_distinct(self):
        assert database.normalize_topic_key("Force & Motion") != database.normalize_topic_key("Newton's Laws")

    def test_does_not_unify_language_variants(self):
        # Documented limitation, not a bug: Bangla/English/Banglish
        # spellings of the same real-world topic are NOT unified.
        assert database.normalize_topic_key("Newton's Laws") != database.normalize_topic_key("নিউটনের সূত্র")

    def test_empty_and_whitespace_only_input(self):
        assert database.normalize_topic_key("") == ""
        assert database.normalize_topic_key("   ") == ""

    def test_non_string_input_returns_empty_string(self):
        assert database.normalize_topic_key(None) == ""


class TestGetUserTopicProfile:
    """Covers backend/database.py:get_user_topic_profile +
    _build_topic_profile. Grouping/translation-into-EvidenceEvent logic
    is exercised here; the mastery algorithm itself is covered
    exhaustively in test_mastery_engine.py."""

    def _rows_response(self, rows):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/quiz_attempts")
            assert request.method == "GET"
            return httpx.Response(200, json=rows)
        return handler

    def test_not_configured_raises(self, monkeypatch):
        monkeypatch.setattr(database, "SUPABASE_URL", "")
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.get_user_topic_profile(user_token="t", user_id="u"))

    def test_forwards_students_own_token(self, monkeypatch):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("authorization")
            seen["params"] = dict(request.url.params)
            return httpx.Response(200, json=[])

        _patch_async_client(monkeypatch, _make_transport(handler))
        asyncio.run(database.get_user_topic_profile(user_token="STUDENT_TOKEN", user_id="u"))
        assert seen["auth"] == "Bearer STUDENT_TOKEN"
        # Legacy (topic_key IS NULL) rows must be excluded server-side.
        assert seen["params"]["topic_key"] == "not.is.null"

    def test_empty_result_set_returns_empty_list(self, monkeypatch):
        _patch_async_client(monkeypatch, _make_transport(self._rows_response([])))
        result = asyncio.run(database.get_user_topic_profile(user_token="t", user_id="u"))
        assert result == []

    def test_network_error_raises_database_error(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)
        _patch_async_client(monkeypatch, _make_transport(handler))
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.get_user_topic_profile(user_token="t", user_id="u"))

    def test_non_200_raises_database_error(self, monkeypatch):
        _patch_async_client(monkeypatch, _make_transport(lambda r: httpx.Response(500)))
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.get_user_topic_profile(user_token="t", user_id="u"))

    def test_single_topic_multiple_attempts_aggregated(self, monkeypatch):
        rows = [
            {"id": "a1", "subject": "Physics", "topic": "Force & Motion", "topic_key": "force & motion",
             "total_questions": 10, "score": 4, "created_at": "2026-08-20T10:00:00+00:00"},
            {"id": "a2", "subject": "Physics", "topic": "force & motion", "topic_key": "force & motion",
             "total_questions": 10, "score": 5, "created_at": "2026-08-27T10:00:00+00:00"},
        ]
        _patch_async_client(monkeypatch, _make_transport(self._rows_response(rows)))
        result = asyncio.run(database.get_user_topic_profile(user_token="t", user_id="u"))
        assert len(result) == 1
        assert result[0]["subject"] == "Physics"
        assert result[0]["topic_key"] == "force & motion"
        # Most recently seen row's original topic text wins for display.
        assert result[0]["topic"] == "force & motion"
        assert result[0]["attempt_count"] == 2
        assert result[0]["total_correct"] == 9
        assert result[0]["total_questions"] == 20

    def test_multiple_topics_kept_separate(self, monkeypatch):
        rows = [
            {"id": "a1", "subject": "Physics", "topic": "Force & Motion", "topic_key": "force & motion",
             "total_questions": 10, "score": 8, "created_at": "2026-08-20T10:00:00+00:00"},
            {"id": "a2", "subject": "Chemistry", "topic": "Atomic Structure", "topic_key": "atomic structure",
             "total_questions": 10, "score": 2, "created_at": "2026-08-21T10:00:00+00:00"},
        ]
        _patch_async_client(monkeypatch, _make_transport(self._rows_response(rows)))
        result = asyncio.run(database.get_user_topic_profile(user_token="t", user_id="u"))
        assert len(result) == 2
        subjects = {r["subject"] for r in result}
        assert subjects == {"Physics", "Chemistry"}

    def test_same_topic_key_different_subject_not_merged(self, monkeypatch):
        # Grouping key is (subject, topic_key) - the SAME normalized topic
        # text under two different subjects must not be merged together.
        rows = [
            {"id": "a1", "subject": "Physics", "topic": "Waves", "topic_key": "waves",
             "total_questions": 10, "score": 9, "created_at": "2026-08-20T10:00:00+00:00"},
            {"id": "a2", "subject": "Bangla", "topic": "Waves", "topic_key": "waves",
             "total_questions": 10, "score": 1, "created_at": "2026-08-21T10:00:00+00:00"},
        ]
        _patch_async_client(monkeypatch, _make_transport(self._rows_response(rows)))
        result = asyncio.run(database.get_user_topic_profile(user_token="t", user_id="u"))
        assert len(result) == 2

    def test_malformed_row_skipped_not_fatal(self, monkeypatch):
        rows = [
            {"id": "bad", "subject": None, "topic": "X", "topic_key": "x",
             "total_questions": 10, "score": 1, "created_at": "2026-08-20T10:00:00+00:00"},
            {"id": "ok", "subject": "Physics", "topic": "Y", "topic_key": "y",
             "total_questions": 10, "score": 7, "created_at": "2026-08-21T10:00:00+00:00"},
        ]
        _patch_async_client(monkeypatch, _make_transport(self._rows_response(rows)))
        result = asyncio.run(database.get_user_topic_profile(user_token="t", user_id="u"))
        assert len(result) == 1
        assert result[0]["subject"] == "Physics"

    def test_unparseable_timestamp_row_skipped_not_fatal(self, monkeypatch):
        rows = [
            {"id": "bad", "subject": "Physics", "topic": "X", "topic_key": "x",
             "total_questions": 10, "score": 1, "created_at": "not-a-timestamp"},
            {"id": "ok", "subject": "Physics", "topic": "X", "topic_key": "x",
             "total_questions": 10, "score": 7, "created_at": "2026-08-21T10:00:00+00:00"},
        ]
        _patch_async_client(monkeypatch, _make_transport(self._rows_response(rows)))
        result = asyncio.run(database.get_user_topic_profile(user_token="t", user_id="u"))
        assert len(result) == 1
        assert result[0]["attempt_count"] == 1

    def test_results_sorted_most_recently_practiced_first(self, monkeypatch):
        rows = [
            {"id": "a1", "subject": "Physics", "topic": "Old Topic", "topic_key": "old topic",
             "total_questions": 10, "score": 5, "created_at": "2026-01-01T10:00:00+00:00"},
            {"id": "a2", "subject": "Chemistry", "topic": "New Topic", "topic_key": "new topic",
             "total_questions": 10, "score": 5, "created_at": "2026-08-01T10:00:00+00:00"},
        ]
        _patch_async_client(monkeypatch, _make_transport(self._rows_response(rows)))
        result = asyncio.run(database.get_user_topic_profile(user_token="t", user_id="u"))
        assert result[0]["subject"] == "Chemistry"
        assert result[1]["subject"] == "Physics"
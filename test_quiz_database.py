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
                subject="S", topic="T", questions=QUESTIONS, selected_answers=[1, 0],
            ))

    def test_length_mismatch_raises(self):
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.save_quiz_attempt(
                user_token="t", user_id="u", board="B", user_class="C",
                subject="S", topic="T", questions=QUESTIONS, selected_answers=[1],
            ))


class TestSaveQuizAttemptScoring:
    def test_successful_insert_computes_score_and_returns_ids(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/quiz_attempts") and request.method == "POST":
                body = jsonlib.loads(request.content)
                assert body["score"] == 1  # only Q1 answered correctly
                assert body["total_questions"] == 2
                assert body["user_id"] == "user-1"
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
            user_class="SSC", subject="Math", topic="Algebra",
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
            subject="S", topic="T", questions=QUESTIONS, selected_answers=[None, None],
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
            user_class="C", subject="S", topic="T",
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
                subject="S", topic="T", questions=QUESTIONS, selected_answers=[1, 0],
            ))

    def test_attempt_insert_network_error_raises(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

        _patch_async_client(monkeypatch, _make_transport(handler))
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.save_quiz_attempt(
                user_token="t", user_id="u", board="B", user_class="C",
                subject="S", topic="T", questions=QUESTIONS, selected_answers=[1, 0],
            ))

    def test_unexpected_attempt_response_shape_raises(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(201, json={"not": "a list"})

        _patch_async_client(monkeypatch, _make_transport(handler))
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.save_quiz_attempt(
                user_token="t", user_id="u", board="B", user_class="C",
                subject="S", topic="T", questions=QUESTIONS, selected_answers=[1, 0],
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
                subject="S", topic="T", questions=QUESTIONS, selected_answers=[1, 0],
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
                subject="S", topic="T", questions=QUESTIONS, selected_answers=[1, 0],
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
                subject="S", topic="T", questions=QUESTIONS, selected_answers=[1, 0],
            ))
        assert deleted["id_param"] == "eq.attempt-net-fail"
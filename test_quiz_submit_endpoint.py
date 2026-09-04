"""
Tests for the /api/quiz/generate + /api/quiz/submit endpoints in
backend/main.py (database-foundation Phase 1).

Run with: GEMINI_API_KEY=dummy python3 -m pytest tests/test_quiz_submit_endpoint.py -v

Auth is stubbed via FastAPI's dependency_overrides (get_current_user_id /
get_current_user_and_token) rather than hitting Supabase's real Auth API -
consistent with the project's existing convention of mocking the AI
provider boundary in kognit_smoke_test.py, applied here to the auth
boundary. backend.database.save_quiz_attempt is mocked at the
backend.main import site for endpoint-contract tests; its own real
behavior is covered separately in tests/test_quiz_database.py.
"""
import json
import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
from fastapi.testclient import TestClient

from backend import main as main_module
from backend.database import DatabaseError


@pytest.fixture
def client():
    return TestClient(main_module.app)


@pytest.fixture(autouse=True)
def clean_state():
    """Every test starts with an empty in-memory quiz store and no
    dependency overrides left over from a previous test."""
    main_module.active_quiz_definitions.clear()
    yield
    main_module.active_quiz_definitions.clear()
    main_module.app.dependency_overrides.clear()


def _override_auth_as(user_id, token="test-token"):
    async def _fake_user_and_token():
        return (user_id, token)

    async def _fake_user_id():
        return user_id

    main_module.app.dependency_overrides[main_module.get_current_user_and_token] = _fake_user_and_token
    main_module.app.dependency_overrides[main_module.get_current_user_id] = _fake_user_id


SAMPLE_QUESTIONS = [
    {"question": "2+2?", "options": ["3", "4"], "correct_index": 1, "explanation": "math"},
    {"question": "Capital of BD?", "options": ["Dhaka", "Delhi"], "correct_index": 0, "explanation": "geo"},
]


def _seed_definition(quiz_id="quiz-abc", user_id="user-1", submitted=False, questions=None):
    main_module.active_quiz_definitions[quiz_id] = {
        "user_id": user_id,
        "board": "NCTB",
        "user_class": "SSC",
        "subject": "Math",
        "topic": "Algebra",
        "questions": questions if questions is not None else SAMPLE_QUESTIONS,
        "submitted": submitted,
        "created_at": 0.0,
    }


class TestQuizSubmitAuth:
    def test_missing_authorization_header_returns_401(self, client):
        resp = client.post("/api/quiz/submit", data={"quiz_id": "x", "answers": "[]"})
        assert resp.status_code == 401


class TestQuizSubmitOwnership:
    def test_unknown_quiz_id_returns_404(self, client):
        _override_auth_as("user-1")
        resp = client.post("/api/quiz/submit", data={"quiz_id": "does-not-exist", "answers": "[1,0]"})
        assert resp.status_code == 404

    def test_other_users_quiz_id_returns_404_not_403(self, client):
        _seed_definition(quiz_id="quiz-abc", user_id="owner-user")
        _override_auth_as("attacker-user")
        resp = client.post("/api/quiz/submit", data={"quiz_id": "quiz-abc", "answers": "[1,0]"})
        # 404, not 403: a client must not be able to distinguish "not
        # yours" from "doesn't exist" - see quiz_submit_endpoint comment.
        assert resp.status_code == 404
        # And the real owner's definition must be completely untouched.
        assert "quiz-abc" in main_module.active_quiz_definitions
        assert main_module.active_quiz_definitions["quiz-abc"]["submitted"] is False


class TestQuizSubmitValidation:
    def test_malformed_json_answers_returns_400(self, client):
        _seed_definition()
        _override_auth_as("user-1")
        resp = client.post("/api/quiz/submit", data={"quiz_id": "quiz-abc", "answers": "not json"})
        assert resp.status_code == 400

    def test_answers_length_mismatch_returns_400(self, client):
        _seed_definition()  # 2 questions
        _override_auth_as("user-1")
        resp = client.post("/api/quiz/submit", data={"quiz_id": "quiz-abc", "answers": "[1]"})
        assert resp.status_code == 400

    def test_bool_answer_rejected(self, client):
        _seed_definition()
        _override_auth_as("user-1")
        resp = client.post("/api/quiz/submit", data={"quiz_id": "quiz-abc", "answers": "[true, false]"})
        assert resp.status_code == 400

    def test_non_int_answer_rejected(self, client):
        _seed_definition()
        _override_auth_as("user-1")
        resp = client.post("/api/quiz/submit", data={"quiz_id": "quiz-abc", "answers": '["a", 0]'})
        assert resp.status_code == 400

    def test_out_of_range_answer_index_rejected(self, client):
        _seed_definition()  # 2 questions, each with exactly 2 options (indices 0-1)
        _override_auth_as("user-1")
        resp = client.post("/api/quiz/submit", data={"quiz_id": "quiz-abc", "answers": "[99, 0]"})
        assert resp.status_code == 400

    def test_negative_answer_index_rejected(self, client):
        _seed_definition()
        _override_auth_as("user-1")
        resp = client.post("/api/quiz/submit", data={"quiz_id": "quiz-abc", "answers": "[-1, 0]"})
        assert resp.status_code == 400

    def test_too_many_answers_rejected(self, client):
        big_questions = [
            {"question": f"Q{i}", "options": ["A", "B"], "correct_index": 0, "explanation": ""}
            for i in range(main_module.MAX_QUIZ_ANSWERS + 1)
        ]
        _seed_definition(questions=big_questions)
        _override_auth_as("user-1")
        answers = json.dumps([0] * len(big_questions))
        resp = client.post("/api/quiz/submit", data={"quiz_id": "quiz-abc", "answers": answers})
        assert resp.status_code == 400

    def test_null_answers_are_accepted_as_unanswered(self, client):
        _seed_definition()
        _override_auth_as("user-1")
        with patch.object(main_module, "save_quiz_attempt", new_callable=AsyncMock) as mock_save:
            mock_save.return_value = {"attempt_id": "a1", "score": 0, "total_questions": 2}
            resp = client.post("/api/quiz/submit", data={"quiz_id": "quiz-abc", "answers": "[null, null]"})
        assert resp.status_code == 200
        _, kwargs = mock_save.call_args
        assert kwargs["selected_answers"] == [None, None]


class TestQuizSubmitDuplicate:
    def test_already_submitted_returns_409(self, client):
        _seed_definition(submitted=True)
        _override_auth_as("user-1")
        resp = client.post("/api/quiz/submit", data={"quiz_id": "quiz-abc", "answers": "[1,0]"})
        assert resp.status_code == 409


class TestQuizSubmitScoreIntegrity:
    def test_score_and_grading_come_from_server_not_client(self, client):
        """The client has no way to supply a score or is_correct - it can
        only supply which option index it picked per question. This test
        proves the endpoint passes the raw selections through untouched
        and lets save_quiz_attempt (which grades against the server-held
        definition) do all the scoring - there is no score/is_correct
        parameter anywhere in the request the client controls."""
        _seed_definition()
        _override_auth_as("user-1")
        with patch.object(main_module, "save_quiz_attempt", new_callable=AsyncMock) as mock_save:
            mock_save.return_value = {"attempt_id": "a1", "score": 2, "total_questions": 2}
            resp = client.post("/api/quiz/submit", data={"quiz_id": "quiz-abc", "answers": "[1, 0]"})

        assert resp.status_code == 200
        assert resp.json() == {"status": "success", "attempt_id": "a1", "score": 2, "total_questions": 2}
        _, kwargs = mock_save.call_args
        assert kwargs["questions"] == SAMPLE_QUESTIONS  # authoritative, server-held copy
        assert kwargs["selected_answers"] == [1, 0]
        assert kwargs["user_id"] == "user-1"
        assert kwargs["user_token"] == "test-token"

    def test_successful_submission_removes_definition(self, client):
        _seed_definition()
        _override_auth_as("user-1")
        with patch.object(main_module, "save_quiz_attempt", new_callable=AsyncMock) as mock_save:
            mock_save.return_value = {"attempt_id": "a1", "score": 1, "total_questions": 2}
            client.post("/api/quiz/submit", data={"quiz_id": "quiz-abc", "answers": "[1, 0]"})
        assert "quiz-abc" not in main_module.active_quiz_definitions

    def test_database_error_resets_submitted_flag_for_retry(self, client):
        _seed_definition()
        _override_auth_as("user-1")
        with patch.object(main_module, "save_quiz_attempt", new_callable=AsyncMock) as mock_save:
            mock_save.side_effect = DatabaseError("db exploded")
            resp = client.post("/api/quiz/submit", data={"quiz_id": "quiz-abc", "answers": "[1, 0]"})

        assert resp.status_code == 502
        # Definition must still exist AND be un-submitted, so the exact
        # same quiz_id can be retried instead of the student losing their
        # answers to a transient DB error.
        assert "quiz-abc" in main_module.active_quiz_definitions
        assert main_module.active_quiz_definitions["quiz-abc"]["submitted"] is False


class TestQuizGenerateDefinitionStorage:
    def test_generate_stores_definition_and_returns_quiz_id(self, client):
        _override_auth_as("user-1")
        with patch.object(main_module, "generate_quiz_questions", return_value=SAMPLE_QUESTIONS):
            resp = client.post("/api/quiz/generate", data={
                "board": "NCTB", "user_class": "SSC", "subject": "Math",
                "topic": "Algebra", "count": 2,
            })
        assert resp.status_code == 200
        body = resp.json()
        assert body["questions"] == SAMPLE_QUESTIONS
        quiz_id = body["quiz_id"]
        assert quiz_id is not None
        stored = main_module.active_quiz_definitions[quiz_id]
        assert stored["user_id"] == "user-1"
        assert stored["questions"] == SAMPLE_QUESTIONS
        assert stored["submitted"] is False

    def test_generate_empty_questions_returns_none_quiz_id_and_stores_nothing(self, client):
        _override_auth_as("user-1")
        with patch.object(main_module, "generate_quiz_questions", return_value=[]):
            resp = client.post("/api/quiz/generate", data={
                "board": "NCTB", "user_class": "SSC", "subject": "Math",
                "topic": "Algebra", "count": 2,
            })
        assert resp.status_code == 200
        assert resp.json() == {"questions": [], "quiz_id": None}
        assert main_module.active_quiz_definitions == {}

    def test_generate_requires_auth(self, client):
        with patch.object(main_module, "generate_quiz_questions", return_value=SAMPLE_QUESTIONS):
            resp = client.post("/api/quiz/generate", data={
                "board": "NCTB", "user_class": "SSC", "subject": "Math",
                "topic": "Algebra", "count": 2,
            })
        assert resp.status_code == 401


class TestQuizEndToEndFlow:
    def test_generate_then_submit_full_flow_and_replay_is_rejected(self, client):
        """Full round trip: generate creates a server-held definition;
        submit grades against it and returns a persisted result without
        the client ever supplying a score; a replay of the same
        (now-consumed) quiz_id is rejected."""
        _override_auth_as("user-1")
        with patch.object(main_module, "generate_quiz_questions", return_value=SAMPLE_QUESTIONS):
            gen_resp = client.post("/api/quiz/generate", data={
                "board": "NCTB", "user_class": "SSC", "subject": "Math",
                "topic": "Algebra", "count": 2,
            })
        quiz_id = gen_resp.json()["quiz_id"]

        with patch.object(main_module, "save_quiz_attempt", new_callable=AsyncMock) as mock_save:
            mock_save.return_value = {"attempt_id": "a1", "score": 2, "total_questions": 2}
            submit_resp = client.post("/api/quiz/submit", data={
                "quiz_id": quiz_id, "answers": "[1, 0]",  # both correct per SAMPLE_QUESTIONS
            })

        assert submit_resp.status_code == 200
        assert submit_resp.json()["score"] == 2
        assert quiz_id not in main_module.active_quiz_definitions

        replay_resp = client.post("/api/quiz/submit", data={"quiz_id": quiz_id, "answers": "[1, 0]"})
        assert replay_resp.status_code == 404


class TestExistingEndpointsRegression:
    """Confirms the get_current_user_id refactor (split into
    _verify_supabase_token + two thin dependencies) didn't change behavior
    for endpoints that existed before this phase."""

    def test_chat_endpoint_still_requires_auth(self, client):
        resp = client.post("/api/chat", data={"prompt": "hello", "mode": "direct"})
        assert resp.status_code == 401

    def test_quiz_generate_still_requires_auth(self, client):
        resp = client.post("/api/quiz/generate", data={"board": "NCTB", "topic": "x"})
        assert resp.status_code == 401
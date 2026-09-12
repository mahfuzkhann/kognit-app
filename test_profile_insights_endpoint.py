"""
Tests for GET /api/profile/insights in backend/main.py (Phase 5E).

Run with: GEMINI_API_KEY=dummy python3 -m pytest test_profile_insights_endpoint.py -v

Auth is stubbed via dependency_overrides on get_current_user_and_token,
same convention as test_learning_history_endpoint.py /
test_profile_topics_endpoint.py. backend.database.get_learning_insights is
mocked at the backend.main import site for endpoint-contract tests; its
own real behavior is covered in test_learning_insights_database.py.
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
    yield
    main_module.app.dependency_overrides.clear()


def _override_auth_as(user_id, token="test-token"):
    async def _fake():
        return (user_id, token)
    main_module.app.dependency_overrides[main_module.get_current_user_and_token] = _fake


SAMPLE_INSIGHT_A = {
    "subject": "Physics", "topic": None, "topic_key": None,
    "insight_type": "recurring_confusion", "confidence": "medium",
    "evidence_count": 3, "first_observed_at": "2026-08-01T10:00:00+00:00",
    "last_observed_at": "2026-08-05T10:00:00+00:00",
}
SAMPLE_INSIGHT_B = {
    "subject": "Chemistry", "topic": None, "topic_key": None,
    "insight_type": "repeated_difficulty", "confidence": "low",
    "evidence_count": 2, "first_observed_at": "2026-08-01T10:00:00+00:00",
    "last_observed_at": "2026-08-02T10:00:00+00:00",
}


class TestAuthentication:
    def test_unauthenticated_request_returns_401(self, client):
        resp = client.get("/api/profile/insights")
        assert resp.status_code == 401

    def test_authenticated_request_succeeds(self, client):
        _override_auth_as("user-a")
        with patch.object(main_module, "get_learning_insights", new=AsyncMock(return_value=[SAMPLE_INSIGHT_A])):
            resp = client.get("/api/profile/insights")
        assert resp.status_code == 200
        assert resp.json() == {"insights": [SAMPLE_INSIGHT_A]}


class TestResponseContract:
    def test_response_envelope_is_named_not_bare_array(self, client):
        _override_auth_as("user-a")
        with patch.object(main_module, "get_learning_insights", new=AsyncMock(return_value=[])):
            resp = client.get("/api/profile/insights")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, dict)
        assert "insights" in body
        assert body["insights"] == []

    def test_never_exposes_a_numeric_score_field(self, client):
        _override_auth_as("user-a")
        with patch.object(main_module, "get_learning_insights", new=AsyncMock(return_value=[SAMPLE_INSIGHT_A])):
            resp = client.get("/api/profile/insights")
        insight = resp.json()["insights"][0]
        assert "score" not in insight
        assert "correct_rate" not in insight
        assert "percentage" not in insight
        assert "status" not in insight  # that word is Phase 5A's (mastery status) vocabulary, not 5E's


class TestFailureHandling:
    def test_database_error_returns_502_not_500_with_stack_trace(self, client):
        _override_auth_as("user-a")
        with patch.object(main_module, "get_learning_insights", new=AsyncMock(side_effect=DatabaseError("boom"))):
            resp = client.get("/api/profile/insights")
        assert resp.status_code == 502
        assert "boom" not in resp.text


# ---------------------------------------------------------------------------
# REQUIRED ADVERSARIAL ISOLATION TESTS (brief Section 18).
#
# These test the endpoint's CONTRACT: identity comes exclusively from the
# verified (user_id, token) tuple, and get_learning_insights is called
# with THAT user's id/token regardless of anything else. This is the same
# isolation guarantee already independently verified live against real
# Supabase RLS for learning_evidence/conversation_index in an earlier
# session - these tests protect the application-layer contract from
# regressing, they do not re-run that live RLS proof (see
# test_live_rls_two_user_isolation.py for that, separately).
# ---------------------------------------------------------------------------

class TestAdversarialUserIsolation:
    def test_user_a_cannot_retrieve_user_b_insights(self, client):
        """User A's request must only ever be able to trigger a fetch
        scoped to User A's own verified identity - there is no code path
        by which User B's insights could be requested from this endpoint
        while authenticated as User A."""
        _override_auth_as("user-a", token="token-a")
        captured = {}

        async def _fake_get_learning_insights(user_token, user_id):
            captured["user_id"] = user_id
            captured["user_token"] = user_token
            return [SAMPLE_INSIGHT_A]

        with patch.object(main_module, "get_learning_insights", new=_fake_get_learning_insights):
            resp = client.get("/api/profile/insights")

        assert resp.status_code == 200
        assert captured["user_id"] == "user-a"
        assert captured["user_token"] == "token-a"
        # The only insight this call could ever see is what the (correctly
        # user-a-scoped) fetch returned - User B's sample insight never
        # appears, by construction of the mock, matching what real RLS
        # guarantees in production.
        assert resp.json()["insights"] == [SAMPLE_INSIGHT_A]

    def test_user_b_cannot_retrieve_user_a_insights(self, client):
        _override_auth_as("user-b", token="token-b")
        captured = {}

        async def _fake_get_learning_insights(user_token, user_id):
            captured["user_id"] = user_id
            captured["user_token"] = user_token
            return [SAMPLE_INSIGHT_B]

        with patch.object(main_module, "get_learning_insights", new=_fake_get_learning_insights):
            resp = client.get("/api/profile/insights")

        assert resp.status_code == 200
        assert captured["user_id"] == "user-b"
        assert captured["user_token"] == "token-b"
        assert resp.json()["insights"] == [SAMPLE_INSIGHT_B]

    def test_client_cannot_supply_an_alternate_user_id(self, client):
        """There is no user_id query/body parameter on this endpoint at
        all - identity comes exclusively from the verified token. Passing
        one as a query param must be silently ignored, not honored."""
        _override_auth_as("user-a", token="token-a")
        captured = {}

        async def _fake_get_learning_insights(user_token, user_id):
            captured["user_id"] = user_id
            return []

        with patch.object(main_module, "get_learning_insights", new=_fake_get_learning_insights):
            resp = client.get("/api/profile/insights", params={"user_id": "user-b"})

        assert resp.status_code == 200
        assert captured["user_id"] == "user-a"  # from the verified token, never the query param

    def test_forged_authorization_header_with_wrong_token_never_reaches_the_database(self, client):
        """A request presenting SOME token that does not verify against
        Supabase (no dependency override active - forces the real
        get_current_user_and_token path against the token in the header)
        must never reach get_learning_insights, regardless of the exact
        error status the shared auth dependency happens to return for an
        unverifiable token in this environment (that behavior is
        pre-existing and shared by every authenticated endpoint - not a
        Phase 5E concern). The one thing that matters here: the database
        is never touched and the response is never a 200."""
        with patch.object(main_module, "get_learning_insights", new=AsyncMock()) as mock_get:
            resp = client.get(
                "/api/profile/insights",
                headers={"Authorization": "Bearer forged-token-belonging-to-no-one"},
            )
        assert resp.status_code != 200
        mock_get.assert_not_called()

    def test_service_role_is_never_used_for_this_endpoint(self, client):
        """Structural check: profile_insights_endpoint's only path to the
        database is get_learning_insights, which (per
        test_learning_insights_database.py) forwards the student's own
        token - there is no alternate, privileged code path here."""
        import inspect
        source = inspect.getsource(main_module.profile_insights_endpoint)
        assert "service_role" not in source.lower()
        assert "SERVICE_ROLE" not in source
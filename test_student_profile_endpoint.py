"""
Tests for GET/PUT /api/profile in backend/main.py (Phase 6A).

Run with: GEMINI_API_KEY=dummy python3 -m pytest test_student_profile_endpoint.py -v

Auth is stubbed via dependency_overrides on get_current_user_and_token,
same convention as test_profile_topics_endpoint.py.
backend.database.get_student_profile / upsert_student_profile are mocked
at the backend.main import site for endpoint-contract tests; their own
real behavior (PostgREST request shape, upsert semantics) is covered in
test_student_profile_database.py.
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
    async def _fake_user_and_token():
        return (user_id, token)
    main_module.app.dependency_overrides[main_module.get_current_user_and_token] = _fake_user_and_token


SAMPLE_PROFILE = {
    "user_id": "user-1",
    "name": "Mahfuz Khan",
    "user_class": "Class 9-10 (SSC)",
    "stream": "Science (বিজ্ঞান)",
    "created_at": "2026-09-01T10:00:00+00:00",
    "updated_at": "2026-09-01T10:00:00+00:00",
}


# ---------------------------------------------------------------------------
# GET /api/profile
# ---------------------------------------------------------------------------

class TestGetProfileAuthentication:
    def test_unauthenticated_request_returns_401(self, client):
        resp = client.get("/api/profile")
        assert resp.status_code == 401

    def test_authenticated_request_with_profile_returns_200(self, client):
        _override_auth_as("user-1")
        with patch.object(main_module, "get_student_profile", new=AsyncMock(return_value=SAMPLE_PROFILE)):
            resp = client.get("/api/profile")
        assert resp.status_code == 200
        assert resp.json() == {"profile": SAMPLE_PROFILE}


class TestGetProfileMissing:
    def test_no_profile_yet_returns_404(self, client):
        _override_auth_as("user-1")
        with patch.object(main_module, "get_student_profile", new=AsyncMock(return_value=None)):
            resp = client.get("/api/profile")
        assert resp.status_code == 404


class TestGetProfileIsolation:
    def test_uses_authenticated_users_own_identity_and_token(self, client):
        _override_auth_as("user-42", token="student-42-token")
        mock_fn = AsyncMock(return_value=None)
        with patch.object(main_module, "get_student_profile", new=mock_fn):
            client.get("/api/profile")
        _, kwargs = mock_fn.call_args
        assert kwargs["user_id"] == "user-42"
        assert kwargs["user_token"] == "student-42-token"

    def test_different_authenticated_users_get_independently_scoped_calls(self, client):
        mock_fn = AsyncMock(return_value=None)

        _override_auth_as("user-A", token="token-A")
        with patch.object(main_module, "get_student_profile", new=mock_fn):
            client.get("/api/profile")
        assert mock_fn.call_args.kwargs["user_id"] == "user-A"

        _override_auth_as("user-B", token="token-B")
        with patch.object(main_module, "get_student_profile", new=mock_fn):
            client.get("/api/profile")
        assert mock_fn.call_args.kwargs["user_id"] == "user-B"


class TestGetProfileFailureHandling:
    def test_database_error_returns_502_not_a_stack_trace(self, client):
        _override_auth_as("user-1")
        with patch.object(main_module, "get_student_profile", new=AsyncMock(side_effect=DatabaseError("boom"))):
            resp = client.get("/api/profile")
        assert resp.status_code == 502
        assert "boom" not in resp.text


# ---------------------------------------------------------------------------
# PUT /api/profile
# ---------------------------------------------------------------------------

VALID_FORM = {
    "name": "Mahfuz Khan",
    "user_class": "Class 9-10 (SSC)",
    "stream": "Science (বিজ্ঞান)",
}


class TestUpdateProfileAuthentication:
    def test_unauthenticated_request_returns_401(self, client):
        resp = client.put("/api/profile", data=VALID_FORM)
        assert resp.status_code == 401

    def test_authenticated_valid_request_succeeds(self, client):
        _override_auth_as("user-1")
        with patch.object(main_module, "upsert_student_profile", new=AsyncMock(return_value=SAMPLE_PROFILE)):
            resp = client.put("/api/profile", data=VALID_FORM)
        assert resp.status_code == 200
        assert resp.json() == {"profile": SAMPLE_PROFILE}


class TestUpdateProfileValidation:
    def test_missing_name_field_returns_422(self, client):
        _override_auth_as("user-1")
        resp = client.put("/api/profile", data={"user_class": "Class 6-8", "stream": "Arts (মানবিক)"})
        assert resp.status_code == 422

    def test_empty_name_returns_422(self, client):
        _override_auth_as("user-1")
        resp = client.put("/api/profile", data={**VALID_FORM, "name": "   "})
        assert resp.status_code == 422

    def test_overlong_name_returns_422(self, client):
        _override_auth_as("user-1")
        resp = client.put("/api/profile", data={**VALID_FORM, "name": "A" * 101})
        assert resp.status_code == 422

    def test_bangla_name_is_accepted(self, client):
        _override_auth_as("user-1")
        with patch.object(main_module, "upsert_student_profile", new=AsyncMock(return_value=SAMPLE_PROFILE)) as mock_fn:
            resp = client.put("/api/profile", data={**VALID_FORM, "name": "মাহফুজ খান"})
        assert resp.status_code == 200
        assert mock_fn.call_args.kwargs["name"] == "মাহফুজ খান"

    def test_invalid_class_returns_422(self, client):
        _override_auth_as("user-1")
        resp = client.put("/api/profile", data={**VALID_FORM, "user_class": "Class 99"})
        assert resp.status_code == 422

    def test_invalid_stream_returns_422(self, client):
        _override_auth_as("user-1")
        resp = client.put("/api/profile", data={**VALID_FORM, "stream": "Underwater Basket Weaving"})
        assert resp.status_code == 422

    def test_all_valid_classes_accepted(self, client):
        _override_auth_as("user-1")
        # Class 6-8 has no stream (see TestUpdateProfileNoStreamClasses
        # below for the dedicated Bug 2 coverage) - every other class
        # still requires one.
        class_and_stream = [
            ("Class 6-8", ""),
            ("Class 9-10 (SSC)", "Science (বিজ্ঞান)"),
            ("Class 11-12 (HSC)", "Science (বিজ্ঞান)"),
        ]
        for cls, stream in class_and_stream:
            with patch.object(main_module, "upsert_student_profile", new=AsyncMock(return_value=SAMPLE_PROFILE)):
                resp = client.put("/api/profile", data={**VALID_FORM, "user_class": cls, "stream": stream})
            assert resp.status_code == 200, cls

    def test_all_valid_streams_accepted(self, client):
        _override_auth_as("user-1")
        for stream in ("Science (বিজ্ঞান)", "Commerce (ব্যবসায় শিক্ষা)", "Arts (মানবিক)"):
            with patch.object(main_module, "upsert_student_profile", new=AsyncMock(return_value=SAMPLE_PROFILE)):
                resp = client.put("/api/profile", data={**VALID_FORM, "stream": stream})
            assert resp.status_code == 200, stream

    def test_name_is_trimmed_before_persisting(self, client):
        _override_auth_as("user-1")
        with patch.object(main_module, "upsert_student_profile", new=AsyncMock(return_value=SAMPLE_PROFILE)) as mock_fn:
            client.put("/api/profile", data={**VALID_FORM, "name": "  Mahfuz Khan  "})
        assert mock_fn.call_args.kwargs["name"] == "Mahfuz Khan"


# ---------------------------------------------------------------------------
# BUG 2 (Phase 6A correction): Class 6-8 has no academic stream in the
# NCTB curriculum (streaming starts at Class 9) - these tests are the
# actual enforcement boundary; the frontend hiding the stream field is
# only a UX convenience.
# ---------------------------------------------------------------------------

SAMPLE_PROFILE_NO_STREAM = {
    "user_id": "user-1",
    "name": "Mahfuz Khan",
    "user_class": "Class 6-8",
    "stream": None,
    "created_at": "2026-09-01T10:00:00+00:00",
    "updated_at": "2026-09-01T10:00:00+00:00",
}


class TestUpdateProfileNoStreamClasses:
    def test_class_6_8_with_empty_stream_succeeds(self, client):
        _override_auth_as("user-1")
        with patch.object(main_module, "upsert_student_profile", new=AsyncMock(return_value=SAMPLE_PROFILE_NO_STREAM)) as mock_fn:
            resp = client.put("/api/profile", data={"name": "Mahfuz Khan", "user_class": "Class 6-8", "stream": ""})
        assert resp.status_code == 200
        assert resp.json() == {"profile": SAMPLE_PROFILE_NO_STREAM}
        assert mock_fn.call_args.kwargs["stream"] is None

    def test_class_6_8_with_stream_field_omitted_entirely_succeeds(self, client):
        # No "stream" key on the form at all - Form("") default covers it.
        _override_auth_as("user-1")
        with patch.object(main_module, "upsert_student_profile", new=AsyncMock(return_value=SAMPLE_PROFILE_NO_STREAM)):
            resp = client.put("/api/profile", data={"name": "Mahfuz Khan", "user_class": "Class 6-8"})
        assert resp.status_code == 200

    def test_class_6_8_with_a_stream_is_rejected(self, client):
        _override_auth_as("user-1")
        resp = client.put("/api/profile", data={"name": "Mahfuz Khan", "user_class": "Class 6-8", "stream": "Science (বিজ্ঞান)"})
        assert resp.status_code == 422

    def test_class_9_10_without_stream_is_rejected(self, client):
        _override_auth_as("user-1")
        resp = client.put("/api/profile", data={"name": "Mahfuz Khan", "user_class": "Class 9-10 (SSC)", "stream": ""})
        assert resp.status_code == 422

    def test_class_11_12_without_stream_is_rejected(self, client):
        _override_auth_as("user-1")
        resp = client.put("/api/profile", data={"name": "Mahfuz Khan", "user_class": "Class 11-12 (HSC)", "stream": ""})
        assert resp.status_code == 422

    def test_class_9_10_with_stream_field_omitted_entirely_is_rejected(self, client):
        _override_auth_as("user-1")
        resp = client.put("/api/profile", data={"name": "Mahfuz Khan", "user_class": "Class 9-10 (SSC)"})
        assert resp.status_code == 422

    def test_stream_none_reaches_upsert_as_python_none_not_the_string_none(self, client):
        # Guards against a subtle regression where "None" (string) leaks
        # through instead of the actual Python None sentinel.
        _override_auth_as("user-1")
        with patch.object(main_module, "upsert_student_profile", new=AsyncMock(return_value=SAMPLE_PROFILE_NO_STREAM)) as mock_fn:
            client.put("/api/profile", data={"name": "Mahfuz Khan", "user_class": "Class 6-8", "stream": ""})
        stored_stream = mock_fn.call_args.kwargs["stream"]
        assert stored_stream is None
        assert stored_stream != "None"


class TestUpdateProfileIsolation:
    def test_uses_authenticated_users_own_identity_and_token_never_client_supplied(self, client):
        # No user_id field exists on the form at all - authorization can
        # only ever come from the verified token, never the request body.
        _override_auth_as("user-42", token="student-42-token")
        mock_fn = AsyncMock(return_value=SAMPLE_PROFILE)
        with patch.object(main_module, "upsert_student_profile", new=mock_fn):
            client.put("/api/profile", data=VALID_FORM)
        _, kwargs = mock_fn.call_args
        assert kwargs["user_id"] == "user-42"
        assert kwargs["user_token"] == "student-42-token"

    def test_different_authenticated_users_get_independently_scoped_calls(self, client):
        mock_fn = AsyncMock(return_value=SAMPLE_PROFILE)

        _override_auth_as("user-A", token="token-A")
        with patch.object(main_module, "upsert_student_profile", new=mock_fn):
            client.put("/api/profile", data=VALID_FORM)
        assert mock_fn.call_args.kwargs["user_id"] == "user-A"

        _override_auth_as("user-B", token="token-B")
        with patch.object(main_module, "upsert_student_profile", new=mock_fn):
            client.put("/api/profile", data=VALID_FORM)
        assert mock_fn.call_args.kwargs["user_id"] == "user-B"


class TestUpdateProfileFailureHandling:
    def test_database_error_returns_502_not_a_stack_trace(self, client):
        _override_auth_as("user-1")
        with patch.object(main_module, "upsert_student_profile", new=AsyncMock(side_effect=DatabaseError("boom"))):
            resp = client.put("/api/profile", data=VALID_FORM)
        assert resp.status_code == 502
        assert "boom" not in resp.text
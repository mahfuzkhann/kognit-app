"""
Unit tests for backend/database.py:get_student_profile() and
upsert_student_profile() (Phase 6A).

Run with: GEMINI_API_KEY=dummy python3 -m pytest test_student_profile_database.py -v

Uses httpx.MockTransport to simulate Supabase's PostgREST responses
without any real network call - same convention as test_quiz_database.py.
"""
import asyncio
import json as jsonlib
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

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


SAMPLE_ROW = {
    "user_id": "user-1",
    "name": "Mahfuz Khan",
    "user_class": "Class 9-10 (SSC)",
    "stream": "Science (বিজ্ঞান)",
    "created_at": "2026-09-01T10:00:00+00:00",
    "updated_at": "2026-09-01T10:00:00+00:00",
}


# ---------------------------------------------------------------------------
# get_student_profile
# ---------------------------------------------------------------------------

class TestGetStudentProfileConfig:
    def test_not_configured_raises(self, monkeypatch):
        monkeypatch.setattr(database, "SUPABASE_URL", "")
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.get_student_profile(user_token="t", user_id="u"))


class TestGetStudentProfileHappyPath:
    def test_existing_profile_returns_row(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/student_profiles")
            assert request.method == "GET"
            return httpx.Response(200, json=[SAMPLE_ROW])

        _patch_async_client(monkeypatch, _make_transport(handler))
        result = asyncio.run(database.get_student_profile(user_token="t", user_id="user-1"))
        assert result == SAMPLE_ROW

    def test_no_profile_yet_returns_none(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        _patch_async_client(monkeypatch, _make_transport(handler))
        result = asyncio.run(database.get_student_profile(user_token="t", user_id="user-1"))
        assert result is None

    def test_forwards_students_own_token_never_a_service_role_key(self, monkeypatch):
        seen_auth_headers = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_auth_headers.append(request.headers.get("authorization"))
            return httpx.Response(200, json=[])

        _patch_async_client(monkeypatch, _make_transport(handler))
        asyncio.run(database.get_student_profile(user_token="THIS_STUDENTS_OWN_TOKEN", user_id="u"))
        assert seen_auth_headers == ["Bearer THIS_STUDENTS_OWN_TOKEN"]

    def test_never_filters_by_user_id_relies_on_rls(self, monkeypatch):
        # RLS is the enforcement boundary - this function must never add a
        # user_id=eq.... filter itself (that would be redundant at best,
        # and a false sense of security if RLS were ever misconfigured).
        def handler(request: httpx.Request) -> httpx.Response:
            assert "user_id" not in request.url.params
            return httpx.Response(200, json=[])

        _patch_async_client(monkeypatch, _make_transport(handler))
        asyncio.run(database.get_student_profile(user_token="t", user_id="user-1"))


class TestGetStudentProfileFailureModes:
    def test_non_200_raises(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"message": "boom"})

        _patch_async_client(monkeypatch, _make_transport(handler))
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.get_student_profile(user_token="t", user_id="u"))

    def test_network_error_raises(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

        _patch_async_client(monkeypatch, _make_transport(handler))
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.get_student_profile(user_token="t", user_id="u"))

    def test_unexpected_shape_raises(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"not": "a list"})

        _patch_async_client(monkeypatch, _make_transport(handler))
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.get_student_profile(user_token="t", user_id="u"))


# ---------------------------------------------------------------------------
# upsert_student_profile
# ---------------------------------------------------------------------------

class TestUpsertStudentProfileConfig:
    def test_not_configured_raises(self, monkeypatch):
        monkeypatch.setattr(database, "SUPABASE_URL", "")
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.upsert_student_profile(
                user_token="t", user_id="u", name="N", user_class="Class 6-8", stream="Arts (মানবিক)",
            ))


class TestUpsertStudentProfileHappyPath:
    def test_sends_upsert_with_on_conflict_user_id(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/student_profiles")
            assert request.method == "POST"
            assert request.url.params.get("on_conflict") == "user_id"
            assert request.headers.get("prefer") == "resolution=merge-duplicates,return=representation"
            body = jsonlib.loads(request.content)
            assert body == {
                "user_id": "user-1",
                "name": "Mahfuz Khan",
                "user_class": "Class 9-10 (SSC)",
                "stream": "Science (বিজ্ঞান)",
            }
            # updated_at/created_at are intentionally never sent by this
            # function - the DB trigger/default own those columns.
            assert "updated_at" not in body
            assert "created_at" not in body
            return httpx.Response(201, json=[SAMPLE_ROW])

        _patch_async_client(monkeypatch, _make_transport(handler))
        result = asyncio.run(database.upsert_student_profile(
            user_token="t", user_id="user-1", name="Mahfuz Khan",
            user_class="Class 9-10 (SSC)", stream="Science (বিজ্ঞান)",
        ))
        assert result == SAMPLE_ROW

    def test_forwards_students_own_token_never_a_service_role_key(self, monkeypatch):
        seen_auth_headers = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_auth_headers.append(request.headers.get("authorization"))
            return httpx.Response(201, json=[SAMPLE_ROW])

        _patch_async_client(monkeypatch, _make_transport(handler))
        asyncio.run(database.upsert_student_profile(
            user_token="THIS_STUDENTS_OWN_TOKEN", user_id="u", name="N",
            user_class="Class 6-8", stream="Arts (মানবিক)",
        ))
        assert seen_auth_headers == ["Bearer THIS_STUDENTS_OWN_TOKEN"]

    def test_none_stream_sent_as_json_null_bug2(self, monkeypatch):
        # BUG 2 (Phase 6A correction): Class 6-8 has no stream -
        # backend/main.py passes Python None for that case, which must
        # serialize to a real JSON null (clearing the column via
        # PostgREST), not the string "None" and not be omitted from the
        # payload (omitting it would leave a previously-set stream
        # untouched on an UPDATE, which would be wrong if a student edits
        # their class down to 6-8).
        def handler(request: httpx.Request) -> httpx.Response:
            body = jsonlib.loads(request.content)
            assert "stream" in body
            assert body["stream"] is None
            no_stream_row = {**SAMPLE_ROW, "user_class": "Class 6-8", "stream": None}
            return httpx.Response(201, json=[no_stream_row])

        _patch_async_client(monkeypatch, _make_transport(handler))
        result = asyncio.run(database.upsert_student_profile(
            user_token="t", user_id="user-1", name="Mahfuz Khan",
            user_class="Class 6-8", stream=None,
        ))
        assert result["stream"] is None


class TestUpsertStudentProfileFailureModes:
    def test_non_2xx_raises(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"message": "rls denied"})

        _patch_async_client(monkeypatch, _make_transport(handler))
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.upsert_student_profile(
                user_token="t", user_id="u", name="N", user_class="Class 6-8", stream="Arts (মানবিক)",
            ))

    def test_network_error_raises(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

        _patch_async_client(monkeypatch, _make_transport(handler))
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.upsert_student_profile(
                user_token="t", user_id="u", name="N", user_class="Class 6-8", stream="Arts (মানবিক)",
            ))

    def test_empty_response_list_raises(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(201, json=[])

        _patch_async_client(monkeypatch, _make_transport(handler))
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.upsert_student_profile(
                user_token="t", user_id="u", name="N", user_class="Class 6-8", stream="Arts (মানবিক)",
            ))
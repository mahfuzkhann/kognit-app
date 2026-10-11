"""
Tests for the Pocket Notes section of backend/database.py.

Run with: GEMINI_API_KEY=dummy python3 -m pytest test_notes_database.py -v

Uses httpx.MockTransport (same convention as test_student_profile_database.py)
so the exact PostgREST request shape is asserted without any network call.
"""
import asyncio
import json as jsonlib
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

import httpx
import pytest

from backend import database

USER = "11111111-1111-1111-1111-111111111111"
NOTE_ID = "123e4567-e89b-12d3-a456-426614174000"
ROW = {"id": NOTE_ID, "client_id": "n_abcdef123456", "content_md": "x", "tags": [], "needs_review": True}


def _patch_client(monkeypatch, handler):
    real = httpx.AsyncClient

    def factory(*a, **k):
        k["transport"] = httpx.MockTransport(handler)
        return real(*a, **k)

    monkeypatch.setattr(database.httpx, "AsyncClient", factory)


@pytest.fixture(autouse=True)
def supabase_env(monkeypatch):
    monkeypatch.setattr(database, "SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setattr(database, "SUPABASE_ANON_KEY", "anon-key-123")


def run(coro):
    return asyncio.run(coro)


class TestCreate:
    def test_request_shape_and_user_id_forced_from_identity(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["method"] = request.method
            seen["params"] = dict(request.url.params)
            seen["headers"] = request.headers
            seen["body"] = jsonlib.loads(request.content)
            return httpx.Response(201, json=[ROW])

        _patch_client(monkeypatch, handler)
        out = run(database.create_pocket_note("tok", USER, {"client_id": "n_abcdef123456", "content_md": "x", "user_id": "ATTACKER"}))
        assert out == ROW
        assert seen["method"] == "POST"
        assert seen["body"]["user_id"] == USER                       # never the caller-supplied value
        assert seen["params"]["on_conflict"] == "user_id,client_id"
        assert seen["headers"]["prefer"] == "resolution=ignore-duplicates,return=representation"
        assert seen["headers"]["authorization"] == "Bearer tok"      # the student's own token

    def test_duplicate_returns_none(self, monkeypatch):
        _patch_client(monkeypatch, lambda r: httpx.Response(201, json=[]))
        assert run(database.create_pocket_note("tok", USER, {"client_id": "n_abcdef123456", "content_md": "x"})) is None

    @pytest.mark.parametrize("status", [400, 401, 403, 409, 500, 503])
    def test_http_failure_raises(self, monkeypatch, status):
        _patch_client(monkeypatch, lambda r: httpx.Response(status, text="secret internals"))
        with pytest.raises(database.DatabaseError) as exc:
            run(database.create_pocket_note("tok", USER, {"client_id": "n_abcdef123456", "content_md": "x"}))
        assert "secret internals" not in str(exc.value)

    def test_network_error_raises(self, monkeypatch):
        def handler(request):
            raise httpx.ConnectError("boom")
        _patch_client(monkeypatch, handler)
        with pytest.raises(database.DatabaseError):
            run(database.create_pocket_note("tok", USER, {"client_id": "n_abcdef123456", "content_md": "x"}))

    def test_unexpected_shape_raises(self, monkeypatch):
        _patch_client(monkeypatch, lambda r: httpx.Response(201, json={"not": "a list"}))
        with pytest.raises(database.DatabaseError):
            run(database.create_pocket_note("tok", USER, {"client_id": "n_abcdef123456", "content_md": "x"}))

    def test_missing_supabase_config_raises(self, monkeypatch):
        monkeypatch.setattr(database, "SUPABASE_URL", "")
        with pytest.raises(database.DatabaseError):
            run(database.get_pocket_note("tok", USER, NOTE_ID))


class TestReads:
    def test_get_filters_by_id_and_user(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["params"] = dict(request.url.params)
            return httpx.Response(200, json=[ROW])

        _patch_client(monkeypatch, handler)
        assert run(database.get_pocket_note("tok", USER, NOTE_ID)) == ROW
        assert seen["params"]["id"] == f"eq.{NOTE_ID}"
        assert seen["params"]["user_id"] == f"eq.{USER}"          # app-level ownership filter

    def test_get_missing_returns_none(self, monkeypatch):
        _patch_client(monkeypatch, lambda r: httpx.Response(200, json=[]))
        assert run(database.get_pocket_note("tok", USER, NOTE_ID)) is None

    def test_columns_never_include_user_id(self):
        assert "user_id" not in database.POCKET_NOTE_COLUMNS.split(",")

    def test_list_pagination_requests_one_extra_row(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["params"] = dict(request.url.params)
            return httpx.Response(200, json=[dict(ROW, id=str(i)) for i in range(4)])

        _patch_client(monkeypatch, handler)
        rows, has_more = run(database.list_pocket_notes("tok", USER, limit=3, offset=6))
        assert seen["params"]["limit"] == "4" and seen["params"]["offset"] == "6"
        assert seen["params"]["order"] == "created_at.desc,id.desc"
        assert len(rows) == 3 and has_more is True

    def test_list_last_page(self, monkeypatch):
        _patch_client(monkeypatch, lambda r: httpx.Response(200, json=[ROW]))
        rows, has_more = run(database.list_pocket_notes("tok", USER, limit=3, offset=0))
        assert len(rows) == 1 and has_more is False

    def test_by_client_id(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["params"] = dict(request.url.params)
            return httpx.Response(200, json=[ROW])

        _patch_client(monkeypatch, handler)
        assert run(database.get_pocket_note_by_client_id("tok", USER, "n_abcdef123456")) == ROW
        assert seen["params"]["client_id"] == "eq.n_abcdef123456"
        assert seen["params"]["user_id"] == f"eq.{USER}"

    @pytest.mark.parametrize("header,expected", [("0-0/42", 42), ("*/0", 0), ("0-0/500", 500)])
    def test_count_reads_content_range(self, monkeypatch, header, expected):
        _patch_client(monkeypatch, lambda r: httpx.Response(206, json=[], headers={"content-range": header}))
        assert run(database.count_pocket_notes("tok", USER)) == expected

    def test_count_without_content_range_raises(self, monkeypatch):
        _patch_client(monkeypatch, lambda r: httpx.Response(200, json=[]))
        with pytest.raises(database.DatabaseError):
            run(database.count_pocket_notes("tok", USER))


class TestUpdateDelete:
    def test_update_whitelists_columns(self, monkeypatch):
        _patch_client(monkeypatch, lambda r: httpx.Response(200, json=[ROW]))
        for forbidden in ({"content_md": "tampered"}, {"user_id": USER}, {"id": NOTE_ID}, {"client_id": "x"}, {}):
            with pytest.raises(database.DatabaseError):
                run(database.update_pocket_note("tok", USER, NOTE_ID, forbidden))

    def test_update_filters_by_owner_and_returns_row(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["method"] = request.method
            seen["params"] = dict(request.url.params)
            seen["body"] = jsonlib.loads(request.content)
            return httpx.Response(200, json=[ROW])

        _patch_client(monkeypatch, handler)
        assert run(database.update_pocket_note("tok", USER, NOTE_ID, {"tags": [], "needs_review": True})) == ROW
        assert seen["method"] == "PATCH"
        assert seen["params"]["user_id"] == f"eq.{USER}" and seen["params"]["id"] == f"eq.{NOTE_ID}"
        assert seen["body"] == {"tags": [], "needs_review": True}

    def test_update_no_match_returns_none(self, monkeypatch):
        _patch_client(monkeypatch, lambda r: httpx.Response(200, json=[]))
        assert run(database.update_pocket_note("tok", USER, NOTE_ID, {"tags": []})) is None

    def test_delete_true_when_row_removed(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["method"] = request.method
            seen["params"] = dict(request.url.params)
            return httpx.Response(200, json=[{"id": NOTE_ID}])

        _patch_client(monkeypatch, handler)
        assert run(database.delete_pocket_note("tok", USER, NOTE_ID)) is True
        assert seen["method"] == "DELETE" and seen["params"]["user_id"] == f"eq.{USER}"

    def test_delete_false_when_no_owned_row_matched(self, monkeypatch):
        _patch_client(monkeypatch, lambda r: httpx.Response(200, json=[]))
        assert run(database.delete_pocket_note("tok", USER, NOTE_ID)) is False

    def test_delete_failure_raises(self, monkeypatch):
        _patch_client(monkeypatch, lambda r: httpx.Response(500, text="x"))
        with pytest.raises(database.DatabaseError):
            run(database.delete_pocket_note("tok", USER, NOTE_ID))
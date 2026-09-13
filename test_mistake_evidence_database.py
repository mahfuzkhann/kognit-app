"""
Unit tests for backend/database.py's Phase 6C addition: get_user_topic_mistakes.

Run with: GEMINI_API_KEY=dummy python3 -m pytest test_mistake_evidence_database.py -v

Same httpx.MockTransport convention as test_quiz_database.py /
test_learning_insights_database.py - no real network calls.
"""
import asyncio
import os
import sys

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


def _row(attempt_id, subject, topic, topic_key, score, total_questions, created_at):
    return {
        "id": attempt_id, "subject": subject, "topic": topic, "topic_key": topic_key,
        "total_questions": total_questions, "score": score, "created_at": created_at,
    }


class TestConfigAndTransport:
    def test_not_configured_raises(self, monkeypatch):
        monkeypatch.setattr(database, "SUPABASE_URL", "")
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.get_user_topic_mistakes(user_token="t", user_id="u"))

    def test_forwards_students_own_token_never_service_role(self, monkeypatch):
        seen = []

        def handler(request):
            seen.append(request.headers.get("authorization"))
            return httpx.Response(200, json=[])

        _patch_async_client(monkeypatch, _make_transport(handler))
        asyncio.run(database.get_user_topic_mistakes(user_token="THIS_STUDENTS_OWN_TOKEN", user_id="u"))
        assert seen == ["Bearer THIS_STUDENTS_OWN_TOKEN"]

    def test_no_user_id_query_filter_is_ever_applied(self, monkeypatch):
        seen_params = []

        def handler(request):
            seen_params.append(dict(request.url.params))
            return httpx.Response(200, json=[])

        _patch_async_client(monkeypatch, _make_transport(handler))
        asyncio.run(database.get_user_topic_mistakes(user_token="t", user_id="user-should-not-appear"))
        for params in seen_params:
            assert "user_id" not in params
            for value in params.values():
                assert "user-should-not-appear" not in value

    def test_query_excludes_legacy_null_topic_key_rows(self, monkeypatch):
        seen_params = []

        def handler(request):
            seen_params.append(dict(request.url.params))
            return httpx.Response(200, json=[])

        _patch_async_client(monkeypatch, _make_transport(handler))
        asyncio.run(database.get_user_topic_mistakes(user_token="t", user_id="u"))
        assert seen_params[0]["topic_key"] == "not.is.null"

    def test_query_is_bounded_by_limit(self, monkeypatch):
        seen_params = []

        def handler(request):
            seen_params.append(dict(request.url.params))
            return httpx.Response(200, json=[])

        _patch_async_client(monkeypatch, _make_transport(handler))
        asyncio.run(database.get_user_topic_mistakes(user_token="t", user_id="u"))
        assert seen_params[0]["limit"] == str(database.MAX_MISTAKE_EVIDENCE_ROWS)

    def test_query_ordered_descending_at_the_wire(self, monkeypatch):
        """Ordered DESC+limit at the HTTP layer (so a bound silently drops
        the OLDEST evidence, not the newest) - restored to ascending
        order internally before grouping, see database.py's comment."""
        seen_params = []

        def handler(request):
            seen_params.append(dict(request.url.params))
            return httpx.Response(200, json=[])

        _patch_async_client(monkeypatch, _make_transport(handler))
        asyncio.run(database.get_user_topic_mistakes(user_token="t", user_id="u"))
        assert seen_params[0]["order"] == "created_at.desc"

    def test_only_safe_columns_are_selected_never_quiz_answers_content(self, monkeypatch):
        seen_params = []

        def handler(request):
            seen_params.append(dict(request.url.params))
            return httpx.Response(200, json=[])

        _patch_async_client(monkeypatch, _make_transport(handler))
        asyncio.run(database.get_user_topic_mistakes(user_token="t", user_id="u"))
        select = seen_params[0]["select"]
        for forbidden in ("question_text", "selected_index", "correct_index", "is_correct"):
            assert forbidden not in select

    def test_non_200_raises(self, monkeypatch):
        _patch_async_client(monkeypatch, _make_transport(lambda r: httpx.Response(500)))
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.get_user_topic_mistakes(user_token="t", user_id="u"))

    def test_network_error_raises(self, monkeypatch):
        def handler(request):
            raise httpx.ConnectError("boom", request=request)

        _patch_async_client(monkeypatch, _make_transport(handler))
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.get_user_topic_mistakes(user_token="t", user_id="u"))

    def test_unexpected_shape_raises(self, monkeypatch):
        _patch_async_client(monkeypatch, _make_transport(lambda r: httpx.Response(200, json={"not": "a list"})))
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.get_user_topic_mistakes(user_token="t", user_id="u"))

    def test_empty_rows_returns_empty_list_not_error(self, monkeypatch):
        _patch_async_client(monkeypatch, _make_transport(lambda r: httpx.Response(200, json=[])))
        result = asyncio.run(database.get_user_topic_mistakes(user_token="t", user_id="u"))
        assert result == []


class TestTranslationAndGrouping:
    def test_repeated_wrong_topic_produces_a_mistake_entry(self, monkeypatch):
        rows = [
            _row("a1", "Physics", "Force & Motion", "force & motion", 5, 10, "2026-08-01T10:00:00+00:00"),
            _row("a2", "Physics", "Force & Motion", "force & motion", 6, 10, "2026-08-02T10:00:00+00:00"),
        ]
        _patch_async_client(monkeypatch, _make_transport(lambda r: httpx.Response(200, json=rows)))
        result = asyncio.run(database.get_user_topic_mistakes(user_token="t", user_id="u"))
        assert len(result) == 1
        assert result[0]["subject"] == "Physics"
        assert result[0]["topic_key"] == "force & motion"
        assert result[0]["mistake_type"] == "repeated_topic_difficulty"

    def test_strong_topic_produces_no_mistake_entry(self, monkeypatch):
        rows = [
            _row("a1", "Physics", "Force & Motion", "force & motion", 10, 10, "2026-08-01T10:00:00+00:00"),
            _row("a2", "Physics", "Force & Motion", "force & motion", 9, 10, "2026-08-02T10:00:00+00:00"),
        ]
        _patch_async_client(monkeypatch, _make_transport(lambda r: httpx.Response(200, json=rows)))
        result = asyncio.run(database.get_user_topic_mistakes(user_token="t", user_id="u"))
        assert result == []

    def test_response_never_contains_raw_content_fields(self, monkeypatch):
        rows = [
            _row("a1", "Physics", "Force & Motion", "force & motion", 5, 10, "2026-08-01T10:00:00+00:00"),
            _row("a2", "Physics", "Force & Motion", "force & motion", 6, 10, "2026-08-02T10:00:00+00:00"),
        ]
        _patch_async_client(monkeypatch, _make_transport(lambda r: httpx.Response(200, json=rows)))
        result = asyncio.run(database.get_user_topic_mistakes(user_token="t", user_id="u"))
        result_str = str(result)
        for forbidden in ("question_text", "selected_index", "correct_index"):
            assert forbidden not in result_str

    def test_unparseable_timestamp_row_is_skipped_not_fatal(self, monkeypatch):
        rows = [
            _row("a1", "Physics", "Force & Motion", "force & motion", 5, 10, "not-a-real-timestamp"),
            _row("a2", "Physics", "Force & Motion", "force & motion", 5, 10, "2026-08-02T10:00:00+00:00"),
            _row("a3", "Physics", "Force & Motion", "force & motion", 5, 10, "2026-08-03T10:00:00+00:00"),
        ]
        _patch_async_client(monkeypatch, _make_transport(lambda r: httpx.Response(200, json=rows)))
        result = asyncio.run(database.get_user_topic_mistakes(user_token="t", user_id="u"))
        assert len(result) == 1
        assert result[0]["affected_attempts"] == 2

    def test_malformed_rows_are_skipped_not_fatal(self, monkeypatch):
        rows = [
            "not a dict",
            {"subject": None},
            _row("a1", "Physics", "Force & Motion", "force & motion", 5, 10, "2026-08-01T10:00:00+00:00"),
            _row("a2", "Physics", "Force & Motion", "force & motion", 5, 10, "2026-08-02T10:00:00+00:00"),
        ]
        _patch_async_client(monkeypatch, _make_transport(lambda r: httpx.Response(200, json=rows)))
        result = asyncio.run(database.get_user_topic_mistakes(user_token="t", user_id="u"))
        assert len(result) == 1

    def test_multiple_topics_and_subjects_grouped_independently(self, monkeypatch):
        rows = [
            _row("a1", "Physics", "Force & Motion", "force & motion", 5, 10, "2026-08-01T10:00:00+00:00"),
            _row("a2", "Physics", "Force & Motion", "force & motion", 5, 10, "2026-08-02T10:00:00+00:00"),
            _row("c1", "Chemistry", "Stoichiometry", "stoichiometry", 5, 10, "2026-08-01T10:00:00+00:00"),
            _row("c2", "Chemistry", "Stoichiometry", "stoichiometry", 5, 10, "2026-08-02T10:00:00+00:00"),
        ]
        _patch_async_client(monkeypatch, _make_transport(lambda r: httpx.Response(200, json=rows)))
        result = asyncio.run(database.get_user_topic_mistakes(user_token="t", user_id="u"))
        subjects = {r["subject"] for r in result}
        assert subjects == {"Physics", "Chemistry"}

    def test_ordering_restored_ascending_before_grouping(self, monkeypatch):
        # Server returns DESC order (most recent first); translation must
        # still compute first/last_observed_at correctly regardless.
        rows = [
            _row("a2", "Physics", "Force & Motion", "force & motion", 5, 10, "2026-08-10T10:00:00+00:00"),
            _row("a1", "Physics", "Force & Motion", "force & motion", 5, 10, "2026-08-01T10:00:00+00:00"),
        ]
        _patch_async_client(monkeypatch, _make_transport(lambda r: httpx.Response(200, json=rows)))
        result = asyncio.run(database.get_user_topic_mistakes(user_token="t", user_id="u"))
        assert result[0]["first_observed_at"] == "2026-08-01T10:00:00+00:00"
        assert result[0]["last_observed_at"] == "2026-08-10T10:00:00+00:00"


class TestCrossUserIsolationAdversarial:
    def test_response_scoped_only_to_calling_users_rows_via_rls_not_query(self, monkeypatch):
        """This test documents the security model rather than proving RLS
        itself (RLS is enforced by live Postgres, not by this code) - it
        proves that get_user_topic_mistakes never adds its own user_id
        filter, so isolation depends entirely on the forwarded token +
        RLS, exactly like get_user_topic_profile / get_learning_insights."""
        seen_params = []

        def handler(request):
            seen_params.append(dict(request.url.params))
            return httpx.Response(200, json=[])

        _patch_async_client(monkeypatch, _make_transport(handler))
        asyncio.run(database.get_user_topic_mistakes(user_token="token-for-user-a", user_id="user-a"))
        assert "user_id" not in seen_params[0]
        assert "user-a" not in str(seen_params[0])
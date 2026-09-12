"""
Unit tests for backend/database.py's Phase 5E addition: get_learning_insights.

Run with: GEMINI_API_KEY=dummy python3 -m pytest test_learning_insights_database.py -v

Same httpx.MockTransport convention as test_quiz_database.py /
test_learning_evidence_database.py - no real network calls.
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


def _row(evidence_id, subject, signal_type, attribution_confidence, occurred_at,
         topic=None, topic_key=None):
    return {
        "id": evidence_id, "subject": subject, "topic": topic, "topic_key": topic_key,
        "signal_type": signal_type, "attribution_confidence": attribution_confidence,
        "occurred_at": occurred_at,
    }


class TestGetLearningInsightsConfigAndTransport:
    def test_not_configured_raises(self, monkeypatch):
        monkeypatch.setattr(database, "SUPABASE_URL", "")
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.get_learning_insights(user_token="t", user_id="u"))

    def test_forwards_students_own_token_never_service_role(self, monkeypatch):
        seen = []

        def handler(request):
            seen.append(request.headers.get("authorization"))
            return httpx.Response(200, json=[])

        _patch_async_client(monkeypatch, _make_transport(handler))
        asyncio.run(database.get_learning_insights(user_token="THIS_STUDENTS_OWN_TOKEN", user_id="u"))
        assert seen == ["Bearer THIS_STUDENTS_OWN_TOKEN"]

    def test_no_user_id_query_filter_is_ever_applied(self, monkeypatch):
        """user_id must never appear as a query filter - RLS is the only
        thing allowed to restrict rows, matching get_user_topic_profile's
        established, explicitly-documented convention."""
        seen_params = []

        def handler(request):
            seen_params.append(dict(request.url.params))
            return httpx.Response(200, json=[])

        _patch_async_client(monkeypatch, _make_transport(handler))
        asyncio.run(database.get_learning_insights(user_token="t", user_id="user-should-not-appear-in-query"))
        for params in seen_params:
            assert "user_id" not in params
            for value in params.values():
                assert "user-should-not-appear-in-query" not in value

    def test_query_is_bounded_by_limit_not_unbounded(self, monkeypatch):
        seen_params = []

        def handler(request):
            seen_params.append(dict(request.url.params))
            return httpx.Response(200, json=[])

        _patch_async_client(monkeypatch, _make_transport(handler))
        asyncio.run(database.get_learning_insights(user_token="t", user_id="u"))
        assert seen_params[0]["limit"] == str(database.MAX_INSIGHT_EVIDENCE_ROWS)

    def test_non_200_raises(self, monkeypatch):
        _patch_async_client(monkeypatch, _make_transport(lambda r: httpx.Response(500)))
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.get_learning_insights(user_token="t", user_id="u"))

    def test_network_error_raises(self, monkeypatch):
        def handler(request):
            raise httpx.ConnectError("boom", request=request)

        _patch_async_client(monkeypatch, _make_transport(handler))
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.get_learning_insights(user_token="t", user_id="u"))

    def test_empty_evidence_returns_empty_list_not_error(self, monkeypatch):
        _patch_async_client(monkeypatch, _make_transport(lambda r: httpx.Response(200, json=[])))
        result = asyncio.run(database.get_learning_insights(user_token="t", user_id="u"))
        assert result == []


class TestGetLearningInsightsTranslationAndShape:
    def test_translates_rows_into_correct_response_shape(self, monkeypatch):
        rows = [
            _row("e1", "Physics", "confusion", "known", "2026-08-01T10:00:00+00:00"),
            _row("e2", "Physics", "confusion", "known", "2026-08-02T10:00:00+00:00"),
        ]
        _patch_async_client(monkeypatch, _make_transport(lambda r: httpx.Response(200, json=rows)))
        result = asyncio.run(database.get_learning_insights(user_token="t", user_id="u"))
        assert len(result) >= 1
        insight = result[0]
        assert set(insight.keys()) == {
            "subject", "topic", "topic_key", "insight_type", "confidence",
            "evidence_count", "first_observed_at", "last_observed_at",
        }
        assert insight["subject"] == "Physics"
        assert insight["evidence_count"] == 2
        assert "score" not in insight
        assert "correct_rate" not in insight

    def test_unparseable_timestamp_row_is_skipped_not_fatal(self, monkeypatch):
        rows = [
            _row("e1", "Physics", "confusion", "known", "not-a-real-timestamp"),
            _row("e2", "Physics", "confusion", "known", "2026-08-02T10:00:00+00:00"),
            _row("e3", "Physics", "confusion", "known", "2026-08-03T10:00:00+00:00"),
        ]
        _patch_async_client(monkeypatch, _make_transport(lambda r: httpx.Response(200, json=rows)))
        # Should not raise; the bad row is dropped, leaving 2 valid ones -
        # still enough to clear MIN_EVENTS_FOR_INSIGHT.
        result = asyncio.run(database.get_learning_insights(user_token="t", user_id="u"))
        assert len(result) >= 1

    def test_unknown_attribution_row_is_excluded_even_if_present(self, monkeypatch):
        # Defensive: even if the CHECK constraint were ever bypassed and
        # an "unknown" row existed, translation must exclude it.
        rows = [
            _row("e1", "Physics", "confusion", "unknown", "2026-08-01T10:00:00+00:00"),
            _row("e2", "Physics", "confusion", "unknown", "2026-08-02T10:00:00+00:00"),
        ]
        _patch_async_client(monkeypatch, _make_transport(lambda r: httpx.Response(200, json=rows)))
        result = asyncio.run(database.get_learning_insights(user_token="t", user_id="u"))
        assert result == []

    def test_malformed_rows_are_skipped_not_fatal(self, monkeypatch):
        rows = [
            "not a dict",
            {"subject": None},  # missing everything
            _row("e1", "Physics", "confusion", "known", "2026-08-01T10:00:00+00:00"),
            _row("e2", "Physics", "confusion", "known", "2026-08-02T10:00:00+00:00"),
        ]
        _patch_async_client(monkeypatch, _make_transport(lambda r: httpx.Response(200, json=rows)))
        result = asyncio.run(database.get_learning_insights(user_token="t", user_id="u"))
        assert len(result) >= 1

    def test_topic_level_vs_subject_level_rows_translate_correctly(self, monkeypatch):
        rows = [
            _row("e1", "Physics", "confusion", "known", "2026-08-01T10:00:00+00:00",
                 topic="Force & Motion", topic_key="force & motion"),
            _row("e2", "Physics", "confusion", "known", "2026-08-02T10:00:00+00:00",
                 topic="Force & Motion", topic_key="force & motion"),
        ]
        _patch_async_client(monkeypatch, _make_transport(lambda r: httpx.Response(200, json=rows)))
        result = asyncio.run(database.get_learning_insights(user_token="t", user_id="u"))
        assert any(r["topic_key"] == "force & motion" for r in result)
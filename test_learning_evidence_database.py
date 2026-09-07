"""
Unit tests for backend/database.py's Phase 5B/5C/5D additions:
save_chat_learning_evidence, save_conversation_index_entry,
get_learning_history.

Run with: GEMINI_API_KEY=dummy python3 -m pytest test_learning_evidence_database.py -v

Same httpx.MockTransport convention as test_quiz_database.py - no real
network calls, backend/database.py's own httpx.AsyncClient is intercepted.
"""
import asyncio
import json as jsonlib
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


# ---------------------------------------------------------------------------
# save_chat_learning_evidence
# ---------------------------------------------------------------------------

class TestSaveChatLearningEvidenceConfigAndInput:
    def test_not_configured_raises(self, monkeypatch):
        monkeypatch.setattr(database, "SUPABASE_URL", "")
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.save_chat_learning_evidence(
                user_token="t", user_id="u", subject="Physics", topic="Force",
                signal_type="confusion", signal_strength="weak", attribution_confidence="known",
            ))

    def test_invalid_signal_type_raises_without_network_call(self, monkeypatch):
        called = []

        def handler(request):
            called.append(request)
            return httpx.Response(201, json=[{"id": "e1"}])

        _patch_async_client(monkeypatch, _make_transport(handler))
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.save_chat_learning_evidence(
                user_token="t", user_id="u", subject="Physics", topic="Force",
                signal_type="not_a_real_signal", signal_strength="weak", attribution_confidence="known",
            ))
        assert called == []

    def test_invalid_signal_strength_raises_without_network_call(self, monkeypatch):
        called = []

        def handler(request):
            called.append(request)
            return httpx.Response(201, json=[{"id": "e1"}])

        _patch_async_client(monkeypatch, _make_transport(handler))
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.save_chat_learning_evidence(
                user_token="t", user_id="u", subject="Physics", topic="Force",
                signal_type="confusion", signal_strength="extremely_strong", attribution_confidence="known",
            ))
        assert called == []

    def test_missing_subject_raises(self):
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.save_chat_learning_evidence(
                user_token="t", user_id="u", subject="", topic="Force",
                signal_type="confusion", signal_strength="weak", attribution_confidence="known",
            ))

    def test_missing_topic_raises(self):
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.save_chat_learning_evidence(
                user_token="t", user_id="u", subject="Physics", topic="   ",
                signal_type="confusion", signal_strength="weak", attribution_confidence="known",
            ))
        # NOTE: this asserts that a BLANK topic ("   ") still raises -
        # save_chat_learning_evidence rejects a whitespace-only topic the
        # same way it always has. A genuinely OMITTED topic (topic=None
        # or the parameter left out entirely) is different and must
        # succeed - see TestSaveChatLearningEvidenceSubjectOnly below for
        # the Phase 5C activation behavior.


class TestSaveChatLearningEvidenceInsert:
    def test_successful_insert_shape(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/learning_evidence")
            assert request.method == "POST"
            body = jsonlib.loads(request.content)
            assert body["user_id"] == "user-1"
            assert body["source"] == "chat"
            assert body["subject"] == "Physics"
            assert body["topic"] == "Force & Motion"
            # topic_key must be server-computed via normalize_topic_key,
            # never trusted from a caller (there is no such parameter at all).
            assert body["topic_key"] == "force & motion"
            assert body["attribution_confidence"] == "known"
            assert body["signal_type"] == "confusion"
            assert body["signal_strength"] == "weak"
            assert body["chat_id"] == "chat_123"
            return httpx.Response(201, json=[{"id": "evidence-1", **body}])

        _patch_async_client(monkeypatch, _make_transport(handler))
        asyncio.run(database.save_chat_learning_evidence(
            user_token="student-token", user_id="user-1",
            subject="Physics", topic="Force & Motion",
            signal_type="confusion", signal_strength="weak", attribution_confidence="known",
            chat_id="chat_123",
        ))

    def test_forwards_students_own_token_never_service_role(self, monkeypatch):
        seen = []

        def handler(request):
            seen.append(request.headers.get("authorization"))
            return httpx.Response(201, json=[{"id": "e1"}])

        _patch_async_client(monkeypatch, _make_transport(handler))
        asyncio.run(database.save_chat_learning_evidence(
            user_token="THIS_STUDENTS_OWN_TOKEN", user_id="u",
            subject="Physics", topic="Force", signal_type="confusion", signal_strength="weak",
            attribution_confidence="known",
        ))
        assert seen == ["Bearer THIS_STUDENTS_OWN_TOKEN"]

    def test_non_2xx_response_raises(self, monkeypatch):
        _patch_async_client(monkeypatch, _make_transport(lambda r: httpx.Response(400)))
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.save_chat_learning_evidence(
                user_token="t", user_id="u", subject="Physics", topic="Force",
                signal_type="confusion", signal_strength="weak", attribution_confidence="known",
            ))

    def test_network_error_raises_database_error(self, monkeypatch):
        def handler(request):
            raise httpx.ConnectError("boom", request=request)

        _patch_async_client(monkeypatch, _make_transport(handler))
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.save_chat_learning_evidence(
                user_token="t", user_id="u", subject="Physics", topic="Force",
                signal_type="confusion", signal_strength="weak", attribution_confidence="known",
            ))


class TestSaveChatLearningEvidenceSubjectOnly:
    """PHASE 5C ACTIVATION: subject-only evidence (topic omitted/None) is
    now a legitimate, common outcome - see
    backend.learning_memory.resolve_academic_context."""

    def test_omitted_topic_succeeds_with_null_topic_and_topic_key(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            body = jsonlib.loads(request.content)
            assert body["subject"] == "Physics"
            assert body["topic"] is None
            assert body["topic_key"] is None
            return httpx.Response(201, json=[{"id": "e1", **body}])

        _patch_async_client(monkeypatch, _make_transport(handler))
        asyncio.run(database.save_chat_learning_evidence(
            user_token="t", user_id="u", subject="Physics",
            signal_type="confusion", signal_strength="weak", attribution_confidence="known",
        ))

    def test_explicit_none_topic_succeeds(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            body = jsonlib.loads(request.content)
            assert body["topic"] is None
            return httpx.Response(201, json=[{"id": "e1", **body}])

        _patch_async_client(monkeypatch, _make_transport(handler))
        asyncio.run(database.save_chat_learning_evidence(
            user_token="t", user_id="u", subject="Physics", topic=None,
            signal_type="confusion", signal_strength="weak", attribution_confidence="known",
        ))

    def test_subject_alone_still_requires_a_real_subject(self):
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.save_chat_learning_evidence(
                user_token="t", user_id="u", subject="   ",
                signal_type="confusion", signal_strength="weak", attribution_confidence="known",
            ))


class TestSaveChatLearningEvidenceAttributionConfidencePropagation:
    """
    Regression tests for the attribution-confidence propagation bugfix.

    ROOT CAUSE (see backend/database.py:save_chat_learning_evidence and
    backend/main.py:_background_persist_chat_evidence): the function used
    to hardcode "attribution_confidence": "known" in its insert payload
    and had no parameter for it at all, so a genuine "probable"
    resolution from resolve_academic_context() was silently written as
    "known". The fix adds a required `attribution_confidence` parameter
    that flows unchanged from the resolver, through backend/main.py's
    chat_endpoint and _background_persist_chat_evidence, into this
    function's insert payload - and rejects "unknown" outright, since
    "unknown" must never produce a learning_evidence row at all.
    """

    def test_known_context_writes_known_row(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            body = jsonlib.loads(request.content)
            assert body["attribution_confidence"] == "known"
            return httpx.Response(201, json=[{"id": "e1", **body}])

        _patch_async_client(monkeypatch, _make_transport(handler))
        asyncio.run(database.save_chat_learning_evidence(
            user_token="t", user_id="u", subject="Physics",
            signal_type="confusion", signal_strength="weak", attribution_confidence="known",
        ))

    def test_probable_context_writes_probable_row_not_known(self, monkeypatch):
        """THE bug this fix addresses: a probable resolution must be
        persisted as "probable", never silently upgraded to "known"."""
        def handler(request: httpx.Request) -> httpx.Response:
            body = jsonlib.loads(request.content)
            assert body["attribution_confidence"] == "probable"
            assert body["attribution_confidence"] != "known"
            return httpx.Response(201, json=[{"id": "e1", **body}])

        _patch_async_client(monkeypatch, _make_transport(handler))
        asyncio.run(database.save_chat_learning_evidence(
            user_token="t", user_id="u", subject="Physics",
            signal_type="confusion", signal_strength="weak", attribution_confidence="probable",
        ))

    def test_unknown_attribution_confidence_is_rejected_without_network_call(self, monkeypatch):
        """Unknown context must NEVER produce a learning_evidence row -
        enforced here at the persistence layer itself, independent of
        whatever gating the caller in backend/main.py also does."""
        called = []

        def handler(request):
            called.append(request)
            return httpx.Response(201, json=[{"id": "e1"}])

        _patch_async_client(monkeypatch, _make_transport(handler))
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.save_chat_learning_evidence(
                user_token="t", user_id="u", subject="Physics",
                signal_type="confusion", signal_strength="weak", attribution_confidence="unknown",
            ))
        assert called == []

    def test_arbitrary_invalid_attribution_confidence_is_rejected_without_network_call(self, monkeypatch):
        """Not just "unknown" - any value outside {known, probable} must
        be rejected before any network call is made, so a caller bug
        cannot silently write malformed evidence."""
        called = []

        def handler(request):
            called.append(request)
            return httpx.Response(201, json=[{"id": "e1"}])

        _patch_async_client(monkeypatch, _make_transport(handler))
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.save_chat_learning_evidence(
                user_token="t", user_id="u", subject="Physics",
                signal_type="confusion", signal_strength="weak", attribution_confidence="very_confident",
            ))
        assert called == []

    def test_attribution_confidence_has_no_default_value(self):
        """A caller MUST be explicit - there is no silent fallback to
        "known" (which is exactly how this bug happened the first time)."""
        import inspect
        params = inspect.signature(database.save_chat_learning_evidence).parameters
        assert params["attribution_confidence"].default is inspect.Parameter.empty


# ---------------------------------------------------------------------------
# save_conversation_index_entry
# ---------------------------------------------------------------------------

class TestSaveConversationIndexEntry:
    def test_missing_chat_id_raises(self):
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.save_conversation_index_entry(
                user_token="t", user_id="u", chat_id="", short_label="Force and Motion",
            ))

    def test_missing_short_label_raises(self):
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.save_conversation_index_entry(
                user_token="t", user_id="u", chat_id="chat_1", short_label="   ",
            ))

    def test_invalid_attribution_confidence_raises_without_network_call(self, monkeypatch):
        called = []

        def handler(request):
            called.append(request)
            return httpx.Response(201)

        _patch_async_client(monkeypatch, _make_transport(handler))
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.save_conversation_index_entry(
                user_token="t", user_id="u", chat_id="chat_1", short_label="Title",
                attribution_confidence="very_sure",
            ))
        assert called == []

    def test_unknown_attribution_is_valid_and_subject_is_null(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            body = jsonlib.loads(request.content)
            assert body["chat_id"] == "chat_1"
            assert body["short_label"] == "Newton's Laws Discussion"
            assert body["attribution_confidence"] == "unknown"
            assert body["subject"] is None
            return httpx.Response(201, json=[{"id": "ci-1", **body}])

        _patch_async_client(monkeypatch, _make_transport(handler))
        asyncio.run(database.save_conversation_index_entry(
            user_token="t", user_id="user-1", chat_id="chat_1",
            short_label="Newton's Laws Discussion", attribution_confidence="unknown",
        ))

    def test_known_attribution_with_subject(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            body = jsonlib.loads(request.content)
            assert body["subject"] == "Physics"
            assert body["attribution_confidence"] == "known"
            return httpx.Response(201, json=[{"id": "ci-2", **body}])

        _patch_async_client(monkeypatch, _make_transport(handler))
        asyncio.run(database.save_conversation_index_entry(
            user_token="t", user_id="user-1", chat_id="chat_1",
            short_label="Force discussion", subject="Physics", attribution_confidence="known",
        ))

    def test_non_2xx_raises(self, monkeypatch):
        _patch_async_client(monkeypatch, _make_transport(lambda r: httpx.Response(500)))
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.save_conversation_index_entry(
                user_token="t", user_id="u", chat_id="chat_1", short_label="Title",
            ))


# ---------------------------------------------------------------------------
# get_learning_history
# ---------------------------------------------------------------------------

class TestGetLearningHistory:
    def test_not_configured_raises(self, monkeypatch):
        monkeypatch.setattr(database, "SUPABASE_URL", "")
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.get_learning_history(
                user_token="t", user_id="u",
                since_iso="2026-08-01T00:00:00+00:00", until_iso="2026-09-01T00:00:00+00:00",
            ))

    def test_combines_conversations_and_evidence_notes(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/conversation_index"):
                return httpx.Response(200, json=[
                    {"chat_id": "chat_1", "subject": None, "attribution_confidence": "unknown",
                     "short_label": "Force and Motion chat", "occurred_at": "2026-08-15T10:00:00+00:00"},
                ])
            if request.url.path.endswith("/learning_evidence"):
                return httpx.Response(200, json=[
                    {"subject": "Physics", "topic": "Force & Motion", "topic_key": "force & motion",
                     "signal_type": "confusion", "signal_strength": "weak",
                     "occurred_at": "2026-08-15T10:05:00+00:00"},
                ])
            raise AssertionError(f"unexpected request {request.url}")

        _patch_async_client(monkeypatch, _make_transport(handler))
        result = asyncio.run(database.get_learning_history(
            user_token="t", user_id="user-1",
            since_iso="2026-08-01T00:00:00+00:00", until_iso="2026-09-01T00:00:00+00:00",
        ))
        assert len(result["conversations"]) == 1
        assert result["conversations"][0]["chat_id"] == "chat_1"
        assert len(result["evidence_notes"]) == 1
        assert result["evidence_notes"][0]["topic_key"] == "force & motion"

    def test_empty_range_returns_empty_lists_not_error(self, monkeypatch):
        _patch_async_client(monkeypatch, _make_transport(lambda r: httpx.Response(200, json=[])))
        result = asyncio.run(database.get_learning_history(
            user_token="t", user_id="user-1",
            since_iso="2026-08-01T00:00:00+00:00", until_iso="2026-09-01T00:00:00+00:00",
        ))
        assert result == {"conversations": [], "evidence_notes": []}

    def test_limit_is_bounded_to_maximum(self, monkeypatch):
        seen_limits = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_limits.append(request.url.params.get("limit"))
            return httpx.Response(200, json=[])

        _patch_async_client(monkeypatch, _make_transport(handler))
        asyncio.run(database.get_learning_history(
            user_token="t", user_id="user-1",
            since_iso="2026-08-01T00:00:00+00:00", until_iso="2026-09-01T00:00:00+00:00",
            limit=999999,
        ))
        assert all(int(lim) <= database.MAX_LEARNING_HISTORY_LIMIT for lim in seen_limits)

    def test_forwards_students_own_token(self, monkeypatch):
        seen = []

        def handler(request):
            seen.append(request.headers.get("authorization"))
            return httpx.Response(200, json=[])

        _patch_async_client(monkeypatch, _make_transport(handler))
        asyncio.run(database.get_learning_history(
            user_token="THIS_STUDENTS_OWN_TOKEN", user_id="u",
            since_iso="2026-08-01T00:00:00+00:00", until_iso="2026-09-01T00:00:00+00:00",
        ))
        assert len(seen) == 2  # one call each to conversation_index and learning_evidence
        assert all(h == "Bearer THIS_STUDENTS_OWN_TOKEN" for h in seen)

    def test_non_200_from_conversation_index_raises(self, monkeypatch):
        def handler(request):
            if request.url.path.endswith("/conversation_index"):
                return httpx.Response(500)
            return httpx.Response(200, json=[])

        _patch_async_client(monkeypatch, _make_transport(handler))
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.get_learning_history(
                user_token="t", user_id="u",
                since_iso="2026-08-01T00:00:00+00:00", until_iso="2026-09-01T00:00:00+00:00",
            ))

    def test_non_200_from_learning_evidence_raises(self, monkeypatch):
        def handler(request):
            if request.url.path.endswith("/learning_evidence"):
                return httpx.Response(500)
            return httpx.Response(200, json=[])

        _patch_async_client(monkeypatch, _make_transport(handler))
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.get_learning_history(
                user_token="t", user_id="u",
                since_iso="2026-08-01T00:00:00+00:00", until_iso="2026-09-01T00:00:00+00:00",
            ))

    def test_network_error_raises(self, monkeypatch):
        def handler(request):
            raise httpx.ConnectError("boom", request=request)

        _patch_async_client(monkeypatch, _make_transport(handler))
        with pytest.raises(database.DatabaseError):
            asyncio.run(database.get_learning_history(
                user_token="t", user_id="u",
                since_iso="2026-08-01T00:00:00+00:00", until_iso="2026-09-01T00:00:00+00:00",
            ))
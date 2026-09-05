"""
Tests for the Phase 4 per-user rate limiter in backend/main.py.

Run with: GEMINI_API_KEY=dummy python3 -m pytest test_rate_limiting.py -v

Covers /api/chat, /api/chat/title, and /api/quiz/generate - the three
Gemini-consuming endpoints wired to _rate_limited_chat /
_rate_limited_chat_title / _rate_limited_quiz_generation respectively.
/api/quiz/submit, /api/pdf/upload, and /api/pdf/clear are intentionally
NOT covered here because Phase 4 deliberately does not rate-limit them
(they never call Gemini) - see the Phase 4 inspection report.

Auth is stubbed via FastAPI's dependency_overrides on get_current_user_id
/ get_current_user_and_token, exactly like test_quiz_submit_endpoint.py,
so these tests exercise the real rate-limit dependency chain
(_rate_limited_chat -> Depends(get_current_user_id) -> _check_rate_limit)
without making a real Supabase Auth API call. The Gemini-calling
functions (generate_ai_response, generate_quiz_questions,
generate_chat_title) are mocked at the backend.main import site, the
same convention test_quiz_submit_endpoint.py already uses for
generate_quiz_questions.

Each test uses its own dedicated, namespaced user_id (e.g.
"rl-chat-user-1") rather than reusing ids like "user-1" from other test
files, specifically so this file's deliberate limit-exhausting tests can
never collide with unrelated tests elsewhere in the same pytest session -
_rate_limit_buckets is a module-level dict shared for the whole process,
regardless of test file or run order.
"""
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

import pytest
from fastapi.testclient import TestClient

from backend import main as main_module


@pytest.fixture
def client():
    return TestClient(main_module.app)


@pytest.fixture(autouse=True)
def clean_rate_limit_state():
    """
    Every test starts with a completely empty rate-limit table and a
    fresh cleanup-interval counter, and leaves dependency_overrides clean
    for whichever test runs next - mirrors the clean_state fixture in
    test_quiz_submit_endpoint.py, applied to the new rate-limit state
    instead of active_quiz_definitions.
    """
    main_module._rate_limit_buckets.clear()
    main_module._rate_limit_check_count = 0
    yield
    main_module._rate_limit_buckets.clear()
    main_module._rate_limit_check_count = 0
    main_module.app.dependency_overrides.clear()


def _override_auth_as(user_id, token="test-token"):
    async def _fake_user_and_token():
        return (user_id, token)

    async def _fake_user_id():
        return user_id

    main_module.app.dependency_overrides[main_module.get_current_user_and_token] = _fake_user_and_token
    main_module.app.dependency_overrides[main_module.get_current_user_id] = _fake_user_id


def _post_chat(client, prompt="hello"):
    return client.post("/api/chat", data={"prompt": prompt, "mode": "direct"})


def _post_chat_title(client, history='[{"role": "user", "text": "hello"}]'):
    return client.post("/api/chat/title", data={"history": history, "board": "NCTB"})


def _post_quiz_generate(client, topic="Algebra"):
    return client.post("/api/quiz/generate", data={
        "board": "NCTB", "user_class": "SSC", "subject": "Math",
        "topic": topic, "count": 2,
    })


SAMPLE_QUESTIONS = [
    {"question": "2+2?", "options": ["3", "4"], "correct_index": 1, "explanation": "math"},
]


# ---------------------------------------------------------------------------
# CHAT category: 20 requests / 60 seconds
# ---------------------------------------------------------------------------

class TestChatRateLimit:
    def test_requests_below_limit_are_allowed(self, client, monkeypatch):
        _override_auth_as("rl-chat-user-1")
        monkeypatch.setattr(main_module, "generate_ai_response", lambda **kwargs: "ok")

        for _ in range(15):
            resp = _post_chat(client)
            assert resp.status_code == 200
            assert resp.json()["reply"] == "ok"

    def test_boundary_at_exact_limit_all_succeed(self, client, monkeypatch):
        _override_auth_as("rl-chat-user-2")
        monkeypatch.setattr(main_module, "generate_ai_response", lambda **kwargs: "ok")

        for i in range(20):
            resp = _post_chat(client)
            assert resp.status_code == 200, f"request {i + 1}/20 should be allowed"

    def test_request_exceeding_limit_returns_429(self, client, monkeypatch):
        _override_auth_as("rl-chat-user-3")
        calls = {"count": 0}

        def fake_generate(**kwargs):
            calls["count"] += 1
            return "ok"

        monkeypatch.setattr(main_module, "generate_ai_response", fake_generate)

        for _ in range(20):
            assert _post_chat(client).status_code == 200

        resp = _post_chat(client)
        assert resp.status_code == 429
        assert resp.json() == {"detail": "Too many requests. Please wait a moment and try again."}
        # The rejected 21st request must never reach Gemini.
        assert calls["count"] == 20

    def test_different_users_have_independent_limits(self, client, monkeypatch):
        monkeypatch.setattr(main_module, "generate_ai_response", lambda **kwargs: "ok")

        _override_auth_as("rl-chat-user-a")
        for _ in range(20):
            assert _post_chat(client).status_code == 200
        assert _post_chat(client).status_code == 429  # user A is now capped

        # User B has made zero requests and must not be affected by user
        # A's usage in any way.
        _override_auth_as("rl-chat-user-b")
        assert _post_chat(client).status_code == 200

    def test_retry_after_header_present_and_reasonable(self, client, monkeypatch):
        _override_auth_as("rl-chat-user-4")
        monkeypatch.setattr(main_module, "generate_ai_response", lambda **kwargs: "ok")

        for _ in range(20):
            assert _post_chat(client).status_code == 200

        resp = _post_chat(client)
        assert resp.status_code == 429
        retry_after = resp.headers.get("retry-after")
        assert retry_after is not None
        retry_after_seconds = int(retry_after)
        assert 0 < retry_after_seconds <= 60

    def test_expired_timestamps_free_up_a_slot(self, client, monkeypatch):
        """
        After the window has fully elapsed, previously-counted requests
        must stop counting - simulated here by rewinding the bucket's
        timestamps rather than sleeping 60 real seconds in a test.
        """
        _override_auth_as("rl-chat-user-5")
        monkeypatch.setattr(main_module, "generate_ai_response", lambda **kwargs: "ok")

        for _ in range(20):
            assert _post_chat(client).status_code == 200
        assert _post_chat(client).status_code == 429  # confirm capped

        bucket = main_module._rate_limit_buckets[("rl-chat-user-5", "chat")]
        expired = time.time() - 61  # just past the 60s chat window
        for i in range(len(bucket)):
            bucket[i] = expired

        resp = _post_chat(client)
        assert resp.status_code == 200

    def test_missing_auth_returns_401_and_does_not_call_gemini(self, client, monkeypatch):
        calls = {"count": 0}
        monkeypatch.setattr(
            main_module, "generate_ai_response",
            lambda **kwargs: calls.__setitem__("count", calls["count"] + 1) or "ok",
        )

        resp = _post_chat(client)
        assert resp.status_code == 401
        assert calls["count"] == 0
        # An unauthenticated request must never create a rate-limit bucket
        # keyed by anything client-supplied - there is no verified user_id
        # to key it by, so nothing is recorded at all.
        assert main_module._rate_limit_buckets == {}


# ---------------------------------------------------------------------------
# CHAT_TITLE category: 10 requests / 60 seconds - independent from CHAT
# ---------------------------------------------------------------------------

class TestChatTitleRateLimit:
    def test_requests_below_limit_are_allowed(self, client, monkeypatch):
        _override_auth_as("rl-title-user-1")
        monkeypatch.setattr(main_module, "generate_chat_title", lambda **kwargs: "A Title")

        for _ in range(8):
            resp = _post_chat_title(client)
            assert resp.status_code == 200

    def test_request_exceeding_limit_returns_429_and_skips_gemini(self, client, monkeypatch):
        _override_auth_as("rl-title-user-2")
        calls = {"count": 0}

        def fake_title(**kwargs):
            calls["count"] += 1
            return "A Title"

        monkeypatch.setattr(main_module, "generate_chat_title", fake_title)

        for _ in range(10):
            assert _post_chat_title(client).status_code == 200

        resp = _post_chat_title(client)
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers
        assert calls["count"] == 10

    def test_chat_title_bucket_is_independent_of_chat_bucket(self, client, monkeypatch):
        """
        The same user exhausting their CHAT_TITLE quota must not affect
        their separate CHAT quota, and vice versa - they are different
        categories (different dict keys), not a shared per-user budget.
        """
        user = "rl-title-user-3"
        _override_auth_as(user)
        monkeypatch.setattr(main_module, "generate_chat_title", lambda **kwargs: "A Title")
        monkeypatch.setattr(main_module, "generate_ai_response", lambda **kwargs: "ok")

        for _ in range(10):
            assert _post_chat_title(client).status_code == 200
        assert _post_chat_title(client).status_code == 429  # chat_title capped

        # The chat category for this exact same user must still be fully
        # available.
        assert _post_chat(client).status_code == 200


# ---------------------------------------------------------------------------
# QUIZ_GENERATION category: 5 requests / 60 seconds
# ---------------------------------------------------------------------------

class TestQuizGenerationRateLimit:
    def test_requests_below_limit_are_allowed(self, client, monkeypatch):
        _override_auth_as("rl-quiz-user-1")
        monkeypatch.setattr(main_module, "generate_quiz_questions", lambda **kwargs: SAMPLE_QUESTIONS)

        for _ in range(4):
            resp = _post_quiz_generate(client)
            assert resp.status_code == 200
            assert resp.json()["questions"] == SAMPLE_QUESTIONS

    def test_boundary_at_exact_limit_all_succeed(self, client, monkeypatch):
        _override_auth_as("rl-quiz-user-2")
        monkeypatch.setattr(main_module, "generate_quiz_questions", lambda **kwargs: SAMPLE_QUESTIONS)

        for i in range(5):
            assert _post_quiz_generate(client).status_code == 200, f"request {i + 1}/5 should be allowed"

    def test_request_exceeding_limit_returns_429_and_skips_gemini(self, client, monkeypatch):
        _override_auth_as("rl-quiz-user-3")
        calls = {"count": 0}

        def fake_generate(**kwargs):
            calls["count"] += 1
            return SAMPLE_QUESTIONS

        monkeypatch.setattr(main_module, "generate_quiz_questions", fake_generate)

        for _ in range(5):
            assert _post_quiz_generate(client).status_code == 200

        resp = _post_quiz_generate(client)
        assert resp.status_code == 429
        assert resp.json() == {"detail": "Too many requests. Please wait a moment and try again."}
        assert calls["count"] == 5
        # The rejected 6th request must not have stored a 6th quiz
        # definition for this user - exactly the 5 successful generations
        # should be present, nothing extra from the 429'd attempt.
        this_users_definitions = [
            d for d in main_module.active_quiz_definitions.values()
            if d["user_id"] == "rl-quiz-user-3"
        ]
        assert len(this_users_definitions) == 5

    def test_different_users_have_independent_limits(self, client, monkeypatch):
        monkeypatch.setattr(main_module, "generate_quiz_questions", lambda **kwargs: SAMPLE_QUESTIONS)

        _override_auth_as("rl-quiz-user-a")
        for _ in range(5):
            assert _post_quiz_generate(client).status_code == 200
        assert _post_quiz_generate(client).status_code == 429

        _override_auth_as("rl-quiz-user-b")
        assert _post_quiz_generate(client).status_code == 200

    def test_retry_after_header_present_and_reasonable(self, client, monkeypatch):
        _override_auth_as("rl-quiz-user-4")
        monkeypatch.setattr(main_module, "generate_quiz_questions", lambda **kwargs: SAMPLE_QUESTIONS)

        for _ in range(5):
            assert _post_quiz_generate(client).status_code == 200

        resp = _post_quiz_generate(client)
        assert resp.status_code == 429
        retry_after = int(resp.headers["Retry-After"])
        assert 0 < retry_after <= 60


# ---------------------------------------------------------------------------
# Cleanup / memory behavior
# ---------------------------------------------------------------------------

class TestRateLimitCleanup:
    def test_idle_bucket_is_removed_on_periodic_sweep(self, client, monkeypatch):
        """
        A bucket left with only expired timestamps must eventually be
        deleted from _rate_limit_buckets entirely - not just have its
        timestamps pruned - so memory does not grow forever as distinct
        users come and go. The periodic sweep only runs every
        RATE_LIMIT_CLEANUP_INTERVAL checks (not every single request), so
        this test drives the counter to that boundary directly rather
        than making hundreds of real requests.
        """
        stale_key = ("rl-cleanup-ghost-user", "chat")
        main_module._rate_limit_buckets[stale_key] = main_module.deque([time.time() - 1000])
        # Sanity: our helper deque import matches main_module's.
        assert stale_key in main_module._rate_limit_buckets

        main_module._rate_limit_check_count = main_module.RATE_LIMIT_CLEANUP_INTERVAL - 1

        _override_auth_as("rl-cleanup-trigger-user")
        monkeypatch.setattr(main_module, "generate_ai_response", lambda **kwargs: "ok")
        resp = _post_chat(client)  # this request's check pushes the counter to the interval boundary
        assert resp.status_code == 200

        assert stale_key not in main_module._rate_limit_buckets

    def test_active_bucket_survives_the_sweep(self, client, monkeypatch):
        """The sweep must only remove buckets that are actually empty
        after pruning - an active user's in-window timestamps must
        survive it."""
        _override_auth_as("rl-cleanup-active-user")
        monkeypatch.setattr(main_module, "generate_ai_response", lambda **kwargs: "ok")

        assert _post_chat(client).status_code == 200
        key = ("rl-cleanup-active-user", "chat")
        assert key in main_module._rate_limit_buckets

        main_module._rate_limit_check_count = main_module.RATE_LIMIT_CLEANUP_INTERVAL - 1
        assert _post_chat(client).status_code == 200  # drives the sweep

        assert key in main_module._rate_limit_buckets
        assert len(main_module._rate_limit_buckets[key]) == 2


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

class TestRateLimitConcurrency:
    def test_concurrent_requests_cannot_exceed_the_limit(self, monkeypatch):
        """
        Fires 40 concurrent /api/chat requests for one user against a
        20/60s limit using real OS threads (TestClient requests block,
        so real threads - not asyncio tasks - are needed to get genuine
        concurrency here). Exactly 20 must succeed; the rest must be
        rejected with 429. If the lock around the check-and-increment
        were missing or incorrect, more than 20 could slip through.
        """
        _override_auth_as("rl-concurrency-user")
        monkeypatch.setattr(main_module, "generate_ai_response", lambda **kwargs: "ok")

        # Each thread needs its own TestClient instance talking to the
        # same shared app/module state, matching how multiple real
        # concurrent HTTP connections would all hit the same in-process
        # _rate_limit_buckets dict.
        def make_request(_):
            local_client = TestClient(main_module.app)
            return _post_chat(local_client).status_code

        with ThreadPoolExecutor(max_workers=40) as pool:
            results = list(pool.map(make_request, range(40)))

        assert results.count(200) == 20
        assert results.count(429) == 20


# ---------------------------------------------------------------------------
# Coverage / non-goals: confirms the endpoints Phase 4 explicitly does NOT
# rate-limit are unaffected, and that exhausting one category doesn't leak
# into an unrelated, non-rate-limited endpoint.
# ---------------------------------------------------------------------------

class TestOutOfScopeEndpointsUnaffected:
    def test_quiz_submit_is_not_rate_limited_even_after_quiz_generation_is_capped(self, client, monkeypatch):
        user = "rl-submit-user-1"
        _override_auth_as(user)
        monkeypatch.setattr(main_module, "generate_quiz_questions", lambda **kwargs: SAMPLE_QUESTIONS)

        for _ in range(5):
            assert _post_quiz_generate(client).status_code == 200
        assert _post_quiz_generate(client).status_code == 429  # quiz_generation now capped for this user

        # /api/quiz/submit must be completely unaffected - it has no
        # rate-limit dependency at all. Using a bogus quiz_id here on
        # purpose: we only care that the response is the normal "not
        # found" 404 (proving the request reached the endpoint's own
        # logic), never a 429.
        resp = client.post("/api/quiz/submit", data={"quiz_id": "does-not-exist", "answers": "[]"})
        assert resp.status_code == 404

    def test_existing_chat_endpoint_auth_requirement_is_unchanged(self, client):
        """Regression: swapping Depends(get_current_user_id) for
        Depends(_rate_limited_chat) must not change the 401-when-
        unauthenticated contract already covered in
        test_quiz_submit_endpoint.py."""
        resp = _post_chat(client)
        assert resp.status_code == 401

    def test_existing_quiz_generate_auth_requirement_is_unchanged(self, client):
        resp = _post_quiz_generate(client)
        assert resp.status_code == 401
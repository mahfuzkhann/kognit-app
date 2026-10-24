"""
Shared pytest configuration for the whole Kognit test suite.

P0 STABILIZATION: the streaming retry path now waits a jittered backoff
between attempts (backend.ai_engine._backoff_before_retry). Production
should really wait; tests must never. This autouse fixture replaces the
single sleep indirection with a no-op for every test, so existing tests that
simulate provider failures stay fast without each one having to patch
anything.

Tests that need to OBSERVE the waits (test_provider_resilience.py) simply
monkeypatch `backend.ai_engine._sleep` themselves with a recorder - a
test-level monkeypatch runs after this fixture and takes precedence.

PRE-PHASE 10: two more production behaviours must be inert by default in
tests, for the same "no surprises, no network" reason:

  * QUOTA_MODE=off      - plan quotas call Supabase RPCs. Tests that exercise
                          quotas (test_quota_enforcement.py) set QUOTA_MODE=
                          enforce themselves and stub the single RPC function.
  * usage persistence   - the post-response usage writer would POST to the
                          configured SUPABASE_URL. It is replaced with a
                          recorder; tests that check persistence read
                          `usage_writes`.
"""
import pytest


@pytest.fixture(autouse=True)
def _no_real_backoff_sleep(monkeypatch):
    try:
        import backend.ai_engine as ai_engine
    except Exception:
        # Modules that cannot import (e.g. missing env in an unrelated
        # tooling run) are not this fixture's problem.
        yield
        return
    monkeypatch.setattr(ai_engine, "_sleep", lambda seconds: None)
    yield


@pytest.fixture(autouse=True)
def usage_writes(monkeypatch):
    """Keeps quotas off and captures (never sends) usage rows for every test.
    Returns the list the fake writer appends to."""
    monkeypatch.setenv("QUOTA_MODE", "off")
    writes = []
    try:
        from backend import usage_telemetry
    except Exception:
        yield writes
        return

    async def _fake_save(user_token, user_id, rows):
        writes.append({"user_id": user_id, "rows": [dict(r) for r in rows]})

    monkeypatch.setattr(usage_telemetry, "save_ai_usage_events", _fake_save)
    yield writes
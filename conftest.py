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
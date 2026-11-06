"""
Pooled outbound HTTP clients (AI Engine 2.0, CEO decision D-9).

Before this module every Supabase call (token check, quota RPC, profile read,
REST write...) built a brand-new httpx.AsyncClient - a fresh TCP + TLS handshake
each time, several times per chat request, all before the first token. Clients
are now created once per (event loop, timeout), reused with keep-alive, bounded
by explicit connection limits, and closed at application shutdown.

Design constraints, each deliberate:
  * One client per running event loop. An httpx.AsyncClient is bound to the loop
    that first used it; sharing one across loops (the test-suite runs many)
    raises "event loop is closed". Keyed by loop identity, with the loop held
    weakly so a dead loop's entry is dropped.
  * If a test (or any caller) has replaced httpx.AsyncClient with a double, that
    double is used as before - the existing database/quota tests keep working
    unchanged and still exercise the real call sites.
  * The returned context manager NEVER closes the shared client.
  * Timeouts are exactly what each call site used before (same constants), so
    request behaviour is unchanged; only connection reuse is new.
  * No cookies/auth are stored on the client: every request supplies its own
    headers, so one student's token can never leak into another's request.
"""
import asyncio
import logging
import weakref
from typing import Optional

import httpx

logger = logging.getLogger("kognit.http")

_REAL_ASYNC_CLIENT = httpx.AsyncClient

LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=40, keepalive_expiry=30.0)

# (id(loop), timeout) -> (weakref(loop), client)
_clients: dict = {}


class _Borrowed:
    """async context manager that lends the shared client and never closes it."""

    def __init__(self, client):
        self._client = client

    async def __aenter__(self):
        return self._client

    async def __aexit__(self, exc_type, exc, tb):
        return False


def client(*, timeout: float):
    """Drop-in for `httpx.AsyncClient(timeout=...)` used as `async with`."""
    if httpx.AsyncClient is not _REAL_ASYNC_CLIENT:
        return httpx.AsyncClient(timeout=timeout)       # a test double is installed
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return httpx.AsyncClient(timeout=timeout)       # no loop: behave exactly as before
    key = (id(loop), float(timeout))
    entry = _clients.get(key)
    if entry is not None:
        ref, existing = entry
        if ref() is loop and not existing.is_closed:
            return _Borrowed(existing)
    created = _REAL_ASYNC_CLIENT(timeout=timeout, limits=LIMITS)
    _clients[key] = (weakref.ref(loop), created)
    _drop_dead_loops()
    return _Borrowed(created)


def _drop_dead_loops() -> None:
    for key in [k for k, (ref, _c) in _clients.items() if ref() is None]:
        _clients.pop(key, None)


def open_clients() -> int:
    _drop_dead_loops()
    return sum(1 for _ref, c in _clients.values() if not c.is_closed)


async def aclose_all() -> int:
    """Close every client that belongs to the CURRENT loop (application
    shutdown). Returns how many were closed. Safe to call twice."""
    try:
        loop: Optional[asyncio.AbstractEventLoop] = asyncio.get_running_loop()
    except RuntimeError:
        return 0
    closed = 0
    for key in [k for k, (ref, _c) in _clients.items() if ref() is loop]:
        _ref, c = _clients.pop(key)
        try:
            await c.aclose()
            closed += 1
        except Exception:  # noqa: BLE001 - shutdown must not raise
            logger.warning("closing a pooled http client failed", exc_info=True)
    return closed
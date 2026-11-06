"""
In-process model health tracking and circuit breaker.

SCOPE AND LIMITS (be honest about them): state lives in this process only.
Under multiple workers/instances each one keeps its own view, so a breaker
trips per worker, not globally. That is acceptable for a single-worker
deployment (the documented configuration) and degrades safely - a worker that
has not yet seen failures simply keeps trying. A shared store (e.g. Redis) is
only justified once more than one worker is actually run (see
docs/AI_ENGINE_2_0.md, "Known limitations").

Breaker: CLOSED -> OPEN after N consecutive transient failures; after the
cool-down ONE probe request is allowed (HALF_OPEN); success closes it,
failure re-opens it. Only transient provider failures count (unavailable,
timeout, transport, rate-limit). A bad request or auth error is a Kloriz
problem, not model health, and never trips the breaker.
"""
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

from backend.ai import config

STATE_CLOSED = "closed"
STATE_OPEN = "open"
STATE_HALF_OPEN = "half_open"

WINDOW_SECONDS = 300.0
MAX_EVENTS = 500


@dataclass(frozen=True)
class HealthSnapshot:
    model_id: str
    state: str
    consecutive_failures: int
    window_requests: int
    window_failures: int
    window_rate_limits: int
    window_timeouts: int
    window_interruptions: int
    error_rate: Optional[float]
    p50_latency_ms: Optional[int]
    p95_latency_ms: Optional[int]


class _ModelHealth:
    __slots__ = ("events", "consecutive", "state", "opened_at", "probe_in_flight")

    def __init__(self):
        self.events = deque(maxlen=MAX_EVENTS)   # (ts, ok, kind, latency_ms)
        self.consecutive = 0
        self.state = STATE_CLOSED
        self.opened_at = 0.0
        self.probe_in_flight = False


class HealthTracker:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._models: dict = {}

    def _get(self, model_id: str) -> _ModelHealth:
        h = self._models.get(model_id)
        if h is None:
            h = self._models[model_id] = _ModelHealth()
        return h

    # -- recording ---------------------------------------------------------
    def record_success(self, model_id: str, latency_seconds: Optional[float] = None) -> None:
        with self._lock:
            h = self._get(model_id)
            h.events.append((self._clock(), True, None, _ms(latency_seconds)))
            h.consecutive = 0
            h.state = STATE_CLOSED
            h.probe_in_flight = False

    def record_failure(self, model_id: str, kind: str, latency_seconds: Optional[float] = None) -> None:
        """kind: unavailable | timeout | rate_limited | interrupted | transport."""
        with self._lock:
            h = self._get(model_id)
            now = self._clock()
            h.events.append((now, False, kind, _ms(latency_seconds)))
            h.consecutive += 1
            if h.state == STATE_HALF_OPEN:
                h.state = STATE_OPEN
                h.opened_at = now
                h.probe_in_flight = False
            elif h.consecutive >= config.breaker_failure_threshold():
                h.state = STATE_OPEN
                h.opened_at = now

    # -- decisions ----------------------------------------------------------
    def allow(self, model_id: str) -> bool:
        """May a NEW request be sent to this model right now? In HALF_OPEN only
        a single probe is let through."""
        with self._lock:
            h = self._get(model_id)
            if h.state == STATE_CLOSED:
                return True
            now = self._clock()
            if h.state == STATE_OPEN:
                if now - h.opened_at >= config.breaker_cooldown_seconds():
                    h.state = STATE_HALF_OPEN
                    h.probe_in_flight = True
                    return True
                return False
            # HALF_OPEN
            if h.probe_in_flight:
                return False
            h.probe_in_flight = True
            return True

    def state(self, model_id: str) -> str:
        with self._lock:
            return self._get(model_id).state

    def snapshot(self, model_id: str) -> HealthSnapshot:
        with self._lock:
            h = self._get(model_id)
            cutoff = self._clock() - WINDOW_SECONDS
            window = [e for e in h.events if e[0] >= cutoff]
            fails = [e for e in window if not e[1]]
            lat = sorted(e[3] for e in window if e[1] and e[3] is not None)
            return HealthSnapshot(
                model_id=model_id, state=h.state, consecutive_failures=h.consecutive,
                window_requests=len(window), window_failures=len(fails),
                window_rate_limits=sum(1 for e in fails if e[2] == "rate_limited"),
                window_timeouts=sum(1 for e in fails if e[2] == "timeout"),
                window_interruptions=sum(1 for e in fails if e[2] == "interrupted"),
                error_rate=(len(fails) / len(window)) if window else None,
                p50_latency_ms=_pct(lat, 0.50), p95_latency_ms=_pct(lat, 0.95),
            )

    def reset(self) -> None:
        with self._lock:
            self._models.clear()


def _ms(seconds: Optional[float]) -> Optional[int]:
    return None if seconds is None else int(seconds * 1000)


def _pct(sorted_values: list, q: float) -> Optional[int]:
    if not sorted_values:
        return None
    idx = min(len(sorted_values) - 1, int(round(q * (len(sorted_values) - 1))))
    return sorted_values[idx]


# One tracker per process (see the module docstring for the multi-worker note).
tracker = HealthTracker()
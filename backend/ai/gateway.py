"""
Gateway policy: everything that decides WHETHER and WHEN the engine may call a
provider again, independent of any SDK.

  * RetryBudget       - process-wide token bucket; stops retry storms.
  * AttemptGovernor   - one per request: wall-clock deadlines, retry decisions,
                        in-provider model fallback, health recording.
  * FAILURE_POLICY    - the explicit failure-class -> action table (data, so it
                        can be tested row by row and read by a human).
  * AdmissionController - bounded in-flight generations + bounded queue.

The attempt loop that yields stream events stays in backend/ai_engine.py; it
asks the governor before every retry and reports every outcome to it.
"""
import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from backend.ai import config, registry
from backend.ai.health import tracker as default_tracker, HealthTracker

logger = logging.getLogger("kognit.ai.gateway")

# --- failure policy table -------------------------------------------------------
ACTION_FAIL = "fail"                 # surface a stable error, no retry
ACTION_RETRY = "retry"               # bounded, jittered, budgeted retry
ACTION_RECOVER = "recover_once"      # discard partial, ONE fresh generation
ACTION_INTERRUPT = "interrupt"       # after text: finalize as interrupted
ACTION_ALT_MODEL = "alternate_model_then_fail"   # rate limit: other approved model once, never same-model retry


@dataclass(frozen=True)
class FailureRule:
    kind: str
    pre_token: str
    mid_stream: str
    counts_for_health: bool
    note: str


FAILURE_POLICY = {
    "invalid_request": FailureRule("invalid_request", ACTION_FAIL, ACTION_INTERRUPT, False,
                                   "400/404/413: our request is wrong; retrying cannot help"),
    "auth": FailureRule("auth", ACTION_FAIL, ACTION_INTERRUPT, False,
                        "401/403: operator problem; never retried; must alert"),
    "rate_limited": FailureRule("rate_limited", ACTION_ALT_MODEL, ACTION_INTERRUPT, True,
                                "429: never retried on the same model; Retry-After respected only as a hint"),
    "timeout": FailureRule("timeout", ACTION_RETRY, ACTION_INTERRUPT, True,
                           "408/client timeout/connect: retried within the deadline and budget"),
    "unavailable": FailureRule("unavailable", ACTION_RETRY, ACTION_RECOVER, True,
                               "5xx: retried pre-token; ONE recovery generation after partial output"),
    "conflict": FailureRule("conflict", ACTION_RETRY, ACTION_INTERRUPT, True, "409 transient conflict"),
    "unknown": FailureRule("unknown", ACTION_RETRY, ACTION_INTERRUPT, True,
                           "unclassified SDK/transport error: treated as transient but budgeted"),
}


def rule_for(kind_value: str) -> FailureRule:
    """kind_value is ProviderErrorKind.value ('UNAVAILABLE'); matching is case-insensitive."""
    return FAILURE_POLICY.get(str(kind_value).lower(), FAILURE_POLICY["unknown"])


def health_kind(kind_value: str) -> Optional[str]:
    rule = rule_for(kind_value)
    if not rule.counts_for_health:
        return None
    key = str(kind_value).lower()
    return {"unknown": "transport"}.get(key, key)


# --- retry budget ----------------------------------------------------------------
class RetryBudget:
    """Token bucket. Every retry or recovery generation spends one token; when
    the bucket is empty retries are refused and the request fails fast instead
    of multiplying load on a struggling provider."""

    def __init__(self, burst: Optional[int] = None, refill_per_second: Optional[float] = None,
                 clock: Callable[[], float] = time.monotonic):
        self._burst = burst
        self._refill = refill_per_second
        self._clock = clock
        self._lock = threading.Lock()
        self._tokens: Optional[float] = None
        self._last = clock()

    def _params(self):
        burst = config.retry_budget_burst() if self._burst is None else self._burst
        refill = config.retry_budget_refill_per_second() if self._refill is None else self._refill
        return burst, refill

    def try_spend(self) -> bool:
        burst, refill = self._params()
        with self._lock:
            now = self._clock()
            if self._tokens is None:
                self._tokens = float(burst)
            self._tokens = min(float(burst), self._tokens + (now - self._last) * refill)
            self._last = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False

    def reset(self) -> None:
        with self._lock:
            self._tokens = None
            self._last = self._clock()


retry_budget = RetryBudget()


# --- per-request governor ---------------------------------------------------------
class AttemptGovernor:
    """Created once per request by the attempt loop."""

    def __init__(self, request_id: str, *, model_id: str, role: str, fallback_allowed: Optional[bool] = None,
                 first_token_deadline: Optional[float] = None, stream_deadline: Optional[float] = None,
                 budget: Optional[RetryBudget] = None, health: Optional[HealthTracker] = None,
                 clock: Callable[[], float] = time.monotonic):
        self.request_id = request_id
        self.model_id = model_id
        self.role = role
        self.enabled = config.gateway_enabled()
        self.fallback_allowed = (config.model_fallback_enabled() if fallback_allowed is None else fallback_allowed) and self.enabled
        self._clock = clock
        self._t0 = clock()
        self._first_token_deadline = config.first_token_deadline_seconds() if first_token_deadline is None else first_token_deadline
        self._stream_deadline = config.stream_deadline_seconds() if stream_deadline is None else stream_deadline
        self._budget = budget or retry_budget
        self._health = health or default_tracker
        self.first_token_seen = False
        self.retries_spent = 0
        self.budget_refusals = 0
        self.fell_back_from: Optional[str] = None
        self.attempt_kinds: list = []

    # -- time ------------------------------------------------------------------
    def elapsed(self) -> float:
        return self._clock() - self._t0

    def mark_first_token(self) -> None:
        self.first_token_seen = True

    def remaining(self) -> float:
        if not self.enabled:
            return 1e9
        limit = self._stream_deadline if self.first_token_seen else self._first_token_deadline
        return limit - self.elapsed()

    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def attempt_timeout(self, proposed: float) -> Optional[float]:
        """The per-attempt SDK timeout, capped by what is left of the overall
        deadline. None means there is no time left: do not call the provider."""
        if not self.enabled:
            return float(proposed)
        left = self.remaining()
        if left <= 0.5:
            return None
        return max(1.0, min(float(proposed), left))

    # -- retries ---------------------------------------------------------------
    def allow_retry(self, reason: str = "") -> bool:
        """One call per retry/recovery decision. False when the deadline cannot
        fit another attempt or the process-wide retry budget is empty."""
        if not self.enabled:
            self.retries_spent += 1
            return True
        if self.remaining() <= 2.0:
            logger.warning("request_id=%s retry refused: deadline reached (%s)", self.request_id, reason)
            return False
        if not self._budget.try_spend():
            self.budget_refusals += 1
            logger.warning("request_id=%s retry refused: retry budget exhausted (%s)", self.request_id, reason)
            return False
        self.retries_spent += 1
        return True

    # -- health + fallback -------------------------------------------------------
    def record_success(self, latency_seconds: Optional[float]) -> None:
        self._health.record_success(self.model_id, latency_seconds)

    def record_failure(self, kind_value: str, latency_seconds: Optional[float] = None, *, interrupted: bool = False) -> None:
        hk = "interrupted" if interrupted else health_kind(kind_value)
        if hk is not None:
            self._health.record_failure(self.model_id, hk, latency_seconds)

    def maybe_switch_model(self, kind_value: str, *, needs_inputs: tuple = ()) -> Optional[str]:
        """In-provider fallback: returns the other approved model id (and
        switches to it) when allowed, healthy and capable; else None. Never
        crosses providers, never switches twice in one request."""
        if not self.fallback_allowed or self.fell_back_from is not None:
            return None
        rule = rule_for(kind_value)
        if not rule.counts_for_health:
            return None
        other = registry.get_model(registry.other_role(self.role))
        if not all(registry.supports(other, inputs=needs_inputs).values()):
            return None
        if not self._health.allow(other.model_id):
            return None
        self.fell_back_from = self.model_id
        self.model_id, self.role = other.model_id, other.role
        return self.model_id

    def breaker_open_for_current(self) -> bool:
        return self._health.state(self.model_id) == "open"


# --- bounded admission --------------------------------------------------------------
class Overloaded(Exception):
    """Raised when the in-flight limit AND the bounded queue are both full or
    the queue wait elapsed. Mapped to a safe 503 by the endpoint."""


class AdmissionController:
    """Bounds simultaneously running generations and the number waiting.

    Created lazily per running event loop (asyncio primitives bind to a loop),
    which also keeps tests that spin up several loops independent."""

    def __init__(self):
        self._lock = threading.Lock()
        self._loop = None
        self._sem: Optional[asyncio.Semaphore] = None
        self._limit = 0
        self.inflight = 0
        self.queued = 0
        self.shed = 0

    def _ensure(self):
        loop = asyncio.get_running_loop()
        limit = config.max_inflight_generations()
        if self._sem is None or self._loop is not loop or self._limit != limit:
            self._loop, self._limit = loop, limit
            self._sem = asyncio.Semaphore(limit)
            self.inflight = 0
            self.queued = 0
        return self._sem

    async def acquire(self) -> None:
        sem = self._ensure()
        if sem.locked():
            if self.queued >= config.max_queued_generations():
                self.shed += 1
                raise Overloaded("queue full")
            self.queued += 1
            try:
                await asyncio.wait_for(sem.acquire(), timeout=config.queue_wait_seconds())
            except asyncio.TimeoutError:
                self.shed += 1
                raise Overloaded("queue wait elapsed")
            finally:
                self.queued -= 1
        else:
            await sem.acquire()
        self.inflight += 1

    def release(self) -> None:
        if self._sem is not None and self.inflight > 0:
            self.inflight -= 1
            self._sem.release()


admission = AdmissionController()
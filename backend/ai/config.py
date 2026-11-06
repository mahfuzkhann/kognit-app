"""
Single place for AI Engine 2.0 environment configuration.

Every switch has a safe default that reproduces the pre-2.0 behaviour, so
deploying this code changes nothing until a flag is turned on (rollback = set
the flag back). Values are read at call time, not import time, so tests and
operators can change them without a restart where the process allows it.
"""
import os

ROUTER_MODES = ("static", "shadow", "policy")
VERIFY_MODES = ("off", "shadow")
CONTEXT_BUILDER_MODES = ("legacy", "v2")


def _choice(name: str, allowed: tuple, default: str) -> str:
    value = (os.getenv(name) or default).strip().lower()
    return value if value in allowed else default


def _int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int((os.getenv(name) or "").strip())
    except ValueError:
        return default
    return value if lo <= value <= hi else default


def _float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        value = float((os.getenv(name) or "").strip())
    except ValueError:
        return default
    return value if lo <= value <= hi else default


def _flag(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if raw in ("1", "true", "on", "yes"):
        return True
    if raw in ("0", "false", "off", "no"):
        return False
    return default


def gateway_enabled() -> bool:
    """AI_GATEWAY=legacy turns OFF the 2.0 reliability controls (first-token
    and stream deadlines, process-wide retry budget, health/breaker influence)
    so the pre-2.0 attempt behaviour can be restored by configuration alone."""
    return (os.getenv("AI_GATEWAY") or "v2").strip().lower() != "legacy"


# --- routing ---------------------------------------------------------------
def router_mode() -> str:
    """static  = legacy: every request uses GEMINI_MODEL, no routing.
    shadow  = the router decides and logs, but the legacy model is still used.
    policy  = the router's model is used."""
    return _choice("AI_ROUTER_MODE", ROUTER_MODES, "static")


def pro_advanced_share_percent() -> int:
    """Share of Pro requests, among those where BOTH approved models are
    suitable, sent to the advanced model (CEO target: 70)."""
    return _int("AI_PRO_ADVANCED_SHARE_PERCENT", 70, 0, 100)


def model_fallback_enabled() -> bool:
    """Allows ONE pre-token switch to the other approved Gemini model when the
    chosen model's circuit breaker is open or the first attempt fails
    transiently. Same provider only - cross-provider fallback does not exist."""
    return _flag("AI_MODEL_FALLBACK", False)


# --- context / verification --------------------------------------------------
def context_builder_mode() -> str:
    """legacy = pre-2.0 prompt assembly. v2 = layered prompt blocks, the
    uploaded document passed as delimited user content (never in the system
    instruction), and an explicit token budget."""
    return _choice("AI_CONTEXT_BUILDER", CONTEXT_BUILDER_MODES, "legacy")


def verify_mode() -> str:
    """off | shadow. There is deliberately no student-facing mode."""
    return _choice("AI_VERIFY_MODE", VERIFY_MODES, "off")


def verify_sample_percent() -> int:
    return _int("AI_VERIFY_SAMPLE_PERCENT", 100, 0, 100)


# --- reliability --------------------------------------------------------------
def first_token_deadline_seconds() -> float:
    """Wall-clock budget from the start of the request until the FIRST token,
    across every attempt and backoff. Per-attempt SDK timeouts alone cannot
    bound this (they are per network phase)."""
    return _float("AI_FIRST_TOKEN_DEADLINE_SECONDS", 75.0, 5.0, 600.0)


def stream_deadline_seconds() -> float:
    """Wall-clock budget for the whole answer, checked between chunks."""
    return _float("AI_STREAM_DEADLINE_SECONDS", 240.0, 10.0, 1800.0)


def retry_budget_burst() -> int:
    return _int("AI_RETRY_BUDGET_BURST", 30, 0, 100000)


def retry_budget_refill_per_second() -> float:
    return _float("AI_RETRY_BUDGET_REFILL_PER_SECOND", 3.0, 0.0, 10000.0)


def breaker_failure_threshold() -> int:
    return _int("AI_BREAKER_FAILURES", 5, 1, 1000)


def breaker_cooldown_seconds() -> float:
    return _float("AI_BREAKER_COOLDOWN_SECONDS", 30.0, 1.0, 3600.0)


# --- concurrency ----------------------------------------------------------------
def max_inflight_generations() -> int:
    """Upper bound on simultaneously running provider generations in this
    process; excess requests wait in a bounded queue, then are shed."""
    return _int("AI_MAX_INFLIGHT_GENERATIONS", 32, 1, 5000)


def max_queued_generations() -> int:
    return _int("AI_MAX_QUEUED_GENERATIONS", 64, 0, 100000)


def queue_wait_seconds() -> float:
    return _float("AI_QUEUE_WAIT_SECONDS", 8.0, 0.0, 120.0)


# --- plan gating ----------------------------------------------------------------
def plan_gating_mode() -> str:
    """enforce (default) | off. 'off' is the documented rollback for the
    server-side feature gating introduced with the Free/Pro policy."""
    return _choice("PLAN_GATING", ("enforce", "off"), "enforce")
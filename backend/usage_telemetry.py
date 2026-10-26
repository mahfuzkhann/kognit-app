"""
Persistence for privacy-safe AI usage records, and quota refund settlement
(Pre-Phase 10 remediation M5, hardened in the pre-Phase 10 finalization: Q-01,
Q-03, Q-04, Q-06, Q-15).

The collection half (ContextVar collector, record_ai_call) lives in
backend/usage_collector.py so ai_engine and the evaluation package can use it
without importing the database layer. This module re-exports it and adds:

  * save_ai_usage_events - writes rows through the database function
    public.record_ai_usage_events (migration 0011), authenticated by a SERVER
    SECRET (USAGE_WRITE_KEY). Students can no longer INSERT into
    ai_usage_events at all: the old "insert own rows" policy let any student
    forge cost/plan/model/token values and write unbounded rows. The function
    derives user_id and plan itself, forces cost columns to NULL, validates and
    bounds every field, and caps rows per user per day. The key is a shared
    secret held only in the server environment - it is NOT a service-role key
    and grants nothing but "write usage rows for the signed-in user".
  * settle_request       - the ONE idempotent place where usage rows are
    persisted and a quota unit is (or is not) given back. It is called both by
    the post-response BackgroundTask AND directly on failure paths, because
    FastAPI does not run background tasks when an exception escapes the
    endpoint (Q-03/Q-15). Calling it twice is harmless.
  * settle_on_exception  - async context manager the endpoints wrap around
    their post-quota work so an unexpected exception can never strand a
    charged request without a refund decision.
  * finalize_ai_request  - the BackgroundTask wrapper around settle_request.
    It never raises, so telemetry can never delay or fail an answer.

Refund rule (CEO decision A): a unit is refunded only if NO generated text was
delivered - see usage_collector.UsageCollector.refund_due.

estimated_cost_usd / pricing_version are always NULL for now: no verified price
list exists in this repository and none is hard-coded.

The usage table is TELEMETRY, not an accounting ledger for billing or quotas.
Quotas use public.quota_ledger, which students cannot write directly.
"""

import contextlib
import logging
import os

from backend import database, quota_service
from backend.usage_collector import (  # noqa: F401  (re-exported public API)
    OPERATIONS,
    STATUSES,
    UsageCollector,
    bind_current,
    current_collector,
    record_ai_call,
    slug,
    start_collection,
)

logger = logging.getLogger("kognit.usage")

# Columns the database function accepts. Anything else in a record is dropped
# here and ignored there (user_id, plan, created_at, cost are server-derived).
_EVENT_FIELDS = (
    "request_id", "provider", "model", "operation", "attempt", "input_tokens",
    "output_tokens", "thinking_tokens", "total_tokens", "duration_ms", "ttft_ms",
    "status", "error_code", "retry_count", "recovery_used",
)

_warned_missing_key = False


def usage_write_key() -> str:
    return (os.getenv("USAGE_WRITE_KEY") or "").strip()


async def save_ai_usage_events(user_token: str, user_id: str, rows: list) -> None:
    """Writes the rows via public.record_ai_usage_events(), authenticated with
    USAGE_WRITE_KEY. Raises database.DatabaseError on failure; callers treat
    that as best-effort. `user_id` is accepted for call compatibility and
    logging only - the database uses the verified JWT identity, so a wrong
    value here could never write under another account."""
    global _warned_missing_key
    if not rows:
        return
    key = usage_write_key()
    if not key:
        if not _warned_missing_key:
            _warned_missing_key = True
            logger.warning("USAGE_WRITE_KEY is not set - usage telemetry is not being recorded")
        return
    events = [{k: row.get(k) for k in _EVENT_FIELDS if k in row} for row in rows]
    try:
        await quota_service._rpc(
            user_token, "record_ai_usage_events", {"p_write_key": key, "p_events": events}
        )
    except quota_service.QuotaUnavailable as exc:
        logger.error("save_ai_usage_events failed (user_id=%s): %s", user_id, exc)
        raise database.DatabaseError("usage telemetry write failed") from exc


async def settle_request(collector: UsageCollector) -> None:
    """Persists usage rows (once) and gives the quota unit back when - and only
    when - the policy says so (once). Idempotent, never raises, safe to call
    from the background task, from a failure path, or both."""
    user_id, user_token = collector.user_id, collector.user_token
    if not user_id or not user_token:
        return
    if not collector.usage_persisted:
        collector.usage_persisted = True
        try:
            rows = collector.snapshot()
            for row in rows:
                row["plan"] = slug(collector.plan, 32)
            await save_ai_usage_events(user_token, user_id, rows)
        except Exception:  # noqa: BLE001
            logger.warning("usage persistence failed (request_id=%s)", collector.request_id)
    try:
        if not collector.refund_done and collector.refund_attempts < 2 and collector.refund_due():
            collector.refund_attempts += 1
            refunded = await quota_service.refund_quota(user_token, collector.refund_ticket)
            collector.refund_done = bool(refunded)
            logger.info(
                "quota refund request_id=%s kind=%s refunded=%s",
                collector.request_id, collector.refund_ticket.kind, refunded,
            )
    except Exception:  # noqa: BLE001
        logger.warning("quota refund failed (request_id=%s)", collector.request_id)


@contextlib.asynccontextmanager
async def settle_on_exception(collector: UsageCollector):
    """Wrap an endpoint's work AFTER quota was consumed. If an exception escapes
    (the framework will turn it into an error response and will NOT run the
    registered background tasks), mark the request failed, settle it right now
    - the refund decision still honours "no refund after text delivery" - and
    re-raise unchanged. Client cancellation is deliberately not a failure."""
    try:
        yield
    except Exception:
        collector.force_refund = True
        await settle_request(collector)
        raise


async def finalize_ai_request(collector: UsageCollector) -> None:
    """FastAPI BackgroundTask run AFTER the response. See settle_request."""
    await settle_request(collector)
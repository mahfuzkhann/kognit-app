"""
Persistence for privacy-safe AI usage records, and quota refund finalization
(Pre-Phase 10 remediation, finding M5).

The collection half (ContextVar collector, record_ai_call) lives in
backend/usage_collector.py so ai_engine and the evaluation package can use it
without importing the database layer. This module re-exports it and adds:

  * save_ai_usage_events - inserts rows into public.ai_usage_events under the
    STUDENT'S OWN token (RLS: insert-own only; migration 0009)
  * finalize_ai_request  - FastAPI BackgroundTask run AFTER the response:
    persists usage, then gives a quota unit back when the student received
    nothing usable. It never raises, so telemetry can never delay or fail an
    answer.

estimated_cost_usd / pricing_version are always NULL for now: no verified price
list exists in this repository and none is hard-coded.

The usage table is TELEMETRY, not an accounting ledger for billing or quotas:
a student can only insert rows for themselves and could pollute their own rows.
Quotas use public.quota_ledger, which students cannot write directly.
"""

import logging

import httpx

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


async def save_ai_usage_events(user_token: str, user_id: str, rows: list) -> None:
    """Inserts the rows under the student's own token (RLS: insert-own only).
    Raises database.DatabaseError on failure; callers treat that as best-effort."""
    if not rows:
        return
    if not database.SUPABASE_URL or not database.SUPABASE_ANON_KEY:
        raise database.DatabaseError("Supabase is not configured on the server.")
    payload = [{**row, "user_id": user_id} for row in rows]
    headers = database._rest_headers(user_token)
    headers["Prefer"] = "return=minimal"
    try:
        async with httpx.AsyncClient(timeout=database.DB_REQUEST_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{database.SUPABASE_URL}/rest/v1/ai_usage_events", headers=headers, json=payload
            )
    except httpx.RequestError:
        logger.error("save_ai_usage_events: network error (user_id=%s)", user_id)
        raise database.DatabaseError("network error inserting ai_usage_events")
    if resp.status_code not in (200, 201, 204):
        logger.error(
            "save_ai_usage_events: insert failed status=%d body=%s (user_id=%s)",
            resp.status_code, resp.text[:300], user_id,
        )
        raise database.DatabaseError(f"ai_usage_events insert failed with status {resp.status_code}")


async def finalize_ai_request(collector: UsageCollector) -> None:
    """Runs after the response (FastAPI BackgroundTask). Persists usage, then
    refunds a quota unit if the student got nothing usable. Never raises. Does
    nothing if the request was never bound to a verified identity."""
    user_id, user_token = collector.user_id, collector.user_token
    if not user_id or not user_token:
        return
    try:
        rows = collector.snapshot()
        for row in rows:
            row["plan"] = slug(collector.plan, 32)
        await save_ai_usage_events(user_token, user_id, rows)
    except Exception:  # noqa: BLE001
        logger.warning("usage persistence failed (request_id=%s)", collector.request_id)
    try:
        if collector.refund_due():
            refunded = await quota_service.refund_quota(user_token, collector.refund_ticket)
            logger.info(
                "quota refund request_id=%s kind=%s refunded=%s",
                collector.request_id, collector.refund_ticket.kind, refunded,
            )
    except Exception:  # noqa: BLE001
        logger.warning("quota refund failed (request_id=%s)", collector.request_id)
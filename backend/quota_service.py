"""
Server-side plan entitlements and usage quotas (Pre-Phase 10 remediation, H2).

WHERE THE RULES LIVE
--------------------
The single authority is the database, not this file:

  * public.plan_limits        - what each plan may do (kind, period, limit)
  * public.subscriptions      - who currently has Pro (only a privileged,
                                payment-verified path can write it)
  * public.quota_ledger       - one row per counted request
  * public.consume_quota()    - atomic check-and-count (advisory-locked per user
                                and kind, so concurrent requests cannot exceed
                                a limit)
  * public.refund_quota()     - gives one counted request back, but ONLY to the
                                caller that holds the secret refund token

See supabase/migrations/0008_plans_quota_subscriptions.sql for the full
rationale, including why the browser can neither raise its own limits nor
grant itself refunds even though it can reach PostgREST with the anon key.

This module is the thin Python side: it calls those functions with the
STUDENT'S OWN token (never a service-role key), turns the answer into a
QuotaDecision, and maps decisions/failures to stable HTTP responses.

POLICY (CEO decisions recorded for this implementation)
------------------------------------------------------
  Free : 3 chats on the first (Asia/Dhaka) day the account exists, then 1 chat
         per day; 3 quizzes per week.
  Pro  : price 299 BDT/month, 3,199 BDT/year; limits are NOT yet decided, so
         no 'pro' rows are seeded in plan_limits. A Pro user with no configured
         limits is denied with reason "plan_not_configured" rather than being
         silently given invented numbers. Insert the rows once decided.
  Days/weeks reset in Asia/Dhaka; weeks start on Monday.
  A "chat" is one request to /api/chat or /api/chat/stream. Counting requests
  (not distinct chat_ids) means opening many chats cannot bypass the limit.
  Notes/title AI (kind "notes_ai", migration 0011) is a SEPARATE meter, so a
  chat title or a Pocket Notes flashcard set never spends a chat. It covers
  POST /api/chat/title and the three on-demand Pocket Notes AI routes
  (suggest-tags, flashcards, mini-quiz). Its Free limit is a PROPOSAL awaiting
  CEO approval (see 0011 and docs/DEPLOYMENT.md); like every limit it lives in
  public.plan_limits, never in this file. Returning already-stored study
  material and ordinary note CRUD are not AI operations and are not counted.

WHAT COUNTS
-----------
A request is counted when it is accepted, BEFORE the provider is called. A
request rejected for exceeding the quota is not counted. If the request then
fails and the student received NO generated text, the backend refunds the unit.
Once even one piece of generated text has been delivered the request is final
and is never refunded (CEO decision A - see usage_collector.refund_due). A
client that simply re-sends is a new request and is counted again.

If the consume call itself fails ambiguously (timeout / lost response) the
database may or may not have recorded the unit. enforce_quota therefore makes
one best-effort compensating refund using the request's own secret ticket
before it fails closed: refund_quota only ever flips an EXISTING, matching,
not-yet-refunded ledger row, so when nothing was recorded it is a harmless
no-op and can never create a credit.

ROLLOUT SWITCH
--------------
QUOTA_MODE=enforce (default) | off. "off" skips every database call and is the
documented rollback if the migration is not yet applied in an environment.
If enforcement is on and the quota backend is unreachable, requests are
refused with a safe 503 (fail closed) - never silently unmetered.
"""

import hashlib
import logging
import os
import secrets
import uuid
from dataclasses import dataclass
from typing import Optional

import httpx
from fastapi import HTTPException

from backend import database

logger = logging.getLogger("kognit.quota")

KIND_CHAT = "chat"
KIND_QUIZ = "quiz"
KIND_NOTES_AI = "notes_ai"   # chat titles + Pocket Notes AI tools (migration 0011)
VALID_KINDS = (KIND_CHAT, KIND_QUIZ, KIND_NOTES_AI)

PLAN_UNMETERED = "unmetered"  # reported when QUOTA_MODE=off

CODE_QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
CODE_QUOTA_UNAVAILABLE = "QUOTA_UNAVAILABLE"
CODE_PLAN_NOT_CONFIGURED = "PLAN_NOT_CONFIGURED"

_KIND_NOUN = {KIND_CHAT: "chats", KIND_QUIZ: "quizzes", KIND_NOTES_AI: "AI note tools"}


class QuotaUnavailable(Exception):
    """The quota backend could not give a trustworthy answer."""


@dataclass(frozen=True)
class QuotaDecision:
    allowed: bool
    kind: str
    plan: str
    reason: str = "ok"                 # ok | quota_exceeded | plan_not_configured | unmetered
    limit: Optional[int] = None
    used: Optional[int] = None
    remaining: Optional[int] = None
    resets_at: Optional[str] = None    # ISO-8601 timestamp, UTC offset included
    replay: bool = False               # same request_id seen again; not double-counted


@dataclass(frozen=True)
class RefundTicket:
    """Held only in server memory for the life of one request."""
    kind: str
    request_id: str
    token: str


def quota_mode() -> str:
    mode = (os.getenv("QUOTA_MODE") or "enforce").strip().lower()
    return "off" if mode == "off" else "enforce"


def new_ledger_request_id() -> str:
    """A full 128-bit request id for ledger rows of the notes_ai meter. The
    chat endpoints' 12-hex-char display ids are fine for logs, but a ledger
    collision would be treated as a replay (allowed, uncounted), so new meters
    use the full uuid4 hex (32 chars; the SQL accepts 8-64)."""
    return uuid.uuid4().hex


def new_refund_ticket(kind: str, request_id: str) -> RefundTicket:
    return RefundTicket(kind=kind, request_id=request_id, token=secrets.token_urlsafe(24))


def hash_refund_token(token: str) -> str:
    """Matches encode(sha256(convert_to(token,'UTF8')),'hex') in the SQL."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def _rpc(user_token: str, function: str, params: dict):
    """The single place this module talks to PostgREST. Returns decoded JSON or
    raises QuotaUnavailable. Logs a truncated body server-side only."""
    if not database.SUPABASE_URL or not database.SUPABASE_ANON_KEY:
        raise QuotaUnavailable("supabase not configured")
    try:
        async with httpx.AsyncClient(timeout=database.DB_REQUEST_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{database.SUPABASE_URL}/rest/v1/rpc/{function}",
                headers=database._rest_headers(user_token),
                json=params,
            )
    except httpx.RequestError as exc:
        logger.error("quota rpc %s network error type=%s", function, type(exc).__name__)
        raise QuotaUnavailable("network error") from exc

    if resp.status_code != 200:
        logger.error("quota rpc %s failed status=%d body=%s", function, resp.status_code, resp.text[:300])
        raise QuotaUnavailable(f"rpc status {resp.status_code}")
    try:
        return resp.json()
    except ValueError as exc:
        raise QuotaUnavailable("non-json response") from exc


def parse_decision(kind: str, payload) -> QuotaDecision:
    """Turns the database function's JSON into a QuotaDecision. Anything that is
    not clearly an allow/deny answer raises QuotaUnavailable (fail closed)."""
    if not isinstance(payload, dict) or not isinstance(payload.get("allowed"), bool):
        raise QuotaUnavailable("unexpected quota response shape")

    def _int(name):
        value = payload.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    plan = payload.get("plan")
    reason = payload.get("reason")
    resets_at = payload.get("resets_at")
    return QuotaDecision(
        allowed=payload["allowed"],
        kind=kind,
        plan=plan if isinstance(plan, str) and plan else "free",
        reason=reason if isinstance(reason, str) and reason else ("ok" if payload["allowed"] else "quota_exceeded"),
        limit=_int("limit"),
        used=_int("used"),
        remaining=_int("remaining"),
        resets_at=resets_at if isinstance(resets_at, str) else None,
        replay=bool(payload.get("replay", False)),
    )


async def consume_quota(user_token: str, kind: str, request_id: str, ticket: Optional[RefundTicket]) -> QuotaDecision:
    """Counts one request against the caller's quota (atomically, in the
    database). Never raises for "over quota" - that is a normal decision."""
    if kind not in VALID_KINDS:
        raise ValueError(f"unknown quota kind {kind!r}")
    if quota_mode() == "off":
        return QuotaDecision(allowed=True, kind=kind, plan=PLAN_UNMETERED, reason="unmetered")
    payload = await _rpc(
        user_token,
        "consume_quota",
        {
            "p_kind": kind,
            "p_request_id": request_id,
            "p_refund_token_hash": hash_refund_token(ticket.token) if ticket else None,
        },
    )
    return parse_decision(kind, payload)


async def refund_quota(user_token: str, ticket: RefundTicket) -> bool:
    """Gives one counted request back. Best-effort: returns False (and logs) on
    any failure; never raises."""
    if quota_mode() == "off":
        return False
    try:
        result = await _rpc(
            user_token,
            "refund_quota",
            {"p_kind": ticket.kind, "p_request_id": ticket.request_id, "p_refund_token": ticket.token},
        )
    except QuotaUnavailable:
        return False
    return result is True


_EXCEEDED_MESSAGES = {
    KIND_CHAT: "You've used all of your chats for this period. Your chats reset at the time shown.",
    KIND_QUIZ: "You've used all of your quizzes for this period. Your quizzes reset at the time shown.",
    KIND_NOTES_AI: "You've used all of your AI note tools for this period. They reset at the time shown.",
}


def exceeded_http_exception(decision: QuotaDecision) -> HTTPException:
    """429 with a machine-readable body. The existing rate limiter also uses
    429 but with a plain-string detail, so clients tell them apart by
    detail.code. Carries no internal state beyond the student's own numbers."""
    if decision.reason == "plan_not_configured":
        return HTTPException(
            status_code=403,
            detail={
                "code": CODE_PLAN_NOT_CONFIGURED,
                "kind": decision.kind,
                "plan": decision.plan,
                "message": "This plan isn't available yet. Please contact support.",
            },
        )
    headers = {}
    detail = {
        "code": CODE_QUOTA_EXCEEDED,
        "kind": decision.kind,
        "plan": decision.plan,
        "limit": decision.limit,
        "used": decision.used,
        "resets_at": decision.resets_at,
        "message": _EXCEEDED_MESSAGES.get(decision.kind, "You've reached your limit for this period."),
    }
    return HTTPException(status_code=429, detail=detail, headers=headers or None)


def unavailable_http_exception() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "code": CODE_QUOTA_UNAVAILABLE,
            "message": "Kloriz can't check your usage right now. Please try again in a moment.",
        },
    )


async def enforce_quota(user_token: str, kind: str, request_id: str):
    """Endpoint helper. Returns (decision, ticket). Raises the appropriate
    HTTPException when the request must not proceed.

    Fails closed: if the quota backend cannot give a trustworthy answer the
    request is refused (503) and never runs unmetered. Because a lost response
    does not prove the unit was NOT recorded, one compensating refund is
    attempted first (a safe no-op when nothing was recorded)."""
    ticket = new_refund_ticket(kind, request_id) if quota_mode() != "off" else None
    try:
        decision = await consume_quota(user_token, kind, request_id, ticket)
    except QuotaUnavailable:
        if ticket is not None:
            try:
                await refund_quota(user_token, ticket)  # never raises
            except Exception:  # noqa: BLE001 - must not mask the 503 below
                logger.warning("quota compensation failed request_id=%s kind=%s", request_id, kind)
        raise unavailable_http_exception()
    if not decision.allowed:
        raise exceeded_http_exception(decision)
    return decision, ticket
"""
Per-request AI usage collection (Pre-Phase 10 remediation, finding M5).

This is the LIGHT half of the usage-telemetry system: a ContextVar-held
collector plus `record_ai_call`. It imports nothing from backend.database,
backend.quota_service or FastAPI on purpose, so backend/ai_engine.py,
backend/notes_ai.py and the isolated evaluation package can call it without
gaining a transitive dependency on the database layer.

Persistence and quota refunds live in backend/usage_telemetry.py.

REFUND POLICY (CEO decision A, pre-Phase 10 finalization)
---------------------------------------------------------
A counted request is given back ONLY when the student received no generated
text. The moment ONE non-empty piece of generated text is handed to the
response (`mark_text_delivered`), the request is final: a later provider
failure, MAX_TOKENS, timeout, failed recovery or client disconnect never
refunds it. `refund_due()` enforces this above every other signal. Delivery is
set by the endpoint, from server-side state only - never from a client flag.

HOW IT HOOKS IN (no signature changes anywhere)
-----------------------------------------------
backend/main.py's endpoints depend on `_usage_collection`, which calls
start_collection(). The provider call sites then call `record_ai_call(...)`,
which appends to the current collector if there is one and otherwise does
NOTHING - so unit tests and the evaluation subsystem, which never start a
collection, are unaffected. ContextVars are copied into threadpool work by
anyio, so records made inside run_in_threadpool / iterate_in_threadpool reach
the same collector object.

WHAT A RECORD HOLDS (see supabase/migrations/0009_ai_usage_events.sql)
----------------------------------------------------------------------
request id, plan, provider, model, operation (chat/stream/quiz/title/notes),
attempt number, token counts the PROVIDER reported (None when it did not -
never a fabricated 0), duration, time-to-first-token, completion status, a
short stable error code, retry/recovery flags. Never prompts, PDF text,
answers, quiz content, tokens/keys or free-form error text: every string is
allow-listed or clipped before it is stored.
"""

import contextvars
import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("kognit.usage")

OPERATIONS = ("chat", "stream", "quiz", "title", "notes")
STATUSES = ("success", "error", "interrupted")
MAX_RECORDS_PER_REQUEST = 20
DEFAULT_PROVIDER = "gemini"

_SLUG_RE = re.compile(r"[^A-Za-z0-9_.-]")


def slug(value, max_len: int = 64) -> Optional[str]:
    """Clips and character-restricts a label so no free text can sneak in."""
    if value is None:
        return None
    cleaned = _SLUG_RE.sub("_", str(value))[:max_len]
    return cleaned or None


def _non_negative_int(value) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = int(value)
    return value if value >= 0 else None


@dataclass
class UsageCollector:
    request_id: str
    operation_hint: str
    plan: Optional[str] = None
    records: list = field(default_factory=list)
    refund_ticket: Any = None          # backend.quota_service.RefundTicket (server memory only)
    force_refund: bool = False
    # Decision A: True once ANY generated text has been handed to the response.
    text_delivered: bool = False
    # Streaming lifecycle. A stream that started but never reached a terminal
    # event was abandoned (client closed the tab / pressed Stop). That is the
    # student's own choice, not a service failure, and is never refunded.
    stream_started: bool = False
    stream_finished: bool = False
    # Idempotency guards for the (possibly repeated) settlement paths.
    usage_persisted: bool = False
    refund_attempts: int = 0
    refund_done: bool = False
    user_id: Optional[str] = None
    # The student's own token, held in server memory for the life of ONE request
    # so the post-response writer can use it. repr=False keeps it out of any
    # accidental log line.
    user_token: Optional[str] = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def bind(self, user_id: str, user_token: str) -> None:
        """Attaches the verified identity. Until bound, nothing is persisted
        (a request rejected by auth/rate limiting never reaches this)."""
        self.user_id = user_id
        self.user_token = user_token

    def mark_text_delivered(self) -> None:
        """Called at the instant generated text is handed to the response."""
        self.text_delivered = True

    def mark_stream_started(self) -> None:
        self.stream_started = True

    def mark_stream_finished(self) -> None:
        self.stream_finished = True

    def mark_text_delivered_from_records(self) -> None:
        """For NON-streaming endpoints: the generated text is returned in the
        JSON body in one piece, so it counts as delivered exactly when the
        provider produced usable text (a success/interrupted record). Error
        records mean the body carried a canned error message, not AI text."""
        if any(r["status"] in ("success", "interrupted") for r in self.snapshot()):
            self.text_delivered = True

    def interrupted_code(self) -> Optional[str]:
        """Error code of the last 'interrupted' record (truncated output), or None."""
        for r in reversed(self.snapshot()):
            if r["status"] == "interrupted":
                return r.get("error_code") or "STREAM_INTERRUPTED"
        return None

    def add(self, record: dict) -> None:
        with self._lock:
            if len(self.records) < MAX_RECORDS_PER_REQUEST:
                self.records.append(record)

    def snapshot(self) -> list:
        with self._lock:
            return [dict(r) for r in self.records]

    def refund_due(self) -> bool:
        """True when a counted unit should be given back.

        Order matters (most authoritative first):
          1. no ticket (request was never counted)       -> never
          2. text delivered (CEO decision A)             -> never, whatever
             happened afterwards (failure, MAX_TOKENS, timeout, disconnect)
          3. abandoned stream (started, no terminal)     -> never (the student
             left; refunding would let anyone cancel for free credits)
          4. the endpoint said nothing usable was made   -> refund
          5. otherwise the provider records decide: a success means keep the
             count; only errors/interruptions with NO success means refund.
        """
        if self.refund_ticket is None:
            return False
        if self.text_delivered:
            return False
        if self.stream_started and not self.stream_finished:
            return False
        if self.force_refund:
            return True
        records = self.snapshot()
        if not records:
            return False
        if any(r["status"] == "success" for r in records):
            return False
        return any(r["status"] in ("error", "interrupted") for r in records)


_current: contextvars.ContextVar = contextvars.ContextVar("kognit_usage_collector", default=None)


def start_collection(request_id: str, operation_hint: str) -> UsageCollector:
    collector = UsageCollector(request_id=request_id, operation_hint=operation_hint)
    _current.set(collector)
    return collector


def current_collector() -> Optional[UsageCollector]:
    return _current.get()


def bind_current(user_id: str, user_token: str) -> None:
    """Binds identity to the active collection, if any. Never raises."""
    collector = _current.get()
    if collector is not None:
        collector.bind(user_id, user_token)


def record_ai_call(
    *,
    operation: str,
    status: str,
    model: Optional[str] = None,
    usage=None,
    duration_seconds: Optional[float] = None,
    ttft_seconds: Optional[float] = None,
    error_code: Optional[str] = None,
    attempt: Optional[int] = None,
    retry_count: Optional[int] = None,
    recovery_used: bool = False,
    provider: str = DEFAULT_PROVIDER,
) -> None:
    """Appends one usage record to the active collector. NEVER raises and does
    nothing when no collection is active. `usage` is the provider-neutral
    ProviderUsage (or None); missing numbers stay None."""
    try:
        collector = _current.get()
        if collector is None:
            return
        if operation not in OPERATIONS or status not in STATUSES:
            return
        collector.add(
            {
                "request_id": slug(collector.request_id, 64),
                "plan": slug(collector.plan, 32),
                "provider": slug(provider, 32),
                "model": slug(model, 64),
                "operation": operation,
                "attempt": _non_negative_int(attempt),
                "input_tokens": _non_negative_int(getattr(usage, "prompt_tokens", None)),
                "output_tokens": _non_negative_int(getattr(usage, "output_tokens", None)),
                "thinking_tokens": _non_negative_int(getattr(usage, "thoughts_tokens", None)),
                "total_tokens": _non_negative_int(getattr(usage, "total_tokens", None)),
                "duration_ms": _non_negative_int(duration_seconds * 1000) if duration_seconds is not None else None,
                "ttft_ms": _non_negative_int(ttft_seconds * 1000) if ttft_seconds is not None else None,
                "status": status,
                "error_code": slug(error_code, 64),
                "retry_count": _non_negative_int(retry_count),
                "recovery_used": bool(recovery_used),
            }
        )
    except Exception:  # noqa: BLE001 - telemetry must never affect an answer
        logger.debug("record_ai_call failed", exc_info=True)
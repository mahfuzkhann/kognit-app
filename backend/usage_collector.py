"""
Per-request AI usage collection (Pre-Phase 10 remediation, finding M5).

This is the LIGHT half of the usage-telemetry system: a ContextVar-held
collector plus `record_ai_call`. It imports nothing from backend.database,
backend.quota_service or FastAPI on purpose, so backend/ai_engine.py,
backend/notes_ai.py and the isolated evaluation package can call it without
gaining a transitive dependency on the database layer.

Persistence and quota refunds live in backend/usage_telemetry.py.

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

    def add(self, record: dict) -> None:
        with self._lock:
            if len(self.records) < MAX_RECORDS_PER_REQUEST:
                self.records.append(record)

    def snapshot(self) -> list:
        with self._lock:
            return [dict(r) for r in self.records]

    def refund_due(self) -> bool:
        """True when a counted unit should be given back: the endpoint said so,
        or the request ended in failure with no successful provider result.
        A student merely cancelling a stream is NOT a failure (no refunds for
        reading an answer and pressing Stop)."""
        if self.refund_ticket is None:
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
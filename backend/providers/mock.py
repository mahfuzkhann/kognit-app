"""
Kognit Phase 10 - deterministic mock provider (tests only).

Implements backend.providers.base.Provider with NO SDK, NO network, NO clock and
NO randomness: every call consumes the next scripted `MockAttempt`, so a test
reproduces any provider failure exactly. The last scripted attempt repeats once
the script runs out (so "always 503" is a one-element script).

An attempt is "deliver these events, then optionally raise this error":

    MockProvider([
        failing_attempt(ProviderErrorKind.UNAVAILABLE, http_status=503),  # try 1
        text_attempt("Hello ", "world"),                                  # try 2
    ])

Streaming delivers the events then raises the error (a mid-stream failure when
text came first, a pre-token failure when there were no events). Unary calls
(`chat`, `generate`) cannot deliver partial output, so an attempt with an error
simply raises it; otherwise the events are folded into one ProviderResult.

The mock records every ProviderRequest it receives (`requests`) and which
operation was called (`calls`), so tests can assert model, history, timeout,
reasoning effort and web-search flags.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, List, Optional, Sequence

from backend.providers.base import (
    FinishReason,
    GroundingResult,
    ProviderError,
    ProviderErrorKind,
    ProviderEvent,
    ProviderRequest,
    ProviderResult,
    ProviderUsage,
)

MOCK_PROVIDER_NAME = "mock"


@dataclass
class MockAttempt:
    """One scripted provider call: events to deliver, then an optional error."""
    events: List[ProviderEvent] = field(default_factory=list)
    error: Optional[BaseException] = None


# ---------------------------------------------------------------------------
# Scenario builders
# ---------------------------------------------------------------------------

def _finish_event(finish, usage, grounding) -> Optional[ProviderEvent]:
    if finish is None and usage is None and grounding is None:
        return None
    return ProviderEvent(
        finish_reason=finish,
        raw_finish_reason=finish.value if finish is not None else None,
        usage=usage,
        grounding=grounding,
    )


def text_attempt(
    *chunks: str,
    finish: Optional[FinishReason] = FinishReason.STOP,
    usage: Optional[ProviderUsage] = None,
    grounding: Optional[GroundingResult] = None,
) -> MockAttempt:
    """Normal success: the text chunks, then one metadata event (finish reason,
    usage, grounding). finish=None models a stream that never reports one."""
    events = [ProviderEvent(text=chunk) for chunk in chunks]
    tail = _finish_event(finish, usage, grounding)
    if tail is not None:
        events.append(tail)
    return MockAttempt(events=events)


def failing_attempt(
    kind: ProviderErrorKind,
    *,
    http_status: Optional[int] = None,
    status: Optional[str] = None,
    after_text: Sequence[str] = (),
) -> MockAttempt:
    """A provider failure. With `after_text` it is a MID-STREAM failure (those
    chunks arrive first); without, a PRE-TOKEN failure."""
    return MockAttempt(
        events=[ProviderEvent(text=chunk) for chunk in after_text],
        error=ProviderError(
            kind, http_status=http_status, status=status,
            provider=MOCK_PROVIDER_NAME, original_type="MockProviderError",
        ),
    )


def raising_attempt(error: BaseException, *, after_text: Sequence[str] = ()) -> MockAttempt:
    """Raise an arbitrary exception (e.g. RuntimeError/ValueError to model a
    malformed provider response or an unexpected bug), optionally mid-stream."""
    return MockAttempt(events=[ProviderEvent(text=chunk) for chunk in after_text], error=error)


def safety_block_attempt() -> MockAttempt:
    """No text and a SAFETY finish reason - a blocked response."""
    return text_attempt(finish=FinishReason.SAFETY)


def max_tokens_attempt(*chunks: str) -> MockAttempt:
    """Text that ends with MAX_TOKENS - a truncated answer."""
    return text_attempt(*chunks, finish=FinishReason.MAX_TOKENS)


def empty_attempt() -> MockAttempt:
    """A chunk with no text and no finish reason - an empty response."""
    return MockAttempt(events=[ProviderEvent()])


# ---------------------------------------------------------------------------
# The provider
# ---------------------------------------------------------------------------

class MockProvider:
    """Scripted, deterministic Provider. See the module docstring."""

    name = MOCK_PROVIDER_NAME

    def __init__(self, attempts: Sequence[MockAttempt], model_version: str = "mock-model-1"):
        if not attempts:
            raise ValueError("MockProvider needs at least one scripted attempt")
        self._attempts = list(attempts)
        self._model_version = model_version
        self.requests: List[ProviderRequest] = []
        self.calls: List[str] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def _next(self, operation: str, request: ProviderRequest) -> MockAttempt:
        index = min(len(self.calls), len(self._attempts) - 1)
        self.calls.append(operation)
        self.requests.append(request)
        return self._attempts[index]

    def stream_chat(self, request: ProviderRequest) -> Iterator[ProviderEvent]:
        attempt = self._next("stream_chat", request)
        return self._stream(attempt)

    @staticmethod
    def _stream(attempt: MockAttempt) -> Iterator[ProviderEvent]:
        for event in attempt.events:
            yield event
        if attempt.error is not None:
            raise attempt.error

    def chat(self, request: ProviderRequest) -> ProviderResult:
        return self._unary("chat", request)

    def generate(self, request: ProviderRequest) -> ProviderResult:
        return self._unary("generate", request)

    def _unary(self, operation: str, request: ProviderRequest) -> ProviderResult:
        attempt = self._next(operation, request)
        if attempt.error is not None:
            raise attempt.error
        text_parts = [e.text for e in attempt.events if e.text]
        finish = raw_finish = usage = grounding = None
        for event in attempt.events:
            if event.finish_reason is not None:
                finish, raw_finish = event.finish_reason, event.raw_finish_reason
            if event.usage is not None:
                usage = event.usage
            if event.grounding is not None:
                grounding = event.grounding
        return ProviderResult(
            text="".join(text_parts) if text_parts else None,
            finish_reason=finish,
            raw_finish_reason=raw_finish,
            usage=usage,
            grounding=grounding,
            model_version=self._model_version,
            response_id=f"mock-response-{len(self.calls)}",
        )
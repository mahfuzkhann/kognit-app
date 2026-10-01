"""
Kognit Phase 10 - provider contract.

This module is the ONLY vocabulary Kognit's AI/policy layer (backend/ai_engine.py)
uses to talk to an AI provider. It deliberately imports no provider SDK, so
anything that depends on it (the policy layer, the mock provider, tests) can
run without google-genai installed or configured.

OWNERSHIP
---------
Provider (an implementation of `Provider`) owns: calling the model, turning the
neutral request into its SDK's request, turning the SDK's responses/exceptions
into the neutral types below, and its own credentials/tool configuration.

Kognit's policy layer owns everything else: prompts, history, research
decisions, the retry/backoff/recovery policy, student-facing messages, Kognit's
error codes, telemetry, rate limiting. A provider NEVER retries and NEVER
decides what a student sees - it raises `ProviderError` and Kognit decides.

ERRORS ARE EXCEPTIONS, NOT EVENTS
---------------------------------
A provider signals failure by raising `ProviderError` (from the call for
unary methods, from the iterator for streaming). It does not yield an
"error event". This is the same control flow the SDK exceptions had before,
which is what lets the verified Bug 3 mid-stream recovery loop stay
structurally identical: "text arrived, then the iterator raised".
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Iterator, List, Optional, Protocol, Tuple, Union


# ---------------------------------------------------------------------------
# Normalized finish reason
# ---------------------------------------------------------------------------

class FinishReason(str, enum.Enum):
    """Why generation stopped, normalized across providers.

    STOP        - the model finished normally.
    MAX_TOKENS  - the output budget ran out (answer is truncated).
    SAFETY      - blocked/cut by a safety-type filter (includes recitation,
                  blocklist, prohibited content and similar provider reasons).
    OTHER       - any other non-STOP reason. Kognit treats it as "not a clean
                  finish", exactly like the provider-specific values it
                  replaces.

    "No finish reason observed at all" is represented by None, not by a member
    here - Kognit's completion rule treats None as success (see
    ai_engine._is_successful_finish).
    """
    STOP = "STOP"
    MAX_TOKENS = "MAX_TOKENS"
    SAFETY = "SAFETY"
    OTHER = "OTHER"


# ---------------------------------------------------------------------------
# Normalized errors
# ---------------------------------------------------------------------------

class ProviderErrorKind(str, enum.Enum):
    """Provider-neutral failure categories. Kognit's policy layer maps these to
    its own retry decisions, student messages and wire error codes."""
    UNAVAILABLE = "UNAVAILABLE"            # provider-side failure/overload (5xx)
    RATE_LIMITED = "RATE_LIMITED"          # quota / rate limit (429)
    TIMEOUT = "TIMEOUT"                    # our client deadline or a connect failure
    AUTH = "AUTH"                          # credentials rejected (401/403)
    INVALID_REQUEST = "INVALID_REQUEST"    # the request itself is bad (other 4xx)
    CONFLICT = "CONFLICT"                  # transient "try again" conflict (409)
    UNKNOWN = "UNKNOWN"                    # anything not recognised as the above


class ProviderError(Exception):
    """A provider failure, normalized.

    The exception text is a short, SAFE label (kind/status/type) - it never
    contains the provider's own message, request bodies, prompts or keys, so it
    is safe to log. The original exception is available as `__cause__`.
    """

    def __init__(
        self,
        kind: ProviderErrorKind,
        *,
        http_status: Optional[int] = None,
        status: Optional[str] = None,
        provider: str = "",
        original_type: Optional[str] = None,
    ):
        self.kind = kind
        self.http_status = http_status
        self.status = status
        self.provider = provider
        self.original_type = original_type or "ProviderError"
        super().__init__(
            f"provider={provider or '?'} kind={kind.value} "
            f"http_status={http_status} status={status} type={self.original_type}"
        )


# ---------------------------------------------------------------------------
# Normalized usage and grounding
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProviderUsage:
    """Token usage. Every field optional: providers report different subsets."""
    prompt_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    thoughts_tokens: Optional[int] = None
    cached_tokens: Optional[int] = None
    total_tokens: Optional[int] = None


@dataclass(frozen=True)
class GroundingSource:
    """One web source a grounded answer drew on."""
    title: Optional[str] = None
    url: Optional[str] = None
    domain: Optional[str] = None


@dataclass(frozen=True)
class GroundingSupport:
    """One answer span and the sources backing it.

    `source_indices` index into GroundingResult.sources. `text`/`start_index`/
    `end_index` are all None when the provider supplied no answer span.
    """
    text: Optional[str] = None
    start_index: Optional[int] = None
    end_index: Optional[int] = None
    source_indices: Tuple[int, ...] = ()


@dataclass(frozen=True)
class GroundingResult:
    """Provider-neutral web-search grounding, containing only what Kognit uses
    (see backend/research_models.py).

    `sources` is POSITION-PRESERVING: entry i corresponds to the provider's
    i-th grounding chunk, and is None when that chunk is a kind Kognit does not
    support (so `GroundingSupport.source_indices` stay valid and a reference to
    an unsupported chunk is still detectable as "source_unmapped").
    """
    search_queries: Tuple[str, ...] = ()
    sources: Tuple[Optional[GroundingSource], ...] = ()
    supports: Tuple[GroundingSupport, ...] = ()


# ---------------------------------------------------------------------------
# Request / event / result
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProviderMessage:
    """One prior turn. role is "user" or "assistant" (provider-neutral; the
    adapter maps "assistant" to whatever its API calls the model's role)."""
    role: str
    text: str


# One element of ProviderRequest.user_parts: a text string, or an already
# decoded PIL.Image.Image (typed loosely on purpose - base.py imports no
# imaging library; adapters and the mock accept what ai_engine builds).
UserPart = Union[str, object]


@dataclass
class ProviderRequest:
    """Everything a provider needs for ONE generation call.

    Holds AI-generation inputs only - never student profiles, mastery, chat
    rows, rate limits or UI state.

    system_instruction  - the full Kognit system prompt.
    user_parts          - the user turn: strings and/or decoded PIL images.
    model               - the model to use (selected by Kognit's config layer).
    timeout_seconds     - the deadline for this single call (Kognit's attempt
                          policy decides it; the provider just enforces it).
    history             - prior turns, oldest first (may be empty).
    reasoning_effort    - "low"/"medium"/"high"/None. None = provider default.
    enable_web_search   - Kognit decided this request needs web grounding.
    request_id          - correlation id for provider-side logs.
    """
    system_instruction: str
    user_parts: List[UserPart]
    model: str
    timeout_seconds: float
    history: List[ProviderMessage] = field(default_factory=list)
    reasoning_effort: Optional[str] = None
    enable_web_search: bool = False
    request_id: Optional[str] = None


@dataclass(frozen=True)
class ProviderEvent:
    """One streamed chunk, normalized.

    One event is produced per provider chunk, INCLUDING chunks that carry no
    text (a metadata-only chunk is still a real round trip, and Kognit's
    telemetry counts them). Every field is optional; Kognit keeps the most
    recent non-None finish_reason / usage / grounding across the stream.

    raw_finish_reason / finish_message are provider labels kept only for logs.
    """
    text: Optional[str] = None
    finish_reason: Optional[FinishReason] = None
    raw_finish_reason: Optional[str] = None
    finish_message: Optional[str] = None
    usage: Optional[ProviderUsage] = None
    grounding: Optional[GroundingResult] = None


@dataclass(frozen=True)
class ProviderResult:
    """A complete (non-streaming) generation, normalized.

    `text` is None/empty when the provider returned no text (typically a safety
    block) - Kognit decides what that means.
    """
    text: Optional[str] = None
    finish_reason: Optional[FinishReason] = None
    raw_finish_reason: Optional[str] = None
    usage: Optional[ProviderUsage] = None
    grounding: Optional[GroundingResult] = None
    model_version: Optional[str] = None
    response_id: Optional[str] = None


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------

class Provider(Protocol):
    """What Kognit's policy layer calls. Three operations, because they are
    three different provider calls with different shapes:

      stream_chat - a chat turn (with history), streamed.
      chat        - the same chat turn, not streamed.
      generate    - a one-shot generation with no history (quiz, titles).

    All raise ProviderError on failure. None of them retry. None of them log
    prompt text.
    """

    name: str

    def stream_chat(self, request: ProviderRequest) -> Iterator[ProviderEvent]: ...

    def chat(self, request: ProviderRequest) -> ProviderResult: ...

    def generate(self, request: ProviderRequest) -> ProviderResult: ...
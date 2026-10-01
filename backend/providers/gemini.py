"""
Kognit Phase 10 - Gemini provider adapter.

This is the ONLY runtime module that imports the google-genai SDK. Everything
Gemini-specific lives here and does not escape:

  Kognit ProviderRequest          -> google-genai request
      system_instruction          -> GenerateContentConfig.system_instruction
      timeout_seconds             -> HttpOptions.timeout (SDK unit: milliseconds)
      reasoning_effort            -> ThinkingConfig.thinking_level
      enable_web_search           -> Tool(google_search=GoogleSearch())
      history (user/assistant)    -> [{"role": "user"|"model", "parts": [{"text"}]}]
      user_parts (str / PIL)      -> passed as the message contents unchanged

  google-genai response/chunk     -> Kognit ProviderResult / ProviderEvent
      .text                       -> text
      candidates[0].finish_reason -> FinishReason (+ raw label for logs)
      usage_metadata              -> ProviderUsage
      grounding_metadata          -> GroundingResult  (map_grounding)

  google-genai / httpx exception  -> ProviderError    (translate_exception)

What this adapter deliberately does NOT do: retry, back off, recover from a
mid-stream failure, pick a model, choose student-facing text, or log prompt
content. Those belong to Kognit's policy layer (backend/ai_engine.py). An
SDK-internal HTTP retry is also not enabled (HttpOptions.retry_options stays
None) so there is exactly ONE retry owner.

Resource lifecycle: the genai.Client is created once per process and lives for
the process lifetime (no per-request resources are held), so there is no
close() on this class. google-genai's Client does have close(); owning it would
only matter if Kognit created and discarded clients, which it does not.
"""

from __future__ import annotations

import logging
import os
from typing import Iterator, Optional

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from backend.providers.base import (
    FinishReason,
    GroundingResult,
    GroundingSource,
    GroundingSupport,
    ProviderError,
    ProviderErrorKind,
    ProviderEvent,
    ProviderRequest,
    ProviderResult,
    ProviderUsage,
)

logger = logging.getLogger("kognit.providers.gemini")

PROVIDER_NAME = "gemini"

_THINKING_LEVELS = {
    "minimal": types.ThinkingLevel.MINIMAL,
    "low": types.ThinkingLevel.LOW,
    "medium": types.ThinkingLevel.MEDIUM,
    "high": types.ThinkingLevel.HIGH,
}

# Gemini role names. Kognit's neutral roles are "user" and "assistant".
_ROLE_TO_GEMINI = {"user": "user", "assistant": "model"}

# Gemini finish reasons that mean "cut off by a safety-type filter".
_SAFETY_FINISH_REASONS = frozenset({
    "SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII",
    "IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT", "IMAGE_RECITATION",
})

# HTTP status codes this adapter classifies (anything else 4xx is a bad request).
_HTTP_RATE_LIMITED = 429
_HTTP_AUTH = (401, 403)
_HTTP_CONFLICT = 409


# ---------------------------------------------------------------------------
# Small pure translators (module-level so they are directly unit-testable)
# ---------------------------------------------------------------------------

def _seconds_to_ms(seconds: float) -> int:
    """google-genai's HttpOptions.timeout is documented in milliseconds."""
    return int(seconds * 1000)


def translate_exception(exc: BaseException) -> ProviderError:
    """Map a google-genai / httpx exception to a normalized ProviderError.

    A ProviderError passes through unchanged. Anything unrecognised becomes
    kind UNKNOWN (the caller keeps the original as __cause__ by raising
    `from exc`). The result carries only a safe label, never provider text.
    """
    if isinstance(exc, ProviderError):
        return exc
    original_type = type(exc).__name__
    status = getattr(exc, "status", None)
    code = getattr(exc, "code", None)
    if isinstance(exc, genai_errors.ServerError):
        kind = ProviderErrorKind.UNAVAILABLE
    elif isinstance(exc, genai_errors.ClientError):
        if code == _HTTP_RATE_LIMITED:
            kind = ProviderErrorKind.RATE_LIMITED
        elif code in _HTTP_AUTH:
            kind = ProviderErrorKind.AUTH
        elif code == _HTTP_CONFLICT:
            kind = ProviderErrorKind.CONFLICT
        else:
            kind = ProviderErrorKind.INVALID_REQUEST
    elif isinstance(exc, (httpx.TimeoutException, httpx.ConnectError)):
        kind = ProviderErrorKind.TIMEOUT
        code = None
    else:
        kind = ProviderErrorKind.UNKNOWN
        code = None
    return ProviderError(
        kind,
        http_status=code if isinstance(code, int) else None,
        status=status if isinstance(status, str) else None,
        provider=PROVIDER_NAME,
        original_type=original_type,
    )


def _map_finish_reason(raw) -> tuple:
    """(normalized FinishReason or None, raw label or None)."""
    if raw is None:
        return None, None
    name = str(getattr(raw, "name", None) or raw).split(".")[-1].upper()
    if name == "STOP":
        return FinishReason.STOP, name
    if name == "MAX_TOKENS":
        return FinishReason.MAX_TOKENS, name
    if name in _SAFETY_FINISH_REASONS:
        return FinishReason.SAFETY, name
    return FinishReason.OTHER, name


def _map_usage(usage) -> Optional[ProviderUsage]:
    if usage is None:
        return None
    return ProviderUsage(
        prompt_tokens=getattr(usage, "prompt_token_count", None),
        output_tokens=getattr(usage, "candidates_token_count", None),
        thoughts_tokens=getattr(usage, "thoughts_token_count", None),
        cached_tokens=getattr(usage, "cached_content_token_count", None),
        total_tokens=getattr(usage, "total_token_count", None),
    )


def map_grounding(metadata) -> Optional[GroundingResult]:
    """Convert a google.genai GroundingMetadata (or None) to a GroundingResult.

    Reads only what Kognit uses. Chunk positions are preserved: a chunk with no
    `web` part (e.g. a maps/retrieved-context chunk) becomes None so support
    indices that point at it stay detectable as unmapped.
    """
    if metadata is None:
        return None
    queries = tuple(getattr(metadata, "web_search_queries", None) or [])

    sources = []
    for chunk in (getattr(metadata, "grounding_chunks", None) or []):
        web = getattr(chunk, "web", None)
        if web is None:
            sources.append(None)
        else:
            sources.append(GroundingSource(
                title=getattr(web, "title", None),
                url=getattr(web, "uri", None),
                domain=getattr(web, "domain", None),
            ))

    supports = []
    for support in (getattr(metadata, "grounding_supports", None) or []):
        segment = getattr(support, "segment", None)
        supports.append(GroundingSupport(
            text=getattr(segment, "text", None) if segment else None,
            start_index=getattr(segment, "start_index", None) if segment else None,
            end_index=getattr(segment, "end_index", None) if segment else None,
            source_indices=tuple(getattr(support, "grounding_chunk_indices", None) or []),
        ))

    return GroundingResult(
        search_queries=queries, sources=tuple(sources), supports=tuple(supports),
    )


def _reraise(exc: BaseException):
    """Raise `exc` as a ProviderError, keeping the original as __cause__."""
    translated = translate_exception(exc)
    if translated is exc:
        raise exc
    raise translated from exc


def _first_candidate(response):
    candidates = getattr(response, "candidates", None) or []
    return candidates[0] if candidates else None


def _event_from_chunk(chunk, want_grounding: bool) -> ProviderEvent:
    """One SDK stream chunk -> one ProviderEvent (even a text-less chunk)."""
    finish_reason = raw_finish = finish_message = grounding = None
    cand = _first_candidate(chunk)
    if cand is not None:
        if want_grounding:
            grounding = map_grounding(getattr(cand, "grounding_metadata", None))
        raw = getattr(cand, "finish_reason", None)
        if raw is not None:
            finish_reason, raw_finish = _map_finish_reason(raw)
            finish_message = getattr(cand, "finish_message", None)
    return ProviderEvent(
        text=getattr(chunk, "text", None),
        finish_reason=finish_reason,
        raw_finish_reason=raw_finish,
        finish_message=finish_message,
        usage=_map_usage(getattr(chunk, "usage_metadata", None)),
        grounding=grounding,
    )


def _result_from_response(response, want_grounding: bool) -> ProviderResult:
    finish_reason = raw_finish = grounding = None
    cand = _first_candidate(response)
    if cand is not None:
        if want_grounding:
            grounding = map_grounding(getattr(cand, "grounding_metadata", None))
        raw = getattr(cand, "finish_reason", None)
        if raw is not None:
            finish_reason, raw_finish = _map_finish_reason(raw)
    return ProviderResult(
        text=response.text,
        finish_reason=finish_reason,
        raw_finish_reason=raw_finish,
        usage=_map_usage(getattr(response, "usage_metadata", None)),
        grounding=grounding,
        model_version=getattr(response, "model_version", None),
        response_id=getattr(response, "response_id", None),
    )


# ---------------------------------------------------------------------------
# The provider
# ---------------------------------------------------------------------------

class GeminiProvider:
    """Gemini implementation of backend.providers.base.Provider.

    `client` is the google-genai client. It is a public attribute on purpose:
    it is the single seam tests replace with a fake SDK client.
    """

    name = PROVIDER_NAME

    def __init__(self, api_key: Optional[str] = None, client=None):
        if client is None:
            key = api_key if api_key is not None else os.getenv("GEMINI_API_KEY")
            client = genai.Client(api_key=key)
        self.client = client

    # -- request translation ------------------------------------------------

    @staticmethod
    def _config(request: ProviderRequest) -> types.GenerateContentConfig:
        kwargs = dict(
            system_instruction=request.system_instruction,
            http_options=types.HttpOptions(timeout=_seconds_to_ms(request.timeout_seconds)),
        )
        effort = request.reasoning_effort
        if effort is not None:
            level = _THINKING_LEVELS.get(str(effort).lower())
            if level is None:
                raise ValueError(f"unsupported reasoning_effort: {effort!r}")
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=level)
        if request.enable_web_search:
            kwargs["tools"] = [types.Tool(google_search=types.GoogleSearch())]
        return types.GenerateContentConfig(**kwargs)

    @staticmethod
    def _history(messages) -> list:
        history = []
        for message in messages or []:
            role = _ROLE_TO_GEMINI.get(message.role)
            if role is None:
                raise ValueError(f"unsupported history role: {message.role!r}")
            history.append({"role": role, "parts": [{"text": message.text}]})
        return history

    def _chat_session(self, request: ProviderRequest):
        return self.client.chats.create(
            model=request.model,
            config=self._config(request),
            history=self._history(request.history),
        )

    # -- operations -----------------------------------------------------------

    def stream_chat(self, request: ProviderRequest) -> Iterator[ProviderEvent]:
        logger.debug("request_id=%s gemini stream_chat model=%s", request.request_id, request.model)
        try:
            chat_session = self._chat_session(request)
            for chunk in chat_session.send_message_stream(request.user_parts):
                yield _event_from_chunk(chunk, request.enable_web_search)
        except Exception as exc:
            _reraise(exc)

    def chat(self, request: ProviderRequest) -> ProviderResult:
        logger.debug("request_id=%s gemini chat model=%s", request.request_id, request.model)
        try:
            response = self._chat_session(request).send_message(request.user_parts)
            return _result_from_response(response, request.enable_web_search)
        except Exception as exc:
            _reraise(exc)

    def generate(self, request: ProviderRequest) -> ProviderResult:
        logger.debug("request_id=%s gemini generate model=%s", request.request_id, request.model)
        try:
            response = self.client.models.generate_content(
                model=request.model,
                contents=request.user_parts,
                config=self._config(request),
            )
            return _result_from_response(response, request.enable_web_search)
        except Exception as exc:
            _reraise(exc)
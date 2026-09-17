"""
Kognit Phase 7B-4 - Model Adapter.

Minimal interface (per the approved architecture): generate(request) ->
ModelResponse. One adapter implementation exists today - GeminiAdapter,
wrapping the real production backend.ai_engine.generate_ai_response(...,
return_metadata=True). No provider plugin framework, no registry - a
future second provider implements this same interface independently.

CRITICAL: this module NEVER reimplements prompt construction, retry
policy, or generation logic. It calls the real production function.
Duplicating that logic here would mean the benchmark silently stops
testing what students actually experience the moment the two drift.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from backend import ai_engine
from evaluation import prompt_identity


@dataclass
class ModelRequest:
    """Mirrors generate_ai_response()'s actual current parameters."""
    prompt: str
    mode: str = "direct"
    board: str = "NCTB"
    user_class: Optional[str] = None
    stream: Optional[str] = None
    image_bytes: Optional[bytes] = None
    pdf_context: str = ""
    history: Optional[list] = None


@dataclass
class ModelResponse:
    text: str
    is_error: bool
    generation_failed: bool
    failure_reason: Optional[str]
    resolved_model_version: Optional[str]
    response_id: Optional[str]
    prompt_tokens: Optional[int]
    output_tokens: Optional[int]
    thoughts_tokens: Optional[int]
    cached_tokens: Optional[int]
    total_tokens: Optional[int]
    request_start_ts: float
    ai_call_end_ts: float
    elapsed_seconds: Optional[float]
    model_provider: str
    model_name: str
    prompt_version: str
    prompt_hash: str


class GeminiAdapter:
    """The only ModelAdapter implementation that exists today."""

    model_provider = "google"
    model_name = ai_engine.MODEL_NAME

    def generate(self, request: ModelRequest) -> ModelResponse:
        request_start_ts = time.time()
        try:
            result = ai_engine.generate_ai_response(
                prompt=request.prompt,
                mode=request.mode,
                board=request.board,
                user_class=request.user_class,
                stream=request.stream,
                image_bytes=request.image_bytes,
                pdf_context=request.pdf_context,
                history=request.history,
                return_metadata=True,
            )
        except Exception as exc:  # noqa: BLE001 - a runner-level safety net;
            # generate_ai_response() itself already catches essentially
            # everything internally (see its own Exception handler), but
            # the adapter must never let ANY exception propagate and take
            # down an entire evaluation run over one item (Phase 7B-5
            # "one generation failure must not destroy the complete run").
            ai_call_end_ts = time.time()
            return ModelResponse(
                text="",
                is_error=True,
                generation_failed=True,
                failure_reason=f"{type(exc).__name__}: {exc}",
                resolved_model_version=None,
                response_id=None,
                prompt_tokens=None,
                output_tokens=None,
                thoughts_tokens=None,
                cached_tokens=None,
                total_tokens=None,
                request_start_ts=request_start_ts,
                ai_call_end_ts=ai_call_end_ts,
                elapsed_seconds=None,
                model_provider=self.model_provider,
                model_name=self.model_name,
                prompt_version=prompt_identity.get_chat_prompt_version(),
                prompt_hash=prompt_identity.get_chat_prompt_hash(),
            )

        ai_call_end_ts = time.time()
        usage = result.usage_metadata

        return ModelResponse(
            text=result.text,
            is_error=result.is_error,
            generation_failed=False,  # a real response came back, even if
                                       # it's an error string (e.g. quota
                                       # exhausted) - that's an operational
                                       # failure captured via is_error, not
                                       # a hard adapter-level crash.
            failure_reason=result.text if result.is_error else None,
            resolved_model_version=result.resolved_model_version,
            response_id=result.response_id,
            prompt_tokens=getattr(usage, "prompt_token_count", None) if usage else None,
            output_tokens=getattr(usage, "candidates_token_count", None) if usage else None,
            thoughts_tokens=getattr(usage, "thoughts_token_count", None) if usage else None,
            cached_tokens=getattr(usage, "cached_content_token_count", None) if usage else None,
            total_tokens=getattr(usage, "total_token_count", None) if usage else None,
            request_start_ts=request_start_ts,
            ai_call_end_ts=ai_call_end_ts,
            elapsed_seconds=result.elapsed_seconds,
            model_provider=self.model_provider,
            model_name=self.model_name,
            prompt_version=prompt_identity.get_chat_prompt_version(),
            prompt_hash=prompt_identity.get_chat_prompt_hash(),
        )

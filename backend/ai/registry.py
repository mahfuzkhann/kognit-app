"""
Model registry: the ONLY place that knows which models Kloriz may use, what
they can do, and what they cost.

Everything marked VERIFIED below was read from Google's official Gemini API
documentation on 2026-11-03 (sources in each entry). Nothing here is inferred
from a model's name. Where the documentation does not state something
(e.g. the thinking levels `gemini-3.1-flash-lite` accepts) the registry says
so and the safe behaviour is "do not send the parameter".

Model IDs are configuration: set AI_MODEL_GENERAL / AI_MODEL_ADVANCED to move
to a new ID without a code change. An overridden ID that is not the one whose
metadata/prices were verified gets NO price (cost stays NULL) - the registry
never carries a price across to a different model.
"""
import os
import re
from dataclasses import dataclass, field
from typing import Optional

PROVIDER_GEMINI = "gemini"

# Logical roles the router speaks in. The CEO's two approved models.
ROLE_GENERAL = "general_text"        # Gemini 3.1 Flash-Lite
ROLE_ADVANCED = "advanced_reasoning"  # Gemini 3.5 Flash-Lite

_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")

PRICE_VERSION = "gemini-paid-standard-2026-11-03"
PRICE_SOURCE = "https://ai.google.dev/gemini-api/docs/pricing"


@dataclass(frozen=True)
class PriceSpec:
    """USD per 1,000,000 tokens, Standard (non-batch, non-priority) paid tier.
    Output price INCLUDES thinking tokens (stated on the pricing page)."""
    input_per_m: float            # text / image / video input
    output_per_m: float
    cached_input_per_m: Optional[float]
    version: str = PRICE_VERSION
    verified_on: str = "2026-11-03"
    source_url: str = PRICE_SOURCE
    note: str = ""


@dataclass(frozen=True)
class ModelSpec:
    role: str
    model_id: str                         # the verified API model id
    display_name: str
    provider: str = PROVIDER_GEMINI
    inputs: frozenset = frozenset({"text", "image", "pdf", "audio", "video"})
    outputs: frozenset = frozenset({"text"})
    streaming: bool = True
    structured_output: bool = True
    search_grounding: bool = True
    thinking: bool = True
    # Thinking levels the official docs list for this model. EMPTY means the
    # docs do not enumerate them -> the engine sends no thinking level.
    thinking_levels: tuple = ()
    default_thinking: Optional[str] = None
    context_tokens: int = 1_048_576
    max_output_tokens: int = 65_536
    status: str = "ga"
    verified_on: str = "2026-11-03"
    sources: tuple = ()
    price: Optional[PriceSpec] = None
    notes: str = ""


# --- the two approved models -----------------------------------------------
_GENERAL = ModelSpec(
    role=ROLE_GENERAL,
    model_id="gemini-3.1-flash-lite",
    display_name="Gemini 3.1 Flash-Lite",
    thinking_levels=(),   # official thinking table does not list this model
    default_thinking=None,
    sources=(
        "https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-lite",
        "https://ai.google.dev/gemini-api/docs/thinking",
        PRICE_SOURCE,
    ),
    price=PriceSpec(input_per_m=0.25, output_per_m=1.50, cached_input_per_m=0.025,
                    note="audio input is $0.50/M; Kloriz sends no audio"),
    notes="Stable GA id (May 2026). Inputs: text, image, video, audio, PDF. "
          "Thinking levels are not enumerated in the official table; the "
          "engine therefore sends no thinking level for this model.",
)

_ADVANCED = ModelSpec(
    role=ROLE_ADVANCED,
    model_id="gemini-3.5-flash-lite",
    display_name="Gemini 3.5 Flash-Lite",
    thinking_levels=("minimal", "low", "medium", "high"),
    default_thinking="minimal",
    sources=(
        "https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite",
        "https://ai.google.dev/gemini-api/docs/thinking",
        "https://ai.google.dev/gemini-api/docs/latest-model",
        PRICE_SOURCE,
    ),
    price=PriceSpec(input_per_m=0.30, output_per_m=2.50, cached_input_per_m=0.03,
                    note="context-cache storage is $1.00/M tokens/hour (not modelled)"),
    notes="Stable GA id (July 2026). Removed sampling params: do NOT send "
          "temperature/top_p/top_k. Do not end a request with a model-role turn. "
          "Defaults to MINIMAL thinking.",
)

_VERIFIED = {_GENERAL.role: _GENERAL, _ADVANCED.role: _ADVANCED}
_ENV_FOR_ROLE = {ROLE_GENERAL: "AI_MODEL_GENERAL", ROLE_ADVANCED: "AI_MODEL_ADVANCED"}


def _resolve(spec: ModelSpec) -> ModelSpec:
    override = (os.getenv(_ENV_FOR_ROLE[spec.role]) or "").strip()
    if not override or override == spec.model_id:
        return spec
    if not _MODEL_ID_RE.match(override):
        return spec
    # A different model id: keep structural capabilities the operator is
    # responsible for, drop everything that was verified for the OLD id.
    return ModelSpec(
        role=spec.role, model_id=override, display_name=override,
        thinking_levels=(), default_thinking=None, price=None, sources=(),
        verified_on="unverified",
        notes=f"operator override of {spec.model_id}; capabilities/prices NOT verified",
    )


def get_model(role: str) -> ModelSpec:
    if role not in _VERIFIED:
        raise KeyError(f"unknown model role: {role!r}")
    return _resolve(_VERIFIED[role])


def all_models() -> tuple:
    return tuple(get_model(r) for r in (ROLE_GENERAL, ROLE_ADVANCED))


def role_for_model_id(model_id: Optional[str]) -> Optional[str]:
    for spec in all_models():
        if spec.model_id == model_id:
            return spec.role
    return None


def model_for_id(model_id: Optional[str]) -> Optional[ModelSpec]:
    role = role_for_model_id(model_id)
    return get_model(role) if role else None


def other_role(role: str) -> str:
    return ROLE_ADVANCED if role == ROLE_GENERAL else ROLE_GENERAL


def thinking_level_for(spec: ModelSpec, wanted: Optional[str]) -> Optional[str]:
    """The thinking level to SEND, or None. Only levels the registry knows the
    model accepts are ever sent."""
    if wanted is None or not spec.thinking:
        return None
    return wanted if wanted in spec.thinking_levels else None


def supports(spec: ModelSpec, *, inputs: tuple = (), structured: bool = False,
             search: bool = False) -> dict:
    """Capability check used by the router; returns {capability: bool}."""
    checks = {f"input:{name}": name in spec.inputs for name in inputs}
    if structured:
        checks["structured_output"] = spec.structured_output
    if search:
        checks["search_grounding"] = spec.search_grounding
    checks["streaming"] = spec.streaming
    return checks
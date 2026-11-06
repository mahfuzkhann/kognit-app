"""
Cost accounting from provider-REPORTED token usage and a versioned price table.

Rules (no exceptions):
  * only tokens the provider actually reported are priced; a missing number
    makes the cost NULL, never zero;
  * a model without a verified price (e.g. an operator-overridden id) has a
    NULL cost with a stated reason;
  * output price already includes thinking tokens (official pricing page), so
    thinking tokens are NOT added a second time: billable output =
    output_tokens + thinking_tokens when the provider reports them separately.
  * Google Search grounding is billed per query (5,000 free/month, then
    $14/1,000) - that is NOT token cost and is not estimated here.
"""
from dataclasses import dataclass
from typing import Optional

from backend.ai import registry


@dataclass(frozen=True)
class CostEstimate:
    usd: Optional[float]
    pricing_version: Optional[str]
    basis: str          # "estimated_from_provider_usage" | "unavailable:<reason>"


def _count(value) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if value >= 0 else None


def estimate(model_id: Optional[str], usage) -> CostEstimate:
    spec = registry.model_for_id(model_id)
    if spec is None:
        return CostEstimate(None, None, "unavailable:unknown_model")
    if spec.price is None:
        return CostEstimate(None, None, "unavailable:no_verified_price")
    if usage is None:
        return CostEstimate(None, spec.price.version, "unavailable:no_usage_reported")
    prompt = _count(getattr(usage, "prompt_tokens", None))
    output = _count(getattr(usage, "output_tokens", None))
    if prompt is None or output is None:
        return CostEstimate(None, spec.price.version, "unavailable:incomplete_usage")
    thoughts = _count(getattr(usage, "thoughts_tokens", None)) or 0
    cached = _count(getattr(usage, "cached_tokens", None)) or 0
    cached = min(cached, prompt)
    price = spec.price
    cached_rate = price.cached_input_per_m if price.cached_input_per_m is not None else price.input_per_m
    usd = (
        (prompt - cached) * price.input_per_m
        + cached * cached_rate
        + (output + thoughts) * price.output_per_m
    ) / 1_000_000
    return CostEstimate(round(usd, 8), price.version, "estimated_from_provider_usage")
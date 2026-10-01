"""
Kognit AI provider package.

    from backend.providers import get_provider, ProviderRequest, ProviderError

`get_provider()` returns the process-wide provider, creating the Gemini
provider on first use. `set_provider()` swaps it (tests install a
MockProvider); `set_provider(None)` restores lazy default creation. That is
the whole "factory": no registry, no plugin discovery, no configuration
framework.

The Gemini module (and with it the google-genai SDK) is imported only when the
Gemini provider is actually created, so importing this package - or using
MockProvider - does not require the SDK.
"""

from typing import Optional

from backend.providers.base import (
    FinishReason,
    GroundingResult,
    GroundingSource,
    GroundingSupport,
    Provider,
    ProviderError,
    ProviderErrorKind,
    ProviderEvent,
    ProviderMessage,
    ProviderRequest,
    ProviderResult,
    ProviderUsage,
)

__all__ = [
    "FinishReason",
    "GroundingResult",
    "GroundingSource",
    "GroundingSupport",
    "Provider",
    "ProviderError",
    "ProviderErrorKind",
    "ProviderEvent",
    "ProviderMessage",
    "ProviderRequest",
    "ProviderResult",
    "ProviderUsage",
    "get_provider",
    "set_provider",
]

_provider: Optional[Provider] = None


def get_provider() -> Provider:
    """Return the active provider, creating the Gemini provider if none is set.

    backend/ai_engine.py calls this once at import so a missing GEMINI_API_KEY
    still fails at startup (as the module-level client always did), which also
    means first-use creation never races between request threads.
    """
    global _provider
    if _provider is None:
        from backend.providers.gemini import GeminiProvider
        _provider = GeminiProvider()
    return _provider


def set_provider(provider: Optional[Provider]) -> None:
    """Install `provider` as the active provider (None = recreate the default
    Gemini provider on next get_provider())."""
    global _provider
    _provider = provider
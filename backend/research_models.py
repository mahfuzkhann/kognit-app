"""
Kognit Phase 7C - Normalized research data model.

The rest of Kognit (main.py, the frontend) depends ONLY on the dataclasses
below, never on raw google-genai grounding types directly - this is the
"normalized internal representation" the approved architecture requires,
so switching search providers later never means touching main.py or the
frontend, only normalize_grounding_metadata() here.

GROUNDING STATUS is the honesty-critical field: 'used' is only ever set
when the provider's own response proves search happened
(web_search_queries non-empty). A requested-but-failed/empty research
attempt is 'failed' or 'unavailable', NEVER silently upgraded to 'used' -
this is what prevents Kognit from ever claiming an answer is
web-grounded when it isn't.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

# Only these two schemes are ever considered safe to render as a clickable
# citation URL - explicitly rejects javascript:, data:, file:, and any
# other scheme (Step 8's URL-safety requirement).
_SAFE_URL_SCHEMES = {"http", "https"}


def is_safe_citation_url(url: Optional[str]) -> bool:
    """True only for a well-formed http(s) URL with a real host. Never
    raises - a malformed URL is simply unsafe, not an error to propagate."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme.lower() in _SAFE_URL_SCHEMES and bool(parsed.netloc)


@dataclass
class ResearchSource:
    source_id: str          # stable within one ResearchResult (index-based)
    title: Optional[str]
    url: Optional[str]
    domain: Optional[str]
    provider_reference: Optional[int]  # the provider's own grounding_chunk index, for traceability
    is_url_safe: bool = field(init=False)

    def __post_init__(self):
        self.is_url_safe = is_safe_citation_url(self.url)


@dataclass
class ResearchCitation:
    citation_id: str
    answer_segment: Optional[str]   # the exact text span, when the provider supplies one
    source_ids: list                # ResearchSource.source_id values this segment is backed by
    start_index: Optional[int]
    end_index: Optional[int]
    citation_status: str            # 'cited_with_source' | 'source_unmapped' | 'malformed'


@dataclass
class ResearchResult:
    research_used: bool                  # ground truth: did the provider actually search
    provider: str
    provider_model: str
    search_queries: list
    sources: list                        # list[ResearchSource]
    citations: list                      # list[ResearchCitation]
    grounding_status: str                # 'not_used' | 'used' | 'failed' | 'unavailable'
    failure_reason: Optional[str]
    research_latency_seconds: Optional[float]
    decision_reason: Optional[str]       # from research_decision.ResearchDecision, for traceability
    decision_category: Optional[str]


def _normalize_source(index: int, chunk) -> Optional[ResearchSource]:
    """chunk is a google.genai.types.GroundingChunk. Returns None if the
    chunk has no web sub-object at all (e.g. a maps/retrieved_context
    chunk type this normalizer does not yet support) - never fabricates
    a source from nothing."""
    web = getattr(chunk, "web", None)
    if web is None:
        return None
    return ResearchSource(
        source_id=f"src-{index}",
        title=getattr(web, "title", None),
        url=getattr(web, "uri", None),
        domain=getattr(web, "domain", None),
        provider_reference=index,
    )


def _normalize_citation(index: int, support, sources_by_index: dict) -> ResearchCitation:
    """support is a google.genai.types.GroundingSupport."""
    segment = getattr(support, "segment", None)
    chunk_indices = list(getattr(support, "grounding_chunk_indices", None) or [])
    source_ids = [sources_by_index[i].source_id for i in chunk_indices if i in sources_by_index]

    if not chunk_indices:
        status = "malformed"
    elif not source_ids:
        # The provider referenced chunk indices that didn't normalize to a
        # real source (e.g. a non-web chunk type) - a real, distinct state,
        # never silently treated as "cited".
        status = "source_unmapped"
    else:
        status = "cited_with_source"

    return ResearchCitation(
        citation_id=f"cite-{index}",
        answer_segment=getattr(segment, "text", None) if segment else None,
        source_ids=source_ids,
        start_index=getattr(segment, "start_index", None) if segment else None,
        end_index=getattr(segment, "end_index", None) if segment else None,
        citation_status=status,
    )


def normalize_grounding_metadata(
    grounding_metadata,
    provider: str,
    provider_model: str,
    research_latency_seconds: Optional[float],
    decision_reason: Optional[str],
    decision_category: Optional[str],
) -> ResearchResult:
    """Converts a raw google.genai.types.GroundingMetadata (or None) into
    a ResearchResult. This is the ONE place provider-specific grounding
    structure is read - everything downstream uses ResearchResult only.

    grounding_metadata=None (the tool was enabled but the provider
    returned no grounding_metadata at all) is treated as 'failed', not
    'not_used' - the caller only calls this function when research WAS
    requested, so an absent metadata object here means grounding did not
    come through, not that it was never attempted.
    """
    if grounding_metadata is None:
        return ResearchResult(
            research_used=False, provider=provider, provider_model=provider_model,
            search_queries=[], sources=[], citations=[], grounding_status="failed",
            failure_reason="Provider returned no grounding_metadata for a research-requested call.",
            research_latency_seconds=research_latency_seconds,
            decision_reason=decision_reason, decision_category=decision_category,
        )

    search_queries = list(getattr(grounding_metadata, "web_search_queries", None) or [])
    raw_chunks = list(getattr(grounding_metadata, "grounding_chunks", None) or [])
    raw_supports = list(getattr(grounding_metadata, "grounding_supports", None) or [])

    sources = []
    sources_by_index = {}
    for i, chunk in enumerate(raw_chunks):
        source = _normalize_source(i, chunk)
        if source is not None:
            sources.append(source)
            sources_by_index[i] = source

    citations = [_normalize_citation(i, support, sources_by_index) for i, support in enumerate(raw_supports)]

    if not search_queries:
        # The tool was enabled, the model responded, but it genuinely
        # decided not to search (a real, correct Gemini behavior for a
        # borderline prompt) - this is 'not_used', not 'failed'. Kognit
        # must not claim research happened when the model itself chose
        # not to search.
        return ResearchResult(
            research_used=False, provider=provider, provider_model=provider_model,
            search_queries=[], sources=[], citations=[], grounding_status="not_used",
            failure_reason=None, research_latency_seconds=research_latency_seconds,
            decision_reason=decision_reason, decision_category=decision_category,
        )

    return ResearchResult(
        research_used=True, provider=provider, provider_model=provider_model,
        search_queries=search_queries, sources=sources, citations=citations,
        grounding_status="used", failure_reason=None,
        research_latency_seconds=research_latency_seconds,
        decision_reason=decision_reason, decision_category=decision_category,
    )


def research_result_to_dict(result: ResearchResult) -> dict:
    """JSON-serializable form for the /api/chat response. Only ever
    includes sources whose URL passed is_safe_citation_url - an unsafe
    URL is dropped from the payload entirely, never sent to the frontend
    for it to have to also remember to check."""
    safe_sources = [s for s in result.sources if s.is_url_safe]
    safe_source_ids = {s.source_id for s in safe_sources}
    return {
        "research_used": result.research_used,
        "grounding_status": result.grounding_status,
        "failure_reason": result.failure_reason,
        "search_queries": result.search_queries,
        "sources": [
            {"source_id": s.source_id, "title": s.title, "url": s.url, "domain": s.domain}
            for s in safe_sources
        ],
        "citations": [
            {
                "citation_id": c.citation_id,
                "answer_segment": c.answer_segment,
                "source_ids": [sid for sid in c.source_ids if sid in safe_source_ids],
                "citation_status": c.citation_status if any(sid in safe_source_ids for sid in c.source_ids) or not c.source_ids else "source_unmapped",
            }
            for c in result.citations
        ],
    }

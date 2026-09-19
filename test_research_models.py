"""Phase 7C tests: backend/research_models.py.

Uses REAL google.genai.types objects (GroundingMetadata, GroundingChunk,
etc.) constructed directly - not mocks - since these are plain data
structures the installed SDK provides, and testing the normalizer
against the SDK's actual shape is more meaningful than testing it
against a hand-rolled mock that could silently drift from reality.
"""

from __future__ import annotations

from google.genai import types

from backend.research_models import (
    is_safe_citation_url,
    normalize_grounding_metadata,
    research_result_to_dict,
)


class TestUrlSafety:
    def test_https_url_is_safe(self):
        assert is_safe_citation_url("https://example.com/article") is True

    def test_http_url_is_safe(self):
        assert is_safe_citation_url("http://example.com") is True

    def test_javascript_scheme_is_unsafe(self):
        assert is_safe_citation_url("javascript:alert(1)") is False

    def test_data_scheme_is_unsafe(self):
        assert is_safe_citation_url("data:text/html,<script>alert(1)</script>") is False

    def test_file_scheme_is_unsafe(self):
        assert is_safe_citation_url("file:///etc/passwd") is False

    def test_none_is_unsafe(self):
        assert is_safe_citation_url(None) is False

    def test_empty_string_is_unsafe(self):
        assert is_safe_citation_url("") is False

    def test_malformed_url_is_unsafe(self):
        assert is_safe_citation_url("not a url at all ::::") is False

    def test_scheme_with_no_host_is_unsafe(self):
        assert is_safe_citation_url("https://") is False


class TestNormalizeGroundingMetadata:
    def test_none_metadata_is_failed_not_not_used(self):
        result = normalize_grounding_metadata(
            None, provider="google", provider_model="gemini-3.6-flash",
            research_latency_seconds=1.2, decision_reason="test", decision_category="temporal_signal",
        )
        assert result.grounding_status == "failed"
        assert result.research_used is False
        assert result.failure_reason is not None

    def test_metadata_with_no_search_queries_is_not_used(self):
        metadata = types.GroundingMetadata(web_search_queries=[], grounding_chunks=[], grounding_supports=[])
        result = normalize_grounding_metadata(
            metadata, provider="google", provider_model="gemini-3.6-flash",
            research_latency_seconds=0.9, decision_reason="test", decision_category=None,
        )
        assert result.grounding_status == "not_used"
        assert result.research_used is False
        assert result.failure_reason is None  # not a failure - the model just chose not to search

    def test_full_grounding_response_normalizes_correctly(self):
        chunk0 = types.GroundingChunk(web=types.GroundingChunkWeb(
            title="Bangladesh Bank Official Site", uri="https://www.bb.org.bd/", domain="bb.org.bd",
        ))
        chunk1 = types.GroundingChunk(web=types.GroundingChunkWeb(
            title="Reuters Article", uri="https://reuters.com/article/123", domain="reuters.com",
        ))
        support = types.GroundingSupport(
            segment=types.Segment(start_index=0, end_index=42, text="The current exchange rate is 110 BDT/USD."),
            grounding_chunk_indices=[0, 1],
        )
        metadata = types.GroundingMetadata(
            web_search_queries=["current BDT USD exchange rate"],
            grounding_chunks=[chunk0, chunk1],
            grounding_supports=[support],
        )
        result = normalize_grounding_metadata(
            metadata, provider="google", provider_model="gemini-3.6-flash-001",
            research_latency_seconds=2.1, decision_reason="temporal signal", decision_category="temporal_signal",
        )
        assert result.grounding_status == "used"
        assert result.research_used is True
        assert result.search_queries == ["current BDT USD exchange rate"]
        assert len(result.sources) == 2
        assert result.sources[0].domain == "bb.org.bd"
        assert result.sources[0].is_url_safe is True
        assert len(result.citations) == 1
        assert result.citations[0].citation_status == "cited_with_source"
        assert set(result.citations[0].source_ids) == {"src-0", "src-1"}

    def test_citation_referencing_out_of_range_chunk_is_source_unmapped(self):
        metadata = types.GroundingMetadata(
            web_search_queries=["query"],
            grounding_chunks=[],  # no chunks at all
            grounding_supports=[types.GroundingSupport(
                segment=types.Segment(start_index=0, end_index=10, text="Some claim."),
                grounding_chunk_indices=[0],  # references a chunk that doesn't exist
            )],
        )
        result = normalize_grounding_metadata(
            metadata, provider="google", provider_model="gemini-3.6-flash",
            research_latency_seconds=1.0, decision_reason="r", decision_category=None,
        )
        assert result.citations[0].citation_status == "source_unmapped"
        assert result.citations[0].source_ids == []

    def test_support_with_no_chunk_indices_is_malformed(self):
        metadata = types.GroundingMetadata(
            web_search_queries=["query"],
            grounding_chunks=[types.GroundingChunk(web=types.GroundingChunkWeb(title="X", uri="https://x.com", domain="x.com"))],
            grounding_supports=[types.GroundingSupport(
                segment=types.Segment(start_index=0, end_index=5, text="Claim."),
                grounding_chunk_indices=[],
            )],
        )
        result = normalize_grounding_metadata(
            metadata, provider="google", provider_model="gemini-3.6-flash",
            research_latency_seconds=1.0, decision_reason="r", decision_category=None,
        )
        assert result.citations[0].citation_status == "malformed"

    def test_never_fabricates_a_source_from_a_non_web_chunk(self):
        # A chunk with no .web sub-object (e.g. hypothetically a maps-only
        # chunk) must not become a fabricated web source.
        metadata = types.GroundingMetadata(
            web_search_queries=["query"],
            grounding_chunks=[types.GroundingChunk(web=None)],
            grounding_supports=[],
        )
        result = normalize_grounding_metadata(
            metadata, provider="google", provider_model="gemini-3.6-flash",
            research_latency_seconds=1.0, decision_reason="r", decision_category=None,
        )
        assert result.sources == []


class TestResearchResultToDict:
    def test_unsafe_url_source_is_dropped_from_payload(self):
        chunk_safe = types.GroundingChunk(web=types.GroundingChunkWeb(title="Safe", uri="https://safe.com", domain="safe.com"))
        chunk_unsafe = types.GroundingChunk(web=types.GroundingChunkWeb(title="Unsafe", uri="javascript:alert(1)", domain="evil"))
        metadata = types.GroundingMetadata(
            web_search_queries=["q"], grounding_chunks=[chunk_safe, chunk_unsafe], grounding_supports=[],
        )
        result = normalize_grounding_metadata(
            metadata, provider="google", provider_model="gemini-3.6-flash",
            research_latency_seconds=1.0, decision_reason="r", decision_category=None,
        )
        payload = research_result_to_dict(result)
        urls = [s["url"] for s in payload["sources"]]
        assert "https://safe.com" in urls
        assert "javascript:alert(1)" not in urls
        assert len(payload["sources"]) == 1

    def test_not_used_result_has_empty_sources_and_citations(self):
        metadata = types.GroundingMetadata(web_search_queries=[], grounding_chunks=[], grounding_supports=[])
        result = normalize_grounding_metadata(
            metadata, provider="google", provider_model="gemini-3.6-flash",
            research_latency_seconds=0.5, decision_reason="r", decision_category=None,
        )
        payload = research_result_to_dict(result)
        assert payload["research_used"] is False
        assert payload["sources"] == []
        assert payload["citations"] == []

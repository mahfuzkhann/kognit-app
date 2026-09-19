"""Phase 7C tests: evaluation/research_evaluators.py (pure functions)."""

from __future__ import annotations

from backend.research_models import ResearchResult, ResearchSource, ResearchCitation
from evaluation.research_evaluators import (
    evaluate_research_decision_correctness,
    evaluate_citation_url_validity,
    evaluate_citation_mapping_validity,
    evaluate_citation_coverage,
    evaluate_source_count,
    evaluate_search_used_consistency,
)


def _result(**overrides) -> ResearchResult:
    defaults = dict(
        research_used=True, provider="google", provider_model="gemini-3.6-flash",
        search_queries=["q"], sources=[], citations=[], grounding_status="used",
        failure_reason=None, research_latency_seconds=1.0, decision_reason="r", decision_category=None,
    )
    defaults.update(overrides)
    return ResearchResult(**defaults)


class TestResearchDecisionCorrectness:
    def test_correct_required_decision(self):
        r = evaluate_research_decision_correctness("required", research_requested=True)
        assert r.score == 1.0

    def test_correct_not_required_decision(self):
        r = evaluate_research_decision_correctness("not_required", research_requested=False)
        assert r.score == 1.0

    def test_unnecessary_research_flagged(self):
        r = evaluate_research_decision_correctness("not_required", research_requested=True)
        assert r.score == 0.0
        assert r.failure_flags[0].failure_type == "unnecessary_research"

    def test_missed_research_flagged(self):
        r = evaluate_research_decision_correctness("required", research_requested=False)
        assert r.score == 0.0
        assert r.failure_flags[0].failure_type == "missed_research"
        assert r.failure_flags[0].severity == "high"


class TestCitationUrlValidity:
    def test_all_valid_scores_one(self):
        result = _result(sources=[ResearchSource("src-0", "T", "https://x.com", "x.com", 0)])
        r = evaluate_citation_url_validity(result)
        assert r.score == 1.0

    def test_no_sources_unscoreable(self):
        r = evaluate_citation_url_validity(_result(sources=[]))
        assert r.score is None

    def test_partial_validity_scored_and_flagged(self):
        result = _result(sources=[
            ResearchSource("src-0", "T", "https://x.com", "x.com", 0),
            ResearchSource("src-1", "Bad", "javascript:alert(1)", "evil", 1),
        ])
        r = evaluate_citation_url_validity(result)
        assert r.score == 0.5
        assert r.failure_flags[0].failure_type == "invalid_citation_url"


class TestCitationMappingValidity:
    def test_no_citations_unscoreable(self):
        r = evaluate_citation_mapping_validity(_result(citations=[]))
        assert r.score is None

    def test_all_valid_scores_one(self):
        result = _result(citations=[ResearchCitation("c0", "text", ["src-0"], 0, 10, "cited_with_source")])
        r = evaluate_citation_mapping_validity(result)
        assert r.score == 1.0

    def test_malformed_citation_flagged(self):
        result = _result(citations=[ResearchCitation("c0", None, [], None, None, "malformed")])
        r = evaluate_citation_mapping_validity(result)
        assert r.score == 0.0
        assert any(f.failure_type == "malformed_citation" for f in r.failure_flags)

    def test_source_unmapped_flagged_differently_from_malformed(self):
        result = _result(citations=[ResearchCitation("c0", "text", [], 0, 5, "source_unmapped")])
        r = evaluate_citation_mapping_validity(result)
        assert any(f.failure_type == "citation_extraction_failure" for f in r.failure_flags)


class TestCitationCoverage:
    def test_no_expectations_declared_unscoreable(self):
        r = evaluate_citation_coverage(_result(), citation_expectations=None)
        assert r.score is None

    def test_requires_citation_but_none_present_flagged(self):
        result = _result(citations=[], sources=[ResearchSource("s0", "T", "https://x.com", "x.com", 0)])
        r = evaluate_citation_coverage(result, {"requires_citation": True, "min_sources": 1})
        assert r.score == 0.0
        assert any(f.failure_type == "missing_citation" for f in r.failure_flags)

    def test_meets_expectations_scores_one(self):
        result = _result(
            sources=[ResearchSource("s0", "T", "https://x.com", "x.com", 0)],
            citations=[ResearchCitation("c0", "t", ["s0"], 0, 5, "cited_with_source")],
        )
        r = evaluate_citation_coverage(result, {"requires_citation": True, "min_sources": 1})
        assert r.score == 1.0

    def test_insufficient_source_count_flagged(self):
        result = _result(sources=[ResearchSource("s0", "T", "https://x.com", "x.com", 0)])
        r = evaluate_citation_coverage(result, {"min_sources": 3})
        assert any(f.failure_type == "no_useful_sources" for f in r.failure_flags)


class TestSourceCount:
    def test_not_used_is_unscoreable(self):
        r = evaluate_source_count(_result(research_used=False, grounding_status="not_used"))
        assert r.score is None

    def test_zero_sources_when_used_is_flagged(self):
        r = evaluate_source_count(_result(research_used=True, sources=[]))
        assert r.score == 0.0
        assert r.failure_flags[0].severity == "high"

    def test_sources_present_scores_one(self):
        r = evaluate_source_count(_result(sources=[ResearchSource("s0", "T", "https://x.com", "x.com", 0)]))
        assert r.score == 1.0


class TestSearchUsedConsistency:
    def test_used_true_with_used_status_is_consistent(self):
        r = evaluate_search_used_consistency(True, _result(research_used=True, grounding_status="used"))
        assert r.score == 1.0

    def test_used_false_with_not_used_status_is_consistent(self):
        r = evaluate_search_used_consistency(True, _result(research_used=False, grounding_status="not_used"))
        assert r.score == 1.0

    def test_contradiction_is_flagged_critical(self):
        r = evaluate_search_used_consistency(True, _result(research_used=True, grounding_status="failed"))
        assert r.score == 0.0
        assert r.failure_flags[0].severity == "critical"

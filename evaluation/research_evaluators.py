"""
Kognit Phase 7C - Deterministic research evaluators (Step 13).

Reuses evaluation.evaluators.deterministic's DimensionEvaluation and
FailureFlagDraft dataclasses directly (imported, not redefined) - per
the explicit instruction to reuse Phase 7B evaluation infrastructure
rather than duplicate it. Also imports backend.research_models.
ResearchResult (the same normalized type main.py uses in production) so
these evaluators score the SAME shape of data the live chat endpoint
actually produces, not a parallel research-specific representation.

Deterministic ONLY - source_relevance, claim_grounding, and the semantic
half of citation_correctness/citation_completeness require judgment and
are NOT here (see evaluation/research_judge.py).
"""

from __future__ import annotations

from evaluation.evaluators.deterministic import DimensionEvaluation, FailureFlagDraft
from backend.research_models import ResearchResult, is_safe_citation_url


def evaluate_research_decision_correctness(expected_decision: str, research_requested: bool) -> DimensionEvaluation:
    """expected_decision is the benchmark item's ground truth
    ('required' | 'not_required'). Scores 1.0 for a correct decision,
    0.0 otherwise, with the specific failure type distinguishing
    unnecessary research from missed research (Step 10.1's own two
    named failure modes, not collapsed into one generic 'wrong')."""
    actual_requested = research_requested
    expected_requested = expected_decision == "required"

    if actual_requested == expected_requested:
        return DimensionEvaluation(
            dimension="research_decision_correctness", score=1.0, confidence=1.0, method="deterministic",
            evidence=f"Decision matched expected ({expected_decision}).",
        )
    if actual_requested and not expected_requested:
        return DimensionEvaluation(
            dimension="research_decision_correctness", score=0.0, confidence=1.0, method="deterministic",
            evidence="Research was requested but the benchmark item did not require it (unnecessary research).",
            failure_flags=[FailureFlagDraft(
                dimension="research_decision_correctness", failure_type="unnecessary_research", severity="medium",
            )],
        )
    return DimensionEvaluation(
        dimension="research_decision_correctness", score=0.0, confidence=1.0, method="deterministic",
        evidence="Research was NOT requested but the benchmark item required it (missed research).",
        failure_flags=[FailureFlagDraft(
            dimension="research_decision_correctness", failure_type="missed_research", severity="high",
        )],
    )


def evaluate_citation_url_validity(result: ResearchResult) -> DimensionEvaluation:
    """Fraction of sources whose URL is safe/well-formed. Reuses
    is_safe_citation_url directly - the same check the production
    endpoint already applies before a source ever reaches a student."""
    if not result.sources:
        return DimensionEvaluation(
            dimension="citation_url_validity", score=None, confidence=None, method="deterministic",
            evidence="No sources to evaluate.",
        )
    safe_count = sum(1 for s in result.sources if is_safe_citation_url(s.url))
    total = len(result.sources)
    score = safe_count / total
    flags = []
    if safe_count < total:
        flags.append(FailureFlagDraft(
            dimension="citation_url_validity", failure_type="invalid_citation_url",
            severity="high" if score < 0.5 else "medium",
            explanation=f"{total - safe_count} of {total} source URL(s) were unsafe/malformed.",
        ))
    return DimensionEvaluation(
        dimension="citation_url_validity", score=score, confidence=1.0, method="deterministic",
        evidence=f"{safe_count}/{total} source URLs valid.", failure_flags=flags,
    )


def evaluate_citation_mapping_validity(result: ResearchResult) -> DimensionEvaluation:
    """Fraction of citations that mapped to a real source
    (citation_status == 'cited_with_source'), out of all citations the
    provider actually produced. A citation with no citations at all is
    unscoreable here, not zero - that's citation_completeness's job."""
    if not result.citations:
        return DimensionEvaluation(
            dimension="citation_mapping_validity", score=None, confidence=None, method="deterministic",
            evidence="No citations returned by the provider to evaluate.",
        )
    valid_count = sum(1 for c in result.citations if c.citation_status == "cited_with_source")
    total = len(result.citations)
    score = valid_count / total
    flags = []
    malformed = [c for c in result.citations if c.citation_status == "malformed"]
    unmapped = [c for c in result.citations if c.citation_status == "source_unmapped"]
    if malformed:
        flags.append(FailureFlagDraft(
            dimension="citation_mapping_validity", failure_type="malformed_citation", severity="high",
            explanation=f"{len(malformed)} citation(s) had no chunk indices at all.",
        ))
    if unmapped:
        flags.append(FailureFlagDraft(
            dimension="citation_mapping_validity", failure_type="citation_extraction_failure", severity="medium",
            explanation=f"{len(unmapped)} citation(s) referenced chunk indices that did not map to a real source.",
        ))
    return DimensionEvaluation(
        dimension="citation_mapping_validity", score=score, confidence=1.0, method="deterministic",
        evidence=f"{valid_count}/{total} citations validly mapped to a source.", failure_flags=flags,
    )


def evaluate_citation_coverage(result: ResearchResult, citation_expectations: dict) -> DimensionEvaluation:
    """Checks against the benchmark item's own stated expectations
    (min_sources, requires_citation) - never invents an expectation the
    item didn't declare."""
    if not citation_expectations:
        return DimensionEvaluation(
            dimension="citation_coverage", score=None, confidence=None, method="deterministic",
            evidence="This item declares no citation_expectations to check against.",
        )
    min_sources = citation_expectations.get("min_sources", 0)
    requires_citation = citation_expectations.get("requires_citation", False)

    flags = []
    if requires_citation and not result.citations:
        flags.append(FailureFlagDraft(
            dimension="citation_coverage", failure_type="missing_citation", severity="high",
            explanation="This item requires a citation, but none was produced.",
        ))
    source_count_ok = len(result.sources) >= min_sources
    if not source_count_ok:
        flags.append(FailureFlagDraft(
            dimension="citation_coverage", failure_type="no_useful_sources", severity="medium",
            explanation=f"Expected at least {min_sources} source(s), got {len(result.sources)}.",
        ))

    score = 1.0 if not flags else 0.0
    return DimensionEvaluation(
        dimension="citation_coverage", score=score, confidence=1.0, method="deterministic",
        evidence=f"min_sources={min_sources} (got {len(result.sources)}), requires_citation={requires_citation} (got {len(result.citations)} citations).",
        failure_flags=flags,
    )


def evaluate_source_count(result: ResearchResult) -> DimensionEvaluation:
    """A pure descriptive metric, not a pass/fail check - score is the
    raw count itself is not meaningful as a 0-1 score, so this dimension
    reports via `evidence` and leaves `score` at a simple presence
    indicator (1.0 if any source exists, 0.0 if research was used but
    produced zero sources - a real, checkable signal)."""
    if not result.research_used:
        return DimensionEvaluation(
            dimension="source_count", score=None, confidence=None, method="deterministic",
            evidence="Research was not used for this item.",
        )
    count = len(result.sources)
    flags = []
    if count == 0:
        flags.append(FailureFlagDraft(
            dimension="source_count", failure_type="no_useful_sources", severity="high",
            explanation="Research was used (search queries were issued) but zero sources came back.",
        ))
    return DimensionEvaluation(
        dimension="source_count", score=1.0 if count > 0 else 0.0, confidence=1.0, method="deterministic",
        evidence=f"{count} source(s) returned.", failure_flags=flags,
    )


def evaluate_search_used_consistency(research_requested: bool, result: ResearchResult) -> DimensionEvaluation:
    """Checks the two DISTINCT facts the pipeline records (Step 2's own
    distinction) are not internally contradictory: research_used=True
    must never coexist with grounding_status != 'used', and vice versa -
    this catches a real class of bug (a normalization error), not a
    model-quality issue."""
    consistent = (result.research_used and result.grounding_status == "used") or \
                 (not result.research_used and result.grounding_status in ("not_used", "failed", "unavailable"))
    if consistent:
        return DimensionEvaluation(
            dimension="search_used_consistency", score=1.0, confidence=1.0, method="deterministic",
            evidence=f"research_used={result.research_used} is consistent with grounding_status={result.grounding_status!r}.",
        )
    return DimensionEvaluation(
        dimension="search_used_consistency", score=0.0, confidence=1.0, method="deterministic",
        evidence=f"INCONSISTENT: research_used={result.research_used} but grounding_status={result.grounding_status!r}.",
        failure_flags=[FailureFlagDraft(
            dimension="search_used_consistency", failure_type="malformed_grounding_metadata", severity="critical",
        )],
    )

"""
Kognit Phase 7C - LLM judge for research semantic dimensions (Step 14).

Mirrors evaluation/judge.py's structure exactly (structured output only,
never free-form prose parsing; never fabricates a score on failure) -
see that module's own docstring for the shared rationale. This is a
separate module, not a generalization of judge.py's dimension set,
because the research judge's INPUT SHAPE is genuinely different (a
question + retrieved sources + the answer + citations, not an NCTB
curriculum item + expected_behavior).

Covers exactly the four dimensions Step 14 names as needing calibration:
source_relevance, claim_grounding, citation_correctness (the semantic
half - deterministic citation_url_validity/citation_mapping_validity in
research_evaluators.py cover the mechanical half), citation_completeness.

SAME LIMITATION AS PHASE 7B'S JUDGE, disclosed identically: this has
never been called against a live model in this environment (no usable
GEMINI_API_KEY, and the sandboxed network in this session has no route
to Google's API domains regardless). Tested exclusively against mocked
client responses.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from google import genai
from google.genai import types

from evaluation.evaluators.deterministic import DimensionEvaluation, FailureFlagDraft
from backend.research_models import ResearchResult

_VALID_RESEARCH_JUDGE_DIMENSIONS = {
    "source_relevance", "claim_grounding", "citation_correctness", "citation_completeness",
}

_JUDGE_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "number"},
        "confidence": {"type": "number"},
        "explanation": {"type": "string"},
        "failure_types": {"type": "array", "items": {"type": "string"}},
        "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
    },
    "required": ["score", "confidence", "explanation", "failure_types"],
}

_FAILURE_TYPES_BY_RESEARCH_DIMENSION = {
    "source_relevance": ["irrelevant_source", "no_useful_sources"],
    "claim_grounding": ["unsupported_claim"],
    "citation_correctness": ["unsupported_claim", "malformed_citation"],
    "citation_completeness": ["missing_citation"],
}


@dataclass
class ResearchJudgeConfig:
    judge_model: str
    judge_rubric_id: str
    judge_rubric_version: int
    temperature: float = 0.0


def _build_research_judge_prompt(dimension: str, question_text: str, answer_text: str, result: ResearchResult) -> str:
    rubric_instructions = {
        "source_relevance": (
            "Judge whether the retrieved sources are actually relevant to the question asked. "
            "A source about a different topic, an outdated version of the same fact, or a "
            "tangentially related page should score low even if it's a reputable domain."
        ),
        "claim_grounding": (
            "Judge whether the ANSWER's claims are genuinely supported by the retrieved sources' "
            "content (as best as can be inferred from the source titles/domains provided), not "
            "merely whether a citation exists syntactically."
        ),
        "citation_correctness": (
            "Judge whether each citation actually supports the specific claim it is attached to - "
            "not just whether the source is relevant in general, but whether THIS citation, at "
            "THIS position, backs THIS specific sentence."
        ),
        "citation_completeness": (
            "Judge whether every important externally-sourced factual claim in the answer has a "
            "citation - flag claims that read as current/external information but have no citation "
            "attached at all."
        ),
    }.get(dimension, "Judge this dimension.")

    sources_summary = "\n".join(f"- {s.title} ({s.domain})" for s in result.sources) or "(no sources)"

    return (
        f"You are evaluating the '{dimension}' dimension of a research-grounded AI tutoring answer.\n\n"
        f"Question: {question_text}\n\n"
        f"Retrieved sources:\n{sources_summary}\n\n"
        f"The AI's answer:\n{answer_text}\n\n"
        f"Search queries used: {result.search_queries}\n\n"
        f"Evaluation instructions for '{dimension}':\n{rubric_instructions}\n\n"
        f"Allowed failure_types for this dimension: {_FAILURE_TYPES_BY_RESEARCH_DIMENSION.get(dimension, [])}. "
        f"Only use failure_types from this exact list, or leave the list empty if there is no failure."
    )


def evaluate_with_research_judge(
    client: "genai.Client",
    config: ResearchJudgeConfig,
    dimension: str,
    question_text: str,
    answer_text: str,
    result: ResearchResult,
) -> DimensionEvaluation:
    if dimension not in _VALID_RESEARCH_JUDGE_DIMENSIONS:
        raise ValueError(f"Unsupported research judge dimension: {dimension!r}")

    prompt = _build_research_judge_prompt(dimension, question_text, answer_text, result)

    try:
        response = client.models.generate_content(
            model=config.judge_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=config.temperature,
                response_mime_type="application/json",
                response_schema=_JUDGE_RESPONSE_SCHEMA,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return DimensionEvaluation(
            dimension=dimension, score=None, confidence=None, method="llm_judge",
            evidence=f"Research judge call failed: {type(exc).__name__}: {exc}",
        )

    try:
        parsed = json.loads(response.text)
        score = float(parsed["score"])
        confidence = float(parsed["confidence"])
        explanation = str(parsed["explanation"])
        failure_types = list(parsed.get("failure_types") or [])
        severity = parsed.get("severity") or "medium"
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return DimensionEvaluation(
            dimension=dimension, score=None, confidence=None, method="llm_judge",
            evidence=f"Research judge returned malformed structured output: {type(exc).__name__}: {exc}",
        )

    allowed = set(_FAILURE_TYPES_BY_RESEARCH_DIMENSION.get(dimension, []))
    failure_flags = [
        FailureFlagDraft(dimension=dimension, failure_type=ft, severity=severity, explanation=explanation)
        for ft in failure_types if ft in allowed
    ]

    return DimensionEvaluation(
        dimension=dimension, score=max(0.0, min(1.0, score)), confidence=max(0.0, min(1.0, confidence)),
        method="llm_judge", evidence=explanation, failure_flags=failure_flags,
    )


# ---------------------------------------------------------------------
# Calibration (Step 14) - reuses evaluation.calibration.compute_disagreement_summary
# directly (imported, not reimplemented) since that function is already
# generic (plain counts + mean absolute difference over any paired
# human/judge scores) and has no dependency on Phase 7B's table names.
# Only the actual INSERT statement differs (a different table,
# research_judge_calibrations, in a different database file), which is
# an unavoidable, minimal amount of duplication given the two benchmarks
# are deliberately kept in separate SQLite files.
# ---------------------------------------------------------------------

import uuid as _uuid

from evaluation import db as _evaldb
from evaluation.calibration import CalibrationSample, compute_disagreement_summary


def record_research_calibration(
    conn,
    evaluator_version_id: str,
    judge_model: str,
    judge_rubric_id: str,
    judge_rubric_version: int,
    dimension: str,
    samples: list,
    trust_status: str,
    judge_model_version: Optional[str] = None,
    calibration_run_id: Optional[str] = None,
) -> str:
    if dimension not in _VALID_RESEARCH_JUDGE_DIMENSIONS:
        raise ValueError(f"Unsupported research judge dimension: {dimension!r}")
    if trust_status not in ("uncalibrated", "trusted", "disputed"):
        raise ValueError(f"Invalid trust_status: {trust_status!r}")

    disagreement = compute_disagreement_summary(samples)
    calibration_id = str(_uuid.uuid4())

    conn.execute(
        """
        INSERT INTO research_judge_calibrations
            (calibration_id, evaluator_version_id, judge_model, judge_model_version,
             judge_rubric_id, judge_rubric_version, dimension, calibration_run_id,
             sample_item_version_ids_json, human_reference_scores_json,
             judge_scores_json, disagreement_summary_json, trust_status, calibrated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            calibration_id, evaluator_version_id, judge_model, judge_model_version,
            judge_rubric_id, judge_rubric_version, dimension, calibration_run_id,
            json.dumps([s.item_version_id for s in samples]),
            json.dumps({s.item_version_id: s.human_score for s in samples}),
            json.dumps({s.item_version_id: s.judge_score for s in samples}),
            json.dumps(disagreement), trust_status, _evaldb.utc_now_iso(),
        ),
    )
    conn.commit()
    return calibration_id


def get_research_judge_trust_status(conn, evaluator_version_id: str, dimension: str) -> str:
    row = conn.execute(
        """
        SELECT trust_status FROM research_judge_calibrations
        WHERE evaluator_version_id = ? AND dimension = ?
        ORDER BY calibrated_at DESC LIMIT 1
        """,
        (evaluator_version_id, dimension),
    ).fetchone()
    return row["trust_status"] if row is not None else "uncalibrated"

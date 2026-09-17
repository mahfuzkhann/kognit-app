"""
Kognit Phase 7B-7 - LLM Judge.

Used for dimensions deterministic evaluators cannot reliably score:
reasoning, curriculum_alignment, and language naturalness (as distinct
from the deterministic script_correctness check in evaluators/deterministic.py).

STRUCTURED OUTPUT ONLY - the judge never returns free-form prose that
this module then tries to parse with regex. A JSON schema is passed via
GenerateContentConfig.response_schema, and the SDK guarantees the
response conforms to it.

CRITICAL LIMITATION, disclosed here and required to be surfaced in every
report (per the approved architecture, Step 2 Section 21 / master-prompt
Section 12): a judge's trust_status is 'uncalibrated' until a real
calibration run has been performed against human reference scores (see
evaluation/calibration.py). This module does NOT claim reliability for
any judge configuration - that claim can only come from calibration data
that does not yet exist in this environment (no live GEMINI_API_KEY was
available during Phase 7B implementation - see the final implementation
report).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from google import genai
from google.genai import types

from evaluation.evaluators.deterministic import DimensionEvaluation, FailureFlagDraft

_VALID_DIMENSIONS_FOR_JUDGE = {"correctness", "curriculum_alignment", "reasoning", "language"}

_JUDGE_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "number", "description": "0.0 (fails this dimension entirely) to 1.0 (fully meets it)."},
        "confidence": {"type": "number", "description": "The judge's own confidence in this score, 0.0 to 1.0."},
        "explanation": {"type": "string", "description": "A brief, specific justification for the score."},
        "failure_types": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Zero or more failure type identifiers that apply, from the provided taxonomy.",
        },
        "severity": {
            "type": "string",
            "enum": ["low", "medium", "high", "critical"],
            "description": "Severity of the most serious failure found, if any; ignored if failure_types is empty.",
        },
    },
    "required": ["score", "confidence", "explanation", "failure_types"],
}

# Compact failure taxonomy per dimension - matches the schema's own
# failure_flags.failure_type CHECK constraint exactly, so a judge can
# never emit a failure_type the database would reject.
_FAILURE_TYPES_BY_DIMENSION = {
    "correctness": ["factual_error", "numerical_error", "reasoning_error", "unsupported_claim"],
    "curriculum_alignment": ["curriculum_mismatch"],
    "reasoning": ["reasoning_error", "unsupported_claim"],
    "language": ["language_issue"],
}


@dataclass
class JudgeConfig:
    judge_model: str
    judge_rubric_id: str
    judge_rubric_version: int
    temperature: float = 0.0  # deterministic-as-possible judging, per the
                               # architecture's "keep it minimal" instruction -
                               # no reason to introduce judge-side randomness
                               # deliberately.


def _build_judge_prompt(dimension: str, question_text: str, expected_behavior: dict,
                          curriculum_ref: dict, mode: str, answer_text: str) -> str:
    """Builds the judge's input prompt. This is the JUDGE's prompt, not
    Kognit's production system prompt - it is never hashed as
    prompt_hash (that field always refers to the production chat prompt
    being evaluated, not the evaluator's own prompt) and is versioned
    separately via JudgeConfig.judge_rubric_id/judge_rubric_version.
    """
    allowed_failure_types = _FAILURE_TYPES_BY_DIMENSION.get(dimension, [])
    rubric_instructions = {
        "correctness": (
            "Judge whether the FINAL ANSWER and the reasoning that produced it are "
            "factually/mathematically/scientifically correct. Do not reward verbosity."
        ),
        "curriculum_alignment": (
            "Judge whether the answer is appropriate for this student's class, subject, "
            "and topic - not merely factually correct. A correct answer that uses methods "
            "far above the stated class level, or that ignores the stated curriculum scope, "
            "should NOT score highly here even if Correctness is separately high."
        ),
        "reasoning": (
            "Judge logical validity, step correctness, assumption validity, and internal "
            "consistency. Penalize unnecessary length and unsupported elaboration exactly as "
            "much as you credit genuine completeness - a short, fully correct chain of "
            "reasoning should score AT LEAST as well as a long one with the same conclusion."
        ),
        "language": (
            "Judge whether the answer reads as natural, student-appropriate language in the "
            "requested output language - not merely grammatically parseable. Flag anything "
            "that reads as a stiff, word-for-word translation."
        ),
    }.get(dimension, "Judge this dimension of the answer.")

    return (
        f"You are evaluating one dimension ({dimension}) of an AI tutoring answer for a "
        f"secondary/higher-secondary student in Bangladesh (NCTB curriculum).\n\n"
        f"Curriculum context: class={curriculum_ref.get('class')}, "
        f"subject={curriculum_ref.get('subject')}, topic(s)={curriculum_ref.get('topics')}.\n"
        f"Mode: {mode}.\n\n"
        f"Question given to the student:\n{question_text}\n\n"
        f"Expected behavior / reference material (for your reference, not to be echoed): "
        f"{json.dumps(expected_behavior)}\n\n"
        f"The AI's actual answer to evaluate:\n{answer_text}\n\n"
        f"Evaluation instructions for the '{dimension}' dimension:\n{rubric_instructions}\n\n"
        f"Allowed failure_types for this dimension: {allowed_failure_types}. "
        f"Only use failure_types from this exact list, or leave the list empty if there is no failure."
    )


def evaluate_with_judge(
    client: "genai.Client",
    config: JudgeConfig,
    dimension: str,
    question_text: str,
    expected_behavior: dict,
    curriculum_ref: dict,
    mode: str,
    answer_text: str,
) -> DimensionEvaluation:
    """Calls the judge model with a structured-output request and returns
    a DimensionEvaluation. Never returns a fabricated score if the judge
    call fails or returns malformed output - returns score=None with the
    failure surfaced in `evidence` instead (the caller/runner decides how
    to record that as an operational matter, this function never
    silently substitutes a guess)."""
    if dimension not in _VALID_DIMENSIONS_FOR_JUDGE:
        raise ValueError(
            f"evaluate_with_judge does not support dimension={dimension!r}; "
            f"supported: {sorted(_VALID_DIMENSIONS_FOR_JUDGE)}"
        )

    prompt = _build_judge_prompt(dimension, question_text, expected_behavior, curriculum_ref, mode, answer_text)

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
    except Exception as exc:  # noqa: BLE001 - a judge-call failure must
        # never crash the evaluation run; it is recorded as an
        # unscoreable result the runner can surface, exactly like a
        # missing reference_answer in the deterministic evaluators.
        return DimensionEvaluation(
            dimension=dimension, score=None, confidence=None, method="llm_judge",
            evidence=f"Judge call failed: {type(exc).__name__}: {exc}",
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
            evidence=f"Judge returned malformed/unparseable structured output: {type(exc).__name__}: {exc}. "
                     f"Raw response text: {getattr(response, 'text', None)!r}",
        )

    allowed = set(_FAILURE_TYPES_BY_DIMENSION.get(dimension, []))
    failure_flags = [
        FailureFlagDraft(dimension=dimension, failure_type=ft, severity=severity, explanation=explanation)
        for ft in failure_types
        if ft in allowed  # never persist a failure_type the DB schema
                           # would reject - a judge hallucinating an
                           # invalid category is dropped, not crashed on.
    ]

    return DimensionEvaluation(
        dimension=dimension,
        score=max(0.0, min(1.0, score)),
        confidence=max(0.0, min(1.0, confidence)),
        method="llm_judge",
        evidence=explanation,
        failure_flags=failure_flags,
    )

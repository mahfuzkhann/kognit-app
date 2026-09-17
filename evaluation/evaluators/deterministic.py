"""
Kognit Phase 7B-6 - Deterministic evaluators.

Each function below is pure: it takes the benchmark item's expected
behavior + the model's answer text, and returns a DimensionEvaluation.
No I/O, no randomness, no dependency on evaluation.db - identical inputs
always produce identical outputs, which is what makes these safe to
trust for the "objectively checkable" slice of the dimension matrix
(Phase 7B Step 2, Section 12: exact_numerical / numerical_tolerance /
mcq are deterministic-eligible; symbolic / proof / long-form explanation
are NOT - those route to the LLM judge in judge.py instead).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class FailureFlagDraft:
    dimension: str
    failure_type: str
    severity: str
    explanation: Optional[str] = None


@dataclass
class DimensionEvaluation:
    dimension: str
    score: Optional[float]          # None only when truly unscoreable
    confidence: Optional[float]
    method: str                      # 'deterministic' for every evaluator in this module
    evidence: Optional[str]
    failure_flags: list = field(default_factory=list)


# ---------------------------------------------------------------------
# Correctness: numerical (exact + tolerance-based)
# ---------------------------------------------------------------------

# Matches signed decimals/integers, including scientific notation
# (e.g. "6", "-3.5", "1.2e3") - deliberately does NOT do string
# similarity matching (explicitly forbidden by the approved architecture:
# "avoid simple string similarity... do not incorrectly mark
# mathematically equivalent answers as wrong").
_NUMBER_PATTERN = re.compile(r"[-+]?\d+\.?\d*(?:[eE][-+]?\d+)?")


def _extract_numbers(text: str) -> list:
    return [float(m) for m in _NUMBER_PATTERN.findall(text)]


def evaluate_numerical_tolerance(expected_behavior: dict, answer_text: str) -> DimensionEvaluation:
    """Correctness check for exact_numerical / numerical_tolerance items.

    expected_behavior must contain 'reference_answer' (a string containing
    the expected number, e.g. "6 N") and optionally 'numerical_tolerance'
    (a fraction, e.g. 0.01 for 1% - defaults to exact match, tolerance=0.0,
    if not specified).

    Looks for ANY number in the answer that falls within tolerance of the
    expected value - not just the last number - since students/models may
    show intermediate work before stating a final answer. This is a
    deliberate, simple heuristic: it does not attempt to identify "the
    final answer" specifically, which is a real limitation, disclosed
    here rather than silently assumed solved.
    """
    reference_answer = expected_behavior.get("reference_answer")
    if not reference_answer:
        return DimensionEvaluation(
            dimension="correctness", score=None, confidence=None,
            method="deterministic",
            evidence="No reference_answer configured for this item - cannot evaluate deterministically.",
        )

    expected_numbers = _extract_numbers(str(reference_answer))
    if not expected_numbers:
        return DimensionEvaluation(
            dimension="correctness", score=None, confidence=None,
            method="deterministic",
            evidence=f"Could not parse a numeric value from reference_answer={reference_answer!r}.",
        )
    expected_value = expected_numbers[0]
    tolerance = float(expected_behavior.get("numerical_tolerance") or 0.0)

    answer_numbers = _extract_numbers(answer_text)
    if not answer_numbers:
        return DimensionEvaluation(
            dimension="correctness", score=0.0, confidence=1.0,
            method="deterministic",
            evidence="No numeric value found anywhere in the answer.",
            failure_flags=[FailureFlagDraft(
                dimension="correctness", failure_type="numerical_error", severity="critical",
                explanation="Expected a numeric answer but none was found in the response.",
            )],
        )

    allowed_delta = abs(expected_value) * tolerance if tolerance > 0 else 0.0
    match_found = any(abs(n - expected_value) <= max(allowed_delta, 1e-9) for n in answer_numbers)

    if match_found:
        return DimensionEvaluation(
            dimension="correctness", score=1.0, confidence=1.0, method="deterministic",
            evidence=f"Expected {expected_value} (tolerance={tolerance}); found a matching value in the answer.",
        )

    closest = min(answer_numbers, key=lambda n: abs(n - expected_value))
    return DimensionEvaluation(
        dimension="correctness", score=0.0, confidence=1.0, method="deterministic",
        evidence=f"Expected {expected_value} (tolerance={tolerance}); closest value found was {closest}.",
        failure_flags=[FailureFlagDraft(
            dimension="correctness", failure_type="numerical_error",
            severity="critical" if abs(closest - expected_value) > abs(expected_value) * 0.5 + 1e-9 else "medium",
            explanation=f"Expected {expected_value}, closest found value was {closest}.",
        )],
    )


# ---------------------------------------------------------------------
# Correctness: MCQ
# ---------------------------------------------------------------------

_MCQ_OPTION_PATTERN_TEMPLATE = r"(?<![A-Za-z]){letter}(?=[).:\s]|$)"


def evaluate_mcq(expected_behavior: dict, answer_text: str) -> DimensionEvaluation:
    """Correctness check for mcq items.

    expected_behavior must contain 'correct_option' as a single letter
    (e.g. "B"). Looks for that letter as a standalone token (not part of
    a longer word), commonly formatted as "B)", "(B)", "B.", "B:", or
    the letter on its own - a simple, explicit heuristic, not an attempt
    at full natural-language answer extraction.
    """
    correct_option = expected_behavior.get("correct_option")
    if not correct_option:
        return DimensionEvaluation(
            dimension="correctness", score=None, confidence=None, method="deterministic",
            evidence="No correct_option configured for this MCQ item.",
        )
    letter = str(correct_option).strip().upper()
    pattern = re.compile(_MCQ_OPTION_PATTERN_TEMPLATE.format(letter=re.escape(letter)))

    found = bool(pattern.search(answer_text.upper()))
    if found:
        return DimensionEvaluation(
            dimension="correctness", score=1.0, confidence=0.9, method="deterministic",
            evidence=f"Found expected option '{letter}' as a standalone token in the answer.",
        )
    return DimensionEvaluation(
        dimension="correctness", score=0.0, confidence=0.9, method="deterministic",
        evidence=f"Expected option '{letter}' was not found as a standalone token in the answer.",
        failure_flags=[FailureFlagDraft(
            dimension="correctness", failure_type="numerical_error", severity="critical",
            explanation=f"Expected MCQ option '{letter}' not detected in the answer text.",
        )],
    )


# ---------------------------------------------------------------------
# Language: script correctness
# ---------------------------------------------------------------------

_BENGALI_RANGE = re.compile(r"[\u0980-\u09FF]")
_LATIN_LETTER = re.compile(r"[A-Za-z]")


def evaluate_script_correctness(requested_output_language: str, answer_text: str) -> DimensionEvaluation:
    """Language sub-check: is the reply's DOMINANT script the one that
    was requested? This is deliberately narrow - it detects script
    (Bengali vs Latin), not grammatical naturalness, which requires
    LLM-judge evaluation (see judge.py) per the approved architecture's
    explicit split between script_correctness (deterministic) and
    naturalness (LLM-judge).
    """
    bengali_count = len(_BENGALI_RANGE.findall(answer_text))
    latin_count = len(_LATIN_LETTER.findall(answer_text))
    total = bengali_count + latin_count

    if total == 0:
        return DimensionEvaluation(
            dimension="language", score=None, confidence=None, method="deterministic",
            evidence="Answer contains no Bengali or Latin script characters to evaluate (e.g. pure numeric/symbolic answer).",
        )

    dominant_is_bengali = bengali_count > latin_count

    if requested_output_language == "en":
        expected_bengali = False
    elif requested_output_language == "bn":
        expected_bengali = True
    else:
        # 'bn-latn' (Banglish) and 'mixed' - a real mix of scripts is
        # expected/acceptable, so script alone cannot fail this check.
        return DimensionEvaluation(
            dimension="language", score=1.0, confidence=0.5, method="deterministic",
            evidence=f"requested_output_language={requested_output_language!r} permits mixed/Latin-Bangla script; no script mismatch is possible to detect deterministically.",
        )

    if dominant_is_bengali == expected_bengali:
        return DimensionEvaluation(
            dimension="language", score=1.0, confidence=0.9, method="deterministic",
            evidence=f"Dominant script matches requested_output_language={requested_output_language!r} "
                     f"(bengali_chars={bengali_count}, latin_chars={latin_count}).",
        )
    return DimensionEvaluation(
        dimension="language", score=0.0, confidence=0.9, method="deterministic",
        evidence=f"Dominant script does NOT match requested_output_language={requested_output_language!r} "
                 f"(bengali_chars={bengali_count}, latin_chars={latin_count}).",
        failure_flags=[FailureFlagDraft(
            dimension="language", failure_type="language_issue", severity="high",
            explanation=f"Expected dominant script for {requested_output_language!r}, got the opposite.",
        )],
    )


# ---------------------------------------------------------------------
# Formatting: raw-output artifact detection
# ---------------------------------------------------------------------

def evaluate_formatting_artifacts(answer_text: str) -> DimensionEvaluation:
    """Formatting check on the RAW model output (not rendered HTML/MathJax
    - see the Step 2 architecture's explicit note that a rendered-output
    checker is real future work, not built in Stage 1).

    Detects: unbalanced $ / $$ math delimiters, and the literal "svg"
    text artifact - a real, previously-documented Kognit-specific
    MathJax-copy failure class (see templates/index.html's own comment
    on this).
    """
    failure_flags = []
    issues = []

    dollar_count = answer_text.count("$")
    if dollar_count % 2 != 0:
        issues.append("Unbalanced '$' math delimiters (odd count).")
        failure_flags.append(FailureFlagDraft(
            dimension="formatting", failure_type="formatting_issue", severity="high",
            explanation="Odd number of '$' characters - a math delimiter is likely unclosed.",
        ))

    if re.search(r"\\text\{svg\}|\bsvg\b", answer_text, re.IGNORECASE):
        issues.append("Literal 'svg' artifact detected (known MathJax-copy failure class).")
        failure_flags.append(FailureFlagDraft(
            dimension="formatting", failure_type="formatting_issue", severity="medium",
            explanation="Literal 'svg' text found in the raw answer - likely a rendering-artifact leak.",
        ))

    if issues:
        return DimensionEvaluation(
            dimension="formatting", score=0.0, confidence=0.8, method="deterministic",
            evidence="; ".join(issues), failure_flags=failure_flags,
        )
    return DimensionEvaluation(
        dimension="formatting", score=1.0, confidence=0.6, method="deterministic",
        evidence="No known raw-output formatting artifacts detected. "
                 "Note: this does NOT verify rendered-output correctness (no browser/MathJax rendering is performed).",
    )


# ---------------------------------------------------------------------
# Operational: generation failure -> a formatting/correctness-independent flag
# ---------------------------------------------------------------------

def evaluate_generation_failure() -> DimensionEvaluation:
    """Used by the runner when the model adapter reports generation_failed
    or an operational is_error - no dimension score is meaningful, only
    an operational failure flag."""
    return DimensionEvaluation(
        dimension="operational", score=None, confidence=None, method="deterministic",
        evidence="Generation failed or returned an operational error - no answer content to evaluate.",
        failure_flags=[FailureFlagDraft(
            dimension="operational", failure_type="generation_failure", severity="critical",
        )],
    )

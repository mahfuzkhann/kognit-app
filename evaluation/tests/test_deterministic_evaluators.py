"""Phase 7B-6 tests: deterministic evaluators (pure functions, no I/O)."""

from __future__ import annotations

from evaluation.evaluators import deterministic as det


class TestNumericalTolerance:
    def test_exact_match_scores_one(self):
        result = det.evaluate_numerical_tolerance(
            {"reference_answer": "6 N", "numerical_tolerance": 0.0},
            "The force is 6 N.",
        )
        assert result.score == 1.0
        assert result.failure_flags == []

    def test_within_tolerance_scores_one(self):
        result = det.evaluate_numerical_tolerance(
            {"reference_answer": "6", "numerical_tolerance": 0.05},  # 5% -> 5.7 to 6.3
            "The answer is approximately 6.2 N.",
        )
        assert result.score == 1.0

    def test_outside_tolerance_scores_zero_with_failure_flag(self):
        result = det.evaluate_numerical_tolerance(
            {"reference_answer": "6", "numerical_tolerance": 0.01},
            "The answer is 9 N.",
        )
        assert result.score == 0.0
        assert len(result.failure_flags) == 1
        assert result.failure_flags[0].failure_type == "numerical_error"

    def test_does_not_use_string_similarity_finds_number_anywhere(self):
        # Explicit requirement: must find the correct number even if it's
        # not the very last token (e.g. shown mid-explanation).
        result = det.evaluate_numerical_tolerance(
            {"reference_answer": "6", "numerical_tolerance": 0.0},
            "Using F=ma, F = 2 * 3 = 6 N. So the block needs a 6 N force applied continuously.",
        )
        assert result.score == 1.0

    def test_no_number_in_answer_scores_zero_critical(self):
        result = det.evaluate_numerical_tolerance(
            {"reference_answer": "6", "numerical_tolerance": 0.0},
            "I'm not sure how to solve this.",
        )
        assert result.score == 0.0
        assert result.failure_flags[0].severity == "critical"

    def test_missing_reference_answer_is_unscoreable_not_zero(self):
        result = det.evaluate_numerical_tolerance({}, "6 N")
        assert result.score is None
        assert result.failure_flags == []

    def test_far_off_answer_is_critical_close_answer_is_medium(self):
        far = det.evaluate_numerical_tolerance(
            {"reference_answer": "100", "numerical_tolerance": 0.01}, "The answer is 1."
        )
        close = det.evaluate_numerical_tolerance(
            {"reference_answer": "100", "numerical_tolerance": 0.01}, "The answer is 96."
        )
        assert far.failure_flags[0].severity == "critical"
        assert close.failure_flags[0].severity == "medium"


class TestMCQ:
    def test_correct_option_detected(self):
        result = det.evaluate_mcq({"correct_option": "B"}, "The correct answer is B) Mitochondria.")
        assert result.score == 1.0

    def test_incorrect_option_scores_zero(self):
        result = det.evaluate_mcq({"correct_option": "B"}, "The correct answer is A) Nucleus.")
        assert result.score == 0.0
        assert result.failure_flags[0].dimension == "correctness"

    def test_does_not_false_match_inside_a_word(self):
        # "B" should not match inside "Because" etc.
        result = det.evaluate_mcq({"correct_option": "B"}, "Because the cell wall is rigid, the answer is A.")
        assert result.score == 0.0

    def test_missing_correct_option_is_unscoreable(self):
        result = det.evaluate_mcq({}, "A")
        assert result.score is None


class TestScriptCorrectness:
    def test_english_requested_and_english_answer_matches(self):
        result = det.evaluate_script_correctness("en", "The force equals mass times acceleration.")
        assert result.score == 1.0

    def test_bangla_requested_but_english_answer_fails(self):
        result = det.evaluate_script_correctness("bn", "The force equals mass times acceleration.")
        assert result.score == 0.0
        assert result.failure_flags[0].failure_type == "language_issue"

    def test_bangla_requested_and_bangla_answer_matches(self):
        result = det.evaluate_script_correctness("bn", "বল সমান ভর গুণ ত্বরণ।")
        assert result.score == 1.0

    def test_mixed_language_is_not_penalized(self):
        result = det.evaluate_script_correctness("mixed", "বল = ma, force calculation.")
        assert result.score == 1.0

    def test_no_scriptable_characters_is_unscoreable(self):
        result = det.evaluate_script_correctness("en", "6.0")
        assert result.score is None


class TestFormattingArtifacts:
    def test_clean_answer_passes(self):
        result = det.evaluate_formatting_artifacts("The force is $F = 6\\ \\text{N}$.")
        assert result.score == 1.0
        assert result.failure_flags == []

    def test_unbalanced_dollar_signs_detected(self):
        result = det.evaluate_formatting_artifacts("The force is $F = 6 N.")
        assert result.score == 0.0
        assert any(f.failure_type == "formatting_issue" for f in result.failure_flags)

    def test_literal_svg_artifact_detected(self):
        result = det.evaluate_formatting_artifacts("The equation renders as svg in your browser.")
        assert result.score == 0.0

    def test_multiple_issues_both_flagged(self):
        result = det.evaluate_formatting_artifacts("$F = 6 N and it shows as svg")
        assert len(result.failure_flags) == 2


class TestGenerationFailure:
    def test_generation_failure_has_no_score_and_operational_flag(self):
        result = det.evaluate_generation_failure()
        assert result.score is None
        assert result.dimension == "operational"
        assert result.failure_flags[0].failure_type == "generation_failure"
        assert result.failure_flags[0].severity == "critical"

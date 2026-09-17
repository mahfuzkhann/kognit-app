"""
Phase 7B-7 tests: LLM judge structured-output handling and calibration
recording.

IMPORTANT DISCLOSURE (also stated in the final implementation report):
no live GEMINI_API_KEY was available in this environment, so the judge
is tested exclusively against mocked client responses here. No real
judge call has ever been made against a live model in this session, and
no calibration data from real human review exists. Every judge
trust_status in this test suite (and in any data this session produces)
is either explicitly 'uncalibrated' or a synthetic value used only to
test that the recording mechanism itself works correctly - never
presented as evidence the judge is actually reliable.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from evaluation import calibration, db as evaldb
from evaluation.judge import JudgeConfig, evaluate_with_judge


@pytest.fixture()
def conn(tmp_path):
    db_path = tmp_path / "judge_test.sqlite"
    connection = evaldb.get_connection(db_path)
    evaldb.initialize_schema(connection)
    yield connection
    connection.close()


class TestJudgeStructuredOutput:
    def _mock_client(self, response_json: dict | None, raise_error: Exception | None = None):
        client = MagicMock()
        if raise_error is not None:
            client.models.generate_content.side_effect = raise_error
        else:
            response = MagicMock()
            response.text = json.dumps(response_json)
            client.models.generate_content.return_value = response
        return client

    def test_well_formed_judge_response_is_parsed_correctly(self):
        client = self._mock_client({
            "score": 0.8, "confidence": 0.9,
            "explanation": "Reasoning is mostly sound but skips a step.",
            "failure_types": ["reasoning_error"], "severity": "medium",
        })
        config = JudgeConfig(judge_model="gemini-3.6-flash", judge_rubric_id="reasoning-v1", judge_rubric_version=1)
        result = evaluate_with_judge(
            client, config, "reasoning",
            question_text="Solve for x: 2x=6", expected_behavior={"reference_answer": "3"},
            curriculum_ref={"class": "Class 10", "subject": "Math", "topics": ["Algebra"]},
            mode="direct", answer_text="x = 3 because we divide both sides.",
        )
        assert result.score == 0.8
        assert result.confidence == 0.9
        assert result.method == "llm_judge"
        assert len(result.failure_flags) == 1
        assert result.failure_flags[0].failure_type == "reasoning_error"
        assert result.failure_flags[0].severity == "medium"

    def test_score_and_confidence_are_clamped_to_0_1(self):
        client = self._mock_client({
            "score": 1.5, "confidence": -0.2, "explanation": "x", "failure_types": [],
        })
        config = JudgeConfig(judge_model="gemini-3.6-flash", judge_rubric_id="r", judge_rubric_version=1)
        result = evaluate_with_judge(
            client, config, "reasoning", "q", {}, {}, "direct", "answer",
        )
        assert result.score == 1.0
        assert result.confidence == 0.0

    def test_invalid_failure_type_is_dropped_not_crashed_on(self):
        client = self._mock_client({
            "score": 0.5, "confidence": 0.5, "explanation": "x",
            "failure_types": ["not_a_real_failure_type"], "severity": "low",
        })
        config = JudgeConfig(judge_model="gemini-3.6-flash", judge_rubric_id="r", judge_rubric_version=1)
        result = evaluate_with_judge(
            client, config, "reasoning", "q", {}, {}, "direct", "answer",
        )
        assert result.failure_flags == []  # dropped, not raised

    def test_malformed_json_response_returns_unscoreable_not_a_crash(self):
        client = self._mock_client(None)
        client.models.generate_content.return_value.text = "not valid json {{{"
        config = JudgeConfig(judge_model="gemini-3.6-flash", judge_rubric_id="r", judge_rubric_version=1)
        result = evaluate_with_judge(
            client, config, "reasoning", "q", {}, {}, "direct", "answer",
        )
        assert result.score is None
        assert "malformed" in result.evidence.lower()

    def test_judge_call_exception_returns_unscoreable_not_a_crash(self):
        client = self._mock_client(None, raise_error=RuntimeError("network error"))
        config = JudgeConfig(judge_model="gemini-3.6-flash", judge_rubric_id="r", judge_rubric_version=1)
        result = evaluate_with_judge(
            client, config, "reasoning", "q", {}, {}, "direct", "answer",
        )
        assert result.score is None
        assert "network error" in result.evidence

    def test_missing_required_field_returns_unscoreable(self):
        client = self._mock_client({"score": 0.5})  # missing confidence/explanation
        config = JudgeConfig(judge_model="gemini-3.6-flash", judge_rubric_id="r", judge_rubric_version=1)
        result = evaluate_with_judge(
            client, config, "reasoning", "q", {}, {}, "direct", "answer",
        )
        assert result.score is None

    def test_unsupported_dimension_raises(self):
        client = self._mock_client({})
        config = JudgeConfig(judge_model="gemini-3.6-flash", judge_rubric_id="r", judge_rubric_version=1)
        with pytest.raises(ValueError):
            evaluate_with_judge(client, config, "latency", "q", {}, {}, "direct", "answer")


class TestCalibration:
    def test_disagreement_summary_plain_counts(self):
        samples = [
            calibration.CalibrationSample("v1", human_score=0.9, judge_score=0.85),  # agree (<=0.1)
            calibration.CalibrationSample("v2", human_score=0.9, judge_score=0.3),   # disagree
        ]
        summary = calibration.compute_disagreement_summary(samples)
        assert summary["agree_count"] == 1
        assert summary["disagree_count"] == 1
        assert summary["mean_absolute_difference"] == pytest.approx((0.05 + 0.6) / 2)

    def test_empty_samples_returns_none_mean(self):
        summary = calibration.compute_disagreement_summary([])
        assert summary["mean_absolute_difference"] is None

    def test_record_and_read_back_trust_status(self, conn):
        conn.execute(
            "INSERT INTO evaluator_versions (evaluator_version_id, evaluator_name, version_label, created_at) VALUES (?, 'llm_judge', 'v1', ?)",
            ("ev1", evaldb.utc_now_iso()),
        )
        conn.commit()
        assert calibration.get_current_trust_status(conn, "ev1", "reasoning") == "uncalibrated"

        samples = [calibration.CalibrationSample("iv1", 0.9, 0.2)]
        calibration.record_calibration(
            conn, "ev1", "gemini-3.6-flash", "reasoning-rubric", 1, "reasoning",
            samples, trust_status="disputed",
        )
        assert calibration.get_current_trust_status(conn, "ev1", "reasoning") == "disputed"

    def test_trust_is_per_dimension_not_global(self, conn):
        conn.execute(
            "INSERT INTO evaluator_versions (evaluator_version_id, evaluator_name, version_label, created_at) VALUES (?, 'llm_judge', 'v1', ?)",
            ("ev1", evaldb.utc_now_iso()),
        )
        conn.commit()
        calibration.record_calibration(
            conn, "ev1", "gemini-3.6-flash", "r1", 1, "language",
            [calibration.CalibrationSample("iv1", 0.9, 0.88)], trust_status="trusted",
        )
        calibration.record_calibration(
            conn, "ev1", "gemini-3.6-flash", "r2", 1, "curriculum_alignment",
            [calibration.CalibrationSample("iv2", 0.9, 0.1)], trust_status="disputed",
        )
        assert calibration.get_current_trust_status(conn, "ev1", "language") == "trusted"
        assert calibration.get_current_trust_status(conn, "ev1", "curriculum_alignment") == "disputed"
        assert calibration.get_current_trust_status(conn, "ev1", "reasoning") == "uncalibrated"

    def test_invalid_trust_status_rejected(self, conn):
        conn.execute(
            "INSERT INTO evaluator_versions (evaluator_version_id, evaluator_name, version_label, created_at) VALUES (?, 'llm_judge', 'v1', ?)",
            ("ev1", evaldb.utc_now_iso()),
        )
        conn.commit()
        with pytest.raises(ValueError):
            calibration.record_calibration(
                conn, "ev1", "gemini-3.6-flash", "r", 1, "reasoning", [], trust_status="very_trusted",
            )

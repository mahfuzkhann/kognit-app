"""
Phase 7C tests: evaluation/research_judge.py.

DISCLOSURE (also in the module docstring and final report): no live
Gemini API call has been made by this module in this session - no
usable GEMINI_API_KEY existed, and this sandbox's network has no route
to Google's API domains regardless. Every test here uses a mocked
client response.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from backend.research_models import ResearchResult, ResearchSource
from evaluation.research_judge import ResearchJudgeConfig, evaluate_with_research_judge


def _result(**overrides) -> ResearchResult:
    defaults = dict(
        research_used=True, provider="google", provider_model="gemini-3.6-flash",
        search_queries=["current BDT rate"],
        sources=[ResearchSource("s0", "Bangladesh Bank", "https://bb.org.bd", "bb.org.bd", 0)],
        citations=[], grounding_status="used", failure_reason=None,
        research_latency_seconds=1.0, decision_reason="r", decision_category=None,
    )
    defaults.update(overrides)
    return ResearchResult(**defaults)


def _mock_client(response_json=None, raw_text=None, raise_error=None):
    client = MagicMock()
    if raise_error is not None:
        client.models.generate_content.side_effect = raise_error
    else:
        response = MagicMock()
        response.text = raw_text if raw_text is not None else json.dumps(response_json)
        client.models.generate_content.return_value = response
    return client


class TestResearchJudge:
    def test_well_formed_response_parsed(self):
        client = _mock_client({
            "score": 0.9, "confidence": 0.85, "explanation": "Source is directly relevant.",
            "failure_types": [], "severity": "low",
        })
        config = ResearchJudgeConfig(judge_model="gemini-3.6-flash", judge_rubric_id="r1", judge_rubric_version=1)
        r = evaluate_with_research_judge(client, config, "source_relevance", "q", "answer", _result())
        assert r.score == 0.9
        assert r.method == "llm_judge"
        assert r.failure_flags == []

    def test_failure_type_recorded_when_flagged(self):
        client = _mock_client({
            "score": 0.2, "confidence": 0.7, "explanation": "Source is unrelated to the question.",
            "failure_types": ["irrelevant_source"], "severity": "high",
        })
        config = ResearchJudgeConfig(judge_model="gemini-3.6-flash", judge_rubric_id="r1", judge_rubric_version=1)
        r = evaluate_with_research_judge(client, config, "source_relevance", "q", "answer", _result())
        assert r.failure_flags[0].failure_type == "irrelevant_source"

    def test_failure_type_not_allowed_for_dimension_is_dropped(self):
        client = _mock_client({
            "score": 0.5, "confidence": 0.5, "explanation": "x",
            "failure_types": ["missing_citation"],  # not in source_relevance's allowed list
            "severity": "medium",
        })
        config = ResearchJudgeConfig(judge_model="gemini-3.6-flash", judge_rubric_id="r1", judge_rubric_version=1)
        r = evaluate_with_research_judge(client, config, "source_relevance", "q", "answer", _result())
        assert r.failure_flags == []

    def test_malformed_json_is_unscoreable_not_a_crash(self):
        client = _mock_client(raw_text="not valid json {{{")
        config = ResearchJudgeConfig(judge_model="gemini-3.6-flash", judge_rubric_id="r1", judge_rubric_version=1)
        r = evaluate_with_research_judge(client, config, "claim_grounding", "q", "answer", _result())
        assert r.score is None

    def test_client_exception_is_unscoreable_not_a_crash(self):
        client = _mock_client(raise_error=RuntimeError("network error"))
        config = ResearchJudgeConfig(judge_model="gemini-3.6-flash", judge_rubric_id="r1", judge_rubric_version=1)
        r = evaluate_with_research_judge(client, config, "claim_grounding", "q", "answer", _result())
        assert r.score is None
        assert "network error" in r.evidence

    def test_unsupported_dimension_raises(self):
        client = _mock_client({})
        config = ResearchJudgeConfig(judge_model="gemini-3.6-flash", judge_rubric_id="r1", judge_rubric_version=1)
        with pytest.raises(ValueError):
            evaluate_with_research_judge(client, config, "correctness", "q", "answer", _result())

    def test_score_and_confidence_clamped(self):
        client = _mock_client({"score": 2.0, "confidence": -1.0, "explanation": "x", "failure_types": []})
        config = ResearchJudgeConfig(judge_model="gemini-3.6-flash", judge_rubric_id="r1", judge_rubric_version=1)
        r = evaluate_with_research_judge(client, config, "citation_completeness", "q", "answer", _result())
        assert r.score == 1.0
        assert r.confidence == 0.0


class TestResearchCalibration:
    @pytest.fixture()
    def conn(self, tmp_path):
        from evaluation import db as evaldb
        db_path = tmp_path / "research_calibration_test.sqlite"
        connection = evaldb.get_connection(db_path)
        evaldb.initialize_schema(connection, schema_path="evaluation/schema/research_schema.sql", schema_version=evaldb.RESEARCH_SCHEMA_VERSION)
        connection.execute(
            "INSERT INTO research_evaluator_versions (evaluator_version_id, evaluator_name, version_label, created_at) VALUES ('ev1', 'research_judge', 'v1', ?)",
            (evaldb.utc_now_iso(),),
        )
        connection.commit()
        yield connection
        connection.close()

    def test_record_and_read_back_trust_status(self, conn):
        from evaluation.calibration import CalibrationSample
        from evaluation.research_judge import record_research_calibration, get_research_judge_trust_status

        assert get_research_judge_trust_status(conn, "ev1", "source_relevance") == "uncalibrated"
        record_research_calibration(
            conn, "ev1", "gemini-3.6-flash", "rubric-1", 1, "source_relevance",
            [CalibrationSample("iv1", 0.9, 0.85)], trust_status="trusted",
        )
        assert get_research_judge_trust_status(conn, "ev1", "source_relevance") == "trusted"

    def test_trust_is_per_dimension(self, conn):
        from evaluation.calibration import CalibrationSample
        from evaluation.research_judge import record_research_calibration, get_research_judge_trust_status

        record_research_calibration(conn, "ev1", "gemini-3.6-flash", "r", 1, "source_relevance", [CalibrationSample("i1", 0.9, 0.9)], trust_status="trusted")
        record_research_calibration(conn, "ev1", "gemini-3.6-flash", "r", 1, "claim_grounding", [CalibrationSample("i2", 0.9, 0.1)], trust_status="disputed")
        assert get_research_judge_trust_status(conn, "ev1", "source_relevance") == "trusted"
        assert get_research_judge_trust_status(conn, "ev1", "claim_grounding") == "disputed"
        assert get_research_judge_trust_status(conn, "ev1", "citation_correctness") == "uncalibrated"

    def test_invalid_dimension_rejected(self, conn):
        from evaluation.calibration import CalibrationSample
        from evaluation.research_judge import record_research_calibration

        with pytest.raises(ValueError):
            record_research_calibration(conn, "ev1", "gemini-3.6-flash", "r", 1, "correctness", [], trust_status="trusted")

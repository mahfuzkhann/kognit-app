"""Phase 7B-9 / 7B-10 tests: report generation and regression comparison,
against two real evaluation runs (mocked Gemini calls, real SQLite)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from backend import ai_engine
from evaluation import authoring, db as evaldb
from evaluation.regression import compare_runs
from evaluation.report import generate_report, render_markdown
from evaluation.runner import RunConfig, run_evaluation


@pytest.fixture()
def conn(tmp_path):
    db_path = tmp_path / "report_test.sqlite"
    connection = evaldb.get_connection(db_path)
    evaldb.initialize_schema(connection)
    yield connection
    connection.close()


@pytest.fixture()
def evaluator_version_id(conn):
    conn.execute(
        "INSERT INTO evaluator_versions (evaluator_version_id, evaluator_name, version_label, created_at) VALUES ('ev1', 'deterministic_v1', 'v1', ?)",
        (evaldb.utc_now_iso(),),
    )
    conn.commit()
    return "ev1"


def _numerical_item_draft(**overrides):
    defaults = dict(
        question_type="numerical_tolerance", curriculum_class="Class 10",
        curriculum_subject="Physics", curriculum_topics=["Force & Motion"],
        source_type="original", author="mahfuz",
        question_text="A 2kg block accelerates at 3 m/s^2. Find the force.",
        input_language="en", requested_output_language="en", mode="direct",
        expected_behavior={"reference_answer": "6", "numerical_tolerance": 0.01},
        difficulty="easy", mode_expected_behavior={"expects_complete_final_answer": True},
    )
    defaults.update(overrides)
    return authoring.ItemDraft(**defaults)


def _run_with_answer_text(conn, evaluator_version_id, answer_text: str, dataset_version_id: str = None) -> tuple:
    if dataset_version_id is None:
        _, item_version_id = authoring.create_item_with_first_version(conn, _numerical_item_draft())
        dataset_id = authoring.create_dataset(conn, "Report Test Dataset")
        dataset_version_id = authoring.cut_dataset_version(conn, dataset_id, [item_version_id])
    else:
        item_version_id = conn.execute(
            "SELECT item_version_id FROM dataset_version_items WHERE dataset_version_id = ?",
            (dataset_version_id,),
        ).fetchone()["item_version_id"]

    response = MagicMock()
    response.text = answer_text
    response.model_version = "gemini-3.6-flash-001"
    response.response_id = "resp-1"
    usage = MagicMock()
    usage.prompt_token_count = 40
    usage.candidates_token_count = 8
    usage.thoughts_token_count = 0
    usage.total_token_count = 48
    del usage.cached_content_token_count
    response.usage_metadata = usage

    with patch.object(ai_engine, "_client") as client:
        chat = MagicMock()
        chat.send_message.return_value = response
        client.chats.create.return_value = chat
        run_id = run_evaluation(conn, RunConfig(
            dataset_version_id=dataset_version_id, evaluator_version_id=evaluator_version_id,
        ))
    return run_id, item_version_id, dataset_version_id


def _two_runs_same_item(conn, evaluator_version_id, answer_a: str, answer_b: str) -> tuple:
    """Runs the SAME item/dataset version twice with two different mocked
    answers - the correct setup for testing regression comparison, which
    only compares items common to both runs."""
    run_a, item_version_id, dataset_version_id = _run_with_answer_text(conn, evaluator_version_id, answer_a)
    run_b, _, _ = _run_with_answer_text(conn, evaluator_version_id, answer_b, dataset_version_id=dataset_version_id)
    return run_a, run_b, item_version_id


class TestReportGeneration:
    def test_report_contains_all_required_sections(self, conn, evaluator_version_id):
        run_id, _, _ = _run_with_answer_text(conn, evaluator_version_id, "The force is 6 N.")
        report = generate_report(conn, run_id)

        assert report["run_identity"]["run_id"] == run_id
        assert report["item_counts"]["total_items"] == 1
        assert report["item_counts"]["completed_items"] == 1
        assert "correctness" in report["dimension_scores"]
        assert report["dimension_scores"]["correctness"]["mean_score"] == 1.0
        assert "subject_breakdown" in report
        assert "Physics" in report["subject_breakdown"]
        assert report["usage_summary"]["total_tokens"] == 48
        assert report["usage_summary"]["calculated_cost"] is None  # never invented

    def test_report_never_produces_a_single_overall_score(self, conn, evaluator_version_id):
        run_id, _, _ = _run_with_answer_text(conn, evaluator_version_id, "The force is 6 N.")
        report = generate_report(conn, run_id)
        assert "overall_score" not in report
        assert "quality_score" not in report
        # every top-level key should be a breakdown, not a single blended number
        assert isinstance(report["dimension_scores"], dict)

    def test_render_markdown_produces_readable_output(self, conn, evaluator_version_id):
        run_id, _, _ = _run_with_answer_text(conn, evaluator_version_id, "The force is 6 N.")
        report = generate_report(conn, run_id)
        markdown = render_markdown(report)
        assert "# Evaluation Run Report" in markdown
        assert "correctness" in markdown
        assert "never invented" in markdown or "never combined" in markdown

    def test_report_unknown_run_raises(self, conn):
        with pytest.raises(ValueError):
            generate_report(conn, "does-not-exist")


class TestRegressionComparison:
    def test_improvement_is_detected(self, conn, evaluator_version_id):
        run_a, run_b, _ = _two_runs_same_item(conn, evaluator_version_id, "The force is 100 N.", "The force is 6 N.")

        comparison = compare_runs(conn, run_a, run_b)
        assert comparison["dimension_deltas"]["correctness"]["improved"] == 1
        assert comparison["dimension_deltas"]["correctness"]["regressed"] == 0
        assert comparison["summary_counts"]["improved"] == 1

    def test_regression_is_detected(self, conn, evaluator_version_id):
        run_a, run_b, _ = _two_runs_same_item(conn, evaluator_version_id, "The force is 6 N.", "The force is 100 N.")

        comparison = compare_runs(conn, run_a, run_b)
        assert comparison["dimension_deltas"]["correctness"]["regressed"] == 1

    def test_no_single_winner_field_exists(self, conn, evaluator_version_id):
        run_a, run_b, _ = _two_runs_same_item(conn, evaluator_version_id, "The force is 6 N.", "The force is 6 N.")
        comparison = compare_runs(conn, run_a, run_b)
        assert "winner" not in comparison
        assert "overall_score" not in comparison
        assert comparison["dimension_deltas"]["correctness"]["unchanged"] == 1

    def test_cannot_compare_a_run_against_itself(self, conn, evaluator_version_id):
        run_a, _, _ = _run_with_answer_text(conn, evaluator_version_id, "The force is 6 N.")
        with pytest.raises(ValueError):
            compare_runs(conn, run_a, run_a)

    def test_items_unique_to_one_run_are_reported_not_dropped(self, conn, evaluator_version_id):
        # Two entirely separate datasets/items - no common items at all.
        run_a, item_a, _ = _run_with_answer_text(conn, evaluator_version_id, "6 N")
        run_b, item_b, _ = _run_with_answer_text(conn, evaluator_version_id, "6 N")
        comparison = compare_runs(conn, run_a, run_b)
        assert item_a in comparison["items_only_in_run_a"]
        assert item_b in comparison["items_only_in_run_b"]

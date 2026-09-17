"""Phase 7B-5 tests: the evaluation runner, end-to-end against mocked
Gemini calls but a real SQLite database."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from backend import ai_engine
from evaluation import authoring, db as evaldb
from evaluation.runner import RunConfig, run_evaluation


@pytest.fixture()
def conn(tmp_path):
    db_path = tmp_path / "runner_test.sqlite"
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


def _dataset_with_one_item(conn, draft=None):
    _, item_version_id = authoring.create_item_with_first_version(conn, draft or _numerical_item_draft())
    dataset_id = authoring.create_dataset(conn, "Test Dataset")
    dataset_version_id = authoring.cut_dataset_version(conn, dataset_id, [item_version_id])
    return dataset_version_id, item_version_id


class TestRunnerHappyPath:
    def test_correct_answer_persists_success_and_correctness_score(self, conn, evaluator_version_id):
        dataset_version_id, item_version_id = _dataset_with_one_item(conn)

        response = MagicMock()
        response.text = "Using F=ma, the force is 6 N."
        response.model_version = "gemini-3.6-flash-001"
        response.response_id = "resp-1"
        usage = MagicMock()
        usage.prompt_token_count = 50
        usage.candidates_token_count = 10
        usage.thoughts_token_count = 0
        usage.total_token_count = 60
        del usage.cached_content_token_count
        response.usage_metadata = usage

        with patch.object(ai_engine, "_client") as client:
            chat = MagicMock()
            chat.send_message.return_value = response
            client.chats.create.return_value = chat

            run_id = run_evaluation(conn, RunConfig(
                dataset_version_id=dataset_version_id, evaluator_version_id=evaluator_version_id,
            ))

        run = conn.execute("SELECT * FROM evaluation_runs WHERE run_id = ?", (run_id,)).fetchone()
        assert run["dataset_version_id"] == dataset_version_id
        assert run["git_commit_sha"]
        assert run["prompt_hash"]
        assert run["evaluation_schema_version"] == evaldb.EVALUATION_SCHEMA_VERSION

        item_run = conn.execute(
            "SELECT * FROM evaluation_item_runs WHERE run_id = ? AND item_version_id = ?",
            (run_id, item_version_id),
        ).fetchone()
        assert item_run["generation_failed"] == 0
        assert item_run["raw_answer_text"] == "Using F=ma, the force is 6 N."
        assert item_run["resolved_model_version"] == "gemini-3.6-flash-001"
        assert item_run["prompt_tokens"] == 50

        correctness = conn.execute(
            "SELECT * FROM evaluation_results WHERE run_id = ? AND item_version_id = ? AND dimension = 'correctness'",
            (run_id, item_version_id),
        ).fetchone()
        assert correctness["score"] == 1.0
        assert correctness["method"] == "deterministic"

    def test_wrong_answer_produces_failure_flag(self, conn, evaluator_version_id):
        dataset_version_id, item_version_id = _dataset_with_one_item(conn)

        response = MagicMock()
        response.text = "The force is 100 N."
        response.model_version = None
        response.response_id = None
        usage = MagicMock()
        usage.prompt_token_count = 50
        usage.candidates_token_count = 10
        usage.thoughts_token_count = 0
        usage.total_token_count = 60
        del usage.cached_content_token_count
        response.usage_metadata = usage

        with patch.object(ai_engine, "_client") as client:
            chat = MagicMock()
            chat.send_message.return_value = response
            client.chats.create.return_value = chat

            run_id = run_evaluation(conn, RunConfig(
                dataset_version_id=dataset_version_id, evaluator_version_id=evaluator_version_id,
            ))

        correctness = conn.execute(
            "SELECT score FROM evaluation_results WHERE run_id = ? AND dimension = 'correctness'",
            (run_id,),
        ).fetchone()
        assert correctness["score"] == 0.0

        flags = conn.execute(
            "SELECT failure_type FROM failure_flags WHERE run_id = ? AND item_version_id = ?",
            (run_id, item_version_id),
        ).fetchall()
        assert any(f["failure_type"] == "numerical_error" for f in flags)

        # resolved_model_version genuinely absent from this mocked
        # response -> must be stored as NULL, never fabricated.
        item_run = conn.execute(
            "SELECT resolved_model_version FROM evaluation_item_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        assert item_run["resolved_model_version"] is None


class TestRunnerFailureIsolation:
    def test_one_item_crash_does_not_abort_the_run(self, conn, evaluator_version_id):
        _, item_version_id_1 = authoring.create_item_with_first_version(conn, _numerical_item_draft())
        _, item_version_id_2 = authoring.create_item_with_first_version(
            conn, _numerical_item_draft(question_text="A different question entirely.")
        )
        dataset_id = authoring.create_dataset(conn, "Two items")
        dataset_version_id = authoring.cut_dataset_version(conn, dataset_id, [item_version_id_1, item_version_id_2])

        with patch("evaluation.model_adapter.ai_engine.generate_ai_response") as mock_generate:
            def side_effect(**kwargs):
                if "different question" in kwargs["prompt"]:
                    raise RuntimeError("simulated adapter-level crash")
                result = MagicMock()
                result.text = "The force is 6 N."
                result.is_error = False
                result.resolved_model_version = None
                result.response_id = None
                result.usage_metadata = None
                result.elapsed_seconds = 0.1
                return result
            mock_generate.side_effect = side_effect

            run_id = run_evaluation(conn, RunConfig(
                dataset_version_id=dataset_version_id, evaluator_version_id=evaluator_version_id,
            ))

        item_runs = conn.execute(
            "SELECT item_version_id, generation_failed FROM evaluation_item_runs WHERE run_id = ?", (run_id,)
        ).fetchall()
        results_by_item = {r["item_version_id"]: r["generation_failed"] for r in item_runs}
        assert results_by_item[item_version_id_1] == 0
        assert results_by_item[item_version_id_2] == 1  # crashed, but isolated

        failure_flags = conn.execute(
            "SELECT failure_type FROM failure_flags WHERE run_id = ? AND item_version_id = ?",
            (run_id, item_version_id_2),
        ).fetchall()
        assert any(f["failure_type"] == "generation_failure" for f in failure_flags)


class TestHoldoutEligibilityGate:
    def test_run_refuses_when_a_holdout_item_is_not_approved(self, conn, evaluator_version_id):
        _, item_version_id = authoring.create_item_with_first_version(conn, _numerical_item_draft())
        # Manually force partition='holdout' bypassing authoring.py's own
        # gate, to simulate the "direct DB tampering" scenario the
        # runner's own defense-in-depth check exists to catch.
        conn.execute(
            "UPDATE benchmark_item_versions SET partition = 'holdout' WHERE item_version_id = ?",
            (item_version_id,),
        )
        conn.commit()
        dataset_id = authoring.create_dataset(conn, "Bad holdout dataset")
        dataset_version_id = authoring.cut_dataset_version(conn, dataset_id, [item_version_id])

        with pytest.raises(RuntimeError, match="Holdout eligibility"):
            run_evaluation(conn, RunConfig(
                dataset_version_id=dataset_version_id, evaluator_version_id=evaluator_version_id,
            ))

        # Confirm nothing was persisted for this refused run.
        assert conn.execute("SELECT COUNT(*) AS c FROM evaluation_runs").fetchone()["c"] == 0

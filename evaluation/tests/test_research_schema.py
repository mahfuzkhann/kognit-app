"""Phase 7C tests: evaluation/schema/research_schema.sql via evaluation/db.py."""

from __future__ import annotations

import sqlite3
import uuid

import pytest

from evaluation import db as evaldb

RESEARCH_SCHEMA_PATH = "evaluation/schema/research_schema.sql"


@pytest.fixture()
def conn(tmp_path):
    db_path = tmp_path / "research_test.sqlite"
    connection = evaldb.get_connection(db_path)
    evaldb.initialize_schema(connection, schema_path=RESEARCH_SCHEMA_PATH, schema_version=evaldb.RESEARCH_SCHEMA_VERSION)
    yield connection
    connection.close()


def _uid() -> str:
    return str(uuid.uuid4())


def _now() -> str:
    return evaldb.utc_now_iso()


class TestResearchSchemaCreation:
    def test_all_expected_tables_exist(self, conn):
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        table_names = {r["name"] for r in rows}
        expected = {
            "research_benchmark_items", "research_item_versions", "research_verification_records",
            "research_datasets", "research_dataset_versions", "research_dataset_version_items",
            "research_evaluator_versions", "research_judge_calibrations", "research_evaluation_runs",
            "research_item_runs", "research_evaluation_results", "research_failure_flags",
        }
        assert expected.issubset(table_names)

    def test_schema_version_self_reported_independently_of_answer_quality_schema(self, conn):
        assert evaldb.get_schema_version_from_db(conn) == evaldb.RESEARCH_SCHEMA_VERSION
        assert evaldb.RESEARCH_SCHEMA_VERSION != "answer_quality"  # sanity: distinct concept

    def test_research_schema_is_a_separate_file_from_answer_quality_schema(self):
        import evaluation.db as db_module
        assert db_module._SCHEMA_SQL_PATH != db_module._RESEARCH_SCHEMA_SQL_PATH


class TestResearchItemFixtures:
    @pytest.fixture()
    def benchmark_item(self, conn):
        item_id = _uid()
        conn.execute(
            """
            INSERT INTO research_benchmark_items
                (item_id, category, expected_research_decision, freshness_requirement,
                 source_type, author, created_at)
            VALUES (?, 'current_bangladesh_info', 'required', 'changes_daily', 'original', 'mahfuz', ?)
            """,
            (item_id, _now()),
        )
        conn.commit()
        return item_id

    @pytest.fixture()
    def item_version(self, conn, benchmark_item):
        item_version_id = _uid()
        conn.execute(
            """
            INSERT INTO research_item_versions
                (item_version_id, item_id, version_number, question_text, language,
                 difficulty, created_at, updated_at)
            VALUES (?, ?, 1, 'What is the current BDT to USD exchange rate?', 'en', 'medium', ?, ?)
            """,
            (item_version_id, benchmark_item, _now(), _now()),
        )
        conn.commit()
        return item_version_id

    def test_item_and_version_created_correctly(self, conn, benchmark_item, item_version):
        item = conn.execute("SELECT * FROM research_benchmark_items WHERE item_id = ?", (benchmark_item,)).fetchone()
        assert item["category"] == "current_bangladesh_info"
        assert item["expected_research_decision"] == "required"

        version = conn.execute(
            "SELECT * FROM research_item_versions WHERE item_version_id = ?", (item_version,)
        ).fetchone()
        assert version["content_status"] == "draft"
        assert version["partition"] == "dev"

    def test_content_is_immutable(self, conn, item_version):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE research_item_versions SET question_text = 'changed' WHERE item_version_id = ?",
                (item_version,),
            )

    def test_content_status_and_partition_are_mutable(self, conn, item_version):
        conn.execute("UPDATE research_item_versions SET content_status = 'approved' WHERE item_version_id = ?", (item_version,))
        conn.execute("UPDATE research_item_versions SET partition = 'holdout' WHERE item_version_id = ?", (item_version,))
        conn.commit()
        row = conn.execute("SELECT content_status, partition FROM research_item_versions WHERE item_version_id = ?", (item_version,)).fetchone()
        assert row["content_status"] == "approved"
        assert row["partition"] == "holdout"

    def test_verification_record_temporal_field_nullable_by_default(self, conn, item_version):
        conn.execute(
            "INSERT INTO research_verification_records (verification_id, item_version_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (_uid(), item_version, _now(), _now()),
        )
        conn.commit()
        row = conn.execute("SELECT verified_as_of_date FROM research_verification_records WHERE item_version_id = ?", (item_version,)).fetchone()
        assert row["verified_as_of_date"] is None

    def test_verified_as_of_date_format_enforced(self, conn, item_version):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO research_verification_records
                    (verification_id, item_version_id, verified_as_of_date, created_at, updated_at)
                VALUES (?, ?, 'not-a-date', ?, ?)
                """,
                (_uid(), item_version, _now(), _now()),
            )

    def test_verified_as_of_date_accepts_correct_format(self, conn, item_version):
        conn.execute(
            """
            INSERT INTO research_verification_records
                (verification_id, item_version_id, verified_as_of_date, created_at, updated_at)
            VALUES (?, ?, '2026-09-17', ?, ?)
            """,
            (_uid(), item_version, _now(), _now()),
        )
        conn.commit()  # must not raise


class TestResearchDatasetLocking:
    def test_dataset_version_lockfile_immutable(self, conn):
        dataset_id = _uid()
        conn.execute("INSERT INTO research_datasets (dataset_id, name, created_at) VALUES (?, 'Research Core v1', ?)", (dataset_id, _now()))
        dv_id = _uid()
        conn.execute(
            "INSERT INTO research_dataset_versions (dataset_version_id, dataset_id, version_number, dataset_name_at_cut, created_at) VALUES (?, ?, 1, 'Research Core v1', ?)",
            (dv_id, dataset_id, _now()),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE research_dataset_versions SET dataset_name_at_cut = 'x' WHERE dataset_version_id = ?", (dv_id,))


class TestResearchFailureTaxonomy:
    def test_valid_failure_types_accepted(self, conn):
        conn.execute(
            "INSERT INTO research_evaluator_versions (evaluator_version_id, evaluator_name, version_label, created_at) VALUES ('ev1', 'deterministic', 'v1', ?)",
            (_now(),),
        )
        item_id = _uid()
        conn.execute(
            "INSERT INTO research_benchmark_items (item_id, category, expected_research_decision, freshness_requirement, source_type, author, created_at) VALUES (?, 'current_technology', 'required', 'changes_frequently', 'original', 'mahfuz', ?)",
            (item_id, _now()),
        )
        iv_id = _uid()
        conn.execute(
            "INSERT INTO research_item_versions (item_version_id, item_id, version_number, question_text, language, difficulty, created_at, updated_at) VALUES (?, ?, 1, 'q', 'en', 'easy', ?, ?)",
            (iv_id, item_id, _now(), _now()),
        )
        dataset_id = _uid()
        conn.execute("INSERT INTO research_datasets (dataset_id, name, created_at) VALUES (?, 'd', ?)", (dataset_id, _now()))
        dv_id = _uid()
        conn.execute("INSERT INTO research_dataset_versions (dataset_version_id, dataset_id, version_number, dataset_name_at_cut, created_at) VALUES (?, ?, 1, 'd', ?)", (dv_id, dataset_id, _now()))
        run_id = _uid()
        conn.execute(
            "INSERT INTO research_evaluation_runs (run_id, created_at, dataset_version_id, git_commit_sha, research_schema_version, model_provider, model_name, model_config_snapshot_json, prompt_version, prompt_hash, evaluator_version_id) VALUES (?, ?, ?, 'abc', '1', 'google', 'gemini-3.6-flash', '{}', 'v1', 'h1', 'ev1')",
            (run_id, _now(), dv_id),
        )
        conn.commit()
        conn.execute(
            "INSERT INTO research_failure_flags (failure_flag_id, run_id, item_version_id, dimension, failure_type, severity, evaluator_version_id, created_at) VALUES (?, ?, ?, 'operational', 'unnecessary_research', 'medium', 'ev1', ?)",
            (_uid(), run_id, iv_id, _now()),
        )
        conn.commit()  # must not raise

    def test_invalid_failure_type_rejected(self, conn):
        conn.execute(
            "INSERT INTO research_evaluator_versions (evaluator_version_id, evaluator_name, version_label, created_at) VALUES ('ev1', 'x', 'v1', ?)",
            (_now(),),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO research_failure_flags (failure_flag_id, run_id, item_version_id, dimension, failure_type, severity, evaluator_version_id, created_at) VALUES (?, 'r', 'iv', 'operational', 'made_up_failure', 'medium', 'ev1', ?)",
                (_uid(), _now()),
            )

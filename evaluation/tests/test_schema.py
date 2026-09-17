"""
Phase 7B-1 tests: the SQLite evaluation schema foundation only.

Scope note (read before adding more tests here): this file tests
evaluation/schema/schema.sql and evaluation/db.py exclusively - table
creation, foreign key enforcement, uniqueness constraints, and the
immutability triggers. It does NOT test a runner, a model adapter,
evaluators, judge calibration *logic*, prompt hashing, or regression
comparison, because none of that exists yet (those are Phase 7B-3
through 7B-9). Tests for that functionality belong in their own phase's
test files when that code is written - adding placeholder/skipped tests
for unbuilt functionality here would misrepresent what Phase 7B-1
actually delivers.
"""

from __future__ import annotations

import sqlite3
import uuid

import pytest

from evaluation import db as evaldb


@pytest.fixture()
def conn(tmp_path):
    db_path = tmp_path / "evaluation_test.sqlite"
    connection = evaldb.get_connection(db_path)
    evaldb.initialize_schema(connection)
    yield connection
    connection.close()


def _uid() -> str:
    return str(uuid.uuid4())


def _now() -> str:
    return evaldb.utc_now_iso()


# ---------------------------------------------------------------------
# Schema creation / self-description
# ---------------------------------------------------------------------

class TestSchemaCreation:
    def test_initialize_schema_is_idempotent(self, tmp_path):
        db_path = tmp_path / "idempotent.sqlite"
        c = evaldb.get_connection(db_path)
        evaldb.initialize_schema(c)
        # Calling it again must not raise (every CREATE uses IF NOT EXISTS).
        evaldb.initialize_schema(c)
        c.close()

    def test_all_expected_tables_exist(self, conn):
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {r["name"] for r in rows}
        expected = {
            "schema_metadata",
            "benchmark_items",
            "benchmark_item_versions",
            "evaluation_policies",
            "verification_records",
            "context_fixtures",
            "datasets",
            "dataset_versions",
            "dataset_version_items",
            "evaluator_versions",
            "judge_calibrations",
            "evaluation_runs",
            "evaluation_item_runs",
            "pricing_configs",
            "evaluation_results",
            "failure_flags",
        }
        assert expected.issubset(table_names)

    def test_schema_version_self_reported(self, conn):
        version = evaldb.get_schema_version_from_db(conn)
        assert version == evaldb.EVALUATION_SCHEMA_VERSION

    def test_foreign_keys_pragma_is_on(self, conn):
        row = conn.execute("PRAGMA foreign_keys").fetchone()
        assert row[0] == 1


# ---------------------------------------------------------------------
# Foreign key enforcement
# ---------------------------------------------------------------------

class TestForeignKeyEnforcement:
    def test_item_version_rejects_unknown_item_id(self, conn):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO benchmark_item_versions
                    (item_version_id, item_id, version_number, question_text,
                     input_language, requested_output_language, mode,
                     mode_expected_behavior_json, expected_behavior_json,
                     difficulty, created_at, updated_at)
                VALUES (?, ?, 1, 'q', 'en', 'en', 'direct', '{}', '{}',
                        'easy', ?, ?)
                """,
                (_uid(), "does-not-exist", _now(), _now()),
            )

    def test_dataset_version_items_rejects_unknown_dataset_version(self, conn, benchmark_item_version):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO dataset_version_items (dataset_version_id, item_version_id) VALUES (?, ?)",
                ("does-not-exist", benchmark_item_version),
            )

    def test_evaluation_run_rejects_unknown_dataset_version(self, conn, evaluator_version):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO evaluation_runs
                    (run_id, created_at, dataset_version_id, git_commit_sha,
                     evaluation_schema_version, model_provider, model_name,
                     model_config_snapshot_json, prompt_version, prompt_hash,
                     evaluator_version_id)
                VALUES (?, ?, ?, 'abc123', '1', 'google', 'gemini-3.6-flash',
                        '{}', 'v1', 'hash1', ?)
                """,
                (_uid(), _now(), "does-not-exist", evaluator_version),
            )


# ---------------------------------------------------------------------
# Shared fixtures used across multiple test classes
# ---------------------------------------------------------------------

@pytest.fixture()
def benchmark_item(conn):
    item_id = _uid()
    conn.execute(
        """
        INSERT INTO benchmark_items
            (item_id, question_type, curriculum_class, curriculum_subject,
             curriculum_topics, source_type, author, created_at)
        VALUES (?, 'numerical_tolerance', 'Class 10', 'Physics',
                '["Force & Motion"]', 'original', 'mahfuz', ?)
        """,
        (item_id, _now()),
    )
    conn.commit()
    return item_id


@pytest.fixture()
def benchmark_item_version(conn, benchmark_item):
    item_version_id = _uid()
    conn.execute(
        """
        INSERT INTO benchmark_item_versions
            (item_version_id, item_id, version_number, question_text,
             input_language, requested_output_language, mode,
             mode_expected_behavior_json, expected_behavior_json,
             difficulty, created_at, updated_at)
        VALUES (?, ?, 1, 'A 2kg block accelerates at 3 m/s^2. Find the force.',
                'en', 'en', 'direct', '{"expects_complete_final_answer": true}',
                '{"reference_answer": "6 N", "numerical_tolerance": 0.01}',
                'easy', ?, ?)
        """,
        (item_version_id, benchmark_item, _now(), _now()),
    )
    conn.commit()
    return item_version_id


@pytest.fixture()
def dataset_version(conn):
    dataset_id = _uid()
    dataset_version_id = _uid()
    conn.execute(
        "INSERT INTO datasets (dataset_id, name, created_at) VALUES (?, ?, ?)",
        (dataset_id, "Kognit Answer Quality Benchmark", _now()),
    )
    conn.execute(
        """
        INSERT INTO dataset_versions
            (dataset_version_id, dataset_id, version_number, dataset_name_at_cut, created_at)
        VALUES (?, ?, 1, 'Kognit Answer Quality Benchmark - Core v1', ?)
        """,
        (dataset_version_id, dataset_id, _now()),
    )
    conn.commit()
    return dataset_version_id


@pytest.fixture()
def evaluator_version(conn):
    evaluator_version_id = _uid()
    conn.execute(
        """
        INSERT INTO evaluator_versions
            (evaluator_version_id, evaluator_name, version_label, created_at)
        VALUES (?, 'deterministic_numerical', 'v1', ?)
        """,
        (evaluator_version_id, _now()),
    )
    conn.commit()
    return evaluator_version_id


@pytest.fixture()
def evaluation_run(conn, dataset_version, evaluator_version):
    run_id = _uid()
    conn.execute(
        """
        INSERT INTO evaluation_runs
            (run_id, created_at, dataset_version_id, git_commit_sha,
             evaluation_schema_version, model_provider, model_name,
             model_config_snapshot_json, prompt_version, prompt_hash,
             evaluator_version_id)
        VALUES (?, ?, ?, '5f39c20bf2f9d4d037252579bb177771b9ae0415', '1',
                'google', 'gemini-3.6-flash', '{"thinking_level": "LOW"}',
                '2026-09-15-a', 'deadbeef', ?)
        """,
        (run_id, _now(), dataset_version, evaluator_version),
    )
    conn.commit()
    return run_id


# ---------------------------------------------------------------------
# Uniqueness constraints
# ---------------------------------------------------------------------

class TestUniquenessConstraints:
    def test_item_version_number_unique_per_item(self, conn, benchmark_item, benchmark_item_version):
        # benchmark_item_version fixture already inserted version_number=1
        # for benchmark_item - inserting another version_number=1 for the
        # SAME item must fail.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO benchmark_item_versions
                    (item_version_id, item_id, version_number, question_text,
                     input_language, requested_output_language, mode,
                     mode_expected_behavior_json, expected_behavior_json,
                     difficulty, created_at, updated_at)
                VALUES (?, ?, 1, 'duplicate version number', 'en', 'en',
                        'direct', '{}', '{}', 'easy', ?, ?)
                """,
                (_uid(), benchmark_item, _now(), _now()),
            )

    def test_verification_record_unique_per_item_version(self, conn, benchmark_item_version):
        conn.execute(
            """
            INSERT INTO verification_records
                (verification_id, item_version_id, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (_uid(), benchmark_item_version, _now(), _now()),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO verification_records
                    (verification_id, item_version_id, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (_uid(), benchmark_item_version, _now(), _now()),
            )

    def test_context_fixture_content_hash_unique(self, conn):
        conn.execute(
            """
            INSERT INTO context_fixtures (fixture_id, source_type, content_hash, created_at)
            VALUES (?, 'pdf', 'sha256:abc', ?)
            """,
            (_uid(), _now()),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO context_fixtures (fixture_id, source_type, content_hash, created_at)
                VALUES (?, 'pdf', 'sha256:abc', ?)
                """,
                (_uid(), _now()),
            )

    def test_evaluation_result_unique_per_run_item_dimension(self, conn, evaluation_run, benchmark_item_version, evaluator_version):
        conn.execute(
            """
            INSERT INTO evaluation_results
                (result_id, run_id, item_version_id, dimension, score,
                 method, evaluator_version_id, created_at)
            VALUES (?, ?, ?, 'correctness', 1.0, 'deterministic', ?, ?)
            """,
            (_uid(), evaluation_run, benchmark_item_version, evaluator_version, _now()),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO evaluation_results
                    (result_id, run_id, item_version_id, dimension, score,
                     method, evaluator_version_id, created_at)
                VALUES (?, ?, ?, 'correctness', 0.5, 'deterministic', ?, ?)
                """,
                (_uid(), evaluation_run, benchmark_item_version, evaluator_version, _now()),
            )


# ---------------------------------------------------------------------
# Immutability triggers
# ---------------------------------------------------------------------

class TestImmutabilityTriggers:
    def test_item_version_content_cannot_be_changed(self, conn, benchmark_item_version):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE benchmark_item_versions SET question_text = 'changed' WHERE item_version_id = ?",
                (benchmark_item_version,),
            )

    def test_item_version_content_status_can_be_changed(self, conn, benchmark_item_version):
        # content_status and partition are explicitly the mutable lifecycle
        # fields - this must NOT raise.
        conn.execute(
            "UPDATE benchmark_item_versions SET content_status = 'reviewed' WHERE item_version_id = ?",
            (benchmark_item_version,),
        )
        conn.commit()
        row = conn.execute(
            "SELECT content_status FROM benchmark_item_versions WHERE item_version_id = ?",
            (benchmark_item_version,),
        ).fetchone()
        assert row["content_status"] == "reviewed"

    def test_item_version_partition_can_be_changed(self, conn, benchmark_item_version):
        conn.execute(
            "UPDATE benchmark_item_versions SET partition = 'holdout' WHERE item_version_id = ?",
            (benchmark_item_version,),
        )
        conn.commit()
        row = conn.execute(
            "SELECT partition FROM benchmark_item_versions WHERE item_version_id = ?",
            (benchmark_item_version,),
        ).fetchone()
        assert row["partition"] == "holdout"

    def test_dataset_version_is_fully_immutable(self, conn, dataset_version):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE dataset_versions SET dataset_name_at_cut = 'renamed' WHERE dataset_version_id = ?",
                (dataset_version,),
            )

    def test_dataset_version_items_cannot_be_updated_or_deleted(self, conn, dataset_version, benchmark_item_version):
        conn.execute(
            "INSERT INTO dataset_version_items (dataset_version_id, item_version_id) VALUES (?, ?)",
            (dataset_version, benchmark_item_version),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE dataset_version_items SET item_version_id = ? WHERE dataset_version_id = ?",
                (_uid(), dataset_version),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "DELETE FROM dataset_version_items WHERE dataset_version_id = ?",
                (dataset_version,),
            )

    def test_evaluation_run_is_fully_immutable(self, conn, evaluation_run):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE evaluation_runs SET git_commit_sha = 'different' WHERE run_id = ?",
                (evaluation_run,),
            )

    def test_evaluation_result_is_append_only(self, conn, evaluation_run, benchmark_item_version, evaluator_version):
        result_id = _uid()
        conn.execute(
            """
            INSERT INTO evaluation_results
                (result_id, run_id, item_version_id, dimension, score,
                 method, evaluator_version_id, created_at)
            VALUES (?, ?, ?, 'reasoning', 0.8, 'llm_judge', ?, ?)
            """,
            (result_id, evaluation_run, benchmark_item_version, evaluator_version, _now()),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE evaluation_results SET score = 0.9 WHERE result_id = ?",
                (result_id,),
            )


# ---------------------------------------------------------------------
# Dataset version locking (the immutable "lockfile" behavior)
# ---------------------------------------------------------------------

class TestDatasetVersionLocking:
    def test_dataset_version_preserves_exact_item_set(self, conn, dataset_version, benchmark_item_version):
        conn.execute(
            "INSERT INTO dataset_version_items (dataset_version_id, item_version_id) VALUES (?, ?)",
            (dataset_version, benchmark_item_version),
        )
        conn.commit()
        rows = conn.execute(
            "SELECT item_version_id FROM dataset_version_items WHERE dataset_version_id = ?",
            (dataset_version,),
        ).fetchall()
        assert [r["item_version_id"] for r in rows] == [benchmark_item_version]

    def test_correcting_an_item_creates_new_version_not_edit(self, conn, benchmark_item, benchmark_item_version):
        # Simulates the Section-6 rule: a corrected reference answer is a
        # NEW row, never an edit of the existing one.
        new_version_id = _uid()
        conn.execute(
            """
            INSERT INTO benchmark_item_versions
                (item_version_id, item_id, version_number, question_text,
                 input_language, requested_output_language, mode,
                 mode_expected_behavior_json, expected_behavior_json,
                 difficulty, created_at, updated_at)
            VALUES (?, ?, 2, 'A 2kg block accelerates at 3 m/s^2. Find the force.',
                    'en', 'en', 'direct', '{"expects_complete_final_answer": true}',
                    '{"reference_answer": "6 N (corrected)", "numerical_tolerance": 0.01}',
                    'easy', ?, ?)
            """,
            (new_version_id, benchmark_item, _now(), _now()),
        )
        conn.commit()
        versions = conn.execute(
            "SELECT version_number FROM benchmark_item_versions WHERE item_id = ? ORDER BY version_number",
            (benchmark_item,),
        ).fetchall()
        assert [v["version_number"] for v in versions] == [1, 2]
        # The original version's content is untouched.
        original = conn.execute(
            "SELECT expected_behavior_json FROM benchmark_item_versions WHERE item_version_id = ?",
            (benchmark_item_version,),
        ).fetchone()
        assert "6 N\"" in original["expected_behavior_json"]


# ---------------------------------------------------------------------
# Verification gate / self-verification identifiability
# ---------------------------------------------------------------------

class TestVerificationModel:
    def test_verification_status_defaults_to_unverified(self, conn, benchmark_item_version):
        conn.execute(
            "INSERT INTO verification_records (verification_id, item_version_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (_uid(), benchmark_item_version, _now(), _now()),
        )
        conn.commit()
        row = conn.execute(
            "SELECT verification_status FROM verification_records WHERE item_version_id = ?",
            (benchmark_item_version,),
        ).fetchone()
        assert row["verification_status"] == "unverified"

    def test_self_verification_is_recorded_explicitly(self, conn, benchmark_item_version):
        # Per the approved decision: author and verifier may be the same
        # person, but this must be identifiable in the data, never
        # silently indistinguishable from independent verification.
        conn.execute(
            "INSERT INTO verification_records (verification_id, item_version_id, verification_status, verified_by, is_self_verified, verified_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (_uid(), benchmark_item_version, "verified_correct", "mahfuz", 1, _now(), _now(), _now()),
        )
        conn.commit()
        row = conn.execute(
            "SELECT is_self_verified, verified_by FROM verification_records WHERE item_version_id = ?",
            (benchmark_item_version,),
        ).fetchone()
        assert row["is_self_verified"] == 1
        assert row["verified_by"] == "mahfuz"

    def test_rubric_and_language_checks_are_nullable_for_not_applicable(self, conn, benchmark_item_version):
        # A numerical, English-only item has no rubric and no Bangla
        # language review to perform - these must be storable as NULL
        # (not applicable), distinct from 0 (checked and failed).
        conn.execute(
            "INSERT INTO verification_records (verification_id, item_version_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (_uid(), benchmark_item_version, _now(), _now()),
        )
        conn.commit()
        row = conn.execute(
            "SELECT rubric_verified, language_reviewed FROM verification_records WHERE item_version_id = ?",
            (benchmark_item_version,),
        ).fetchone()
        assert row["rubric_verified"] is None
        assert row["language_reviewed"] is None


# ---------------------------------------------------------------------
# Judge calibration - per-dimension trust
# ---------------------------------------------------------------------

class TestJudgeCalibration:
    def test_same_evaluator_can_have_different_trust_per_dimension(self, conn, evaluator_version, evaluation_run):
        conn.execute(
            """
            INSERT INTO judge_calibrations
                (calibration_id, evaluator_version_id, judge_model, judge_rubric_id,
                 judge_rubric_version, dimension, calibration_run_id,
                 sample_item_version_ids_json, human_reference_scores_json,
                 judge_scores_json, disagreement_summary_json, trust_status, calibrated_at)
            VALUES (?, ?, 'gemini-3.6-flash', 'rubric-lang', 1, 'language', ?,
                    '[]', '{}', '{}', '{}', 'trusted', ?)
            """,
            (_uid(), evaluator_version, evaluation_run, _now()),
        )
        conn.execute(
            """
            INSERT INTO judge_calibrations
                (calibration_id, evaluator_version_id, judge_model, judge_rubric_id,
                 judge_rubric_version, dimension, calibration_run_id,
                 sample_item_version_ids_json, human_reference_scores_json,
                 judge_scores_json, disagreement_summary_json, trust_status, calibrated_at)
            VALUES (?, ?, 'gemini-3.6-flash', 'rubric-curr', 1, 'curriculum_alignment', ?,
                    '[]', '{}', '{}', '{}', 'disputed', ?)
            """,
            (_uid(), evaluator_version, evaluation_run, _now()),
        )
        conn.commit()
        rows = conn.execute(
            "SELECT dimension, trust_status FROM judge_calibrations WHERE evaluator_version_id = ? ORDER BY dimension",
            (evaluator_version,),
        ).fetchall()
        trust_by_dimension = {r["dimension"]: r["trust_status"] for r in rows}
        assert trust_by_dimension == {
            "curriculum_alignment": "disputed",
            "language": "trusted",
        }


# ---------------------------------------------------------------------
# Failure flags - multi-valued, with severity
# ---------------------------------------------------------------------

class TestFailureFlags:
    def test_one_item_can_have_multiple_failure_flags(self, conn, evaluation_run, benchmark_item_version, evaluator_version):
        conn.execute(
            """
            INSERT INTO failure_flags
                (failure_flag_id, run_id, item_version_id, dimension, failure_type,
                 severity, evaluator_version_id, created_at)
            VALUES (?, ?, ?, 'curriculum_alignment', 'curriculum_mismatch', 'medium', ?, ?)
            """,
            (_uid(), evaluation_run, benchmark_item_version, evaluator_version, _now()),
        )
        conn.execute(
            """
            INSERT INTO failure_flags
                (failure_flag_id, run_id, item_version_id, dimension, failure_type,
                 severity, evaluator_version_id, created_at)
            VALUES (?, ?, ?, 'reasoning', 'reasoning_error', 'high', ?, ?)
            """,
            (_uid(), evaluation_run, benchmark_item_version, evaluator_version, _now()),
        )
        conn.commit()
        rows = conn.execute(
            "SELECT dimension, severity FROM failure_flags WHERE run_id = ? AND item_version_id = ?",
            (evaluation_run, benchmark_item_version),
        ).fetchall()
        assert len(rows) == 2

    def test_invalid_severity_rejected(self, conn, evaluation_run, benchmark_item_version, evaluator_version):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO failure_flags
                    (failure_flag_id, run_id, item_version_id, dimension, failure_type,
                     severity, evaluator_version_id, created_at)
                VALUES (?, ?, ?, 'reasoning', 'reasoning_error', 'catastrophic', ?, ?)
                """,
                (_uid(), evaluation_run, benchmark_item_version, evaluator_version, _now()),
            )


# ---------------------------------------------------------------------
# Evaluation run identity - nullable resolved_model_version, never fabricated
# ---------------------------------------------------------------------

class TestEvaluationRunIdentity:
    def test_resolved_model_version_is_nullable(self, conn, dataset_version, evaluator_version):
        run_id = _uid()
        conn.execute(
            """
            INSERT INTO evaluation_runs
                (run_id, created_at, dataset_version_id, git_commit_sha,
                 evaluation_schema_version, model_provider, model_name,
                 resolved_model_version, model_config_snapshot_json,
                 prompt_version, prompt_hash, evaluator_version_id)
            VALUES (?, ?, ?, 'abc', '1', 'google', 'gemini-3.6-flash', NULL,
                    '{}', 'v1', 'hash1', ?)
            """,
            (run_id, _now(), dataset_version, evaluator_version),
        )
        conn.commit()
        row = conn.execute(
            "SELECT resolved_model_version FROM evaluation_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        assert row["resolved_model_version"] is None

    def test_mandatory_reproducibility_fields_are_enforced_not_null(self, conn, dataset_version, evaluator_version):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO evaluation_runs
                    (run_id, created_at, dataset_version_id, git_commit_sha,
                     evaluation_schema_version, model_provider, model_name,
                     model_config_snapshot_json, prompt_version, prompt_hash,
                     evaluator_version_id)
                VALUES (?, ?, ?, NULL, '1', 'google', 'gemini-3.6-flash',
                        '{}', 'v1', 'hash1', ?)
                """,
                (_uid(), _now(), dataset_version, evaluator_version),
            )

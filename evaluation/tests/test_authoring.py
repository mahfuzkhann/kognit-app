"""Phase 7B-2 tests: benchmark authoring, verification gate, holdout promotion."""

from __future__ import annotations

import json

import pytest

from evaluation import authoring, db as evaldb


@pytest.fixture()
def conn(tmp_path):
    db_path = tmp_path / "authoring_test.sqlite"
    connection = evaldb.get_connection(db_path)
    evaldb.initialize_schema(connection)
    yield connection
    connection.close()


def _sample_draft(**overrides) -> authoring.ItemDraft:
    defaults = dict(
        question_type="numerical_tolerance",
        curriculum_class="Class 10",
        curriculum_subject="Physics",
        curriculum_topics=["Force & Motion"],
        source_type="original",
        author="mahfuz",
        question_text="A 2kg block accelerates at 3 m/s^2. Find the force.",
        input_language="en",
        requested_output_language="en",
        mode="direct",
        expected_behavior={"reference_answer": "6 N", "numerical_tolerance": 0.01},
        difficulty="easy",
        mode_expected_behavior={"expects_complete_final_answer": True},
    )
    defaults.update(overrides)
    return authoring.ItemDraft(**defaults)


class TestItemCreation:
    def test_create_item_with_first_version(self, conn):
        item_id, item_version_id = authoring.create_item_with_first_version(conn, _sample_draft())

        item = conn.execute("SELECT * FROM benchmark_items WHERE item_id = ?", (item_id,)).fetchone()
        assert item["curriculum_subject"] == "Physics"
        assert json.loads(item["curriculum_topics"]) == ["Force & Motion"]

        version = conn.execute(
            "SELECT * FROM benchmark_item_versions WHERE item_version_id = ?", (item_version_id,)
        ).fetchone()
        assert version["version_number"] == 1
        assert version["content_status"] == "draft"
        assert version["partition"] == "dev"

        verification = conn.execute(
            "SELECT * FROM verification_records WHERE item_version_id = ?", (item_version_id,)
        ).fetchone()
        assert verification["verification_status"] == "unverified"

    def test_create_corrected_version_increments_version_number(self, conn):
        item_id, v1 = authoring.create_item_with_first_version(conn, _sample_draft())
        v2 = authoring.create_corrected_version(
            conn, item_id, _sample_draft(expected_behavior={"reference_answer": "6 N (corrected)", "numerical_tolerance": 0.01})
        )
        versions = conn.execute(
            "SELECT version_number, item_version_id FROM benchmark_item_versions WHERE item_id = ? ORDER BY version_number",
            (item_id,),
        ).fetchall()
        assert [row["version_number"] for row in versions] == [1, 2]
        assert versions[0]["item_version_id"] == v1
        assert versions[1]["item_version_id"] == v2

    def test_create_corrected_version_unknown_item_raises(self, conn):
        with pytest.raises(authoring.BenchmarkAuthoringError):
            authoring.create_corrected_version(conn, "does-not-exist", _sample_draft())


class TestVerification:
    def test_self_verification_is_detected_and_recorded(self, conn):
        item_id, item_version_id = authoring.create_item_with_first_version(
            conn, _sample_draft(author="mahfuz")
        )
        authoring.record_verification(
            conn, item_version_id,
            authoring.VerificationInput(
                verified_by="mahfuz",  # same as author
                curriculum_reviewed=True, reference_answer_verified=True,
                ambiguity_check_passed=True, difficulty_sanity_checked=True,
                rubric_verified=None, language_reviewed=None,
            ),
        )
        row = conn.execute(
            "SELECT is_self_verified, verification_status FROM verification_records WHERE item_version_id = ?",
            (item_version_id,),
        ).fetchone()
        assert row["is_self_verified"] == 1
        assert row["verification_status"] == "verified_correct"

    def test_independent_verification_is_detected_and_recorded(self, conn):
        item_id, item_version_id = authoring.create_item_with_first_version(
            conn, _sample_draft(author="mahfuz")
        )
        authoring.record_verification(
            conn, item_version_id,
            authoring.VerificationInput(
                verified_by="a-different-reviewer",
                curriculum_reviewed=True, reference_answer_verified=True,
                ambiguity_check_passed=True, difficulty_sanity_checked=True,
            ),
        )
        row = conn.execute(
            "SELECT is_self_verified FROM verification_records WHERE item_version_id = ?",
            (item_version_id,),
        ).fetchone()
        assert row["is_self_verified"] == 0

    def test_record_verification_unknown_version_raises(self, conn):
        with pytest.raises(authoring.BenchmarkAuthoringError):
            authoring.record_verification(
                conn, "does-not-exist",
                authoring.VerificationInput(
                    verified_by="mahfuz", curriculum_reviewed=True,
                    reference_answer_verified=True, ambiguity_check_passed=True,
                    difficulty_sanity_checked=True,
                ),
            )


class TestApprovalGate:
    def _verified_item(self, conn, **verification_overrides):
        item_id, item_version_id = authoring.create_item_with_first_version(conn, _sample_draft())
        defaults = dict(
            verified_by="mahfuz", curriculum_reviewed=True,
            reference_answer_verified=True, ambiguity_check_passed=True,
            difficulty_sanity_checked=True,
        )
        defaults.update(verification_overrides)
        authoring.record_verification(conn, item_version_id, authoring.VerificationInput(**defaults))
        return item_id, item_version_id

    def test_approval_succeeds_when_fully_verified(self, conn):
        _, item_version_id = self._verified_item(conn)
        authoring.advance_content_status(conn, item_version_id, "approved")
        row = conn.execute(
            "SELECT content_status FROM benchmark_item_versions WHERE item_version_id = ?",
            (item_version_id,),
        ).fetchone()
        assert row["content_status"] == "approved"

    def test_approval_rejected_if_not_verified_correct(self, conn):
        _, item_version_id = self._verified_item(conn, final_status="verified_needs_revision")
        with pytest.raises(authoring.BenchmarkAuthoringError, match="verification_status"):
            authoring.advance_content_status(conn, item_version_id, "approved")

    def test_approval_rejected_if_a_required_check_is_false(self, conn):
        _, item_version_id = self._verified_item(conn, ambiguity_check_passed=False)
        with pytest.raises(authoring.BenchmarkAuthoringError, match="ambiguity_check_passed"):
            authoring.advance_content_status(conn, item_version_id, "approved")

    def test_approval_succeeds_when_rubric_check_is_not_applicable(self, conn):
        # rubric_verified=None (not applicable, e.g. a pure numerical item)
        # must NOT block approval.
        _, item_version_id = self._verified_item(conn, rubric_verified=None)
        authoring.advance_content_status(conn, item_version_id, "approved")  # must not raise

    def test_approval_rejected_when_applicable_rubric_check_failed(self, conn):
        _, item_version_id = self._verified_item(conn, rubric_verified=False)
        with pytest.raises(authoring.BenchmarkAuthoringError, match="rubric_verified"):
            authoring.advance_content_status(conn, item_version_id, "approved")

    def test_non_approval_status_transitions_do_not_require_verification(self, conn):
        item_id, item_version_id = authoring.create_item_with_first_version(conn, _sample_draft())
        authoring.advance_content_status(conn, item_version_id, "reviewed")  # must not raise
        row = conn.execute(
            "SELECT content_status FROM benchmark_item_versions WHERE item_version_id = ?",
            (item_version_id,),
        ).fetchone()
        assert row["content_status"] == "reviewed"


class TestHoldoutPromotion:
    def _approved_item(self, conn):
        item_id, item_version_id = authoring.create_item_with_first_version(conn, _sample_draft())
        authoring.record_verification(
            conn, item_version_id,
            authoring.VerificationInput(
                verified_by="mahfuz", curriculum_reviewed=True,
                reference_answer_verified=True, ambiguity_check_passed=True,
                difficulty_sanity_checked=True,
            ),
        )
        authoring.advance_content_status(conn, item_version_id, "approved")
        return item_version_id

    def test_promotion_succeeds_when_approved(self, conn):
        item_version_id = self._approved_item(conn)
        authoring.promote_to_holdout(conn, item_version_id)
        row = conn.execute(
            "SELECT partition FROM benchmark_item_versions WHERE item_version_id = ?",
            (item_version_id,),
        ).fetchone()
        assert row["partition"] == "holdout"

    def test_promotion_rejected_when_not_approved(self, conn):
        item_id, item_version_id = authoring.create_item_with_first_version(conn, _sample_draft())
        with pytest.raises(authoring.BenchmarkAuthoringError, match="not 'approved'"):
            authoring.promote_to_holdout(conn, item_version_id)
        row = conn.execute(
            "SELECT partition FROM benchmark_item_versions WHERE item_version_id = ?",
            (item_version_id,),
        ).fetchone()
        assert row["partition"] == "dev"  # unchanged


class TestDatasetCutting:
    def test_cut_dataset_version_locks_exact_item_set(self, conn):
        _, v1 = authoring.create_item_with_first_version(conn, _sample_draft())
        _, v2 = authoring.create_item_with_first_version(conn, _sample_draft(question_text="Different question"))
        dataset_id = authoring.create_dataset(conn, "Kognit Answer Quality Benchmark")
        dv_id = authoring.cut_dataset_version(conn, dataset_id, [v1, v2])

        rows = conn.execute(
            "SELECT item_version_id FROM dataset_version_items WHERE dataset_version_id = ?", (dv_id,)
        ).fetchall()
        assert {r["item_version_id"] for r in rows} == {v1, v2}

        dv = conn.execute("SELECT * FROM dataset_versions WHERE dataset_version_id = ?", (dv_id,)).fetchone()
        assert dv["version_number"] == 1
        assert dv["dataset_name_at_cut"] == "Kognit Answer Quality Benchmark"

    def test_second_cut_increments_version_number(self, conn):
        _, v1 = authoring.create_item_with_first_version(conn, _sample_draft())
        dataset_id = authoring.create_dataset(conn, "Core v1")
        dv1 = authoring.cut_dataset_version(conn, dataset_id, [v1])
        _, v2 = authoring.create_item_with_first_version(conn, _sample_draft(question_text="Another one"))
        dv2 = authoring.cut_dataset_version(conn, dataset_id, [v1, v2])

        v_numbers = conn.execute(
            "SELECT version_number FROM dataset_versions WHERE dataset_id = ? ORDER BY version_number",
            (dataset_id,),
        ).fetchall()
        assert [r["version_number"] for r in v_numbers] == [1, 2]
        assert dv1 != dv2

    def test_cut_dataset_version_unknown_dataset_raises(self, conn):
        with pytest.raises(authoring.BenchmarkAuthoringError):
            authoring.cut_dataset_version(conn, "does-not-exist", [])

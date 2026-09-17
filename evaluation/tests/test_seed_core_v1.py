"""Phase 7B-8 tests: Core v1 benchmark seed script.

These tests verify the SEEDING MECHANISM and coverage claims made in
evaluation/seed_core_v1.py's own docstring - they do NOT verify academic
correctness of the content (that requires human subject-matter review,
explicitly disclosed as not yet done in this session)."""

from __future__ import annotations

import json

import pytest

from evaluation import db as evaldb
from evaluation.seed_core_v1 import DATASET_NAME, build_core_v1_drafts, seed_database


@pytest.fixture()
def conn(tmp_path):
    db_path = tmp_path / "core_v1_test.sqlite"
    connection = evaldb.get_connection(db_path)
    evaldb.initialize_schema(connection)
    yield connection
    connection.close()


class TestCoreV1Coverage:
    def test_exactly_fifteen_items(self):
        assert len(build_core_v1_drafts()) == 15

    def test_covers_all_five_core_subjects(self):
        subjects = {d.curriculum_subject for d in build_core_v1_drafts()}
        assert subjects == {"Physics", "Chemistry", "Mathematics", "Bangla", "English"}

    def test_covers_both_ssc_and_hsc_classes(self):
        classes = {d.curriculum_class for d in build_core_v1_drafts()}
        ssc_classes = {"Class 9", "Class 10"}
        hsc_classes = {"Class 11", "Class 12"}
        assert classes & ssc_classes, "No SSC-level items found"
        assert classes & hsc_classes, "No HSC-level items found"

    def test_covers_both_modes(self):
        modes = {d.mode for d in build_core_v1_drafts()}
        assert modes == {"direct", "socratic"}

    def test_covers_bangla_english_and_banglish_input(self):
        input_languages = {d.input_language for d in build_core_v1_drafts()}
        assert input_languages == {"bn", "en", "bn-latn"}

    def test_covers_multiple_question_types(self):
        question_types = {d.question_type for d in build_core_v1_drafts()}
        assert {"numerical_tolerance", "mcq", "short_factual", "long_explanation", "proof_derivation"} <= question_types

    def test_every_numerical_item_has_a_parseable_reference_answer(self):
        import re
        number_pattern = re.compile(r"[-+]?\d+\.?\d*")
        for draft in build_core_v1_drafts():
            if draft.question_type == "numerical_tolerance":
                ref = draft.expected_behavior.get("reference_answer", "")
                assert number_pattern.search(str(ref)), f"No parseable number in {ref!r}"

    def test_every_mcq_item_has_a_single_letter_correct_option(self):
        for draft in build_core_v1_drafts():
            if draft.question_type == "mcq":
                option = draft.expected_behavior.get("correct_option")
                assert option and len(option) == 1 and option.isalpha()

    def test_every_item_has_an_ai_authored_pending_review_author(self):
        for draft in build_core_v1_drafts():
            assert "pending human review" in draft.author


class TestSeedDatabase:
    def test_seed_creates_all_items_as_draft_unverified_dev(self, conn):
        dataset_version_id = seed_database(conn)

        rows = conn.execute(
            """
            SELECT biv.content_status, biv.partition, vr.verification_status
            FROM dataset_version_items dvi
            JOIN benchmark_item_versions biv ON biv.item_version_id = dvi.item_version_id
            JOIN verification_records vr ON vr.item_version_id = biv.item_version_id
            WHERE dvi.dataset_version_id = ?
            """,
            (dataset_version_id,),
        ).fetchall()

        assert len(rows) == 15
        for row in rows:
            assert row["content_status"] == "draft"
            assert row["partition"] == "dev"
            assert row["verification_status"] == "unverified"

    def test_seed_creates_dataset_with_correct_name(self, conn):
        dataset_version_id = seed_database(conn)
        dv = conn.execute(
            "SELECT dataset_name_at_cut FROM dataset_versions WHERE dataset_version_id = ?",
            (dataset_version_id,),
        ).fetchone()
        assert dv["dataset_name_at_cut"] == DATASET_NAME

    def test_no_item_is_holdout_eligible_yet(self, conn):
        # Directly confirms the honest-scope disclosure: none of these
        # items could pass authoring.promote_to_holdout() as seeded.
        from evaluation import authoring

        dataset_version_id = seed_database(conn)
        item_version_ids = [
            r["item_version_id"] for r in conn.execute(
                "SELECT item_version_id FROM dataset_version_items WHERE dataset_version_id = ?",
                (dataset_version_id,),
            ).fetchall()
        ]
        for item_version_id in item_version_ids:
            with pytest.raises(authoring.BenchmarkAuthoringError):
                authoring.promote_to_holdout(conn, item_version_id)

"""Phase 7C tests: evaluation/research_authoring.py."""

from __future__ import annotations

import pytest

from evaluation import db as evaldb, research_authoring as ra


@pytest.fixture()
def conn(tmp_path):
    db_path = tmp_path / "research_authoring_test.sqlite"
    connection = evaldb.get_connection(db_path)
    evaldb.initialize_schema(connection, schema_path="evaluation/schema/research_schema.sql", schema_version=evaldb.RESEARCH_SCHEMA_VERSION)
    yield connection
    connection.close()


def _draft(**overrides) -> ra.ResearchItemDraft:
    defaults = dict(
        category="current_bangladesh_info", expected_research_decision="required",
        freshness_requirement="changes_daily", source_type="original", author="mahfuz",
        question_text="What is the current BDT to USD exchange rate?",
        language="en", difficulty="medium",
    )
    defaults.update(overrides)
    return ra.ResearchItemDraft(**defaults)


class TestResearchItemCreation:
    def test_create_item_with_first_version(self, conn):
        item_id, item_version_id = ra.create_research_item_with_first_version(conn, _draft())
        item = conn.execute("SELECT * FROM research_benchmark_items WHERE item_id = ?", (item_id,)).fetchone()
        assert item["category"] == "current_bangladesh_info"
        version = conn.execute("SELECT * FROM research_item_versions WHERE item_version_id = ?", (item_version_id,)).fetchone()
        assert version["content_status"] == "draft"


class TestTemporalVerificationRequirement:
    def test_non_timeless_item_requires_verified_as_of_date(self, conn):
        _, iv = ra.create_research_item_with_first_version(conn, _draft(freshness_requirement="changes_daily"))
        with pytest.raises(ra.ResearchBenchmarkAuthoringError, match="verified_as_of_date"):
            ra.record_research_verification(conn, iv, ra.ResearchVerificationInput(
                verified_by="mahfuz", curriculum_reviewed=True, reference_facts_verified=True,
                ambiguity_check_passed=True, verified_as_of_date=None,
            ))

    def test_non_timeless_item_succeeds_with_verified_as_of_date(self, conn):
        _, iv = ra.create_research_item_with_first_version(conn, _draft(freshness_requirement="changes_daily"))
        ra.record_research_verification(conn, iv, ra.ResearchVerificationInput(
            verified_by="mahfuz", curriculum_reviewed=True, reference_facts_verified=True,
            ambiguity_check_passed=True, verified_as_of_date="2026-09-17",
        ))
        row = conn.execute("SELECT verification_status, verified_as_of_date FROM research_verification_records WHERE item_version_id = ?", (iv,)).fetchone()
        assert row["verification_status"] == "verified_correct"
        assert row["verified_as_of_date"] == "2026-09-17"

    def test_timeless_item_does_not_require_verified_as_of_date(self, conn):
        _, iv = ra.create_research_item_with_first_version(conn, _draft(freshness_requirement="timeless", expected_research_decision="not_required"))
        ra.record_research_verification(conn, iv, ra.ResearchVerificationInput(
            verified_by="mahfuz", curriculum_reviewed=True, reference_facts_verified=True,
            ambiguity_check_passed=True, verified_as_of_date=None,
        ))  # must not raise
        row = conn.execute("SELECT verification_status FROM research_verification_records WHERE item_version_id = ?", (iv,)).fetchone()
        assert row["verification_status"] == "verified_correct"

    def test_needs_revision_status_does_not_trigger_temporal_requirement(self, conn):
        # Only a final_status of 'verified_correct' requires the date -
        # rejecting/flagging an item for revision should not be blocked
        # by a missing date.
        _, iv = ra.create_research_item_with_first_version(conn, _draft(freshness_requirement="changes_daily"))
        ra.record_research_verification(conn, iv, ra.ResearchVerificationInput(
            verified_by="mahfuz", curriculum_reviewed=True, reference_facts_verified=False,
            ambiguity_check_passed=True, verified_as_of_date=None, final_status="verified_needs_revision",
        ))  # must not raise


class TestSelfVerification:
    def test_self_verification_detected(self, conn):
        _, iv = ra.create_research_item_with_first_version(conn, _draft(author="mahfuz"))
        ra.record_research_verification(conn, iv, ra.ResearchVerificationInput(
            verified_by="mahfuz", curriculum_reviewed=True, reference_facts_verified=True,
            ambiguity_check_passed=True, verified_as_of_date="2026-09-17",
        ))
        row = conn.execute("SELECT is_self_verified FROM research_verification_records WHERE item_version_id = ?", (iv,)).fetchone()
        assert row["is_self_verified"] == 1


class TestApprovalAndHoldoutGate:
    def _approved_item(self, conn):
        _, iv = ra.create_research_item_with_first_version(conn, _draft())
        ra.record_research_verification(conn, iv, ra.ResearchVerificationInput(
            verified_by="mahfuz", curriculum_reviewed=True, reference_facts_verified=True,
            ambiguity_check_passed=True, verified_as_of_date="2026-09-17",
        ))
        ra.advance_research_content_status(conn, iv, "approved")
        return iv

    def test_holdout_promotion_succeeds_when_approved(self, conn):
        iv = self._approved_item(conn)
        ra.promote_research_item_to_holdout(conn, iv)
        row = conn.execute("SELECT partition FROM research_item_versions WHERE item_version_id = ?", (iv,)).fetchone()
        assert row["partition"] == "holdout"

    def test_holdout_promotion_rejected_when_unapproved(self, conn):
        _, iv = ra.create_research_item_with_first_version(conn, _draft())
        with pytest.raises(ra.ResearchBenchmarkAuthoringError):
            ra.promote_research_item_to_holdout(conn, iv)

    def test_approval_rejected_without_verification(self, conn):
        _, iv = ra.create_research_item_with_first_version(conn, _draft())
        with pytest.raises(ra.ResearchBenchmarkAuthoringError):
            ra.advance_research_content_status(conn, iv, "approved")


class TestDatasetCutting:
    def test_cut_dataset_version(self, conn):
        _, iv = ra.create_research_item_with_first_version(conn, _draft())
        dataset_id = ra.create_research_dataset(conn, "Research Core v1")
        dv_id = ra.cut_research_dataset_version(conn, dataset_id, [iv])
        rows = conn.execute("SELECT item_version_id FROM research_dataset_version_items WHERE dataset_version_id = ?", (dv_id,)).fetchall()
        assert [r["item_version_id"] for r in rows] == [iv]

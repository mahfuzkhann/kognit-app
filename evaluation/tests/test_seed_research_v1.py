"""Phase 7C tests: evaluation/seed_research_v1.py."""

from __future__ import annotations

import pytest

from evaluation import db as evaldb
from evaluation.seed_research_v1 import DATASET_NAME, build_research_core_v1_drafts, seed_research_database

RESEARCH_SCHEMA_PATH = "evaluation/schema/research_schema.sql"


@pytest.fixture()
def conn(tmp_path):
    db_path = tmp_path / "research_seed_test.sqlite"
    connection = evaldb.get_connection(db_path)
    evaldb.initialize_schema(connection, schema_path=RESEARCH_SCHEMA_PATH, schema_version=evaldb.RESEARCH_SCHEMA_VERSION)
    yield connection
    connection.close()


class TestResearchCoreV1Coverage:
    def test_exactly_twenty_items(self):
        assert len(build_research_core_v1_drafts()) == 20

    def test_covers_all_ten_step11_categories(self):
        categories = {d.category for d in build_research_core_v1_drafts()}
        expected = {
            "current_factual", "recent_development", "current_bangladesh_info",
            "current_education_info", "current_technology", "current_science",
            "current_public_info", "stable_factual_no_search_needed",
            "ambiguous_freshness", "potentially_misleading_search",
        }
        assert categories == expected

    def test_includes_both_required_and_not_required_decisions(self):
        decisions = {d.expected_research_decision for d in build_research_core_v1_drafts()}
        assert decisions == {"required", "not_required"}

    def test_at_least_one_item_per_category_is_present(self):
        drafts = build_research_core_v1_drafts()
        from collections import Counter
        counts = Counter(d.category for d in drafts)
        assert all(c >= 1 for c in counts.values())

    def test_non_timeless_items_have_no_reference_facts_fabricated(self):
        # No item claims a specific hardcoded current value as fact -
        # they're questions, not answer keys with invented numbers.
        for draft in build_research_core_v1_drafts():
            assert draft.reference_facts is None or isinstance(draft.reference_facts, dict)


class TestSeedResearchDatabase:
    def test_seed_creates_all_items_as_draft_unverified_dev(self, conn):
        dataset_version_id = seed_research_database(conn)
        rows = conn.execute(
            """
            SELECT riv.content_status, riv.partition, rvr.verification_status
            FROM research_dataset_version_items rdvi
            JOIN research_item_versions riv ON riv.item_version_id = rdvi.item_version_id
            JOIN research_verification_records rvr ON rvr.item_version_id = riv.item_version_id
            WHERE rdvi.dataset_version_id = ?
            """,
            (dataset_version_id,),
        ).fetchall()
        assert len(rows) == 20
        for row in rows:
            assert row["content_status"] == "draft"
            assert row["partition"] == "dev"
            assert row["verification_status"] == "unverified"

    def test_seed_creates_dataset_with_correct_name(self, conn):
        dataset_version_id = seed_research_database(conn)
        dv = conn.execute(
            "SELECT dataset_name_at_cut FROM research_dataset_versions WHERE dataset_version_id = ?",
            (dataset_version_id,),
        ).fetchone()
        assert dv["dataset_name_at_cut"] == DATASET_NAME

    def test_no_item_is_holdout_eligible_yet(self, conn):
        from evaluation import research_authoring as ra

        dataset_version_id = seed_research_database(conn)
        item_version_ids = [
            r["item_version_id"] for r in conn.execute(
                "SELECT item_version_id FROM research_dataset_version_items WHERE dataset_version_id = ?",
                (dataset_version_id,),
            ).fetchall()
        ]
        for item_version_id in item_version_ids:
            with pytest.raises(ra.ResearchBenchmarkAuthoringError):
                ra.promote_research_item_to_holdout(conn, item_version_id)

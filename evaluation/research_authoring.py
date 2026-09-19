"""
Kognit Phase 7C - Research benchmark authoring & dataset tooling.

Mirrors evaluation/authoring.py's structure and discipline exactly
(immutable versions, application-enforced self-verification detection,
application-enforced approval/holdout gates) applied to the separate
research_* tables. See evaluation/authoring.py's own docstring for the
shared rationale - not repeated here in full.

ONE genuinely research-specific rule, per Step 12: for any item whose
freshness_requirement is NOT 'timeless', verification cannot reach
'verified_correct' without a verified_as_of_date being recorded - a
current-information fact is only ever true as of a specific date, never
eternally, and the benchmark must not silently treat it as timeless.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Optional

from evaluation import db as evaldb


class ResearchBenchmarkAuthoringError(Exception):
    pass


def _uid() -> str:
    return str(uuid.uuid4())


@dataclass
class ResearchItemDraft:
    category: str
    expected_research_decision: str  # 'required' | 'not_required'
    freshness_requirement: str
    source_type: str
    author: str
    question_text: str
    language: str
    difficulty: str
    source_reference: Optional[str] = None
    reference_facts: Optional[dict] = None
    citation_expectations: Optional[dict] = None
    evaluation_rubric_text: Optional[str] = None
    tags: Optional[list] = None


def create_research_item_with_first_version(conn: sqlite3.Connection, draft: ResearchItemDraft) -> tuple[str, str]:
    now = evaldb.utc_now_iso()
    item_id = _uid()
    item_version_id = _uid()
    verification_id = _uid()

    conn.execute(
        """
        INSERT INTO research_benchmark_items
            (item_id, category, expected_research_decision, freshness_requirement,
             source_type, source_reference, author, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (item_id, draft.category, draft.expected_research_decision, draft.freshness_requirement,
         draft.source_type, draft.source_reference, draft.author, now),
    )
    conn.execute(
        """
        INSERT INTO research_item_versions
            (item_version_id, item_id, version_number, question_text, language, difficulty,
             reference_facts_json, citation_expectations_json, evaluation_rubric_text,
             tags_json, created_at, updated_at)
        VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item_version_id, item_id, draft.question_text, draft.language, draft.difficulty,
            json.dumps(draft.reference_facts) if draft.reference_facts is not None else None,
            json.dumps(draft.citation_expectations) if draft.citation_expectations is not None else None,
            draft.evaluation_rubric_text,
            json.dumps(draft.tags) if draft.tags is not None else None,
            now, now,
        ),
    )
    conn.execute(
        "INSERT INTO research_verification_records (verification_id, item_version_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (verification_id, item_version_id, now, now),
    )
    conn.commit()
    return item_id, item_version_id


@dataclass
class ResearchVerificationInput:
    verified_by: str
    curriculum_reviewed: bool
    reference_facts_verified: bool
    ambiguity_check_passed: bool
    verified_as_of_date: Optional[str] = None  # 'YYYY-MM-DD', required for non-timeless items
    verification_notes: Optional[str] = None
    final_status: str = "verified_correct"


def record_research_verification(conn: sqlite3.Connection, item_version_id: str, v: ResearchVerificationInput) -> None:
    author_row = conn.execute(
        """
        SELECT rbi.author, rbi.freshness_requirement FROM research_benchmark_items rbi
        JOIN research_item_versions riv ON riv.item_id = rbi.item_id
        WHERE riv.item_version_id = ?
        """,
        (item_version_id,),
    ).fetchone()
    if author_row is None:
        raise ResearchBenchmarkAuthoringError(f"No research item version found for {item_version_id!r}")

    # Step 12's temporal requirement, enforced here (not by a CHECK
    # constraint, which cannot cross-reference another table's column).
    if author_row["freshness_requirement"] != "timeless" and v.final_status == "verified_correct" and not v.verified_as_of_date:
        raise ResearchBenchmarkAuthoringError(
            f"Cannot verify {item_version_id!r} as 'verified_correct': freshness_requirement="
            f"{author_row['freshness_requirement']!r} is not 'timeless', so verified_as_of_date "
            f"is required - a current-information fact is only true as of a specific date."
        )

    is_self_verified = 1 if author_row["author"] == v.verified_by else 0

    conn.execute(
        """
        UPDATE research_verification_records SET
            verification_status = ?, verified_by = ?, is_self_verified = ?, verified_at = ?,
            verified_as_of_date = ?, verification_notes = ?,
            curriculum_reviewed = ?, reference_facts_verified = ?, ambiguity_check_passed = ?,
            updated_at = ?
        WHERE item_version_id = ?
        """,
        (
            v.final_status, v.verified_by, is_self_verified, evaldb.utc_now_iso(),
            v.verified_as_of_date, v.verification_notes,
            int(v.curriculum_reviewed), int(v.reference_facts_verified), int(v.ambiguity_check_passed),
            evaldb.utc_now_iso(), item_version_id,
        ),
    )
    conn.commit()


def advance_research_content_status(conn: sqlite3.Connection, item_version_id: str, new_status: str) -> None:
    if new_status == "approved":
        vr = conn.execute(
            "SELECT * FROM research_verification_records WHERE item_version_id = ?", (item_version_id,)
        ).fetchone()
        if vr is None:
            raise ResearchBenchmarkAuthoringError(f"No verification record for {item_version_id!r}")
        if vr["verification_status"] != "verified_correct":
            raise ResearchBenchmarkAuthoringError(
                f"Cannot approve {item_version_id!r}: verification_status is {vr['verification_status']!r}"
            )
        for field_name in ["curriculum_reviewed", "reference_facts_verified", "ambiguity_check_passed"]:
            if not vr[field_name]:
                raise ResearchBenchmarkAuthoringError(f"Cannot approve {item_version_id!r}: {field_name} is not confirmed")

    conn.execute(
        "UPDATE research_item_versions SET content_status = ?, updated_at = ? WHERE item_version_id = ?",
        (new_status, evaldb.utc_now_iso(), item_version_id),
    )
    conn.commit()


def promote_research_item_to_holdout(conn: sqlite3.Connection, item_version_id: str) -> None:
    row = conn.execute(
        "SELECT content_status FROM research_item_versions WHERE item_version_id = ?", (item_version_id,)
    ).fetchone()
    if row is None:
        raise ResearchBenchmarkAuthoringError(f"No research item version found for {item_version_id!r}")
    if row["content_status"] != "approved":
        raise ResearchBenchmarkAuthoringError(
            f"Cannot promote {item_version_id!r} to holdout: content_status is {row['content_status']!r}, not 'approved'."
        )
    conn.execute(
        "UPDATE research_item_versions SET partition = 'holdout', updated_at = ? WHERE item_version_id = ?",
        (evaldb.utc_now_iso(), item_version_id),
    )
    conn.commit()


def create_research_dataset(conn: sqlite3.Connection, name: str) -> str:
    dataset_id = _uid()
    conn.execute("INSERT INTO research_datasets (dataset_id, name, created_at) VALUES (?, ?, ?)", (dataset_id, name, evaldb.utc_now_iso()))
    conn.commit()
    return dataset_id


def cut_research_dataset_version(conn: sqlite3.Connection, dataset_id: str, item_version_ids: list) -> str:
    dataset_row = conn.execute("SELECT name FROM research_datasets WHERE dataset_id = ?", (dataset_id,)).fetchone()
    if dataset_row is None:
        raise ResearchBenchmarkAuthoringError(f"No research dataset found for dataset_id={dataset_id!r}")
    max_v_row = conn.execute(
        "SELECT MAX(version_number) AS max_v FROM research_dataset_versions WHERE dataset_id = ?", (dataset_id,)
    ).fetchone()
    next_version = 1 if max_v_row["max_v"] is None else max_v_row["max_v"] + 1

    dataset_version_id = _uid()
    now = evaldb.utc_now_iso()
    conn.execute(
        "INSERT INTO research_dataset_versions (dataset_version_id, dataset_id, version_number, dataset_name_at_cut, created_at) VALUES (?, ?, ?, ?, ?)",
        (dataset_version_id, dataset_id, next_version, dataset_row["name"], now),
    )
    for item_version_id in item_version_ids:
        conn.execute(
            "INSERT INTO research_dataset_version_items (dataset_version_id, item_version_id) VALUES (?, ?)",
            (dataset_version_id, item_version_id),
        )
    conn.commit()
    return dataset_version_id

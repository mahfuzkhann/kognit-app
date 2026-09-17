"""
Kognit Phase 7B-2 - Benchmark authoring & dataset tooling.

A Python API, not a CLI or admin UI (per the approved architecture:
"a large admin UI is NOT required... CLI/scripts/Python APIs are
acceptable"). Every function here is a thin, explicit wrapper around one
or two SQL statements - there is no framework, no ORM, no workflow
engine. This is deliberate: the approved architecture's complexity rule
is "do not build a workflow engine... this is a data-quality gate, not
an enterprise approval system."

Two cross-table rules that SQLite CHECK constraints cannot enforce
(documented in evaluation/schema/schema.sql and evaluation/README.md)
are enforced HERE, in application code:
  1. is_self_verified must correctly reflect whether verified_by equals
     the item's author.
  2. An item version cannot be promoted to the 'holdout' partition
     unless content_status='approved' AND verification_status=
     'verified_correct'.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Optional

from evaluation import db as evaldb


class BenchmarkAuthoringError(Exception):
    """Raised when an authoring/verification operation would violate a
    rule this module enforces (e.g. promoting an unverified item to
    holdout). Never silently ignored/bypassed - callers must handle or
    let it propagate."""


def _uid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------
# Item creation
# ---------------------------------------------------------------------

@dataclass
class ItemDraft:
    """Everything needed to create a new BenchmarkItem + its first
    BenchmarkItemVersion in one call - the common case for authoring a
    brand new question."""
    question_type: str
    curriculum_class: str
    curriculum_subject: str
    curriculum_topics: list
    source_type: str
    author: str
    question_text: str
    input_language: str
    requested_output_language: str
    mode: str
    expected_behavior: dict
    difficulty: str
    curriculum_stream: Optional[str] = None
    curriculum_chapter: Optional[str] = None
    source_reference: Optional[str] = None
    mode_expected_behavior: dict = field(default_factory=dict)
    context: Optional[dict] = None
    tags: Optional[list] = None
    evaluation_policy_id: Optional[str] = None


def create_item_with_first_version(conn: sqlite3.Connection, draft: ItemDraft) -> tuple[str, str]:
    """Create a new BenchmarkItem and its version_number=1
    BenchmarkItemVersion in one transaction. Returns (item_id, item_version_id).

    A verification_records row is also created (status='unverified') -
    every item version has exactly one verification record from the
    moment it exists, per the schema's UNIQUE(item_version_id) constraint.
    """
    now = evaldb.utc_now_iso()
    item_id = _uid()
    item_version_id = _uid()
    verification_id = _uid()

    conn.execute(
        """
        INSERT INTO benchmark_items
            (item_id, question_type, curriculum_class, curriculum_stream,
             curriculum_subject, curriculum_chapter, curriculum_topics,
             source_type, source_reference, author, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item_id, draft.question_type, draft.curriculum_class, draft.curriculum_stream,
            draft.curriculum_subject, draft.curriculum_chapter, json.dumps(draft.curriculum_topics),
            draft.source_type, draft.source_reference, draft.author, now,
        ),
    )
    conn.execute(
        """
        INSERT INTO benchmark_item_versions
            (item_version_id, item_id, version_number, question_text,
             input_language, requested_output_language, mode,
             mode_expected_behavior_json, context_json, expected_behavior_json,
             difficulty, tags_json, evaluation_policy_id, created_at, updated_at)
        VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item_version_id, item_id, draft.question_text, draft.input_language,
            draft.requested_output_language, draft.mode,
            json.dumps(draft.mode_expected_behavior),
            json.dumps(draft.context) if draft.context is not None else None,
            json.dumps(draft.expected_behavior),
            draft.difficulty,
            json.dumps(draft.tags) if draft.tags is not None else None,
            draft.evaluation_policy_id, now, now,
        ),
    )
    conn.execute(
        "INSERT INTO verification_records (verification_id, item_version_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (verification_id, item_version_id, now, now),
    )
    conn.commit()
    return item_id, item_version_id


def create_corrected_version(
    conn: sqlite3.Connection,
    item_id: str,
    draft: ItemDraft,
) -> str:
    """Create a NEW version of an existing item (a content correction).

    Never updates the previous version's row - per the schema's
    immutability trigger, that would raise sqlite3.IntegrityError anyway,
    but the point is architectural, not just mechanical: a corrected
    reference answer is new evaluated content, and any run that already
    used the old version remains reproducible against exactly what it
    used.
    """
    row = conn.execute(
        "SELECT MAX(version_number) AS max_v FROM benchmark_item_versions WHERE item_id = ?",
        (item_id,),
    ).fetchone()
    if row is None or row["max_v"] is None:
        raise BenchmarkAuthoringError(f"No existing item found for item_id={item_id!r}")
    next_version = row["max_v"] + 1

    now = evaldb.utc_now_iso()
    item_version_id = _uid()
    verification_id = _uid()

    conn.execute(
        """
        INSERT INTO benchmark_item_versions
            (item_version_id, item_id, version_number, question_text,
             input_language, requested_output_language, mode,
             mode_expected_behavior_json, context_json, expected_behavior_json,
             difficulty, tags_json, evaluation_policy_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item_version_id, item_id, next_version, draft.question_text, draft.input_language,
            draft.requested_output_language, draft.mode,
            json.dumps(draft.mode_expected_behavior),
            json.dumps(draft.context) if draft.context is not None else None,
            json.dumps(draft.expected_behavior),
            draft.difficulty,
            json.dumps(draft.tags) if draft.tags is not None else None,
            draft.evaluation_policy_id, now, now,
        ),
    )
    conn.execute(
        "INSERT INTO verification_records (verification_id, item_version_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (verification_id, item_version_id, now, now),
    )
    conn.commit()
    return item_version_id


# ---------------------------------------------------------------------
# Verification (the six-check quality gate)
# ---------------------------------------------------------------------

@dataclass
class VerificationInput:
    verified_by: str
    curriculum_reviewed: bool
    reference_answer_verified: bool
    ambiguity_check_passed: bool
    difficulty_sanity_checked: bool
    rubric_verified: Optional[bool] = None   # None = not applicable
    language_reviewed: Optional[bool] = None  # None = not applicable
    verification_notes: Optional[str] = None
    final_status: str = "verified_correct"    # or 'verified_needs_revision' / 'rejected'


def record_verification(conn: sqlite3.Connection, item_version_id: str, v: VerificationInput) -> None:
    """Record a verification pass against an existing item version.

    This is where the schema's documented, application-layer-only
    self-verification rule is actually enforced: is_self_verified is
    computed HERE by comparing verified_by against the item's real
    author - never trusted from caller input, so a caller cannot
    misrepresent self-verification as independent verification even by
    mistake.
    """
    author_row = conn.execute(
        """
        SELECT bi.author FROM benchmark_items bi
        JOIN benchmark_item_versions biv ON biv.item_id = bi.item_id
        WHERE biv.item_version_id = ?
        """,
        (item_version_id,),
    ).fetchone()
    if author_row is None:
        raise BenchmarkAuthoringError(f"No item version found for item_version_id={item_version_id!r}")

    is_self_verified = 1 if author_row["author"] == v.verified_by else 0

    conn.execute(
        """
        UPDATE verification_records SET
            verification_status = ?,
            verified_by = ?,
            is_self_verified = ?,
            verified_at = ?,
            verification_notes = ?,
            curriculum_reviewed = ?,
            reference_answer_verified = ?,
            rubric_verified = ?,
            language_reviewed = ?,
            ambiguity_check_passed = ?,
            difficulty_sanity_checked = ?,
            updated_at = ?
        WHERE item_version_id = ?
        """,
        (
            v.final_status, v.verified_by, is_self_verified, evaldb.utc_now_iso(),
            v.verification_notes,
            int(v.curriculum_reviewed), int(v.reference_answer_verified),
            None if v.rubric_verified is None else int(v.rubric_verified),
            None if v.language_reviewed is None else int(v.language_reviewed),
            int(v.ambiguity_check_passed), int(v.difficulty_sanity_checked),
            evaldb.utc_now_iso(), item_version_id,
        ),
    )
    conn.commit()


def advance_content_status(conn: sqlite3.Connection, item_version_id: str, new_status: str) -> None:
    """Move an item version's content_status forward
    (draft -> reviewed -> approved -> active -> deprecated).

    'approved' specifically requires verification_status='verified_correct'
    AND all six quality-gate checks to be true where applicable - this is
    the Phase-7B-Step-2-revision Section 23 quality gate, enforced here
    since SQLite cannot express it as a CHECK constraint.
    """
    if new_status == "approved":
        vr = conn.execute(
            "SELECT * FROM verification_records WHERE item_version_id = ?",
            (item_version_id,),
        ).fetchone()
        if vr is None:
            raise BenchmarkAuthoringError(f"No verification record for {item_version_id!r}")
        if vr["verification_status"] != "verified_correct":
            raise BenchmarkAuthoringError(
                f"Cannot approve {item_version_id!r}: verification_status is "
                f"{vr['verification_status']!r}, not 'verified_correct'"
            )
        required_true = ["curriculum_reviewed", "reference_answer_verified",
                          "ambiguity_check_passed", "difficulty_sanity_checked"]
        for field_name in required_true:
            if not vr[field_name]:
                raise BenchmarkAuthoringError(
                    f"Cannot approve {item_version_id!r}: {field_name} is not confirmed"
                )
        # rubric_verified / language_reviewed: only required to be truthy
        # when applicable (non-NULL) - NULL (not applicable) never blocks
        # approval, per the schema's own nullable-means-N/A convention.
        for field_name in ["rubric_verified", "language_reviewed"]:
            value = vr[field_name]
            if value is not None and not value:
                raise BenchmarkAuthoringError(
                    f"Cannot approve {item_version_id!r}: {field_name} is applicable and failed"
                )

    conn.execute(
        "UPDATE benchmark_item_versions SET content_status = ?, updated_at = ? WHERE item_version_id = ?",
        (new_status, evaldb.utc_now_iso(), item_version_id),
    )
    conn.commit()


def promote_to_holdout(conn: sqlite3.Connection, item_version_id: str) -> None:
    """Move an item version's partition to 'holdout'.

    Enforces, in application code, the rule SQLite cannot express as a
    CHECK constraint: content_status must already be 'approved' (which
    itself already required verification_status='verified_correct' - see
    advance_content_status above). Never silently bypassed.
    """
    row = conn.execute(
        "SELECT content_status FROM benchmark_item_versions WHERE item_version_id = ?",
        (item_version_id,),
    ).fetchone()
    if row is None:
        raise BenchmarkAuthoringError(f"No item version found for {item_version_id!r}")
    if row["content_status"] != "approved":
        raise BenchmarkAuthoringError(
            f"Cannot promote {item_version_id!r} to holdout: content_status is "
            f"{row['content_status']!r}, not 'approved'. Holdout requires "
            f"content_status='approved' AND verification_status='verified_correct' "
            f"(the latter is already required to reach 'approved' - see "
            f"advance_content_status())."
        )
    conn.execute(
        "UPDATE benchmark_item_versions SET partition = 'holdout', updated_at = ? WHERE item_version_id = ?",
        (evaldb.utc_now_iso(), item_version_id),
    )
    conn.commit()


# ---------------------------------------------------------------------
# Dataset creation / immutable version cutting
# ---------------------------------------------------------------------

def create_dataset(conn: sqlite3.Connection, name: str) -> str:
    dataset_id = _uid()
    conn.execute(
        "INSERT INTO datasets (dataset_id, name, created_at) VALUES (?, ?, ?)",
        (dataset_id, name, evaldb.utc_now_iso()),
    )
    conn.commit()
    return dataset_id


def cut_dataset_version(conn: sqlite3.Connection, dataset_id: str, item_version_ids: list) -> str:
    """Create an immutable DatasetVersion "lockfile" referencing an exact
    list of item versions. Once created, this list can never be changed
    (dataset_version_items has both an UPDATE-block and a DELETE-block
    trigger) - this is the mechanism that keeps a historical run's
    dataset composition permanently reconstructible.
    """
    dataset_row = conn.execute(
        "SELECT name FROM datasets WHERE dataset_id = ?", (dataset_id,)
    ).fetchone()
    if dataset_row is None:
        raise BenchmarkAuthoringError(f"No dataset found for dataset_id={dataset_id!r}")

    max_v_row = conn.execute(
        "SELECT MAX(version_number) AS max_v FROM dataset_versions WHERE dataset_id = ?",
        (dataset_id,),
    ).fetchone()
    next_version = 1 if max_v_row["max_v"] is None else max_v_row["max_v"] + 1

    dataset_version_id = _uid()
    now = evaldb.utc_now_iso()
    conn.execute(
        """
        INSERT INTO dataset_versions
            (dataset_version_id, dataset_id, version_number, dataset_name_at_cut, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (dataset_version_id, dataset_id, next_version, dataset_row["name"], now),
    )
    for item_version_id in item_version_ids:
        conn.execute(
            "INSERT INTO dataset_version_items (dataset_version_id, item_version_id) VALUES (?, ?)",
            (dataset_version_id, item_version_id),
        )
    conn.commit()
    return dataset_version_id

"""
Kognit Phase 7B-7 - Judge calibration recording.

Records a JudgeCalibration row from a set of paired (human_score,
judge_score) observations for one (evaluator_version, dimension) pair.
Deliberately plain counts + a mean absolute difference - NOT a named
formal statistic (Cohen's kappa etc.) per the approved architecture's
explicit instruction. trust_status is set by a human decision passed
in by the caller, never auto-computed from a threshold this module
invents - see module docstring for why.
"""

from __future__ import annotations

import json
import statistics
import uuid
from dataclasses import dataclass
from typing import Optional

from evaluation import db as evaldb


@dataclass
class CalibrationSample:
    item_version_id: str
    human_score: float
    judge_score: float


def compute_disagreement_summary(samples: list) -> dict:
    """Plain, transparent counts - no formal agreement statistic invented.
    'Agreement' here means the two scores are within 0.1 of each other on
    the 0-1 scale - an explicit, simple threshold, not a validated
    psychometric one. Anyone consuming this data can recompute a
    different definition of agreement later from the raw stored scores
    (human_reference_scores_json / judge_scores_json) without any schema
    change - this summary is a convenience view, not the source of truth.
    """
    if not samples:
        return {"agree_count": 0, "disagree_count": 0, "mean_absolute_difference": None}
    diffs = [abs(s.human_score - s.judge_score) for s in samples]
    agree_count = sum(1 for d in diffs if d <= 0.1)
    return {
        "agree_count": agree_count,
        "disagree_count": len(samples) - agree_count,
        "mean_absolute_difference": statistics.mean(diffs),
    }


def record_calibration(
    conn,
    evaluator_version_id: str,
    judge_model: str,
    judge_rubric_id: str,
    judge_rubric_version: int,
    dimension: str,
    samples: list,
    trust_status: str,
    judge_model_version: Optional[str] = None,
    calibration_run_id: Optional[str] = None,
) -> str:
    """Insert a JudgeCalibration row. trust_status is supplied by the
    caller (a human decision, informed by the disagreement summary this
    function also computes) - this module never decides 'trusted' vs
    'disputed' on its own, since no evidence-backed threshold for that
    exists yet (explicitly deferred, per the approved architecture)."""
    if trust_status not in ("uncalibrated", "trusted", "disputed"):
        raise ValueError(f"Invalid trust_status: {trust_status!r}")

    disagreement = compute_disagreement_summary(samples)
    calibration_id = str(uuid.uuid4())

    conn.execute(
        """
        INSERT INTO judge_calibrations
            (calibration_id, evaluator_version_id, judge_model, judge_model_version,
             judge_rubric_id, judge_rubric_version, dimension, calibration_run_id,
             sample_item_version_ids_json, human_reference_scores_json,
             judge_scores_json, disagreement_summary_json, trust_status, calibrated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            calibration_id, evaluator_version_id, judge_model, judge_model_version,
            judge_rubric_id, judge_rubric_version, dimension, calibration_run_id,
            json.dumps([s.item_version_id for s in samples]),
            json.dumps({s.item_version_id: s.human_score for s in samples}),
            json.dumps({s.item_version_id: s.judge_score for s in samples}),
            json.dumps(disagreement),
            trust_status,
            evaldb.utc_now_iso(),
        ),
    )
    conn.commit()
    return calibration_id


def get_current_trust_status(conn, evaluator_version_id: str, dimension: str) -> str:
    """Returns the most recent trust_status for this (evaluator, dimension)
    pair, or 'uncalibrated' if no calibration has ever been recorded -
    never silently assumes 'trusted' in the absence of data."""
    row = conn.execute(
        """
        SELECT trust_status FROM judge_calibrations
        WHERE evaluator_version_id = ? AND dimension = ?
        ORDER BY calibrated_at DESC LIMIT 1
        """,
        (evaluator_version_id, dimension),
    ).fetchone()
    return row["trust_status"] if row is not None else "uncalibrated"

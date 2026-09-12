"""
Unit tests for backend/insight_engine.py (Phase 5E Student Intelligence).

Run with: python3 -m pytest test_insight_engine.py -v

No I/O, no database, no Gemini - pure function tests, same style as
test_mastery_engine.py.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from backend import insight_engine as ie
from backend.learning_memory import (
    CONFIDENCE_KNOWN,
    CONFIDENCE_PROBABLE,
    CONFIDENCE_UNKNOWN,
    SIGNAL_CONFUSION,
    SIGNAL_MISCONCEPTION,
    SIGNAL_REEXPLANATION,
    SIGNAL_REPEATED_QUESTION,
    SIGNAL_SUCCESSFUL_UNDERSTANDING,
)

_T0 = datetime(2026, 8, 1, 10, 0, 0, tzinfo=timezone.utc)


def _record(
    record_id="r1", subject="Physics", topic=None, topic_key=None,
    signal_type=SIGNAL_CONFUSION, attribution_confidence=CONFIDENCE_KNOWN,
    occurred_at=None,
):
    return ie.ChatEvidenceRecord(
        record_id=record_id, subject=subject, topic=topic, topic_key=topic_key,
        signal_type=signal_type, attribution_confidence=attribution_confidence,
        occurred_at=occurred_at or _T0,
    )


def _at(offset_days):
    return _T0 + timedelta(days=offset_days)


# ---------------------------------------------------------------------------
# 1. No evidence -> no insights.
# ---------------------------------------------------------------------------

def test_no_evidence_no_insights():
    assert ie.compute_insights([]) == []


# ---------------------------------------------------------------------------
# 2 & 3. Single vs. multiple difficulty events.
# ---------------------------------------------------------------------------

def test_single_difficulty_event_produces_no_insight():
    records = [_record(record_id="r1", signal_type=SIGNAL_CONFUSION, occurred_at=_at(0))]
    insights = ie.compute_insights(records)
    assert not any(i.insight_type == ie.INSIGHT_REPEATED_DIFFICULTY for i in insights)


def test_multiple_independent_difficulty_events_produce_repeated_difficulty():
    records = [
        _record(record_id="r1", signal_type=SIGNAL_CONFUSION, occurred_at=_at(0)),
        _record(record_id="r2", signal_type=SIGNAL_REEXPLANATION, occurred_at=_at(1)),
    ]
    insights = ie.compute_insights(records)
    matches = [i for i in insights if i.insight_type == ie.INSIGHT_REPEATED_DIFFICULTY]
    assert len(matches) == 1
    assert matches[0].evidence_count == 2
    assert matches[0].subject == "Physics"


# ---------------------------------------------------------------------------
# 4. Repeated confusion -> recurring_confusion.
# ---------------------------------------------------------------------------

def test_repeated_confusion_produces_recurring_confusion():
    records = [
        _record(record_id="r1", signal_type=SIGNAL_CONFUSION, occurred_at=_at(0)),
        _record(record_id="r2", signal_type=SIGNAL_CONFUSION, occurred_at=_at(1)),
    ]
    insights = ie.compute_insights(records)
    matches = [i for i in insights if i.insight_type == ie.INSIGHT_RECURRING_CONFUSION]
    assert len(matches) == 1
    assert matches[0].evidence_count == 2


def test_recurring_confusion_and_repeated_difficulty_can_both_fire():
    # Two confusion events qualify for BOTH categories at once - this is
    # intentional (see module docstring), not double counting the same
    # insight twice.
    records = [
        _record(record_id="r1", signal_type=SIGNAL_CONFUSION, occurred_at=_at(0)),
        _record(record_id="r2", signal_type=SIGNAL_CONFUSION, occurred_at=_at(1)),
    ]
    insights = ie.compute_insights(records)
    types = {i.insight_type for i in insights}
    assert ie.INSIGHT_REPEATED_DIFFICULTY in types
    assert ie.INSIGHT_RECURRING_CONFUSION in types


def test_single_confusion_event_alone_produces_neither_confusion_insight():
    records = [_record(record_id="r1", signal_type=SIGNAL_CONFUSION, occurred_at=_at(0))]
    insights = ie.compute_insights(records)
    assert insights == []


# ---------------------------------------------------------------------------
# 5. Repeated successful understanding -> emerging_strength only at threshold.
# ---------------------------------------------------------------------------

def test_single_successful_understanding_produces_no_strength_insight():
    records = [_record(record_id="r1", signal_type=SIGNAL_SUCCESSFUL_UNDERSTANDING, occurred_at=_at(0))]
    insights = ie.compute_insights(records)
    assert not any(i.insight_type == ie.INSIGHT_EMERGING_STRENGTH for i in insights)


def test_two_successful_understanding_events_produce_emerging_strength():
    records = [
        _record(record_id="r1", signal_type=SIGNAL_SUCCESSFUL_UNDERSTANDING, occurred_at=_at(0)),
        _record(record_id="r2", signal_type=SIGNAL_SUCCESSFUL_UNDERSTANDING, occurred_at=_at(1)),
    ]
    insights = ie.compute_insights(records)
    matches = [i for i in insights if i.insight_type == ie.INSIGHT_EMERGING_STRENGTH]
    assert len(matches) == 1
    assert matches[0].evidence_count == 2


# ---------------------------------------------------------------------------
# 6, 7, 8. Attribution confidence handling.
# ---------------------------------------------------------------------------

def test_known_attribution_eligible_for_insight():
    records = [
        _record(record_id="r1", signal_type=SIGNAL_CONFUSION, attribution_confidence=CONFIDENCE_KNOWN, occurred_at=_at(0)),
        _record(record_id="r2", signal_type=SIGNAL_CONFUSION, attribution_confidence=CONFIDENCE_KNOWN, occurred_at=_at(1)),
    ]
    insights = ie.compute_insights(records)
    assert len(insights) > 0


def test_probable_attribution_reduces_confidence_to_low():
    records = [
        _record(record_id="r1", signal_type=SIGNAL_CONFUSION, attribution_confidence=CONFIDENCE_PROBABLE, occurred_at=_at(0)),
        _record(record_id="r2", signal_type=SIGNAL_CONFUSION, attribution_confidence=CONFIDENCE_PROBABLE, occurred_at=_at(1)),
        _record(record_id="r3", signal_type=SIGNAL_CONFUSION, attribution_confidence=CONFIDENCE_PROBABLE, occurred_at=_at(2)),
        _record(record_id="r4", signal_type=SIGNAL_CONFUSION, attribution_confidence=CONFIDENCE_PROBABLE, occurred_at=_at(3)),
        _record(record_id="r5", signal_type=SIGNAL_CONFUSION, attribution_confidence=CONFIDENCE_PROBABLE, occurred_at=_at(4)),
    ]
    insights = ie.compute_insights(records)
    # Even with 5 events, ALL probable -> must stay LOW, never medium/high.
    # This is THE regression test for "do not repeat the attribution
    # confidence bug" applied to Phase 5E specifically.
    for insight in insights:
        assert insight.confidence == ie.INSIGHT_CONFIDENCE_LOW, (
            f"{insight.insight_type} reached {insight.confidence} from all-probable evidence"
        )


def test_unknown_attribution_never_produces_an_insight():
    records = [
        _record(record_id="r1", signal_type=SIGNAL_CONFUSION, attribution_confidence=CONFIDENCE_UNKNOWN, occurred_at=_at(0)),
        _record(record_id="r2", signal_type=SIGNAL_CONFUSION, attribution_confidence=CONFIDENCE_UNKNOWN, occurred_at=_at(1)),
    ]
    assert ie.compute_insights(records) == []


def test_confidence_tier_boundaries_are_driven_by_known_count_only():
    # 4 KNOWN events -> HIGH (HIGH_CONFIDENCE_MIN_KNOWN_EVENTS == 4)
    high_records = [
        _record(record_id=f"k{i}", signal_type=SIGNAL_CONFUSION, attribution_confidence=CONFIDENCE_KNOWN, occurred_at=_at(i))
        for i in range(4)
    ]
    insights = ie.compute_insights(high_records)
    confusion_insight = next(i for i in insights if i.insight_type == ie.INSIGHT_RECURRING_CONFUSION)
    assert confusion_insight.confidence == ie.INSIGHT_CONFIDENCE_HIGH

    # 2 KNOWN events -> MEDIUM (MEDIUM_CONFIDENCE_MIN_KNOWN_EVENTS == 2)
    medium_records = [
        _record(record_id=f"k{i}", signal_type=SIGNAL_CONFUSION, attribution_confidence=CONFIDENCE_KNOWN, occurred_at=_at(i))
        for i in range(2)
    ]
    insights = ie.compute_insights(medium_records)
    confusion_insight = next(i for i in insights if i.insight_type == ie.INSIGHT_RECURRING_CONFUSION)
    assert confusion_insight.confidence == ie.INSIGHT_CONFIDENCE_MEDIUM


def test_mixed_known_and_probable_counts_only_known_toward_confidence():
    # 1 known + 3 probable = 4 total events (clears MIN_EVENTS), but only
    # 1 KNOWN -> must stay LOW, not MEDIUM/HIGH, even though total count
    # would clear a naive count-only threshold.
    records = [
        _record(record_id="r1", signal_type=SIGNAL_CONFUSION, attribution_confidence=CONFIDENCE_KNOWN, occurred_at=_at(0)),
        _record(record_id="r2", signal_type=SIGNAL_CONFUSION, attribution_confidence=CONFIDENCE_PROBABLE, occurred_at=_at(1)),
        _record(record_id="r3", signal_type=SIGNAL_CONFUSION, attribution_confidence=CONFIDENCE_PROBABLE, occurred_at=_at(2)),
        _record(record_id="r4", signal_type=SIGNAL_CONFUSION, attribution_confidence=CONFIDENCE_PROBABLE, occurred_at=_at(3)),
    ]
    insights = ie.compute_insights(records)
    confusion_insight = next(i for i in insights if i.insight_type == ie.INSIGHT_RECURRING_CONFUSION)
    assert confusion_insight.evidence_count == 4  # all 4 count as evidence...
    assert confusion_insight.confidence == ie.INSIGHT_CONFIDENCE_LOW  # ...but only 1 is known


# ---------------------------------------------------------------------------
# 9 & 10. Topic-level vs subject-level insights.
# ---------------------------------------------------------------------------

def test_known_topic_enables_topic_level_insight():
    records = [
        _record(record_id="r1", subject="Physics", topic="Force & Motion", topic_key="force & motion",
                signal_type=SIGNAL_CONFUSION, occurred_at=_at(0)),
        _record(record_id="r2", subject="Physics", topic="Force & Motion", topic_key="force & motion",
                signal_type=SIGNAL_CONFUSION, occurred_at=_at(1)),
    ]
    insights = ie.compute_insights(records)
    assert any(i.topic_key == "force & motion" and i.topic == "Force & Motion" for i in insights)


def test_null_topic_produces_subject_level_insight_only():
    records = [
        _record(record_id="r1", subject="Physics", topic=None, topic_key=None, signal_type=SIGNAL_CONFUSION, occurred_at=_at(0)),
        _record(record_id="r2", subject="Physics", topic=None, topic_key=None, signal_type=SIGNAL_CONFUSION, occurred_at=_at(1)),
    ]
    insights = ie.compute_insights(records)
    assert len(insights) > 0
    for insight in insights:
        assert insight.subject == "Physics"
        assert insight.topic is None
        assert insight.topic_key is None


def test_topic_and_subject_only_evidence_for_same_subject_stay_separate():
    records = [
        _record(record_id="r1", subject="Physics", topic="Force", topic_key="force", signal_type=SIGNAL_CONFUSION, occurred_at=_at(0)),
        _record(record_id="r2", subject="Physics", topic="Force", topic_key="force", signal_type=SIGNAL_CONFUSION, occurred_at=_at(1)),
        _record(record_id="r3", subject="Physics", topic=None, topic_key=None, signal_type=SIGNAL_CONFUSION, occurred_at=_at(2)),
        _record(record_id="r4", subject="Physics", topic=None, topic_key=None, signal_type=SIGNAL_CONFUSION, occurred_at=_at(3)),
    ]
    insights = ie.compute_insights(records)
    confusion_insights = [i for i in insights if i.insight_type == ie.INSIGHT_RECURRING_CONFUSION]
    assert len(confusion_insights) == 2  # one topic-level, one subject-level - not merged


# ---------------------------------------------------------------------------
# 11. Duplicate event IDs -> no double counting.
# ---------------------------------------------------------------------------

def test_duplicate_record_ids_are_not_double_counted():
    duplicate = _record(record_id="same-id", signal_type=SIGNAL_CONFUSION, occurred_at=_at(0))
    records = [duplicate, duplicate, duplicate]
    insights = ie.compute_insights(records)
    # Only one unique record after dedup - below MIN_EVENTS_FOR_INSIGHT.
    assert insights == []


def test_distinct_ids_are_each_counted():
    records = [
        _record(record_id="r1", signal_type=SIGNAL_CONFUSION, occurred_at=_at(0)),
        _record(record_id="r2", signal_type=SIGNAL_CONFUSION, occurred_at=_at(1)),
    ]
    insights = ie.compute_insights(records)
    confusion_insight = next(i for i in insights if i.insight_type == ie.INSIGHT_RECURRING_CONFUSION)
    assert confusion_insight.evidence_count == 2


# ---------------------------------------------------------------------------
# 12 & 13. Chronological ordering / old evidence does not override recent.
# ---------------------------------------------------------------------------

def test_first_and_last_observed_at_reflect_true_chronology_regardless_of_input_order():
    records = [
        _record(record_id="r3", signal_type=SIGNAL_CONFUSION, occurred_at=_at(5)),
        _record(record_id="r1", signal_type=SIGNAL_CONFUSION, occurred_at=_at(0)),
        _record(record_id="r2", signal_type=SIGNAL_CONFUSION, occurred_at=_at(2)),
    ]
    insights = ie.compute_insights(records)
    confusion_insight = next(i for i in insights if i.insight_type == ie.INSIGHT_RECURRING_CONFUSION)
    assert confusion_insight.first_observed_at == _at(0)
    assert confusion_insight.last_observed_at == _at(5)


def test_insights_are_returned_most_recently_observed_first():
    records_a = [
        _record(record_id="a1", subject="Physics", signal_type=SIGNAL_CONFUSION, occurred_at=_at(0)),
        _record(record_id="a2", subject="Physics", signal_type=SIGNAL_CONFUSION, occurred_at=_at(1)),
    ]
    records_b = [
        _record(record_id="b1", subject="Chemistry", signal_type=SIGNAL_CONFUSION, occurred_at=_at(10)),
        _record(record_id="b2", subject="Chemistry", signal_type=SIGNAL_CONFUSION, occurred_at=_at(11)),
    ]
    insights = ie.compute_insights(records_a + records_b)
    subjects_in_order = [i.subject for i in insights]
    assert subjects_in_order.index("Chemistry") < subjects_in_order.index("Physics")


# ---------------------------------------------------------------------------
# 14 & 15. Subject/user separation.
# ---------------------------------------------------------------------------

def test_different_subjects_remain_separated():
    records = [
        _record(record_id="r1", subject="Physics", signal_type=SIGNAL_CONFUSION, occurred_at=_at(0)),
        _record(record_id="r2", subject="Physics", signal_type=SIGNAL_CONFUSION, occurred_at=_at(1)),
        _record(record_id="r3", subject="Chemistry", signal_type=SIGNAL_CONFUSION, occurred_at=_at(0)),
    ]
    insights = ie.compute_insights(records)
    physics_insights = [i for i in insights if i.subject == "Physics"]
    chemistry_insights = [i for i in insights if i.subject == "Chemistry"]
    assert len(physics_insights) > 0
    assert chemistry_insights == []  # only 1 Chemistry event - below threshold


def test_compute_insights_has_no_user_identity_parameter_at_all():
    """Structural guarantee, not a runtime check: this pure function
    cannot leak across users because it has no concept of a user at all -
    isolation is entirely the caller's (backend/database.py + RLS)
    responsibility. See test_learning_insights_endpoint.py for the actual
    cross-user isolation tests at the API layer."""
    import inspect
    params = inspect.signature(ie.compute_insights).parameters
    assert "user_id" not in params
    assert "user" not in params


# ---------------------------------------------------------------------------
# 16. Empty/malformed evidence safely ignored.
# ---------------------------------------------------------------------------

def test_malformed_records_are_ignored_not_raised():
    records = [
        "not a record",
        None,
        123,
        _record(record_id="r1", subject="", signal_type=SIGNAL_CONFUSION, occurred_at=_at(0)),  # blank subject
        _record(record_id="r2", signal_type=SIGNAL_CONFUSION, occurred_at=_at(1)),
    ]
    # Should not raise, and the blank-subject record should not count.
    insights = ie.compute_insights(records)
    assert insights == []  # only 1 valid record remains - below threshold


def test_never_produces_a_numeric_score_field():
    records = [
        _record(record_id="r1", signal_type=SIGNAL_CONFUSION, occurred_at=_at(0)),
        _record(record_id="r2", signal_type=SIGNAL_CONFUSION, occurred_at=_at(1)),
    ]
    for insight in ie.compute_insights(records):
        assert not hasattr(insight, "score")
        assert not hasattr(insight, "correct_rate")
        assert not hasattr(insight, "percentage")
        assert insight.confidence in ie.VALID_INSIGHT_CONFIDENCES
        assert insight.insight_type in ie.VALID_INSIGHT_TYPES
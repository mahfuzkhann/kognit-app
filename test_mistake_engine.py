"""
Unit tests for backend/mistake_engine.py (Phase 6C Mistake Intelligence).

Run with: python3 -m pytest test_mistake_engine.py -v

No I/O, no database, no Gemini - pure function tests, same style as
test_mastery_engine.py / test_insight_engine.py / test_learning_snapshot.py.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from backend import mistake_engine as me
from backend.mastery_engine import EvidenceEvent


BASE_TIME = datetime(2026, 8, 1, 10, 0, 0, tzinfo=timezone.utc)


def _event(score, total_questions, days_offset=0, event_id="e"):
    return EvidenceEvent(
        score=score,
        total_questions=total_questions,
        occurred_at=BASE_TIME + timedelta(days=days_offset),
        event_id=event_id,
    )


def _evidence(subject="Physics", topic="Force & Motion", topic_key="force & motion", events=None):
    return me.TopicMistakeEvidence(subject=subject, topic=topic, topic_key=topic_key, events=events or [])


class TestNoDataAndInsufficientEvidence:
    def test_empty_evidence_list_returns_empty(self):
        assert me.compute_mistake_intelligence([]) == []

    def test_topic_with_no_events_is_skipped(self):
        result = me.compute_mistake_intelligence([_evidence(events=[])])
        assert result == []

    def test_single_attempt_with_wrong_answers_is_not_yet_repeated(self):
        # ONE bad quiz sitting (several wrong answers within it) must not
        # be reported as "repeated" - repetition requires multiple
        # SEPARATE attempts (see MIN_AFFECTED_ATTEMPTS_FOR_REPEATED_DIFFICULTY).
        events = [_event(score=2, total_questions=10, days_offset=0, event_id="a1")]
        result = me.compute_mistake_intelligence([_evidence(events=events)])
        assert result == []

    def test_all_correct_single_attempt_is_not_a_mistake(self):
        events = [_event(score=10, total_questions=10, days_offset=0, event_id="a1")]
        result = me.compute_mistake_intelligence([_evidence(events=events)])
        assert result == []


class TestRepeatedTopicDifficulty:
    def test_two_affected_attempts_triggers_repeated_difficulty(self):
        events = [
            _event(score=5, total_questions=10, days_offset=0, event_id="a1"),
            _event(score=6, total_questions=10, days_offset=1, event_id="a2"),
        ]
        result = me.compute_mistake_intelligence([_evidence(events=events)])
        assert len(result) == 1
        assert result[0]["mistake_type"] == me.MISTAKE_TYPE_REPEATED_TOPIC_DIFFICULTY
        assert result[0]["affected_attempts"] == 2

    def test_strong_performance_across_attempts_is_not_a_mistake(self):
        events = [
            _event(score=10, total_questions=10, days_offset=0, event_id="a1"),
            _event(score=9, total_questions=10, days_offset=1, event_id="a2"),
            _event(score=10, total_questions=10, days_offset=2, event_id="a3"),
        ]
        # Only a1/a3 all-correct, a2 has 1 wrong -> only 1 affected attempt.
        result = me.compute_mistake_intelligence([_evidence(events=events)])
        assert result == []

    def test_total_incorrect_is_summed_across_attempts(self):
        events = [
            _event(score=7, total_questions=10, days_offset=0, event_id="a1"),  # 3 wrong
            _event(score=8, total_questions=10, days_offset=1, event_id="a2"),  # 2 wrong
        ]
        result = me.compute_mistake_intelligence([_evidence(events=events)])
        assert result[0]["total_incorrect"] == 5

    def test_one_attempt_with_zero_wrong_does_not_count_as_affected(self):
        events = [
            _event(score=10, total_questions=10, days_offset=0, event_id="a1"),  # 0 wrong
            _event(score=5, total_questions=10, days_offset=1, event_id="a2"),   # 5 wrong
            _event(score=6, total_questions=10, days_offset=2, event_id="a3"),   # 4 wrong
        ]
        result = me.compute_mistake_intelligence([_evidence(events=events)])
        assert result[0]["affected_attempts"] == 2


class TestTemporalFields:
    def test_first_and_last_observed_at_are_correct(self):
        events = [
            _event(score=5, total_questions=10, days_offset=0, event_id="a1"),
            _event(score=6, total_questions=10, days_offset=10, event_id="a2"),
        ]
        result = me.compute_mistake_intelligence([_evidence(events=events)])
        assert result[0]["first_observed_at"] == BASE_TIME.isoformat()
        assert result[0]["last_observed_at"] == (BASE_TIME + timedelta(days=10)).isoformat()

    def test_unsorted_input_is_sorted_defensively(self):
        events = [
            _event(score=6, total_questions=10, days_offset=10, event_id="a2"),
            _event(score=5, total_questions=10, days_offset=0, event_id="a1"),
        ]
        result = me.compute_mistake_intelligence([_evidence(events=events)])
        assert result[0]["first_observed_at"] == BASE_TIME.isoformat()
        assert result[0]["last_observed_at"] == (BASE_TIME + timedelta(days=10)).isoformat()

    def test_results_sorted_by_last_observed_at_descending(self):
        older = _evidence(
            subject="Chemistry", topic="Stoichiometry", topic_key="stoichiometry",
            events=[
                _event(score=5, total_questions=10, days_offset=0, event_id="c1"),
                _event(score=5, total_questions=10, days_offset=1, event_id="c2"),
            ],
        )
        newer = _evidence(
            subject="Physics", topic="Force & Motion", topic_key="force & motion",
            events=[
                _event(score=5, total_questions=10, days_offset=5, event_id="p1"),
                _event(score=5, total_questions=10, days_offset=6, event_id="p2"),
            ],
        )
        result = me.compute_mistake_intelligence([older, newer])
        assert result[0]["subject"] == "Physics"
        assert result[1]["subject"] == "Chemistry"


class TestRecentVsEarlierSplit:
    def test_earlier_is_none_when_all_attempts_within_recent_window(self):
        events = [
            _event(score=5, total_questions=10, days_offset=i, event_id=f"a{i}")
            for i in range(3)
        ]
        result = me.compute_mistake_intelligence([_evidence(events=events)])
        assert result[0]["earlier_incorrect_count"] is None
        assert result[0]["recent_incorrect_count"] == 15  # 3 attempts * 5 wrong each

    def test_earlier_is_populated_when_more_than_window_size_attempts(self):
        # 7 attempts total; RECENT_WINDOW_SIZE = 5 -> 2 earlier, 5 recent.
        events = [
            _event(score=5, total_questions=10, days_offset=i, event_id=f"a{i}")
            for i in range(7)
        ]
        result = me.compute_mistake_intelligence([_evidence(events=events)])
        assert result[0]["earlier_incorrect_count"] == 10  # first 2 attempts * 5 wrong
        assert result[0]["recent_incorrect_count"] == 25   # last 5 attempts * 5 wrong

    def test_exactly_window_size_attempts_has_no_earlier_bucket(self):
        events = [
            _event(score=5, total_questions=10, days_offset=i, event_id=f"a{i}")
            for i in range(me.RECENT_WINDOW_SIZE)
        ]
        result = me.compute_mistake_intelligence([_evidence(events=events)])
        assert result[0]["earlier_incorrect_count"] is None


class TestNoUnjustifiedFields:
    def test_no_severity_field(self):
        events = [
            _event(score=5, total_questions=10, days_offset=0, event_id="a1"),
            _event(score=5, total_questions=10, days_offset=1, event_id="a2"),
        ]
        result = me.compute_mistake_intelligence([_evidence(events=events)])
        assert "severity" not in result[0]

    def test_no_confidence_field(self):
        events = [
            _event(score=5, total_questions=10, days_offset=0, event_id="a1"),
            _event(score=5, total_questions=10, days_offset=1, event_id="a2"),
        ]
        result = me.compute_mistake_intelligence([_evidence(events=events)])
        assert "confidence" not in result[0]

    def test_no_trend_or_percentage_field(self):
        events = [
            _event(score=5, total_questions=10, days_offset=i, event_id=f"a{i}")
            for i in range(7)
        ]
        result = me.compute_mistake_intelligence([_evidence(events=events)])
        entry = result[0]
        for forbidden in ("trend", "improvement_percent", "improvement_pct", "percentage", "is_improving"):
            assert forbidden not in entry

    def test_no_raw_question_or_answer_content(self):
        events = [
            _event(score=5, total_questions=10, days_offset=0, event_id="a1"),
            _event(score=5, total_questions=10, days_offset=1, event_id="a2"),
        ]
        result = me.compute_mistake_intelligence([_evidence(events=events)])
        entry_str = str(result[0])
        for forbidden in ("question_text", "selected_index", "correct_index"):
            assert forbidden not in entry_str


class TestMalformedAndLowDataInputs:
    def test_non_topicmistakeevidence_entries_are_skipped(self):
        good = _evidence(events=[
            _event(score=5, total_questions=10, days_offset=0, event_id="a1"),
            _event(score=5, total_questions=10, days_offset=1, event_id="a2"),
        ])
        result = me.compute_mistake_intelligence([good, "not-a-topic-evidence", None, 42])
        assert len(result) == 1

    def test_zero_total_questions_event_contributes_no_evidence(self):
        events = [
            _event(score=0, total_questions=0, days_offset=0, event_id="a1"),
            _event(score=5, total_questions=10, days_offset=1, event_id="a2"),
        ]
        result = me.compute_mistake_intelligence([_evidence(events=events)])
        # Only one real affected attempt -> below the threshold.
        assert result == []

    def test_multiple_subjects_and_topics_each_evaluated_independently(self):
        physics = _evidence(
            subject="Physics", topic="Force & Motion", topic_key="force & motion",
            events=[
                _event(score=5, total_questions=10, days_offset=0, event_id="p1"),
                _event(score=5, total_questions=10, days_offset=1, event_id="p2"),
            ],
        )
        chemistry_ok = _evidence(
            subject="Chemistry", topic="Stoichiometry", topic_key="stoichiometry",
            events=[
                _event(score=10, total_questions=10, days_offset=0, event_id="c1"),
            ],
        )
        result = me.compute_mistake_intelligence([physics, chemistry_ok])
        assert len(result) == 1
        assert result[0]["subject"] == "Physics"


class TestDeterminism:
    def test_repeated_execution_is_deterministic(self):
        events = [
            _event(score=5, total_questions=10, days_offset=i, event_id=f"a{i}")
            for i in range(7)
        ]
        evidence = [_evidence(events=events)]
        result1 = me.compute_mistake_intelligence(evidence)
        result2 = me.compute_mistake_intelligence(evidence)
        assert result1 == result2
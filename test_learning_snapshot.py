"""
Unit tests for backend/learning_snapshot.py (Phase 6B Learning Snapshot).

Run with: python3 -m pytest test_learning_snapshot.py -v

No I/O, no database, no Gemini - pure function tests, same style as
test_mastery_engine.py / test_insight_engine.py. Inputs here are the
already-computed dict shapes that backend.database.get_user_topic_profile
and backend.database.get_learning_insights actually return (see
test_profile_topics_endpoint.py / test_profile_insights_endpoint.py for
sample shapes) - not raw quiz_attempts/learning_evidence rows.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from backend import learning_snapshot as ls
from backend.mastery_engine import (
    STATUS_DEVELOPING,
    STATUS_IMPROVING,
    STATUS_INSUFFICIENT_EVIDENCE,
    STATUS_MASTERED,
    STATUS_NEEDS_PRACTICE,
    STATUS_STRONG,
)
from backend.insight_engine import (
    INSIGHT_CONFIDENCE_HIGH,
    INSIGHT_CONFIDENCE_LOW,
    INSIGHT_CONFIDENCE_MEDIUM,
    INSIGHT_EMERGING_STRENGTH,
    INSIGHT_RECURRING_CONFUSION,
    INSIGHT_REPEATED_DIFFICULTY,
)


def _topic(
    subject="Physics", topic="Force & Motion", topic_key="force & motion",
    status=STATUS_STRONG, correct_rate=0.8, attempt_count=3,
    last_attempt_at="2026-09-01T10:00:00+00:00",
):
    return {
        "subject": subject,
        "topic": topic,
        "topic_key": topic_key,
        "status": status,
        "attempt_count": attempt_count,
        "total_questions": attempt_count * 10,
        "total_correct": int(attempt_count * 10 * correct_rate) if correct_rate is not None else 0,
        "correct_rate": correct_rate,
        "last_attempt_at": last_attempt_at,
        "recent_attempts": [],
    }


def _insight(
    subject="Physics", topic="Force & Motion", topic_key="force & motion",
    insight_type=INSIGHT_EMERGING_STRENGTH, confidence=INSIGHT_CONFIDENCE_HIGH,
    evidence_count=3, first_observed_at="2026-08-20T10:00:00+00:00",
    last_observed_at="2026-09-01T10:00:00+00:00",
):
    return {
        "subject": subject,
        "topic": topic,
        "topic_key": topic_key,
        "insight_type": insight_type,
        "confidence": confidence,
        "evidence_count": evidence_count,
        "first_observed_at": first_observed_at,
        "last_observed_at": last_observed_at,
    }


class TestNoDataState:
    def test_empty_topics_and_insights_produce_explicit_empty_snapshot(self):
        snapshot = ls.build_learning_snapshot([], [])
        assert snapshot["strengths"] == []
        assert snapshot["needs_practice"] == []
        assert snapshot["recent_progress"] == {"state": "insufficient_evidence", "topics": []}
        assert snapshot["evidence_summary"]["has_quiz_evidence"] is False
        assert snapshot["evidence_summary"]["has_chat_evidence"] is False
        assert snapshot["evidence_summary"]["quiz_topics_count"] == 0
        assert snapshot["evidence_summary"]["chat_insights_count"] == 0

    def test_non_list_inputs_do_not_raise(self):
        snapshot = ls.build_learning_snapshot(None, None)
        assert snapshot["strengths"] == []
        assert snapshot["needs_practice"] == []


class TestStrengthsFromQuizEvidence:
    def test_strong_topic_is_a_strength(self):
        topics = [_topic(status=STATUS_STRONG)]
        snapshot = ls.build_learning_snapshot(topics, [])
        assert len(snapshot["strengths"]) == 1
        assert snapshot["strengths"][0]["source"] == "quiz"
        assert snapshot["strengths"][0]["status"] == STATUS_STRONG

    def test_mastered_topic_is_a_strength(self):
        topics = [_topic(status=STATUS_MASTERED)]
        snapshot = ls.build_learning_snapshot(topics, [])
        assert len(snapshot["strengths"]) == 1
        assert snapshot["strengths"][0]["status"] == STATUS_MASTERED

    def test_developing_topic_is_not_a_strength(self):
        topics = [_topic(status=STATUS_DEVELOPING)]
        snapshot = ls.build_learning_snapshot(topics, [])
        assert snapshot["strengths"] == []

    def test_insufficient_evidence_topic_is_not_a_strength(self):
        topics = [_topic(status=STATUS_INSUFFICIENT_EVIDENCE, attempt_count=1, correct_rate=1.0)]
        snapshot = ls.build_learning_snapshot(topics, [])
        assert snapshot["strengths"] == []


class TestStrengthsFromChatEvidence:
    def test_high_confidence_emerging_strength_is_included(self):
        insights = [_insight(insight_type=INSIGHT_EMERGING_STRENGTH, confidence=INSIGHT_CONFIDENCE_HIGH)]
        snapshot = ls.build_learning_snapshot([], insights)
        assert len(snapshot["strengths"]) == 1
        assert snapshot["strengths"][0]["source"] == "chat"

    def test_medium_confidence_emerging_strength_is_included(self):
        insights = [_insight(insight_type=INSIGHT_EMERGING_STRENGTH, confidence=INSIGHT_CONFIDENCE_MEDIUM)]
        snapshot = ls.build_learning_snapshot([], insights)
        assert len(snapshot["strengths"]) == 1

    def test_low_confidence_emerging_strength_is_excluded(self):
        # LOW confidence = zero KNOWN-attribution qualifying events - too
        # thin a basis to declare a strength (see module docstring).
        insights = [_insight(insight_type=INSIGHT_EMERGING_STRENGTH, confidence=INSIGHT_CONFIDENCE_LOW)]
        snapshot = ls.build_learning_snapshot([], insights)
        assert snapshot["strengths"] == []

    def test_repeated_difficulty_insight_is_never_a_strength(self):
        insights = [_insight(insight_type=INSIGHT_REPEATED_DIFFICULTY, confidence=INSIGHT_CONFIDENCE_HIGH)]
        snapshot = ls.build_learning_snapshot([], insights)
        assert snapshot["strengths"] == []

    def test_recurring_confusion_insight_is_never_a_strength(self):
        insights = [_insight(insight_type=INSIGHT_RECURRING_CONFUSION, confidence=INSIGHT_CONFIDENCE_HIGH)]
        snapshot = ls.build_learning_snapshot([], insights)
        assert snapshot["strengths"] == []


class TestNeedsPractice:
    def test_needs_practice_status_is_surfaced(self):
        topics = [_topic(status=STATUS_NEEDS_PRACTICE, correct_rate=0.3)]
        snapshot = ls.build_learning_snapshot(topics, [])
        assert len(snapshot["needs_practice"]) == 1
        assert snapshot["needs_practice"][0]["status"] == STATUS_NEEDS_PRACTICE

    def test_developing_status_is_surfaced(self):
        topics = [_topic(status=STATUS_DEVELOPING, correct_rate=0.6)]
        snapshot = ls.build_learning_snapshot(topics, [])
        assert len(snapshot["needs_practice"]) == 1
        assert snapshot["needs_practice"][0]["status"] == STATUS_DEVELOPING

    def test_strong_status_is_not_needs_practice(self):
        topics = [_topic(status=STATUS_STRONG)]
        snapshot = ls.build_learning_snapshot(topics, [])
        assert snapshot["needs_practice"] == []

    def test_improving_status_is_not_needs_practice(self):
        topics = [_topic(status=STATUS_IMPROVING, correct_rate=0.6)]
        snapshot = ls.build_learning_snapshot(topics, [])
        assert snapshot["needs_practice"] == []

    def test_chat_evidence_never_overrides_mastery_status(self):
        # A recurring_confusion insight exists for the SAME topic_key as
        # a Strong quiz topic - the topic must still be reported as a
        # strength, not moved into needs_practice, per the Phase 6B brief.
        topics = [_topic(status=STATUS_STRONG, topic_key="force & motion")]
        insights = [_insight(insight_type=INSIGHT_RECURRING_CONFUSION, topic_key="force & motion")]
        snapshot = ls.build_learning_snapshot(topics, insights)
        assert len(snapshot["strengths"]) == 1
        assert snapshot["needs_practice"] == []

    def test_matching_chat_insight_is_attached_as_supporting_evidence(self):
        topics = [_topic(status=STATUS_NEEDS_PRACTICE, topic_key="force & motion", correct_rate=0.3)]
        insights = [_insight(insight_type=INSIGHT_RECURRING_CONFUSION, topic_key="force & motion")]
        snapshot = ls.build_learning_snapshot(topics, insights)
        assert len(snapshot["needs_practice"]) == 1
        supporting = snapshot["needs_practice"][0]["supporting_chat_evidence"]
        assert len(supporting) == 1
        assert supporting[0]["insight_type"] == INSIGHT_RECURRING_CONFUSION

    def test_non_matching_topic_key_is_not_attached(self):
        topics = [_topic(status=STATUS_NEEDS_PRACTICE, topic_key="force & motion", correct_rate=0.3)]
        insights = [_insight(insight_type=INSIGHT_RECURRING_CONFUSION, topic_key="a totally different topic")]
        snapshot = ls.build_learning_snapshot(topics, insights)
        assert snapshot["needs_practice"][0]["supporting_chat_evidence"] == []

    def test_subject_only_insight_never_matched_to_a_specific_topic(self):
        # An insight with no topic_key of its own (subject-wide) must
        # never be attached to one specific quiz topic (see
        # _insight_group_key docstring) - that would be an inference
        # this module is not entitled to make.
        topics = [_topic(status=STATUS_NEEDS_PRACTICE, subject="Physics", topic_key="force & motion", correct_rate=0.3)]
        insights = [_insight(insight_type=INSIGHT_RECURRING_CONFUSION, subject="Physics", topic=None, topic_key=None)]
        snapshot = ls.build_learning_snapshot(topics, insights)
        assert snapshot["needs_practice"][0]["supporting_chat_evidence"] == []


class TestRecentProgress:
    def test_insufficient_evidence_when_no_topic_meets_improving_min_attempts(self):
        topics = [_topic(status=STATUS_INSUFFICIENT_EVIDENCE, attempt_count=1)]
        snapshot = ls.build_learning_snapshot(topics, [])
        assert snapshot["recent_progress"]["state"] == "insufficient_evidence"
        assert snapshot["recent_progress"]["topics"] == []

    def test_no_data_at_all_is_insufficient_evidence(self):
        snapshot = ls.build_learning_snapshot([], [])
        assert snapshot["recent_progress"]["state"] == "insufficient_evidence"

    def test_no_progress_detected_when_eligible_but_none_improving(self):
        topics = [_topic(status=STATUS_STRONG, attempt_count=5)]
        snapshot = ls.build_learning_snapshot(topics, [])
        assert snapshot["recent_progress"]["state"] == "no_progress_detected"
        assert snapshot["recent_progress"]["topics"] == []

    def test_improving_topic_is_surfaced_as_progress(self):
        topics = [_topic(status=STATUS_IMPROVING, attempt_count=3, correct_rate=0.6)]
        snapshot = ls.build_learning_snapshot(topics, [])
        assert snapshot["recent_progress"]["state"] == "improving"
        assert len(snapshot["recent_progress"]["topics"]) == 1
        assert snapshot["recent_progress"]["topics"][0]["subject"] == "Physics"

    def test_never_fabricates_a_percentage_improvement_field(self):
        topics = [_topic(status=STATUS_IMPROVING, attempt_count=3, correct_rate=0.6)]
        snapshot = ls.build_learning_snapshot(topics, [])
        entry = snapshot["recent_progress"]["topics"][0]
        assert "improvement_percent" not in entry
        assert "improvement_pct" not in entry


class TestMixedAndLowDataStates:
    def test_quiz_only_data(self):
        topics = [_topic(status=STATUS_STRONG)]
        snapshot = ls.build_learning_snapshot(topics, [])
        assert snapshot["evidence_summary"]["has_quiz_evidence"] is True
        assert snapshot["evidence_summary"]["has_chat_evidence"] is False

    def test_chat_only_data(self):
        insights = [_insight(insight_type=INSIGHT_EMERGING_STRENGTH, confidence=INSIGHT_CONFIDENCE_HIGH)]
        snapshot = ls.build_learning_snapshot([], insights)
        assert snapshot["evidence_summary"]["has_quiz_evidence"] is False
        assert snapshot["evidence_summary"]["has_chat_evidence"] is True
        assert snapshot["recent_progress"]["state"] == "insufficient_evidence"

    def test_mixed_confidence_evidence_does_not_crash_and_filters_correctly(self):
        insights = [
            _insight(subject="Physics", topic_key="k1", insight_type=INSIGHT_EMERGING_STRENGTH, confidence=INSIGHT_CONFIDENCE_LOW),
            _insight(subject="Chemistry", topic_key="k2", insight_type=INSIGHT_EMERGING_STRENGTH, confidence=INSIGHT_CONFIDENCE_HIGH),
        ]
        snapshot = ls.build_learning_snapshot([], insights)
        assert len(snapshot["strengths"]) == 1
        assert snapshot["strengths"][0]["subject"] == "Chemistry"

    def test_malformed_entries_are_skipped_not_fatal(self):
        topics = [_topic(status=STATUS_STRONG), "not-a-dict", {"status": STATUS_STRONG}]
        insights = [_insight(), None, 42]
        snapshot = ls.build_learning_snapshot(topics, insights)
        # Well-formed entries still produce output despite the malformed
        # ones sitting alongside them.
        assert len(snapshot["strengths"]) >= 1


class TestEvidenceSummaryNeverExposesRawContent:
    def test_evidence_summary_is_counts_only(self):
        topics = [_topic(status=STATUS_STRONG)]
        insights = [_insight()]
        snapshot = ls.build_learning_snapshot(topics, insights)
        summary = snapshot["evidence_summary"]
        for value in summary.values():
            assert isinstance(value, (int, bool))


class TestStrengthsNeedsPracticeReconciliation:
    """
    Hardening fix, pre-Phase-7 (P1 #3): the same (subject, topic_key) must
    never appear in both Strengths and Needs Practice, and must never
    produce a duplicate Strengths entry when both quiz and chat evidence
    exist for it. Four required regression scenarios (A-D).
    """

    def test_a_quiz_weak_plus_chat_emerging_strength_yields_only_needs_practice(self):
        # A: quiz weak + chat emerging_strength => only Needs Practice
        # presentation - no Strengths entry for this topic at all.
        topics = [_topic(status=STATUS_NEEDS_PRACTICE, subject="Math", topic_key="algebra")]
        insights = [_insight(
            insight_type=INSIGHT_EMERGING_STRENGTH, confidence=INSIGHT_CONFIDENCE_HIGH,
            subject="Math", topic_key="algebra",
        )]
        snapshot = ls.build_learning_snapshot(topics, insights)
        assert snapshot["strengths"] == []
        assert len(snapshot["needs_practice"]) == 1
        assert snapshot["needs_practice"][0]["topic_key"] == "algebra"

    def test_a_developing_plus_chat_emerging_strength_yields_only_needs_practice(self):
        # Same as A but for the other weak status, Developing.
        topics = [_topic(status=STATUS_DEVELOPING, subject="Math", topic_key="algebra", correct_rate=0.6)]
        insights = [_insight(
            insight_type=INSIGHT_EMERGING_STRENGTH, confidence=INSIGHT_CONFIDENCE_HIGH,
            subject="Math", topic_key="algebra",
        )]
        snapshot = ls.build_learning_snapshot(topics, insights)
        assert snapshot["strengths"] == []
        assert len(snapshot["needs_practice"]) == 1

    def test_b_quiz_strong_plus_chat_emerging_strength_yields_one_canonical_entry(self):
        # B: quiz strong + chat emerging_strength => ONE canonical
        # Strengths topic, with the chat insight merged in as supporting
        # evidence on that same entry - never a second, separate row.
        topics = [_topic(status=STATUS_STRONG, subject="Math", topic_key="algebra")]
        insights = [_insight(
            insight_type=INSIGHT_EMERGING_STRENGTH, confidence=INSIGHT_CONFIDENCE_HIGH,
            subject="Math", topic_key="algebra",
        )]
        snapshot = ls.build_learning_snapshot(topics, insights)
        assert len(snapshot["strengths"]) == 1
        entry = snapshot["strengths"][0]
        assert entry["source"] == "quiz"
        assert entry["status"] == STATUS_STRONG
        assert len(entry["supporting_chat_evidence"]) == 1
        assert entry["supporting_chat_evidence"][0]["insight_type"] == INSIGHT_EMERGING_STRENGTH
        assert snapshot["needs_practice"] == []

    def test_b_mastered_plus_chat_emerging_strength_yields_one_canonical_entry(self):
        topics = [_topic(status=STATUS_MASTERED, subject="Math", topic_key="algebra")]
        insights = [_insight(
            insight_type=INSIGHT_EMERGING_STRENGTH, confidence=INSIGHT_CONFIDENCE_MEDIUM,
            subject="Math", topic_key="algebra",
        )]
        snapshot = ls.build_learning_snapshot(topics, insights)
        assert len(snapshot["strengths"]) == 1
        assert snapshot["strengths"][0]["status"] == STATUS_MASTERED
        assert len(snapshot["strengths"][0]["supporting_chat_evidence"]) == 1

    def test_c_different_subjects_same_topic_key_remain_separate(self):
        # C: different subjects with the same topic_key must remain
        # separate - a chat emerging_strength for "Physics/algebra" must
        # NOT merge into or suppress a "Math/algebra" quiz entry, and must
        # independently create its own Strengths entry since quiz has no
        # opinion about Physics/algebra at all.
        topics = [_topic(status=STATUS_STRONG, subject="Math", topic_key="algebra")]
        insights = [_insight(
            insight_type=INSIGHT_EMERGING_STRENGTH, confidence=INSIGHT_CONFIDENCE_HIGH,
            subject="Physics", topic_key="algebra",
        )]
        snapshot = ls.build_learning_snapshot(topics, insights)
        assert len(snapshot["strengths"]) == 2
        subjects = {(s["subject"], s["topic_key"]) for s in snapshot["strengths"]}
        assert subjects == {("Math", "algebra"), ("Physics", "algebra")}
        # The Math/algebra entry must not have picked up the Physics
        # insight as supporting evidence.
        math_entry = next(s for s in snapshot["strengths"] if s["subject"] == "Math")
        assert "supporting_chat_evidence" not in math_entry

    def test_c_weak_quiz_topic_not_affected_by_same_topic_key_different_subject_strength(self):
        # The mirror case: a Needs Practice topic in one subject must not
        # be affected by an emerging_strength insight for the identical
        # topic_key under a DIFFERENT subject.
        topics = [_topic(status=STATUS_NEEDS_PRACTICE, subject="Math", topic_key="algebra")]
        insights = [_insight(
            insight_type=INSIGHT_EMERGING_STRENGTH, confidence=INSIGHT_CONFIDENCE_HIGH,
            subject="Physics", topic_key="algebra",
        )]
        snapshot = ls.build_learning_snapshot(topics, insights)
        assert len(snapshot["needs_practice"]) == 1
        assert snapshot["needs_practice"][0]["subject"] == "Math"
        # Physics/algebra has no quiz evidence at all, so the chat
        # insight independently becomes its own Strengths entry.
        assert len(snapshot["strengths"]) == 1
        assert snapshot["strengths"][0]["subject"] == "Physics"

    def test_d_differently_cased_topic_display_text_is_still_the_same_logical_topic(self):
        # D: matching is done on topic_key (already normalized at write
        # time by backend.database.normalize_topic_key), never on the raw
        # `topic` display string - so differently-cased/spaced display
        # text for the identical topic_key must still be recognized as
        # the same logical topic and merge into one canonical entry.
        topics = [_topic(status=STATUS_STRONG, subject="Math", topic="Algebra", topic_key="algebra")]
        insights = [_insight(
            insight_type=INSIGHT_EMERGING_STRENGTH, confidence=INSIGHT_CONFIDENCE_HIGH,
            subject="Math", topic="  algebra   basics", topic_key="algebra",
        )]
        snapshot = ls.build_learning_snapshot(topics, insights)
        assert len(snapshot["strengths"]) == 1
        assert snapshot["strengths"][0]["topic_key"] == "algebra"
        assert len(snapshot["strengths"][0]["supporting_chat_evidence"]) == 1

    def test_insufficient_evidence_quiz_topic_not_promoted_by_chat_strength(self):
        # A quiz topic with only Insufficient Evidence still counts as
        # "quiz has an opinion" and must not gain an independent
        # chat-driven Strengths entry.
        topics = [_topic(status=STATUS_INSUFFICIENT_EVIDENCE, subject="Math", topic_key="algebra", attempt_count=1)]
        insights = [_insight(
            insight_type=INSIGHT_EMERGING_STRENGTH, confidence=INSIGHT_CONFIDENCE_HIGH,
            subject="Math", topic_key="algebra",
        )]
        snapshot = ls.build_learning_snapshot(topics, insights)
        assert snapshot["strengths"] == []
        assert snapshot["needs_practice"] == []

    def test_improving_quiz_topic_not_promoted_by_chat_strength_into_second_entry(self):
        # Improving is a positive but non-Strong/Mastered status - quiz
        # has an opinion, so chat cannot independently add a strength
        # entry, but Improving itself is also not a Strengths status, so
        # the correct outcome is zero strengths for this topic.
        topics = [_topic(status=STATUS_IMPROVING, subject="Math", topic_key="algebra", correct_rate=0.6)]
        insights = [_insight(
            insight_type=INSIGHT_EMERGING_STRENGTH, confidence=INSIGHT_CONFIDENCE_HIGH,
            subject="Math", topic_key="algebra",
        )]
        snapshot = ls.build_learning_snapshot(topics, insights)
        assert snapshot["strengths"] == []

    def test_no_duplicate_topic_in_both_strengths_and_needs_practice_property(self):
        # General property check across a richer mixed fixture: no
        # (subject, topic_key) may ever appear in both lists at once.
        topics = [
            _topic(status=STATUS_NEEDS_PRACTICE, subject="Math", topic_key="algebra"),
            _topic(status=STATUS_STRONG, subject="Physics", topic_key="force & motion"),
            _topic(status=STATUS_DEVELOPING, subject="Chemistry", topic_key="stoichiometry", correct_rate=0.6),
        ]
        insights = [
            _insight(insight_type=INSIGHT_EMERGING_STRENGTH, confidence=INSIGHT_CONFIDENCE_HIGH, subject="Math", topic_key="algebra"),
            _insight(insight_type=INSIGHT_EMERGING_STRENGTH, confidence=INSIGHT_CONFIDENCE_HIGH, subject="Physics", topic_key="force & motion"),
            _insight(insight_type=INSIGHT_EMERGING_STRENGTH, confidence=INSIGHT_CONFIDENCE_MEDIUM, subject="Chemistry", topic_key="stoichiometry"),
        ]
        snapshot = ls.build_learning_snapshot(topics, insights)
        strength_keys = {(s["subject"], s["topic_key"]) for s in snapshot["strengths"]}
        needs_practice_keys = {(n["subject"], n["topic_key"]) for n in snapshot["needs_practice"]}
        assert strength_keys.isdisjoint(needs_practice_keys)
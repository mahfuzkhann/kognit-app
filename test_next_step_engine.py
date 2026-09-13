"""
Unit tests for backend/next_step_engine.py (Phase 6D Next-Step Engine).

Run with: GEMINI_API_KEY=dummy python3 -m pytest test_next_step_engine.py -v

No I/O, no database, no Gemini - pure function tests, same style as
test_mastery_engine.py / test_insight_engine.py / test_learning_snapshot.py /
test_mistake_engine.py.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from backend import next_step_engine as nse


def _topic(
    subject="Math", topic="Algebra", topic_key="algebra", status="Needs Practice",
    attempt_count=3, correct_rate=0.3, last_attempt_at="2026-09-01T10:00:00+00:00",
):
    return {
        "subject": subject, "topic": topic, "topic_key": topic_key, "status": status,
        "attempt_count": attempt_count, "total_questions": attempt_count * 10,
        "total_correct": int(attempt_count * 10 * correct_rate), "correct_rate": correct_rate,
        "last_attempt_at": last_attempt_at, "recent_attempts": [],
    }


def _insight(
    subject="Math", topic="Algebra", topic_key="algebra", insight_type="repeated_difficulty",
    confidence="high", evidence_count=3, first_observed_at="2026-08-01T10:00:00+00:00",
    last_observed_at="2026-08-20T10:00:00+00:00",
):
    return {
        "subject": subject, "topic": topic, "topic_key": topic_key, "insight_type": insight_type,
        "confidence": confidence, "evidence_count": evidence_count,
        "first_observed_at": first_observed_at, "last_observed_at": last_observed_at,
    }


def _mistake(
    subject="Math", topic="Algebra", topic_key="algebra", affected_attempts=3, total_incorrect=15,
    first_observed_at="2026-08-01T10:00:00+00:00", last_observed_at="2026-09-01T10:00:00+00:00",
    recent_incorrect_count=15, earlier_incorrect_count=None,
):
    return {
        "mistake_type": "repeated_topic_difficulty", "subject": subject, "topic": topic,
        "topic_key": topic_key, "affected_attempts": affected_attempts, "total_incorrect": total_incorrect,
        "first_observed_at": first_observed_at, "last_observed_at": last_observed_at,
        "recent_incorrect_count": recent_incorrect_count, "earlier_incorrect_count": earlier_incorrect_count,
    }


class TestNoEvidence:
    def test_all_empty_returns_empty(self):
        assert nse.compute_next_steps([], [], []) == []

    def test_non_list_inputs_do_not_raise(self):
        assert nse.compute_next_steps(None, None, None) == []


class TestBasicSingleTopicRules:
    def test_needs_practice_topic_becomes_practice(self):
        result = nse.compute_next_steps([_topic(status="Needs Practice")], [], [])
        assert len(result) == 1
        assert result[0]["recommendation_type"] == "practice_topic"

    def test_developing_topic_becomes_practice(self):
        result = nse.compute_next_steps([_topic(status="Developing")], [], [])
        assert result[0]["recommendation_type"] == "practice_topic"

    def test_improving_topic_becomes_continue(self):
        result = nse.compute_next_steps([_topic(status="Improving")], [], [])
        assert result[0]["recommendation_type"] == "continue_topic"

    def test_strong_topic_becomes_continue(self):
        result = nse.compute_next_steps([_topic(status="Strong")], [], [])
        assert result[0]["recommendation_type"] == "continue_topic"

    def test_mastered_topic_becomes_continue(self):
        result = nse.compute_next_steps([_topic(status="Mastered")], [], [])
        assert result[0]["recommendation_type"] == "continue_topic"
        assert "Mastered" in result[0]["reason"]

    def test_insufficient_evidence_becomes_take_quiz(self):
        result = nse.compute_next_steps([_topic(status="Insufficient Evidence", attempt_count=1)], [], [])
        assert result[0]["recommendation_type"] == "take_quiz"


class TestMistakeIntegration:
    def test_one_bad_attempt_alone_produces_no_mistake_driven_recommendation(self):
        # No 6C mistake entry at all (single-attempt mistakes never exist
        # per mistake_engine's own threshold) + Strong quiz status ->
        # continue, not practice.
        result = nse.compute_next_steps([_topic(status="Strong")], [], [])
        assert result[0]["recommendation_type"] == "continue_topic"

    def test_repeated_mistake_evidence_overrides_positive_quiz_status(self):
        # Strong aggregate rate BUT 6C found repeated wrong answers ->
        # PRACTICE_TOPIC wins (co-equal quiz-tier evidence, see module
        # docstring).
        topics = [_topic(status="Strong", correct_rate=0.75)]
        mistakes = [_mistake()]
        result = nse.compute_next_steps(topics, [], mistakes)
        assert len(result) == 1
        assert result[0]["recommendation_type"] == "practice_topic"
        assert "quiz_mistake" in result[0]["evidence_sources"]

    def test_mistake_evidence_combined_with_weak_status_is_single_entry(self):
        topics = [_topic(status="Needs Practice")]
        mistakes = [_mistake()]
        result = nse.compute_next_steps(topics, [], mistakes)
        assert len(result) == 1
        assert set(result[0]["evidence_sources"]) >= {"quiz_mastery", "quiz_mistake"}


class TestDeduplication:
    def test_weakness_plus_matching_mistake_is_one_recommendation(self):
        topics = [_topic(status="Needs Practice")]
        insights = [_insight(insight_type="repeated_difficulty")]
        mistakes = [_mistake()]
        result = nse.compute_next_steps(topics, insights, mistakes)
        assert len(result) == 1
        assert result[0]["recommendation_type"] == "practice_topic"

    def test_mastered_plus_emerging_strength_is_one_recommendation(self):
        topics = [_topic(status="Mastered")]
        insights = [_insight(insight_type="emerging_strength", confidence="high")]
        result = nse.compute_next_steps(topics, insights, [])
        assert len(result) == 1
        assert result[0]["recommendation_type"] == "continue_topic"
        assert "chat_insight" in result[0]["evidence_sources"]

    def test_same_subject_topic_from_multiple_sources_is_one_canonical_entry(self):
        topics = [_topic(status="Needs Practice")]
        insights = [
            _insight(insight_type="repeated_difficulty"),
            _insight(insight_type="recurring_confusion"),
        ]
        mistakes = [_mistake()]
        result = nse.compute_next_steps(topics, insights, mistakes)
        matching = [r for r in result if r["subject"] == "Math" and r["topic_key"] == "algebra"]
        assert len(matching) == 1


class TestConflictResolution:
    def test_quiz_weak_status_suppresses_contradictory_strength_insight(self):
        topics = [_topic(status="Needs Practice")]
        insights = [_insight(insight_type="emerging_strength", confidence="high")]
        result = nse.compute_next_steps(topics, insights, [])
        assert len(result) == 1
        assert result[0]["recommendation_type"] == "practice_topic"
        # The contradictory strength insight must not appear as evidence
        # for what is fundamentally a weakness recommendation.
        assert "chat_insight" not in result[0]["evidence_sources"]

    def test_quiz_positive_status_suppresses_contradictory_confusion_insight(self):
        topics = [_topic(status="Strong")]
        insights = [_insight(insight_type="recurring_confusion")]
        result = nse.compute_next_steps(topics, insights, [])
        assert len(result) == 1
        assert result[0]["recommendation_type"] == "continue_topic"
        assert "chat_insight" not in result[0]["evidence_sources"]

    def test_chat_only_conflicting_signals_prefers_weakness(self):
        # Zero quiz evidence; both a confusion signal and an
        # emerging_strength signal exist for the same identity.
        insights = [
            _insight(insight_type="recurring_confusion", topic_key="algebra"),
            _insight(insight_type="emerging_strength", confidence="high", topic_key="algebra"),
        ]
        result = nse.compute_next_steps([], insights, [])
        assert len(result) == 1
        assert result[0]["recommendation_type"] == "review_topic"


class TestChatOnlyEvidence:
    def test_low_confidence_emerging_strength_alone_creates_no_recommendation(self):
        insights = [_insight(insight_type="emerging_strength", confidence="low")]
        result = nse.compute_next_steps([], insights, [])
        assert result == []

    def test_medium_confidence_emerging_strength_alone_creates_continue(self):
        insights = [_insight(insight_type="emerging_strength", confidence="medium")]
        result = nse.compute_next_steps([], insights, [])
        assert len(result) == 1
        assert result[0]["recommendation_type"] == "continue_topic"

    def test_repeated_difficulty_alone_creates_review_at_any_confidence(self):
        insights = [_insight(insight_type="repeated_difficulty", confidence="low")]
        result = nse.compute_next_steps([], insights, [])
        assert len(result) == 1
        assert result[0]["recommendation_type"] == "review_topic"

    def test_subject_wide_insight_with_no_topic_key_is_excluded(self):
        insights = [_insight(insight_type="repeated_difficulty", topic=None, topic_key=None)]
        result = nse.compute_next_steps([], insights, [])
        assert result == []


class TestTopicIdentity:
    def test_same_topic_name_different_subject_stays_separate(self):
        topics = [
            _topic(subject="Math", topic="Algebra", topic_key="algebra", status="Needs Practice"),
            _topic(subject="Physics", topic="Algebra", topic_key="algebra", status="Strong"),
        ]
        result = nse.compute_next_steps(topics, [], [])
        assert len(result) == 2
        types_by_subject = {r["subject"]: r["recommendation_type"] for r in result}
        assert types_by_subject["Math"] == "practice_topic"
        assert types_by_subject["Physics"] == "continue_topic"

    def test_same_subject_different_topic_keys_stay_separate(self):
        topics = [
            _topic(subject="Math", topic="Algebra", topic_key="algebra", status="Needs Practice"),
            _topic(subject="Math", topic="Geometry", topic_key="geometry", status="Strong"),
        ]
        result = nse.compute_next_steps(topics, [], [])
        assert len(result) == 2


class TestRankingAndCap:
    def test_deterministic_ordering_practice_before_review_before_continue_before_take_quiz(self):
        topics = [
            _topic(subject="A", topic="A1", topic_key="a1", status="Improving"),
            _topic(subject="B", topic="B1", topic_key="b1", status="Insufficient Evidence", attempt_count=1),
            _topic(subject="C", topic="C1", topic_key="c1", status="Needs Practice"),
        ]
        insights = [_insight(subject="D", topic="D1", topic_key="d1", insight_type="repeated_difficulty")]
        result = nse.compute_next_steps(topics, insights, [])
        types_in_order = [r["recommendation_type"] for r in result]
        assert types_in_order == ["practice_topic", "review_topic", "continue_topic"]
        # take_quiz (tier 4) dropped by the cap since there are already 3
        # higher-tier recommendations.

    def test_max_three_recommendations_returned(self):
        topics = [
            _topic(subject=f"Subj{i}", topic=f"T{i}", topic_key=f"t{i}", status="Needs Practice")
            for i in range(5)
        ]
        result = nse.compute_next_steps(topics, [], [])
        assert len(result) == 3

    def test_more_recent_evidence_wins_tie_within_same_tier(self):
        topics = [
            _topic(subject="A", topic="A1", topic_key="a1", status="Needs Practice", last_attempt_at="2026-08-01T10:00:00+00:00"),
            _topic(subject="B", topic="B1", topic_key="b1", status="Needs Practice", last_attempt_at="2026-09-01T10:00:00+00:00"),
        ]
        result = nse.compute_next_steps(topics, [], [])
        assert result[0]["subject"] == "B"
        assert result[1]["subject"] == "A"

    def test_priority_field_reflects_final_rank(self):
        topics = [_topic(status="Needs Practice")]
        result = nse.compute_next_steps(topics, [], [])
        assert result[0]["priority"] == 1

    def test_alphabetical_tiebreak_when_recency_equal(self):
        topics = [
            _topic(subject="Zeta", topic="Z1", topic_key="z1", status="Needs Practice", last_attempt_at="2026-09-01T10:00:00+00:00"),
            _topic(subject="Alpha", topic="A1", topic_key="a1", status="Needs Practice", last_attempt_at="2026-09-01T10:00:00+00:00"),
        ]
        result = nse.compute_next_steps(topics, [], [])
        assert result[0]["subject"] == "Alpha"
        assert result[1]["subject"] == "Zeta"


class TestNoOverclaimingAndNoRawContent:
    def test_no_psychological_or_ability_language(self):
        topics = [_topic(status="Needs Practice")]
        mistakes = [_mistake()]
        result = nse.compute_next_steps(topics, [], mistakes)
        reason = result[0]["reason"].lower()
        for forbidden in ("bad at", "weak brain", "learning style", "visual learner", "not smart", "incapable"):
            assert forbidden not in reason

    def test_no_raw_evidence_fields_leak(self):
        topics = [_topic(status="Needs Practice")]
        result = nse.compute_next_steps(topics, [], [])
        entry_str = str(result[0])
        for forbidden in ("question_text", "selected_index", "correct_index", "recent_attempts"):
            assert forbidden not in entry_str

    def test_response_is_stable_across_repeated_calls(self):
        topics = [_topic(status="Needs Practice")]
        insights = [_insight(insight_type="repeated_difficulty")]
        mistakes = [_mistake()]
        r1 = nse.compute_next_steps(topics, insights, mistakes)
        r2 = nse.compute_next_steps(topics, insights, mistakes)
        assert r1 == r2


class TestMalformedInputs:
    def test_malformed_topic_entries_are_skipped(self):
        topics = [_topic(status="Needs Practice"), "not-a-dict", {"status": "Needs Practice"}]
        result = nse.compute_next_steps(topics, [], [])
        assert len(result) == 1

    def test_malformed_insight_entries_are_skipped(self):
        insights = [_insight(), None, 42, {"insight_type": "repeated_difficulty"}]
        result = nse.compute_next_steps([], insights, [])
        assert len(result) == 1

    def test_malformed_mistake_entries_are_skipped(self):
        topics = [_topic(status="Strong")]
        mistakes = [_mistake(), "not-a-dict", {}]
        result = nse.compute_next_steps(topics, [], mistakes)
        assert len(result) == 1
        assert result[0]["recommendation_type"] == "practice_topic"
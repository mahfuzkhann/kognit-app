"""
Unit tests for backend/learning_memory.py (Phase 5B/5C/5E pure domain logic).

Run with: python3 -m pytest test_learning_memory.py -v

No I/O, no Gemini, no database - pure function tests, same style as
test_mastery_engine.py.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from backend import learning_memory as lm


# ---------------------------------------------------------------------------
# Academic Context Resolver
# ---------------------------------------------------------------------------

class TestResolveAcademicContext:
    def test_no_explicit_context_returns_unknown(self):
        result = lm.resolve_academic_context()
        assert result.confidence == lm.CONFIDENCE_UNKNOWN
        assert result.subject is None
        assert result.topic is None

    def test_partial_context_subject_only_returns_unknown(self):
        # Never Known/Probable with only half the required information -
        # this is exactly the "do not fabricate the missing half" guardrail.
        result = lm.resolve_academic_context(explicit_subject="Physics")
        assert result.confidence == lm.CONFIDENCE_UNKNOWN

    def test_partial_context_topic_only_returns_unknown(self):
        result = lm.resolve_academic_context(explicit_topic="Force & Motion")
        assert result.confidence == lm.CONFIDENCE_UNKNOWN

    def test_blank_strings_are_treated_as_absent(self):
        result = lm.resolve_academic_context(explicit_subject="   ", explicit_topic="   ")
        assert result.confidence == lm.CONFIDENCE_UNKNOWN

    def test_full_explicit_context_returns_known(self):
        result = lm.resolve_academic_context(explicit_subject="Physics", explicit_topic="Force & Motion")
        assert result.confidence == lm.CONFIDENCE_KNOWN
        assert result.subject == "Physics"
        assert result.topic == "Force & Motion"

    def test_never_returns_probable_today(self):
        # No caller currently has a "probable" source (see module
        # docstring) - this pins that fact so a future change to this
        # function is a deliberate decision, not an accidental drift.
        for subject in (None, "Physics"):
            for topic in (None, "Force"):
                result = lm.resolve_academic_context(explicit_subject=subject, explicit_topic=topic)
                assert result.confidence != lm.CONFIDENCE_PROBABLE


class TestResolveAcademicContextSubjectDetection:
    """Phase 5C activation: deterministic subject-only detection from the
    student's own message/history. Test names track the brief's own Case
    numbers for traceability."""

    def test_case1_explicit_subject_in_current_message_is_known(self):
        result = lm.resolve_academic_context(prompt="Physics-e Newton's second law bujhte parchi na.")
        assert result.confidence == lm.CONFIDENCE_KNOWN
        assert result.subject == "Physics"

    def test_case1_topic_is_never_extracted_even_when_subject_is_known(self):
        # Deliberate scope boundary - see resolve_academic_context's
        # docstring. This is the one place this implementation
        # intentionally falls short of the brief's own Case 1 example.
        result = lm.resolve_academic_context(prompt="Physics-e Newton's second law bujhte parchi na.")
        assert result.topic is None

    def test_case2_same_context_continuation_inherits_as_probable(self):
        history = [{"role": "user", "text": "Explain Newton's second law of physics."}]
        result = lm.resolve_academic_context(prompt="Aro easy kore bujhao.", history=history)
        assert result.confidence == lm.CONFIDENCE_PROBABLE
        assert result.subject == "Physics"

    def test_case3_unknown_context_no_fabrication(self):
        result = lm.resolve_academic_context(prompt="Ami eta bujhte parchi na.")
        assert result.confidence == lm.CONFIDENCE_UNKNOWN
        assert result.subject is None

    def test_case4_explicit_context_switch_overrides_inherited_context(self):
        history = [{"role": "user", "text": "Physics-er Newton's law ta bujhai dao."}]
        result = lm.resolve_academic_context(
            prompt="Chemistry-te mole concept ta explain koro.", history=history,
        )
        assert result.confidence == lm.CONFIDENCE_KNOWN
        assert result.subject == "Chemistry"

    def test_case5_stream_is_never_read_by_this_function(self):
        # resolve_academic_context doesn't even accept a stream parameter -
        # "Science" (a stream) can never leak in as a fabricated subject
        # like "Physics", by construction, not by a runtime check.
        import inspect
        params = inspect.signature(lm.resolve_academic_context).parameters
        assert "stream" not in params
        assert "board" not in params
        assert "user_class" not in params

    def test_case6_casual_chat_no_context(self):
        result = lm.resolve_academic_context(prompt="How are you?")
        assert result.confidence == lm.CONFIDENCE_UNKNOWN

    def test_ambiguous_message_with_two_subjects_is_unknown(self):
        result = lm.resolve_academic_context(prompt="Physics and Chemistry both confuse me.")
        assert result.confidence == lm.CONFIDENCE_UNKNOWN
        assert result.subject is None

    def test_ambiguous_current_message_still_falls_back_to_history_inheritance(self):
        history = [{"role": "user", "text": "Explain the Physics formula for force."}]
        # Current message is ambiguous on its own (no clean single match),
        # but a prior message in the same bounded history already
        # established Physics - inheritance still applies.
        result = lm.resolve_academic_context(prompt="I don't get it.", history=history)
        assert result.confidence == lm.CONFIDENCE_PROBABLE
        assert result.subject == "Physics"

    def test_bot_messages_in_history_never_establish_inherited_context(self):
        history = [{"role": "bot", "text": "Physics is the study of matter and energy."}]
        result = lm.resolve_academic_context(prompt="I don't understand.", history=history)
        assert result.confidence == lm.CONFIDENCE_UNKNOWN

    def test_most_recent_matching_history_message_wins(self):
        history = [
            {"role": "user", "text": "Explain Physics force concept."},
            {"role": "bot", "text": "Sure, force is..."},
            {"role": "user", "text": "Now explain Chemistry bonding."},
            {"role": "bot", "text": "Sure, bonding is..."},
        ]
        result = lm.resolve_academic_context(prompt="I don't get it.", history=history)
        assert result.subject == "Chemistry"

    def test_bangla_script_subject_keyword_detected(self):
        result = lm.resolve_academic_context(prompt="রসায়ন এই বিষয়টা বুঝতে পারছি না")
        assert result.confidence == lm.CONFIDENCE_KNOWN
        assert result.subject == "Chemistry"

    def test_business_studies_bangla_stream_label_never_matches_as_subject(self):
        # "ব্যবসায় শিক্ষা" is deliberately excluded (see
        # backend/learning_memory.py) because it collides with the
        # Commerce stream's own Bangla label - a message using only that
        # phrase must not resolve to the Business Studies subject.
        result = lm.resolve_academic_context(prompt="ব্যবসায় শিক্ষা niye kichu bolo.")
        assert result.confidence == lm.CONFIDENCE_UNKNOWN

    def test_short_common_word_ict_does_not_match_inside_unrelated_words(self):
        # False-positive guardrail: "ict" as a bare substring appears
        # inside many ordinary English words - word-boundary matching
        # must prevent those from being misread as the ICT subject.
        result = lm.resolve_academic_context(prompt="I made a strict prediction about the district election.")
        assert result.confidence == lm.CONFIDENCE_UNKNOWN

    def test_malformed_history_entries_do_not_raise(self):
        history = ["not a dict", {"role": "user"}, {"role": "user", "text": 123}, None]
        result = lm.resolve_academic_context(prompt="I don't understand.", history=history)
        assert result.confidence == lm.CONFIDENCE_UNKNOWN

    def test_explicit_subject_and_topic_path_still_takes_priority(self):
        # The original (still-unused-in-production) fully-explicit path
        # must keep working and take priority over message-based detection.
        result = lm.resolve_academic_context(
            explicit_subject="Biology", explicit_topic="Cell Structure",
            prompt="Physics-e Newton's law bujhte parchi na.",
        )
        assert result.confidence == lm.CONFIDENCE_KNOWN
        assert result.subject == "Biology"
        assert result.topic == "Cell Structure"


# ---------------------------------------------------------------------------
# Chat signal detection
# ---------------------------------------------------------------------------

class TestDetectLearningSignalsFalsePositives:
    """Ordinary questions must never be flagged - this is the priority
    guardrail per the implementation brief."""

    def test_ordinary_academic_question_no_signal(self):
        signals = lm.detect_learning_signals("What is Newton's second law?")
        assert signals == []

    def test_ordinary_bangla_question_no_signal(self):
        signals = lm.detect_learning_signals("নিউটনের দ্বিতীয় সূত্র কী?")
        assert signals == []

    def test_short_acknowledgement_not_repeated_question(self):
        history = [
            {"role": "user", "text": "ok"},
            {"role": "bot", "text": "Great!"},
        ]
        signals = lm.detect_learning_signals("ok", history=history)
        assert signals == []

    def test_empty_prompt_no_signal(self):
        assert lm.detect_learning_signals("") == []
        assert lm.detect_learning_signals("   ") == []

    def test_non_string_prompt_never_raises(self):
        assert lm.detect_learning_signals(None) == []
        assert lm.detect_learning_signals(12345) == []

    def test_malformed_history_entries_ignored_not_raised(self):
        history = ["not a dict", {"role": "user"}, {"role": "user", "text": 123}, None]
        signals = lm.detect_learning_signals("What is gravity?", history=history)
        assert signals == []


class TestDetectLearningSignalsConfusion:
    def test_english_confusion_phrase_detected(self):
        signals = lm.detect_learning_signals("I don't understand this at all.")
        assert any(s.signal_type == lm.SIGNAL_CONFUSION for s in signals)

    def test_banglish_confusion_phrase_detected(self):
        signals = lm.detect_learning_signals("ami eta bujhi nai")
        assert any(s.signal_type == lm.SIGNAL_CONFUSION for s in signals)

    def test_bangla_script_confusion_phrase_detected(self):
        signals = lm.detect_learning_signals("আমি এটা বুঝতে পারছি না")
        assert any(s.signal_type == lm.SIGNAL_CONFUSION for s in signals)

    def test_confusion_signal_strength_is_weak(self):
        signals = lm.detect_learning_signals("I'm confused about this.")
        confusion = [s for s in signals if s.signal_type == lm.SIGNAL_CONFUSION]
        assert confusion and confusion[0].signal_strength == lm.SIGNAL_STRENGTH_WEAK


class TestDetectLearningSignalsReexplanation:
    def test_english_reexplanation_phrase_detected(self):
        signals = lm.detect_learning_signals("Can you explain this again please?")
        assert any(s.signal_type == lm.SIGNAL_REEXPLANATION for s in signals)

    def test_banglish_reexplanation_phrase_detected(self):
        signals = lm.detect_learning_signals("Vaiya aro shohoj kore bolo please")
        assert any(s.signal_type == lm.SIGNAL_REEXPLANATION for s in signals)


class TestDetectLearningSignalsRepeatedQuestion:
    def test_exact_repeat_in_history_detected(self):
        history = [
            {"role": "user", "text": "What is the formula for kinetic energy?"},
            {"role": "bot", "text": "KE = 1/2 m v^2"},
        ]
        signals = lm.detect_learning_signals("What is the formula for kinetic energy?", history=history)
        assert any(s.signal_type == lm.SIGNAL_REPEATED_QUESTION for s in signals)

    def test_repeat_after_whitespace_and_case_normalization_detected(self):
        history = [{"role": "user", "text": "  What Is The Formula For Kinetic Energy?  "}]
        signals = lm.detect_learning_signals("what is the formula for kinetic energy?", history=history)
        assert any(s.signal_type == lm.SIGNAL_REPEATED_QUESTION for s in signals)

    def test_different_question_not_flagged_as_repeated(self):
        history = [{"role": "user", "text": "What is the formula for kinetic energy?"}]
        signals = lm.detect_learning_signals("What is the formula for potential energy?", history=history)
        assert not any(s.signal_type == lm.SIGNAL_REPEATED_QUESTION for s in signals)

    def test_bot_messages_never_count_as_a_repeat_source(self):
        history = [{"role": "bot", "text": "What is the formula for kinetic energy?"}]
        signals = lm.detect_learning_signals("What is the formula for kinetic energy?", history=history)
        assert not any(s.signal_type == lm.SIGNAL_REPEATED_QUESTION for s in signals)

    def test_no_history_never_flags_repeated_question(self):
        signals = lm.detect_learning_signals("What is the formula for kinetic energy?", history=None)
        assert not any(s.signal_type == lm.SIGNAL_REPEATED_QUESTION for s in signals)


class TestNotDetectedByDesign:
    """explicit_misconception and successful_understanding are valid
    taxonomy values but are NOT produced by this version of the detector -
    see backend/learning_memory.py's module docstring for why."""

    def test_misconception_never_auto_detected(self):
        # Even a message that "sounds like" a misconception must not be
        # guessed at - no detector path currently produces this signal.
        signals = lm.detect_learning_signals("I think force equals mass times distance.")
        assert not any(s.signal_type == lm.SIGNAL_MISCONCEPTION for s in signals)

    def test_successful_understanding_never_auto_detected(self):
        signals = lm.detect_learning_signals("Oh I get it now, thank you!")
        assert not any(s.signal_type == lm.SIGNAL_SUCCESSFUL_UNDERSTANDING for s in signals)


class TestIsMeaningfulSignalSet:
    def test_empty_list_is_not_meaningful(self):
        assert lm.is_meaningful_signal_set([]) is False

    def test_nonempty_list_is_meaningful(self):
        assert lm.is_meaningful_signal_set([lm.DetectedSignal(lm.SIGNAL_CONFUSION, lm.SIGNAL_STRENGTH_WEAK)]) is True


# ---------------------------------------------------------------------------
# Combined-intelligence explanation notes (Phase 5E)
# ---------------------------------------------------------------------------

class TestBuildConversationNotes:
    def test_empty_input_returns_empty_list(self):
        assert lm.build_conversation_notes([]) == []

    def test_groups_by_topic_key_and_counts_signal_types(self):
        rows = [
            {"subject": "Physics", "topic": "Force & Motion", "topic_key": "force & motion", "signal_type": lm.SIGNAL_CONFUSION},
            {"subject": "Physics", "topic": "Force & Motion", "topic_key": "force & motion", "signal_type": lm.SIGNAL_CONFUSION},
            {"subject": "Physics", "topic": "Force & Motion", "topic_key": "force & motion", "signal_type": lm.SIGNAL_REEXPLANATION},
        ]
        notes = lm.build_conversation_notes(rows)
        assert len(notes) == 1
        assert notes[0]["subject"] == "Physics"
        assert notes[0]["topic_key"] == "force & motion"
        assert any("2 time(s)" in n for n in notes[0]["notes"] if "confusion" in n or "expressed" in n)

    def test_never_produces_a_numeric_score_field(self):
        rows = [{"subject": "X", "topic": "X", "topic_key": "x", "signal_type": lm.SIGNAL_CONFUSION}]
        notes = lm.build_conversation_notes(rows)
        for entry in notes:
            assert "score" not in entry
            assert "status" not in entry
            assert "correct_rate" not in entry
            for note in entry["notes"]:
                assert isinstance(note, str)

    def test_rows_without_subject_are_skipped(self):
        # There is nothing to attribute a note to at all without a
        # subject - this should never happen in practice (see the
        # function's docstring) but stays defensive.
        rows = [{"topic": "X", "topic_key": "x", "signal_type": lm.SIGNAL_CONFUSION}]  # missing subject
        assert lm.build_conversation_notes(rows) == []

    def test_rows_without_topic_key_are_grouped_by_subject_not_skipped(self):
        """
        PHASE 5C ACTIVATION CHANGE: subject-only evidence (no topic_key)
        is now a legitimate, common outcome of
        resolve_academic_context's deterministic subject-only detection
        (see backend/learning_memory.py) - it must be grouped and
        surfaced, not silently dropped. This intentionally supersedes
        this function's pre-activation behavior, which assumed a missing
        topic_key meant malformed/unexpected data; that assumption no
        longer holds.
        """
        rows = [
            {"subject": "Physics", "signal_type": lm.SIGNAL_CONFUSION},
            {"subject": "Physics", "signal_type": lm.SIGNAL_CONFUSION},
        ]
        notes = lm.build_conversation_notes(rows)
        assert len(notes) == 1
        assert notes[0]["subject"] == "Physics"
        assert notes[0]["topic"] is None
        assert notes[0]["topic_key"] is None
        assert any("2 time(s)" in n for n in notes[0]["notes"])

    def test_topic_and_subject_only_evidence_for_same_subject_are_kept_separate(self):
        """A topic-attributed row and a subject-only row for the same
        subject must NOT be merged into one bucket - they represent
        different attribution granularity and merging them would silently
        upgrade a subject-only signal to look topic-specific."""
        rows = [
            {"subject": "Physics", "topic": "Force & Motion", "topic_key": "force & motion", "signal_type": lm.SIGNAL_CONFUSION},
            {"subject": "Physics", "signal_type": lm.SIGNAL_CONFUSION},
        ]
        notes = lm.build_conversation_notes(rows)
        assert len(notes) == 2

    def test_invalid_signal_type_is_skipped(self):
        rows = [{"subject": "X", "topic": "X", "topic_key": "x", "signal_type": "not_a_real_signal"}]
        notes = lm.build_conversation_notes(rows)
        # The topic group is created but has no notes, since the only
        # signal on it was invalid.
        assert notes == [] or notes[0]["notes"] == []

    def test_preserves_first_seen_order(self):
        rows = [
            {"subject": "S", "topic": "B", "topic_key": "b", "signal_type": lm.SIGNAL_CONFUSION},
            {"subject": "S", "topic": "A", "topic_key": "a", "signal_type": lm.SIGNAL_CONFUSION},
        ]
        notes = lm.build_conversation_notes(rows)
        assert [n["topic_key"] for n in notes] == ["b", "a"]
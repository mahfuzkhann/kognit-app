"""Phase 7C tests: backend/research_decision.py (pure heuristic, no I/O)."""

from __future__ import annotations

from backend.research_decision import decide_research


class TestResearchDecision:
    def test_stable_academic_question_does_not_trigger_research(self):
        d = decide_research("Solve the quadratic equation x^2 - 5x + 6 = 0.")
        assert d.research_requested is False
        assert d.category is None

    def test_explicit_temporal_signal_triggers_research(self):
        d = decide_research("What is the latest news on climate change policy?")
        assert d.research_requested is True
        assert d.category == "current_event_topic"  # "news" matches the current-event pattern first

    def test_pure_temporal_word_triggers_research(self):
        d = decide_research("What is happening currently in Bangladesh politics?")
        assert d.research_requested is True
        assert d.category in ("temporal_signal", "current_event_topic")

    def test_current_event_topic_triggers_research(self):
        d = decide_research("Who is the current Prime Minister of Bangladesh?")
        assert d.research_requested is True
        assert d.category == "current_event_topic"

    def test_recent_year_reference_triggers_research(self):
        d = decide_research("What were the major scientific discoveries of 2026?")
        assert d.research_requested is True
        assert d.category == "recent_year_reference"

    def test_old_year_reference_does_not_trigger_research(self):
        d = decide_research("What happened in the Liberation War of 1971?")
        assert d.research_requested is False

    def test_bangla_temporal_signal_triggers_research(self):
        d = decide_research("বাংলাদেশে বর্তমান বাজেট কত?")
        assert d.research_requested is True

    def test_grammar_question_does_not_trigger_research(self):
        d = decide_research("Change this sentence into passive voice: 'He wrote a letter.'")
        assert d.research_requested is False

    def test_pdf_explanation_request_does_not_trigger_research(self):
        d = decide_research("Explain the third paragraph of this PDF in simpler terms.")
        assert d.research_requested is False

    def test_decision_always_has_a_disclosed_reason(self):
        for prompt in ["2x + 3 = 7, solve for x.", "What's the latest iPhone price?"]:
            d = decide_research(prompt)
            assert d.reason  # never empty, always explains itself

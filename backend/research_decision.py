"""
Kognit Phase 7C - Research decisioning.

PRINCIPLE (per the approved architecture): this is NOT meant to be the
final intelligence layer. It is a small, explicit, disclosed v1 heuristic
whose only job is deciding whether to PAY for a Google-Search-grounded
Gemini call at all (grounding adds latency/cost) - it is not the ground
truth for whether search actually happened. That ground truth always
comes from the Gemini response's own grounding_metadata.web_search_queries
after the call (see backend/research_models.py), never from this module.

This module is deliberately small and swappable: `decide_research()` is
the one function the rest of the pipeline calls, so a future, better
decisioning strategy (a small classifier, a cheap pre-call to Gemini
itself asking "does this need current information", etc.) can replace
the body of this function without touching anything else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# Deliberately small and illustrative, NOT an exhaustive keyword list
# treated as final intelligence (explicitly forbidden by the approved
# architecture). Each category is disclosed so a reviewer can see exactly
# what triggers a decision - nothing hidden in a giant unreviewable list.
_TEMPORAL_SIGNAL_PATTERN = re.compile(
    r"\b(latest|current(ly)?|today|this (year|week|month)|recent(ly)?|now|"
    r"as of \d{4}|breaking|just (announced|released|happened)|"
    r"এখন|বর্তমান|সাম্প্রতিক|আজ|এই বছর)\b",
    re.IGNORECASE,
)
_CURRENT_EVENT_TOPIC_PATTERN = re.compile(
    r"\b(news|election|prime minister|president|budget|stock price|"
    r"exchange rate|weather (today|now)|sports? (score|result)|"
    r"who (is|won)|market price|"
    r"নির্বাচন|প্রধানমন্ত্রী|রাষ্ট্রপতি|বাজেট)\b",
    re.IGNORECASE,
)
_RECENT_YEAR_PATTERN = re.compile(r"\b20(2[5-9]|[3-9]\d)\b")  # 2025 onward


@dataclass
class ResearchDecision:
    research_requested: bool
    reason: str
    category: Optional[str]  # 'temporal_signal' | 'current_event_topic' | 'recent_year_reference' | None


def decide_research(prompt: str) -> ResearchDecision:
    """Pure function: prompt text -> a decision + a disclosed reason.

    Returns research_requested=False by default - research is opt-in per
    message, not the default path, so normal academic questions (the
    overwhelming majority of Kognit's traffic) never pay grounding
    latency/cost and never risk an unrelated web search derailing a
    curriculum answer.
    """
    if _CURRENT_EVENT_TOPIC_PATTERN.search(prompt):
        return ResearchDecision(True, "Prompt references a current-event topic category.", "current_event_topic")
    if _TEMPORAL_SIGNAL_PATTERN.search(prompt):
        return ResearchDecision(True, "Prompt contains an explicit temporal-freshness signal word.", "temporal_signal")
    if _RECENT_YEAR_PATTERN.search(prompt):
        return ResearchDecision(True, "Prompt references a recent year, suggesting a need for current data.", "recent_year_reference")
    return ResearchDecision(False, "No current-information signal detected; treated as a stable academic question.", None)

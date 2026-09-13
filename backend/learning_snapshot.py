"""
Kognit Phase 6B - Learning Snapshot.

PURE DOMAIN MODULE, same architectural rule as backend/mastery_engine.py
and backend/insight_engine.py: no I/O, no network calls, no database
access, no Gemini calls. Takes the ALREADY-COMPUTED outputs of
backend.database.get_user_topic_profile (Phase 5A quiz mastery status)
and backend.database.get_learning_insights (Phase 5E chat insights) and
combines them into one small, explainable "Learning Snapshot" - it does
NOT recompute mastery and does NOT re-derive insights itself.

ARCHITECTURE BOUNDARY:
  - This module never imports backend.database or backend.main, matching
    the same layering rule as mastery_engine.py/insight_engine.py - a
    straightforward way to catch an accidental layering violation during
    review (importing either would be exactly backward).
  - It DOES import public constants/predicates from backend.mastery_engine
    and backend.insight_engine (is_weak_status, status labels, insight
    type/confidence labels) - reusing their published vocabulary rather
    than redefining or re-deriving it, per the Phase 6B brief's explicit
    instruction not to duplicate either engine's logic.
  - backend/main.py's profile_snapshot_endpoint calls the existing,
    UNMODIFIED get_user_topic_profile and get_learning_insights and
    passes their results straight into build_learning_snapshot below -
    the endpoint computes nothing itself, matching the established
    convention that backend/main.py handles HTTP/auth only.

WHAT THIS MODULE DELIBERATELY DOES NOT DO (Phase 6B scope guardrails):
  - It does not call Gemini or any LLM.
  - It does not introduce a new mastery/insight algorithm or numeric
    threshold beyond what mastery_engine.py/insight_engine.py already
    publish - see _STRENGTH_MIN_INSIGHT_CONFIDENCE below for the one
    place this module makes a selection among ALREADY-PUBLISHED tiers.
  - It does not merge quiz correctness and chat insight confidence into
    one combined score - see insight_engine's own module docstring for
    why those two evidence kinds are kept structurally separate. A
    "needs_practice" entry's status/correct_rate always comes from
    mastery_engine; any attached chat insight is exposed as separate,
    clearly-labeled supporting_chat_evidence, never blended into the
    quiz numbers.
  - It does not guess a progress trend. "Recent progress" reuses
    mastery_engine's own STATUS_IMPROVING directly (already a verified
    positive trend - see mastery_engine._shows_positive_trend) rather
    than inventing a new comparison or a fake percentage improvement.
"""

from typing import Optional

from backend.mastery_engine import (
    IMPROVING_MIN_ATTEMPTS,
    STATUS_IMPROVING,
    STATUS_MASTERED,
    STATUS_STRONG,
    is_weak_status,
)
from backend.insight_engine import (
    INSIGHT_CONFIDENCE_HIGH,
    INSIGHT_CONFIDENCE_MEDIUM,
    INSIGHT_EMERGING_STRENGTH,
)

# ---------------------------------------------------------------------------
# Phase 6B scope constant: which insight_engine confidence tiers are
# trustworthy enough to surface a chat-derived insight as a "strength" on
# its own (with no quiz evidence backing it). NOT a new numeric threshold
# - insight_engine already defines exactly three confidence tiers
# (low/medium/high, see VALID_INSIGHT_CONFIDENCES); this simply selects
# which of those PUBLISHED tiers qualify. LOW confidence means zero
# KNOWN-attribution qualifying events (see insight_engine._confidence_tier)
# - i.e. every qualifying event was inherited/probable context, never
# independently re-stated - too thin a basis to declare a "strength."
# ---------------------------------------------------------------------------
_STRENGTH_MIN_INSIGHT_CONFIDENCE = (INSIGHT_CONFIDENCE_MEDIUM, INSIGHT_CONFIDENCE_HIGH)

_STRONG_QUIZ_STATUSES = (STATUS_STRONG, STATUS_MASTERED)


def _is_dict(value) -> bool:
    return isinstance(value, dict)


def _topic_group_key(topic_entry: dict) -> Optional[str]:
    """
    The grouping key for a Phase 5A topic entry.

    Phase 5A topics always carry a non-null, non-empty topic_key (see
    backend.database.get_user_topic_profile's `topic_key=not.is.null`
    filter) - so unlike insight_engine._group_key (which must fall back
    to a subject-only key when topic_key is absent), a topic entry's own
    topic_key IS its group key. Returns None only if the entry is
    malformed (missing/blank topic_key) so callers can skip it
    defensively rather than mismatching it against unrelated insights.
    """
    topic_key = topic_entry.get("topic_key")
    if isinstance(topic_key, str) and topic_key.strip():
        return topic_key
    return None


def _insight_group_key(insight_entry: dict) -> Optional[str]:
    """
    The grouping key for a Phase 5E insight entry, mirroring
    insight_engine._group_key's own rule (topic_key when present, else a
    subject-only fallback) WITHOUT importing that private function - see
    module docstring's architecture boundary note. Duplicating this one
    small, stable rule here is the same tradeoff insight_engine.py itself
    already made against backend.learning_memory.build_conversation_notes.

    A subject-only fallback key can never equal a real topic_key (a
    normalized topic_key can never contain "::"), so an insight with no
    topic_key of its own can never accidentally match a specific quiz
    topic entry in _supporting_chat_evidence below - only an exact
    topic_key match does.
    """
    topic_key = insight_entry.get("topic_key")
    if isinstance(topic_key, str) and topic_key.strip():
        return topic_key
    subject = insight_entry.get("subject")
    if isinstance(subject, str) and subject.strip():
        return f"subject::{subject.strip().casefold()}"
    return None


def _quiz_strengths(topics: list) -> list:
    """
    Strengths from Phase 5A quiz evidence: topics currently Strong or
    Mastered. mastery_engine.MIN_EVIDENCE already guarantees at least 2
    attempts before either status can ever be reached - no single-event
    strength claim is possible here.

    `topics` arrives pre-sorted most-recently-practiced-first (see
    backend.database._build_topic_profile) - that order is preserved
    here rather than re-sorted, since "most recently practiced strength"
    is a reasonable default surface order.
    """
    strengths = []
    for entry in topics:
        if not _is_dict(entry):
            continue
        if entry.get("status") not in _STRONG_QUIZ_STATUSES:
            continue
        strengths.append({
            "source": "quiz",
            "subject": entry.get("subject"),
            "topic": entry.get("topic"),
            "topic_key": entry.get("topic_key"),
            "status": entry.get("status"),
            "correct_rate": entry.get("correct_rate"),
            "attempt_count": entry.get("attempt_count"),
            "last_attempt_at": entry.get("last_attempt_at"),
        })
    return strengths


def _chat_strengths(insights: list) -> list:
    """
    Strengths from Phase 5E chat evidence: emerging_strength insights
    whose confidence clears _STRENGTH_MIN_INSIGHT_CONFIDENCE.
    insight_engine.MIN_EVENTS_FOR_INSIGHT already guarantees at least 2
    qualifying events before ANY insight (of any type) is emitted - the
    confidence filter here is an additional, deliberately stricter bar
    specific to declaring something a "strength" (see module-level
    constant docstring above).

    `insights` arrives pre-sorted most-recently-observed-first (see
    insight_engine.compute_insights) - preserved, not re-sorted.
    """
    strengths = []
    for entry in insights:
        if not _is_dict(entry):
            continue
        if entry.get("insight_type") != INSIGHT_EMERGING_STRENGTH:
            continue
        if entry.get("confidence") not in _STRENGTH_MIN_INSIGHT_CONFIDENCE:
            continue
        strengths.append({
            "source": "chat",
            "subject": entry.get("subject"),
            "topic": entry.get("topic"),
            "topic_key": entry.get("topic_key"),
            "insight_type": entry.get("insight_type"),
            "confidence": entry.get("confidence"),
            "evidence_count": entry.get("evidence_count"),
            "last_observed_at": entry.get("last_observed_at"),
        })
    return strengths


def _supporting_chat_evidence(topic_group_key: str, insights: list) -> list:
    """
    Chat insights (any type, any confidence - this is explicitly
    SUPPORTING context, not a gate, and must never override the quiz
    status it is attached to) sharing the same topic_key as one specific
    quiz topic entry. Deliberately exact topic_key equality only (never
    the subject-only fallback form) - see _insight_group_key docstring:
    matching a subject-wide chat insight to one specific quiz topic would
    be an inference this module is not entitled to make.
    """
    supporting = []
    for entry in insights:
        if not _is_dict(entry):
            continue
        if _insight_group_key(entry) != topic_group_key:
            continue
        supporting.append({
            "insight_type": entry.get("insight_type"),
            "confidence": entry.get("confidence"),
            "evidence_count": entry.get("evidence_count"),
            "last_observed_at": entry.get("last_observed_at"),
        })
    return supporting


def _needs_practice(topics: list, insights: list) -> list:
    """
    Directly from mastery_engine.is_weak_status (Needs Practice /
    Developing) - the Phase 5A mastery status is NEVER overridden or
    reweighted by chat evidence, per the Phase 6B brief. Matching chat
    insights (exact topic_key match only) are attached as
    supporting_chat_evidence for extra context only.

    `topics` arrives pre-sorted most-recently-practiced-first; that order
    is preserved here rather than re-sorted.
    """
    needs_practice = []
    for entry in topics:
        if not _is_dict(entry):
            continue
        status = entry.get("status")
        if not isinstance(status, str) or not is_weak_status(status):
            continue

        group_key = _topic_group_key(entry)
        supporting = _supporting_chat_evidence(group_key, insights) if group_key else []

        needs_practice.append({
            "subject": entry.get("subject"),
            "topic": entry.get("topic"),
            "topic_key": entry.get("topic_key"),
            "status": status,
            "correct_rate": entry.get("correct_rate"),
            "attempt_count": entry.get("attempt_count"),
            "last_attempt_at": entry.get("last_attempt_at"),
            "supporting_chat_evidence": supporting,
        })
    return needs_practice


def _recent_progress(topics: list) -> dict:
    """
    Reuses mastery_engine.STATUS_IMPROVING directly - never a new trend
    computation (see module docstring). Three explicit, non-guessing
    states, distinguishing "we cannot say yet" from "we can say, and the
    answer is no upward trend right now":

      - "insufficient_evidence": no topic yet has enough attempts
        (>= IMPROVING_MIN_ATTEMPTS) for mastery_engine to have even
        evaluated a trend for it. Genuinely cannot say anything about
        progress yet - NOT the same as "no progress found".
      - "no_progress_detected": at least one topic HAS enough attempts
        to have been trend-evaluated, but none currently carry the
        Improving status. An honest "stable, not currently trending up"
        report - not a claim of decline (a declining/weak topic is
        already visible via needs_practice).
      - "improving": at least one topic currently carries the Improving
        status; each is returned as evidence.
    """
    eligible = [
        entry for entry in topics
        if _is_dict(entry)
        and isinstance(entry.get("attempt_count"), int)
        and entry.get("attempt_count") >= IMPROVING_MIN_ATTEMPTS
    ]

    if not eligible:
        return {"state": "insufficient_evidence", "topics": []}

    improving = [entry for entry in eligible if entry.get("status") == STATUS_IMPROVING]

    if not improving:
        return {"state": "no_progress_detected", "topics": []}

    return {
        "state": "improving",
        "topics": [
            {
                "subject": entry.get("subject"),
                "topic": entry.get("topic"),
                "topic_key": entry.get("topic_key"),
                "correct_rate": entry.get("correct_rate"),
                "attempt_count": entry.get("attempt_count"),
                "last_attempt_at": entry.get("last_attempt_at"),
            }
            for entry in improving
        ],
    }


def build_learning_snapshot(topics: list, insights: list) -> dict:
    """
    The single public entry point of this module - the Phase 6B analogue
    of mastery_engine.compute_topic_status / insight_engine.compute_insights.

    Args:
        topics: the exact list returned by
            backend.database.get_user_topic_profile (Phase 5A - one
            explainable mastery status dict per (subject, topic_key)).
        insights: the exact list returned by
            backend.database.get_learning_insights (Phase 5E - one
            explainable insight dict per (subject, topic_key/subject)).

    Never raises on malformed individual entries in either list (skipped
    defensively), matching both source modules' own resilience
    convention - one bad entry must not hide the rest of a student's
    snapshot. Genuinely empty/insufficient input always produces a valid,
    explicit low-data response - see module docstring and
    _recent_progress - never a fabricated strength/weakness/trend.

    Returns:
        {
            "strengths": [...],          # quiz- and/or chat-derived, see
                                          # _quiz_strengths/_chat_strengths
            "needs_practice": [...],     # quiz-derived, mastery status
                                          # never overridden by chat
            "recent_progress": {...},    # see _recent_progress
            "evidence_summary": {...},   # counts only, no raw content
        }
    """
    topics = topics if isinstance(topics, list) else []
    insights = insights if isinstance(insights, list) else []

    strengths = _quiz_strengths(topics) + _chat_strengths(insights)
    needs_practice = _needs_practice(topics, insights)
    recent_progress = _recent_progress(topics)

    return {
        "strengths": strengths,
        "needs_practice": needs_practice,
        "recent_progress": recent_progress,
        "evidence_summary": {
            "quiz_topics_count": len(topics),
            "chat_insights_count": len(insights),
            "has_quiz_evidence": len(topics) > 0,
            "has_chat_evidence": len(insights) > 0,
            "strengths_count": len(strengths),
            "needs_practice_count": len(needs_practice),
        },
    }
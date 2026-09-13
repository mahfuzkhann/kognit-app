"""
Kognit Phase 6D - Next-Step Engine.

PURE DOMAIN MODULE, same architectural rule as backend/mastery_engine.py,
backend/insight_engine.py, backend/learning_snapshot.py, and
backend/mistake_engine.py: no I/O, no network calls, no database access,
no Gemini/LLM calls of any kind.

ARCHITECTURE BOUNDARY:
  - This module never imports backend.database or backend.main.
  - It takes the exact, already-computed outputs of THREE existing,
    UNMODIFIED database functions - get_user_topic_profile (Phase 5A),
    get_learning_insights (Phase 5E), get_user_topic_mistakes (Phase 6C)
    - and combines them into a small, deterministic, explainable list of
    recommendations. It recomputes NOTHING from those three engines and
    duplicates none of their algorithms.
  - Deliberately does NOT import backend.learning_snapshot (Phase 6B) or
    depend on its output in any way - 6B is independently evolving (and
    has known, documented overlap findings of its own - P6B-01/P6B-02)
    that this module must not inherit. 6D reads the same three
    underlying evidence sources 6B reads, independently, per the Phase
    6D brief's explicit instruction not to modify or couple to 6B.
  - backend/main.py's profile_next_steps_endpoint computes nothing
    itself; it calls the three existing database functions (already used
    elsewhere for 5A/5E/6C) and passes their results straight into
    compute_next_steps below.

==============================================================================
WHAT THIS MODULE DOES AND DOES NOT CLAIM
==============================================================================

This module answers exactly one question: "given evidence Kognit already
has, what is a small, explainable set of next actions for this student?"
It is a RULE-BASED DECISION LAYER over existing structured evidence -
never a new evidence source, never an LLM, never free-text generation.
Every `reason` string is assembled from a small fixed vocabulary plus the
evidence's own already-public fields (a status label, an attempt count) -
never invented, never psychological, never a claim about ability,
intelligence, or learning style.

==============================================================================
CANONICAL TOPIC IDENTITY
==============================================================================

Every recommendation is keyed by (subject, topic_key) - the same
canonical identity Phase 5A/5E/6C already use. An insight with no
topic_key (a genuinely subject-wide chat signal - see
insight_engine._group_key's subject-only fallback) has no specific topic
identity and is therefore EXCLUDED from this module entirely - Section 7
of the Phase 6D brief is explicit: "If topic_key is unavailable, do not
guess." Guessing which specific topic a subject-wide chat signal belongs
to is exactly the kind of inference this module must not make.

==============================================================================
CONFLICT RESOLUTION - THE CENTRAL RULE OF THIS MODULE
==============================================================================

For any given (subject, topic_key):

  - If Phase 5A has an opinion about this topic (it appears in the quiz
    topic profile, i.e. attempt_count >= 1) - THAT OPINION IS
    AUTHORITATIVE for this topic. Chat evidence (Phase 5E) for the same
    topic is then used ONLY as supporting/tagging evidence on top of the
    quiz-driven recommendation - it can never independently create a
    DIFFERENT recommendation type for a topic quiz has already spoken
    about, and it can never flip a quiz-positive topic into a "review"
    recommendation or a quiz-weak topic into a "continue" recommendation.
    This is a direct implementation of the Phase 6D brief's Section 17:
    "Prefer current authoritative mastery status for the primary
    recommendation. Chat evidence can support, but should not silently
    override quiz mastery status."

  - Within that quiz-authoritative branch, Phase 6C mistake evidence
    (also quiz-derived, an independent lens on the SAME rows - see
    mistake_engine.py's own module docstring) is treated as CO-EQUAL
    quiz evidence, not weaker chat-tier evidence: a topic with a
    6C repeated_topic_difficulty entry becomes PRACTICE_TOPIC even if its
    5A aggregate status happens to read Strong/Improving (a genuinely
    possible case - see mistake_engine.py's docstring on why 5A's rate
    and 6C's raw count are different, non-redundant lenses on the same
    data). PRACTICE_TOPIC is checked before CONTINUE_TOPIC for exactly
    this reason.

  - Only when Phase 5A has NO opinion at all about a topic (zero quiz
    attempts ever recorded for that (subject, topic_key)) does Phase 5E
    chat evidence get to independently drive a recommendation
    (REVIEW_TOPIC or a chat-only CONTINUE_TOPIC) - and even then, a
    chat-only "continue" claim requires medium/high insight confidence
    (see _STRENGTH_MIN_INSIGHT_CONFIDENCE below - the same principled cutoff
    already used by backend.learning_snapshot.py for the identical reason,
    independently re-declared here per this codebase's established
    convention of not sharing thresholds across pure modules without
    re-justifying them locally). A chat-only weakness signal (REVIEW_TOPIC)
    is allowed at any insight confidence tier, since Phase 5E's own
    MIN_EVENTS_FOR_INSIGHT already guarantees every insight represents at
    least 2 real qualifying events regardless of confidence tier, and
    REVIEW_TOPIC is deliberately the LOWER of the two weakness tiers (see
    RECOMMENDATION TYPES AND PRIORITY below) - it is never allowed to
    outrank PRACTICE_TOPIC.

  - If a topic has zero quiz evidence AND its chat evidence disagrees
    with itself (both a weakness-type insight and an emerging_strength
    insight exist for the same identity - Phase 5E's compute_insights can
    legitimately produce this), the weakness signal wins (REVIEW_TOPIC)
    - the same conservative, "don't silently reassure" principle applied
    consistently.

==============================================================================
RECOMMENDATION TYPES AND PRIORITY
==============================================================================

Four types, in this fixed, non-negotiable tier order (1 = most urgent):

  1. PRACTICE_TOPIC  - quiz evidence (5A weak status and/or 6C repeated
     difficulty) establishes a confirmed weakness.
  2. REVIEW_TOPIC    - chat-only evidence of repeated difficulty/confusion,
     for a topic quiz has no opinion on yet.
  3. CONTINUE_TOPIC  - positive evidence (5A Improving/Strong/Mastered,
     and/or, when quiz is silent, a sufficiently-confident chat
     emerging_strength signal).
  4. TAKE_QUIZ       - exactly one prior quiz attempt exists for a topic
     (5A status == Insufficient Evidence) - not enough evidence yet to
     say anything else about it, so another attempt is suggested. This
     is the ONLY TAKE_QUIZ trigger implemented - see TAKE_QUIZ SCOPE
     below for why a broader trigger was deliberately not built.

TAKE_QUIZ SCOPE: a broader "you've been discussing this topic in chat but
never quizzed on it" trigger was considered and deliberately NOT
implemented for the initial version - Kognit does not track a distinct
"practice" activity separate from quiz attempts and chat messages, so
inferring "this student has been practicing and is due for an assessment"
from chat activity alone would be a materially weaker, less-defensible
claim than the single-prior-attempt trigger actually implemented. Keeping
the initial rule to the one case the evidence genuinely, unambiguously
supports (Section 22: do not overbuild).

Mastered is intentionally described using the exact same phrasing pattern
as every other status ("classified as Mastered" - an observation about
the existing Phase 5A label, not a new claim) rather than any additional
permanence language ("you have mastered this forever") - this module
never asserts more than Phase 5A itself already asserts.

==============================================================================
DEDUPLICATION
==============================================================================

Deduplication falls directly out of the conflict-resolution rule above: a
given (subject, topic_key) produces AT MOST ONE recommendation entry,
because the quiz-authoritative branch is evaluated first and returns
immediately when it applies, and only genuinely quiz-silent topics reach
the chat-only branch. There is no separate "merge duplicate entries" step
because duplicates are structurally impossible by construction - one
identity in, at most one recommendation out.

==============================================================================
RANKING
==============================================================================

Sort key: (tier ASC, recency DESC, subject ASC, topic_key ASC) - tier
per RECOMMENDATION TYPES above; recency is the single most-recent
timestamp backing that specific recommendation (last_attempt_at for every
quiz-authoritative branch, since Phase 6C mistake evidence is drawn from
the exact same rows and is never more recent than 5A's own last_attempt_at
for that topic; last_observed_at for the chat-only branch); subject/
topic_key alphabetical as the final deterministic tie-breaker so two
recommendations can never compare as equal. At most MAX_RECOMMENDATIONS
(3) are returned - the highest-tier, most-recent evidence wins ties for a
spot, never a random or unstable order.
"""

from datetime import datetime
from typing import Optional

from backend.mastery_engine import (
    STATUS_IMPROVING,
    STATUS_INSUFFICIENT_EVIDENCE,
    STATUS_MASTERED,
    STATUS_STRONG,
    is_weak_status,
)
from backend.insight_engine import (
    INSIGHT_CONFIDENCE_HIGH,
    INSIGHT_CONFIDENCE_MEDIUM,
    INSIGHT_EMERGING_STRENGTH,
    INSIGHT_RECURRING_CONFUSION,
    INSIGHT_REPEATED_DIFFICULTY,
)

# ---------------------------------------------------------------------------
# Recommendation type vocabulary (fixed, small - Section 5/22).
# ---------------------------------------------------------------------------

RECOMMENDATION_PRACTICE_TOPIC = "practice_topic"
RECOMMENDATION_REVIEW_TOPIC = "review_topic"
RECOMMENDATION_CONTINUE_TOPIC = "continue_topic"
RECOMMENDATION_TAKE_QUIZ = "take_quiz"

_TIER_BY_TYPE = {
    RECOMMENDATION_PRACTICE_TOPIC: 1,
    RECOMMENDATION_REVIEW_TOPIC: 2,
    RECOMMENDATION_CONTINUE_TOPIC: 3,
    RECOMMENDATION_TAKE_QUIZ: 4,
}

_POSITIVE_QUIZ_STATUSES = (STATUS_IMPROVING, STATUS_STRONG, STATUS_MASTERED)

# ---------------------------------------------------------------------------
# Named constant - see module docstring's CONFLICT RESOLUTION section for
# full justification. Independently declared, not imported, matching this
# codebase's established convention (see backend/learning_snapshot.py's
# identical constant and reasoning).
# ---------------------------------------------------------------------------
_STRENGTH_MIN_INSIGHT_CONFIDENCE = (INSIGHT_CONFIDENCE_MEDIUM, INSIGHT_CONFIDENCE_HIGH)

MAX_RECOMMENDATIONS = 3
"""Hard cap on the returned list - Section 11. A small, actionable set,
never a long list."""

_EVIDENCE_QUIZ_MASTERY = "quiz_mastery"
_EVIDENCE_QUIZ_MISTAKE = "quiz_mistake"
_EVIDENCE_CHAT_INSIGHT = "chat_insight"


def _is_dict(value) -> bool:
    return isinstance(value, dict)


def _identity(subject, topic_key) -> Optional[tuple]:
    if not isinstance(subject, str) or not subject.strip():
        return None
    if not isinstance(topic_key, str) or not topic_key.strip():
        return None
    return (subject, topic_key)


def _index_topics(topics: list) -> dict:
    """(subject, topic_key) -> topic dict, from Phase 5A's output.
    Malformed entries are skipped defensively."""
    index = {}
    for entry in topics:
        if not _is_dict(entry):
            continue
        key = _identity(entry.get("subject"), entry.get("topic_key"))
        if key is None:
            continue
        index[key] = entry
    return index


def _index_mistakes(mistakes: list) -> dict:
    """(subject, topic_key) -> mistake dict, from Phase 6C's output."""
    index = {}
    for entry in mistakes:
        if not _is_dict(entry):
            continue
        key = _identity(entry.get("subject"), entry.get("topic_key"))
        if key is None:
            continue
        index[key] = entry
    return index


def _index_insights(insights: list) -> dict:
    """
    (subject, topic_key) -> list of insight dicts, from Phase 5E's output.
    Insights with a missing/empty topic_key are EXCLUDED here entirely -
    see module docstring's CANONICAL TOPIC IDENTITY section for why a
    subject-wide signal must never be attached to a guessed topic.
    """
    index: dict = {}
    for entry in insights:
        if not _is_dict(entry):
            continue
        key = _identity(entry.get("subject"), entry.get("topic_key"))
        if key is None:
            continue
        index.setdefault(key, []).append(entry)
    return index


def _display_topic(primary: Optional[dict], insight_entries: list, topic_key: str) -> Optional[str]:
    if primary is not None and isinstance(primary.get("topic"), str) and primary.get("topic"):
        return primary["topic"]
    for entry in insight_entries:
        if isinstance(entry.get("topic"), str) and entry.get("topic"):
            return entry["topic"]
    return topic_key


def _quiz_authoritative_recommendation(
    subject: str, topic_key: str, topic_entry: dict, mistake_entry: Optional[dict], insight_entries: list,
) -> Optional[dict]:
    status = topic_entry.get("status")
    last_attempt_at = topic_entry.get("last_attempt_at")
    topic_display = _display_topic(topic_entry, insight_entries, topic_key)

    has_weak_insight = any(
        e.get("insight_type") in (INSIGHT_REPEATED_DIFFICULTY, INSIGHT_RECURRING_CONFUSION)
        for e in insight_entries
    )
    has_strength_insight = any(
        e.get("insight_type") == INSIGHT_EMERGING_STRENGTH for e in insight_entries
    )

    if (isinstance(status, str) and is_weak_status(status)) or mistake_entry is not None:
        evidence_sources = []
        if isinstance(status, str) and is_weak_status(status):
            evidence_sources.append(_EVIDENCE_QUIZ_MASTERY)
        if mistake_entry is not None:
            evidence_sources.append(_EVIDENCE_QUIZ_MISTAKE)
        if has_weak_insight:
            evidence_sources.append(_EVIDENCE_CHAT_INSIGHT)

        if mistake_entry is not None:
            reason = (
                f"Repeated incorrect answers were observed for this topic across "
                f"{mistake_entry.get('affected_attempts')} separate quiz attempts."
            )
        else:
            reason = f"Recent quiz performance in this topic is classified as {status}."

        return {
            "recommendation_type": RECOMMENDATION_PRACTICE_TOPIC,
            "subject": subject,
            "topic": topic_display,
            "topic_key": topic_key,
            "reason": reason,
            "evidence_sources": evidence_sources,
            "_recency": last_attempt_at,
        }

    if isinstance(status, str) and status in _POSITIVE_QUIZ_STATUSES:
        evidence_sources = [_EVIDENCE_QUIZ_MASTERY]
        if has_strength_insight:
            evidence_sources.append(_EVIDENCE_CHAT_INSIGHT)
        return {
            "recommendation_type": RECOMMENDATION_CONTINUE_TOPIC,
            "subject": subject,
            "topic": topic_display,
            "topic_key": topic_key,
            "reason": f"Recent quiz performance in this topic is classified as {status}.",
            "evidence_sources": evidence_sources,
            "_recency": last_attempt_at,
        }

    if status == STATUS_INSUFFICIENT_EVIDENCE:
        return {
            "recommendation_type": RECOMMENDATION_TAKE_QUIZ,
            "subject": subject,
            "topic": topic_display,
            "topic_key": topic_key,
            "reason": (
                "Only one quiz attempt has been recorded for this topic - another "
                "attempt would help confirm your understanding."
            ),
            "evidence_sources": [_EVIDENCE_QUIZ_MASTERY],
            "_recency": last_attempt_at,
        }

    return None


def _chat_only_recommendation(
    subject: str, topic_key: str, insight_entries: list,
) -> Optional[dict]:
    weak_entries = [
        e for e in insight_entries
        if e.get("insight_type") in (INSIGHT_REPEATED_DIFFICULTY, INSIGHT_RECURRING_CONFUSION)
    ]
    strength_entries = [
        e for e in insight_entries
        if e.get("insight_type") == INSIGHT_EMERGING_STRENGTH
        and e.get("confidence") in _STRENGTH_MIN_INSIGHT_CONFIDENCE
    ]

    topic_display = _display_topic(None, insight_entries, topic_key)

    # Weakness wins on self-contradictory chat-only evidence - see module
    # docstring's CONFLICT RESOLUTION section, final bullet.
    if weak_entries:
        chosen = weak_entries[0]
        insight_type = chosen.get("insight_type")
        if insight_type == INSIGHT_RECURRING_CONFUSION:
            reason = "Recent conversations show recurring confusion about this topic."
        else:
            reason = "Recent conversations show repeated difficulty with this topic."
        last_observed_at = max(
            (e.get("last_observed_at") for e in weak_entries if e.get("last_observed_at")),
            default=None,
        )
        return {
            "recommendation_type": RECOMMENDATION_REVIEW_TOPIC,
            "subject": subject,
            "topic": topic_display,
            "topic_key": topic_key,
            "reason": reason,
            "evidence_sources": [_EVIDENCE_CHAT_INSIGHT],
            "_recency": last_observed_at,
        }

    if strength_entries:
        last_observed_at = max(
            (e.get("last_observed_at") for e in strength_entries if e.get("last_observed_at")),
            default=None,
        )
        return {
            "recommendation_type": RECOMMENDATION_CONTINUE_TOPIC,
            "subject": subject,
            "topic": topic_display,
            "topic_key": topic_key,
            "reason": "Recent conversations show signs of growing strength in this topic.",
            "evidence_sources": [_EVIDENCE_CHAT_INSIGHT],
            "_recency": last_observed_at,
        }

    return None


def compute_next_steps(topics: list, insights: list, mistakes: list) -> list:
    """
    The single public entry point of this module.

    Args:
        topics: the exact list returned by
            backend.database.get_user_topic_profile (Phase 5A).
        insights: the exact list returned by
            backend.database.get_learning_insights (Phase 5E).
        mistakes: the exact list returned by
            backend.database.get_user_topic_mistakes (Phase 6C).

    Returns a bare list of at most MAX_RECOMMENDATIONS recommendation
    dicts (matching the convention already used by
    get_user_topic_profile/get_learning_insights/get_user_topic_mistakes,
    which also return bare lists), ranked highest-priority first, each
    carrying a 1-based "priority" field reflecting its final rank.

    Never raises on malformed individual entries in any of the three
    inputs (skipped defensively) - matching every sibling engine's own
    resilience convention. Genuinely insufficient evidence produces an
    empty list - never a fabricated recommendation.
    """
    topics = topics if isinstance(topics, list) else []
    insights = insights if isinstance(insights, list) else []
    mistakes = mistakes if isinstance(mistakes, list) else []

    topics_by_key = _index_topics(topics)
    mistakes_by_key = _index_mistakes(mistakes)
    insights_by_key = _index_insights(insights)

    all_identities = set(topics_by_key.keys()) | set(mistakes_by_key.keys()) | set(insights_by_key.keys())

    candidates = []
    for (subject, topic_key) in all_identities:
        topic_entry = topics_by_key.get((subject, topic_key))
        mistake_entry = mistakes_by_key.get((subject, topic_key))
        insight_entries = insights_by_key.get((subject, topic_key), [])

        if topic_entry is not None:
            # Phase 5A has an opinion about this topic - quiz-authoritative
            # branch (see module docstring's CONFLICT RESOLUTION section).
            candidate = _quiz_authoritative_recommendation(
                subject, topic_key, topic_entry, mistake_entry, insight_entries,
            )
        else:
            # Zero quiz evidence for this topic - chat evidence may
            # independently drive a recommendation.
            candidate = _chat_only_recommendation(subject, topic_key, insight_entries)

        if candidate is not None:
            candidates.append(candidate)

    candidates.sort(
        key=lambda c: (
            _TIER_BY_TYPE[c["recommendation_type"]],
            _recency_sort_value(c["_recency"]),
            c["subject"],
            c["topic_key"],
        )
    )

    top = candidates[:MAX_RECOMMENDATIONS]

    results = []
    for index, candidate in enumerate(top):
        result = {k: v for k, v in candidate.items() if k != "_recency"}
        result["priority"] = index + 1
        results.append(result)
    return results


def _recency_sort_value(recency: Optional[str]):
    """
    Produces an ASCENDING-sortable key that orders more-recent evidence
    FIRST within a tier (Section 12: "more recent evidence wins ties").
    A missing/unparseable timestamp sorts LAST (tuple's first element 1
    always sorts after 0) rather than crashing or silently winning a tie
    it has no real claim to.
    """
    if not isinstance(recency, str) or not recency:
        return (1, 0.0)
    try:
        dt = datetime.fromisoformat(recency)
    except ValueError:
        return (1, 0.0)
    return (0, -dt.timestamp())
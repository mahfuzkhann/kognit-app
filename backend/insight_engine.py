"""
Kognit Phase 5E - Student Intelligence (chat-evidence aggregation).

PURE DOMAIN MODULE, same architectural rule as backend/mastery_engine.py:
no I/O, no network calls, no database access, no Gemini calls. Every
function here takes plain Python values in and returns plain Python
values out - trivially unit-testable, no mocking required.

ARCHITECTURE BOUNDARY (same convention as mastery_engine.py):
  - backend/database.py is the ONLY module that knows a ChatEvidenceRecord
    came from the `learning_evidence` table - it queries Supabase and
    translates raw rows into ChatEvidenceRecord instances (see
    get_learning_insights there).
  - backend/main.py handles authentication/authorization/HTTP only. It
    must never compute an insight itself.
  - This module never imports backend.database or backend.main (a
    straightforward way to catch an accidental layering violation during
    review - importing either would be exactly backward).
  - This module DOES import a handful of string constants from
    backend.learning_memory (the signal-type/attribution-confidence
    vocabulary) rather than redefining them - that vocabulary is owned by
    learning_memory.py; duplicating the literal strings here would risk
    the two ever silently drifting apart.

RELATIONSHIP TO backend.mastery_engine (Phase 5A):
  These are DELIBERATELY SEPARATE modules producing DELIBERATELY SEPARATE
  kinds of output. mastery_engine.compute_topic_status() consumes
  QUANTITATIVE quiz evidence (score/total_questions) and produces a
  numeric correct_rate + status label. This module consumes QUALITATIVE
  chat evidence (a signal_type, no score) and produces a confidence TIER
  (low/medium/high) + an insight_type label - structurally incapable of
  emitting a percentage, by construction (see Insight below - there is no
  numeric field for one to go in). Nothing here is called by
  mastery_engine, and mastery_engine is never called by this module. Do
  not merge these two outputs into one score - see the Phase 5B/5C
  architecture reviews for why chat and quiz evidence are kept distinct.

THRESHOLDS ARE PRODUCT HEURISTICS, NOT SCIENTIFIC FACTS - same disclaimer
as mastery_engine.py. Every constant below is deliberately named and
isolated so it can be retuned later against real multi-student data,
never off a single anecdote.
"""

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence

from backend.learning_memory import (
    CONFIDENCE_KNOWN,
    CONFIDENCE_PROBABLE,
    SIGNAL_CONFUSION,
    SIGNAL_MISCONCEPTION,
    SIGNAL_REEXPLANATION,
    SIGNAL_REPEATED_QUESTION,
    SIGNAL_SUCCESSFUL_UNDERSTANDING,
)


# ---------------------------------------------------------------------------
# Confidence tiers (low/medium/high) - distinct from, and never to be
# confused with, backend.learning_memory's attribution_confidence
# (known/probable/unknown). Attribution confidence describes how sure
# Kognit is about WHICH SUBJECT a piece of evidence belongs to. This
# confidence describes how sure Kognit is that a PATTERN across multiple
# pieces of evidence is real. Both exist; they answer different questions.
# ---------------------------------------------------------------------------

INSIGHT_CONFIDENCE_LOW = "low"
INSIGHT_CONFIDENCE_MEDIUM = "medium"
INSIGHT_CONFIDENCE_HIGH = "high"

VALID_INSIGHT_CONFIDENCES = (INSIGHT_CONFIDENCE_LOW, INSIGHT_CONFIDENCE_MEDIUM, INSIGHT_CONFIDENCE_HIGH)


# ---------------------------------------------------------------------------
# Insight taxonomy - deliberately exactly three categories, per the Phase
# 5E brief. Do not add more without the same evidentiary rigor applied
# here (a distinct, deterministic, testable rule per category).
# ---------------------------------------------------------------------------

INSIGHT_REPEATED_DIFFICULTY = "repeated_difficulty"
INSIGHT_RECURRING_CONFUSION = "recurring_confusion"
INSIGHT_EMERGING_STRENGTH = "emerging_strength"

VALID_INSIGHT_TYPES = (INSIGHT_REPEATED_DIFFICULTY, INSIGHT_RECURRING_CONFUSION, INSIGHT_EMERGING_STRENGTH)

# Which chat signal_type values count as evidence for each insight
# category. repeated_difficulty deliberately covers a BROADER set than
# recurring_confusion - a group can produce BOTH insights at once (e.g.
# 2 confusion events also count toward repeated_difficulty AND
# separately earn their own, more specific recurring_confusion label) -
# this mirrors mastery_engine's own precedent of layering a more specific
# claim (Mastered) alongside a more general one, rather than treating
# insight categories as mutually exclusive buckets.
_DIFFICULTY_SIGNAL_TYPES = (SIGNAL_REPEATED_QUESTION, SIGNAL_REEXPLANATION, SIGNAL_CONFUSION, SIGNAL_MISCONCEPTION)
_CONFUSION_SIGNAL_TYPES = (SIGNAL_CONFUSION,)
_STRENGTH_SIGNAL_TYPES = (SIGNAL_SUCCESSFUL_UNDERSTANDING,)

_INSIGHT_SIGNAL_MAP = (
    (INSIGHT_REPEATED_DIFFICULTY, _DIFFICULTY_SIGNAL_TYPES),
    (INSIGHT_RECURRING_CONFUSION, _CONFUSION_SIGNAL_TYPES),
    (INSIGHT_EMERGING_STRENGTH, _STRENGTH_SIGNAL_TYPES),
)


# ---------------------------------------------------------------------------
# Named, tunable constants (Phase 5E starting hypotheses - see module
# docstring). Mirrors mastery_engine.MIN_EVIDENCE exactly in spirit.
# ---------------------------------------------------------------------------

MIN_EVENTS_FOR_INSIGHT = 2
"""Fewer than this many qualifying events (of the relevant signal types,
within one subject/topic group) -> no insight of that type is emitted at
all. A single event must NEVER produce an insight - this is the
structural guard against exactly the "one confused message = weak at
algebra" failure mode Phase 5E exists to prevent (see brief Section 3)."""

MEDIUM_CONFIDENCE_MIN_KNOWN_EVENTS = 2
HIGH_CONFIDENCE_MIN_KNOWN_EVENTS = 4
"""
Confidence tier is driven SPECIFICALLY by the count of KNOWN-attribution
qualifying events - never by probable-attribution events, however many
of those exist. This is the direct, structural fix for the exact class
of bug already found once in this codebase (chat evidence's
attribution_confidence being silently treated as more certain than it
was) - see _confidence_tier. A group with zero KNOWN events (all
probable/inherited context) can still produce an insight once
MIN_EVENTS_FOR_INSIGHT total qualifying events exist, but that insight's
confidence is always LOW, regardless of count - inherited context, no
matter how often repeated, is still inherited, not independently
re-stated.
"""


@dataclass(frozen=True)
class ChatEvidenceRecord:
    """
    A single, source-agnostic unit of chat-derived learning evidence -
    the Phase 5E analogue of backend.mastery_engine.EvidenceEvent, for
    qualitative (not quantitative) evidence.

    record_id is used purely as a deduplication key (see _dedupe) - a
    stable identity for one learning_evidence row, not assumed to encode
    chronology.

    attribution_confidence MUST be one of backend.learning_memory's
    CONFIDENCE_KNOWN/CONFIDENCE_PROBABLE - see compute_insights for why a
    record with any other value (including CONFIDENCE_UNKNOWN) is
    filtered out rather than trusted.
    """

    record_id: str
    subject: str
    topic: Optional[str]
    topic_key: Optional[str]
    signal_type: str
    attribution_confidence: str
    occurred_at: datetime


@dataclass(frozen=True)
class Insight:
    """
    One derived Student Intelligence insight. Deliberately has NO numeric
    score/percentage/rate field - see module docstring's "RELATIONSHIP TO
    backend.mastery_engine" section for why that is a structural
    guarantee, not a convention.
    """

    subject: str
    topic: Optional[str]
    topic_key: Optional[str]
    insight_type: str
    confidence: str
    evidence_count: int
    first_observed_at: datetime
    last_observed_at: datetime


def _group_key(subject: str, topic_key: Optional[str]) -> str:
    """
    Groups by topic_key when genuinely known, else by subject alone -
    IDENTICAL semantics to backend.learning_memory.build_conversation_notes'
    own grouping key, reimplemented here (not imported/shared) because
    that function's job is producing display strings, not Insight objects;
    duplicating this one small, stable rule is simpler and safer than
    creating a cross-module dependency for it. A normalized topic_key can
    never contain "::", so the two key spaces never collide.
    """
    if isinstance(topic_key, str) and topic_key.strip():
        return topic_key
    return f"subject::{subject.strip().casefold()}"


def _dedupe(records: Sequence[ChatEvidenceRecord]) -> list:
    """
    Removes records sharing the same non-empty record_id, keeping the
    first occurrence. Defensive - backend/database.py's query should
    never actually produce duplicate rows, but a pure function operating
    on already-fetched data should not assume that unconditionally (see
    brief Section 14: "avoid... counting the same evidence twice")."""
    seen = set()
    deduped = []
    for record in records:
        if record.record_id and record.record_id in seen:
            continue
        if record.record_id:
            seen.add(record.record_id)
        deduped.append(record)
    return deduped


def _confidence_tier(known_count: int) -> str:
    """See HIGH_CONFIDENCE_MIN_KNOWN_EVENTS/MEDIUM_CONFIDENCE_MIN_KNOWN_EVENTS
    docstring above for the full reasoning - probable-only evidence is
    always LOW regardless of how many events exist."""
    if known_count >= HIGH_CONFIDENCE_MIN_KNOWN_EVENTS:
        return INSIGHT_CONFIDENCE_HIGH
    if known_count >= MEDIUM_CONFIDENCE_MIN_KNOWN_EVENTS:
        return INSIGHT_CONFIDENCE_MEDIUM
    return INSIGHT_CONFIDENCE_LOW


def _build_insight(group_records: Sequence[ChatEvidenceRecord], insight_type: str, signal_types: Sequence[str]) -> Optional[Insight]:
    qualifying = [r for r in group_records if r.signal_type in signal_types]
    if len(qualifying) < MIN_EVENTS_FOR_INSIGHT:
        return None

    known_count = sum(1 for r in qualifying if r.attribution_confidence == CONFIDENCE_KNOWN)
    confidence = _confidence_tier(known_count)

    occurred_ats = sorted(r.occurred_at for r in qualifying)
    display = group_records[0]  # every record in a group shares subject/topic/topic_key by construction

    return Insight(
        subject=display.subject,
        topic=display.topic,
        topic_key=display.topic_key,
        insight_type=insight_type,
        confidence=confidence,
        evidence_count=len(qualifying),
        first_observed_at=occurred_ats[0],
        last_observed_at=occurred_ats[-1],
    )


def compute_insights(records: Sequence[ChatEvidenceRecord]) -> list:
    """
    The single public entry point of this module - the Phase 5E analogue
    of backend.mastery_engine.compute_topic_status.

    Filters to genuinely usable records (Known/Probable attribution only
    - CONFIDENCE_UNKNOWN records, and any other malformed value, are
    excluded outright and can never produce a subject-specific insight,
    per brief Section 8/13), deduplicates by record_id, groups by
    (subject, topic_key) using the same coarser-when-topic-absent rule as
    build_conversation_notes, then evaluates each of the three insight
    categories independently per group.

    Returns a list of Insight, most-recently-observed first. Never
    raises on malformed individual records (skipped, not fatal) - one bad
    row must not hide a student's entire insight set, matching
    get_user_topic_profile's established resilience convention.
    """
    usable = [
        r for r in records
        if isinstance(r, ChatEvidenceRecord)
        and isinstance(r.subject, str) and r.subject.strip()
        and r.attribution_confidence in (CONFIDENCE_KNOWN, CONFIDENCE_PROBABLE)
    ]
    usable = _dedupe(usable)

    groups: "OrderedDict[str, list]" = OrderedDict()
    for record in usable:
        key = _group_key(record.subject, record.topic_key)
        groups.setdefault(key, []).append(record)

    insights = []
    for group_records in groups.values():
        for insight_type, signal_types in _INSIGHT_SIGNAL_MAP:
            insight = _build_insight(group_records, insight_type, signal_types)
            if insight is not None:
                insights.append(insight)

    insights.sort(key=lambda i: i.last_observed_at, reverse=True)
    return insights
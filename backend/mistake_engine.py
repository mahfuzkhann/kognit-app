"""
Kognit Phase 6C - Mistake Intelligence (quiz-derived).

PURE DOMAIN MODULE, same architectural rule as backend/mastery_engine.py,
backend/insight_engine.py, and backend/learning_snapshot.py: no I/O, no
network calls, no database access, no Gemini calls.

ARCHITECTURE BOUNDARY:
  - This module never imports backend.database or backend.main, matching
    the same layering rule as its sibling engines.
  - backend/database.py is responsible for querying Supabase and
    translating raw quiz_attempts rows into EvidenceEvent instances
    (imported here as a TYPE only, from backend.mastery_engine - see
    "RELATIONSHIP TO mastery_engine.py" below), exactly mirroring how
    backend/database.py already bridges quiz_attempts rows to
    EvidenceEvent for mastery_engine.
  - backend/main.py's profile_mistakes_endpoint computes nothing itself;
    it calls a new backend.database function that returns the exact
    output of compute_mistake_intelligence below.

==============================================================================
SCOPE: WHAT THIS MODULE DOES AND DOES NOT CLAIM
==============================================================================

This module identifies exactly ONE thing: REPEATED TOPIC DIFFICULTY -
"the evidence shows this student has gotten questions wrong, more than
once, across more than one quiz attempt, in this topic." It does NOT
identify a specific conceptual mistake or misconception, and does not
attempt to.

WHY NOT "recurring_mistake_pattern" (specific misconception detection):
The only per-question evidence Kognit stores (see the quiz_answers table:
question_text, selected_index, correct_index, is_correct) carries no
concept/skill tag distinguishing WHICH kind of error a wrong answer
represents. Two wrong answers on differently-worded questions in the same
topic could be completely unrelated mistakes; two similar-sounding
questions could test different concepts entirely. Clustering by
question_text similarity or keyword matching would be GUESSING at
semantic meaning from free text - exactly what Phase 6C is required not
to do (no LLM classification of answers, no keyword-based semantic
inference standing in for real evidence). Until quiz questions carry a
structured concept/skill tag at generation time, a SPECIFIC recurring
mistake/misconception cannot be honestly identified from what is stored
today. This is an explicit, deliberate scope limitation, not an
oversight - see "KNOWN LIMITATIONS" below.

This module therefore reports only counts and timestamps - never a
"severity" field (no defensible severity scale has been defined) and
never a "confidence" field (unlike Phase 5E's confidence, which has a
concrete meaning in terms of KNOWN-attribution event counts, there is no
equivalent concept here: every event this module looks at is graded
quiz-attempt data, already unambiguous). It also never labels a trend
("improving" / "persistent") - see RECENT VS EARLIER SPLIT below for why.

==============================================================================
RELATIONSHIP TO mastery_engine.py (Phase 5A) - DELIBERATELY NOT REUSED
==============================================================================

This module does NOT import or recompute STATUS_*/compute_topic_status,
and does NOT produce a competing mastery label. mastery_engine.py answers
"what is this student's current competency label in this topic" - a
single categorical judgment derived from an overall correctness RATE,
combined with a positive-trend check. This module answers a narrower,
different question: "how many individual incorrect answers has the
evidence recorded for this topic, across how many attempts, and when" - a
raw COUNT + timestamp view, entirely independent of any rate-bucketing
threshold. The two will often flag the same topics (a topic with many
recorded wrong answers is likely also "Needs Practice"), but that is
expected overlap between two different lenses on the same underlying
evidence, not duplication of one algorithm by the other.

This module DOES reuse backend.mastery_engine.EvidenceEvent as a shared
evidence-unit TYPE (the exact same shape backend.database already
produces from quiz_attempts rows for mastery_engine) - a data-type import
only, not an algorithm import.

==============================================================================
MISTAKE IDENTITY
==============================================================================

Identity here is topic-level only: (subject, topic_key). topic_key is the
existing Phase 5A deterministic normalization (trim + collapse whitespace
+ casefold - see supabase/migrations/0002_quiz_topic_identity.sql) and is
safe for this purpose. There is no attempt at question-level or
concept-level mistake identity - see SCOPE above for why that evidence
does not exist yet.

==============================================================================
RECENT VS EARLIER SPLIT - WHAT IT DOES AND DOES NOT SUPPORT
==============================================================================

RECENT_WINDOW_SIZE mirrors mastery_engine.compute_topic_status's own
`recent_limit` default (5) purely for consistency of what "recent" means
across the intelligence layer - it is declared independently here (not
imported) so a future change to mastery_engine's cap does not silently
change 6C's meaning of "recent" without a deliberate decision.

`earlier_incorrect_count` is None (never 0) when every recorded attempt
for a topic falls within the recent window - i.e. there is no earlier
evidence to compare against. This is required precisely so a caller can
never misread "0 earlier mistakes" (meaning "no earlier data exists") as
"the student used to make mistakes and has since stopped" (a genuine
improvement claim, which this module does not have enough information to
make - the recent and earlier windows can have different numbers of
attempts and different numbers of questions per attempt, so a raw count
comparison between them is not normalized and would not honestly support
a trend claim). NO "improving" / "persistent" / "recent" trend LABEL is
produced anywhere in this module for this reason - only the raw counts
are returned, deliberately leaving trend interpretation to a later,
explicitly-scoped phase with a properly normalized comparison.
"""

from dataclasses import dataclass
from typing import Optional, Sequence

from backend.mastery_engine import EvidenceEvent

# ---------------------------------------------------------------------------
# Named, tunable constant (Phase 6C heuristic - NOT empirically validated,
# same status as every mastery_engine/insight_engine threshold).
# ---------------------------------------------------------------------------

MIN_AFFECTED_ATTEMPTS_FOR_REPEATED_DIFFICULTY = 2
"""
Fewer than this many SEPARATE quiz attempts containing at least one
incorrect answer in a topic -> no repeated_topic_difficulty mistake is
reported for it at all.

Deliberately gated on AFFECTED ATTEMPTS (distinct quiz-taking occasions),
not on total incorrect-answer count - a single quiz attempt with several
wrong answers is one bad sitting, not yet "repeated" in the sense this
module reports (see module docstring's WRONG ANSWER vs REPEATED
DIFFICULTY distinction). Independently declared here rather than reusing
mastery_engine.MIN_EVIDENCE (which gates on total ATTEMPT count for a
different purpose - status computation - not on how many of those
attempts contained a mistake).
"""

RECENT_WINDOW_SIZE = 5
"""See module docstring's RECENT VS EARLIER SPLIT section."""

MISTAKE_TYPE_REPEATED_TOPIC_DIFFICULTY = "repeated_topic_difficulty"


@dataclass(frozen=True)
class TopicMistakeEvidence:
    """
    All of one student's graded quiz evidence for one (subject,
    topic_key), handed to this module by backend.database - mirrors the
    grouping backend.database._build_topic_profile already performs for
    mastery_engine, but exposes the raw event list instead of collapsing
    it into a status.
    """
    subject: str
    topic: Optional[str]
    topic_key: str
    events: Sequence[EvidenceEvent]


def _sort_events(events: Sequence[EvidenceEvent]) -> list:
    """
    Defensive, deterministic ascending sort by (occurred_at, event_id) -
    the same tie-break convention as mastery_engine._sort_events
    (independently implemented here rather than imported, since it is a
    private helper of that module - see the architecture boundary note in
    backend.learning_snapshot for the same tradeoff already made
    elsewhere in this codebase). This module must never assume its caller
    already sorted the input.
    """
    return sorted(events, key=lambda e: (e.occurred_at, e.event_id))


def _incorrect_count(event: EvidenceEvent) -> int:
    """Number of incorrect answers in a single attempt. Defensive against
    malformed total_questions (<=0), matching mastery_engine's own
    treatment of such events as contributing no usable evidence."""
    if event.total_questions <= 0:
        return 0
    incorrect = event.total_questions - event.score
    return incorrect if incorrect > 0 else 0


def _compute_one_topic_mistake(evidence: TopicMistakeEvidence) -> Optional[dict]:
    events_sorted = _sort_events(evidence.events)
    if not events_sorted:
        return None

    affected_attempts = sum(1 for e in events_sorted if _incorrect_count(e) > 0)
    if affected_attempts < MIN_AFFECTED_ATTEMPTS_FOR_REPEATED_DIFFICULTY:
        return None

    total_incorrect = sum(_incorrect_count(e) for e in events_sorted)

    first_observed_at = events_sorted[0].occurred_at.isoformat()
    last_observed_at = events_sorted[-1].occurred_at.isoformat()

    recent_window = events_sorted[-RECENT_WINDOW_SIZE:]
    earlier_window = events_sorted[:-RECENT_WINDOW_SIZE] if len(events_sorted) > RECENT_WINDOW_SIZE else []

    recent_incorrect_count = sum(_incorrect_count(e) for e in recent_window)
    earlier_incorrect_count = sum(_incorrect_count(e) for e in earlier_window) if earlier_window else None

    return {
        "mistake_type": MISTAKE_TYPE_REPEATED_TOPIC_DIFFICULTY,
        "subject": evidence.subject,
        "topic": evidence.topic,
        "topic_key": evidence.topic_key,
        "affected_attempts": affected_attempts,
        "total_incorrect": total_incorrect,
        "first_observed_at": first_observed_at,
        "last_observed_at": last_observed_at,
        "recent_incorrect_count": recent_incorrect_count,
        "earlier_incorrect_count": earlier_incorrect_count,
    }


def compute_mistake_intelligence(topic_evidence: Sequence[TopicMistakeEvidence]) -> list:
    """
    The single public entry point of this module - the Phase 6C analogue
    of mastery_engine.compute_topic_status / insight_engine.compute_insights.

    Args:
        topic_evidence: one TopicMistakeEvidence per (subject, topic_key)
            the student has quiz evidence for - see backend.database's
            new mistake-evidence read function for how these are built
            from quiz_attempts rows.

    Returns a bare list of mistake dicts (matching the convention already
    used by get_user_topic_profile/get_learning_insights, which also
    return bare lists - the single top-level JSON key is added by
    backend/main.py, not here), one entry per topic that meets
    MIN_AFFECTED_ATTEMPTS_FOR_REPEATED_DIFFICULTY, most-recently-observed
    first. Topics with insufficient evidence are silently omitted (an
    empty list is the correct, honest answer when no topic qualifies -
    never a fabricated entry).

    Never raises on malformed individual entries (skipped defensively) -
    matching every sibling engine's own resilience convention.
    """
    mistakes = []
    for evidence in topic_evidence:
        if not isinstance(evidence, TopicMistakeEvidence):
            continue
        result = _compute_one_topic_mistake(evidence)
        if result is not None:
            mistakes.append(result)

    mistakes.sort(key=lambda m: m["last_observed_at"], reverse=True)
    return mistakes
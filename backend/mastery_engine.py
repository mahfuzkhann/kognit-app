"""
Kognit Phase 5A - Student Intelligence domain logic.

PURE DOMAIN MODULE. No I/O, no network calls, no database access, no
knowledge of Supabase, PostgREST, quizzes, subjects, or topics. Every
function here takes plain Python values in and returns plain Python
values out - this is what makes it trivially unit-testable without any
mocking, and what keeps it reusable for a future evidence source (e.g.
Phase 5B chat signals) without rewriting a single line of the algorithm
below.

ARCHITECTURE BOUNDARY (approved in the Phase 5A guardrail review):
  - backend/database.py is responsible for querying Supabase and
    translating raw quiz_attempts rows into EvidenceEvent instances. It
    is the ONLY module that knows an EvidenceEvent came from a quiz.
  - backend/main.py is responsible for authentication/authorization and
    HTTP concerns only. It must never compute a status itself.
  - This module never imports backend.database or backend.main, and never
    will - that import direction is a straightforward way to catch an
    accidental layering violation during review (mastery_engine importing
    database.py would be exactly backward).

THRESHOLDS ARE PRODUCT HYPOTHESES, NOT SCIENTIFIC FACTS. Every constant
below is a deliberately named, isolated value so it can be retuned later
against real multi-student evidence - never off a single anecdote. Do not
describe these thresholds as validated in any UI copy, report, or comment
beyond this one.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence


# ---------------------------------------------------------------------------
# Named, tunable constants (Phase 5A approved starting hypotheses).
# ---------------------------------------------------------------------------

MIN_EVIDENCE = 2
"""Fewer than this many attempts on a topic -> "Insufficient Evidence".
A single attempt (good or bad) must never produce a weakness/strength
claim - this is the structural guarantee that enforces that."""

MASTERED_MIN_ATTEMPTS = 3
IMPROVING_MIN_ATTEMPTS = 3

NEEDS_PRACTICE_MAX_RATE = 0.50
"""Overall correctness strictly below this -> "Needs Practice"."""

DEVELOPING_MAX_RATE = 0.70
"""Overall correctness in [NEEDS_PRACTICE_MAX_RATE, this) -> "Developing"."""

STRONG_MIN_RATE = 0.70
"""Overall correctness at/above this, with no Mastered/Improving override
and sufficient evidence -> "Strong". Intentionally the same value as
DEVELOPING_MAX_RATE - there is exactly one boundary between the two
bands, not a gap or an overlap."""

MASTERED_MIN_RATE = 0.85
"""Both the OVERALL rate and the LAST TWO individual attempts must each be
at/above this for "Mastered" - see _is_mastered() for why both checks
exist (an old high score must not be able to prop up a stale average)."""


# ---------------------------------------------------------------------------
# Status labels. Defined as string constants (not a bare enum) so the JSON
# API can return them directly without a serialization step - the values
# themselves are the public contract.
# ---------------------------------------------------------------------------

STATUS_INSUFFICIENT_EVIDENCE = "Insufficient Evidence"
STATUS_NEEDS_PRACTICE = "Needs Practice"
STATUS_DEVELOPING = "Developing"
STATUS_IMPROVING = "Improving"
STATUS_STRONG = "Strong"
STATUS_MASTERED = "Mastered"


@dataclass(frozen=True)
class EvidenceEvent:
    """
    A single, source-agnostic unit of graded evidence.

    This is deliberately NOT shaped like a quiz_attempts row - it has no
    `subject`, `topic`, `board`, or `user_id` field, because this module
    must remain usable for any future evidence source (chat signals,
    assignments, practice sessions, ...) without modification. The only
    caller-visible identity is `event_id`, used purely as a deterministic
    tie-break for ordering (see _sort_events) - it is NOT assumed to
    encode chronology (e.g. a UUIDv4 primary key is not sortable by
    creation time), only to be stable and unique per event.

    score / total_questions: score is bounded 0..total_questions by
    callers (this module does not re-validate that here - the boundary
    that owns the underlying data, e.g. the `quiz_attempts` check
    constraint or backend/database.py's translation step, is responsible
    for that). total_questions must be > 0 for an event to be meaningful;
    a zero/negative value is rejected defensively in compute_topic_status.
    """

    score: int
    total_questions: int
    occurred_at: datetime
    event_id: str = ""


def _sort_events(events: Sequence[EvidenceEvent]) -> list:
    """
    Defensive, deterministic ascending sort by (occurred_at, event_id).

    REQUIRED GUARDRAIL: every function below that depends on "most
    recent" or "earliest" must call this first and must NEVER assume the
    caller already sorted its input - a database ordering change, a
    PostgREST behavior change, or a future refactor of the caller must
    not be able to silently corrupt a student's computed status.

    event_id is a tie-break only (see EvidenceEvent docstring) - it makes
    the sort deterministic when two events share an identical
    `occurred_at`, but does not itself claim to represent chronological
    order.
    """
    return sorted(events, key=lambda e: (e.occurred_at, e.event_id))


def _per_event_rate(event: EvidenceEvent) -> Optional[float]:
    if event.total_questions <= 0:
        return None
    return event.score / event.total_questions


def _overall_rate(events_sorted: Sequence[EvidenceEvent]) -> Optional[float]:
    total_questions = sum(e.total_questions for e in events_sorted)
    if total_questions <= 0:
        return None
    total_correct = sum(e.score for e in events_sorted)
    return total_correct / total_questions


def _last_n_rates(events_sorted: Sequence[EvidenceEvent], n: int) -> list:
    """Per-event rates (score/total_questions) of the last `n` events, in
    the same ascending order as `events_sorted` (i.e. the most recent
    event is last in the returned list, not first). Events with
    total_questions <= 0 are skipped defensively rather than raising -
    they simply cannot contribute a rate."""
    tail = events_sorted[-n:] if n > 0 else []
    rates = [r for r in (_per_event_rate(e) for e in tail) if r is not None]
    return rates


def _is_mastered(events_sorted: Sequence[EvidenceEvent]) -> bool:
    """
    Mastered requires ALL of:
      - at least MASTERED_MIN_ATTEMPTS attempts
      - the OVERALL correct rate across all attempts >= MASTERED_MIN_RATE
      - the LAST TWO individual attempts EACH >= MASTERED_MIN_RATE

    The last-two-attempts check exists specifically so a single old, high
    score cannot prop up an average that no longer reflects the student's
    current performance - a topic that scored 95% once and then 40%
    twice must not read as Mastered just because the mean is still high
    enough on its own.
    """
    if len(events_sorted) < MASTERED_MIN_ATTEMPTS:
        return False

    overall = _overall_rate(events_sorted)
    if overall is None or overall < MASTERED_MIN_RATE:
        return False

    recent_two = _last_n_rates(events_sorted, 2)
    if len(recent_two) < 2:
        return False

    return all(rate >= MASTERED_MIN_RATE for rate in recent_two)


def _shows_positive_trend(events_sorted: Sequence[EvidenceEvent]) -> bool:
    """
    "Clear positive trend" (Phase 5A hypothesis, deliberately simple):
    the most recent attempt's own rate is strictly higher than the
    unweighted mean of every prior attempt's rate.

    This is a per-attempt-rate average (not a pooled score/question
    total), so quizzes of different lengths (5/10/15 questions - see
    templates/index.html #quiz-count-select) are compared on equal
    footing rather than the longer quiz dominating the average.

    Requires at least IMPROVING_MIN_ATTEMPTS events to be considered at
    all (checked by the caller); with that guarantee there are always at
    least 2 prior events to average here.
    """
    if len(events_sorted) < 2:
        return False

    *prior, most_recent = events_sorted
    prior_rates = [r for r in (_per_event_rate(e) for e in prior) if r is not None]
    recent_rate = _per_event_rate(most_recent)

    if not prior_rates or recent_rate is None:
        return False

    prior_mean = sum(prior_rates) / len(prior_rates)
    return recent_rate > prior_mean


def _determine_status(events_sorted: Sequence[EvidenceEvent]) -> str:
    """
    Deterministic status precedence (explicitly encoded, not accidental
    conditional ordering - see the test suite for boundary coverage of
    every branch below):

      1. n < MIN_EVIDENCE                                -> Insufficient Evidence
      2. Mastered check passes                           -> Mastered
      3. overall_rate < NEEDS_PRACTICE_MAX_RATE           -> Needs Practice
      4. n >= IMPROVING_MIN_ATTEMPTS and positive trend   -> Improving
      5. overall_rate < DEVELOPING_MAX_RATE               -> Developing
      6. otherwise (overall_rate >= STRONG_MIN_RATE)      -> Strong

    Design decision worth stating explicitly (not dictated by the
    approved spec verbatim, and flagged here rather than buried): step 3
    is checked BEFORE step 4, so "Improving" can only ever apply to a
    topic whose overall rate is already >= NEEDS_PRACTICE_MAX_RATE (i.e.
    at least "Developing"-level or better). A topic still failing overall
    (e.g. attempts of 10%, 20%, 45% - overall 25%, clearly trending up)
    is reported as "Needs Practice", not "Improving" - the trend is real,
    but labeling a topic the student is still failing as "Improving"
    would risk the student reading it as "this is fine now," which is
    not an honest reading of the evidence. Step 2 (Mastered) is checked
    before step 4 (Improving) because Mastered is the more specific,
    stronger claim - a topic that both trends upward and clears the
    Mastered bar is reported as Mastered.
    """
    n = len(events_sorted)

    if n < MIN_EVIDENCE:
        return STATUS_INSUFFICIENT_EVIDENCE

    if _is_mastered(events_sorted):
        return STATUS_MASTERED

    overall = _overall_rate(events_sorted)
    if overall is None:
        # Every event had total_questions <= 0 - can't happen with real
        # quiz data (DB check constraint enforces total_questions > 0),
        # but a pure function should not divide by zero on malformed
        # input either. Treated the same as no usable evidence.
        return STATUS_INSUFFICIENT_EVIDENCE

    if overall < NEEDS_PRACTICE_MAX_RATE:
        return STATUS_NEEDS_PRACTICE

    if n >= IMPROVING_MIN_ATTEMPTS and _shows_positive_trend(events_sorted):
        return STATUS_IMPROVING

    if overall < DEVELOPING_MAX_RATE:
        return STATUS_DEVELOPING

    return STATUS_STRONG


def compute_topic_status(events: Sequence[EvidenceEvent], recent_limit: int = 5) -> dict:
    """
    The single public entry point of this module.

    Computes the full, explainable evidence dict for one topic's worth of
    EvidenceEvents - never a bare status label (Phase 5A explicitly
    requires this: a student-facing "why" must always be derivable).

    Always re-sorts `events` defensively (see _sort_events) regardless of
    the order they were passed in.

    Returns:
        {
            "status": str,                  # one of the STATUS_* constants
            "attempt_count": int,
            "total_questions": int,
            "total_correct": int,
            "correct_rate": float | None,   # None only if attempt_count == 0
            "last_attempt_at": str | None,  # ISO 8601, None if attempt_count == 0
            "recent_attempts": [
                {"score": int, "total_questions": int, "occurred_at": str},
                ...
            ],  # most recent first, capped at `recent_limit`
        }
    """
    events_sorted = _sort_events(events)
    n = len(events_sorted)

    total_questions = sum(e.total_questions for e in events_sorted)
    total_correct = sum(e.score for e in events_sorted)
    correct_rate = _overall_rate(events_sorted)
    last_attempt_at = events_sorted[-1].occurred_at.isoformat() if n > 0 else None

    recent_attempts = [
        {
            "score": e.score,
            "total_questions": e.total_questions,
            "occurred_at": e.occurred_at.isoformat(),
        }
        for e in reversed(events_sorted[-recent_limit:])
    ]

    return {
        "status": _determine_status(events_sorted),
        "attempt_count": n,
        "total_questions": total_questions,
        "total_correct": total_correct,
        "correct_rate": correct_rate,
        "last_attempt_at": last_attempt_at,
        "recent_attempts": recent_attempts,
    }


def is_weak_status(status: str) -> bool:
    """
    "Weak topic" (Phase 5A scope) = Needs Practice or Developing. Not a
    separately persisted concept - a thin, named predicate over the same
    computed status, so callers (e.g. a future `?status=weak` filter on
    GET /api/profile/topics) don't have to hardcode this set themselves.
    """
    return status in (STATUS_NEEDS_PRACTICE, STATUS_DEVELOPING)
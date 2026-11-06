"""
Centralised Free/Pro entitlement policy (CEO decision D-4).

ONE place answers "may this plan use this feature?" and "which quota meter
does this feature spend?". Endpoints call require_feature(); they never
compare plan strings themselves. The plan itself always comes from the
database (public.get_my_plan(), which reads public.subscriptions) - never
from the client.

NUMERIC QUOTAS live in the database (public.plan_limits), enforced atomically
by public.consume_quota(); QUOTA_POLICY below is the same policy written down
as data so a test can assert the migration seeds match it (single source of
truth, no drift). It is documentation + test oracle, not a second enforcer.

AMBIGUITIES (implemented configurable, flagged for the CEO):
  * Free quiz allowance: "3 general quiz generations" has no stated reset
    period. The existing, shipped policy is 3 per WEEK (Asia/Dhaka, weeks start
    Monday); it is kept and can be changed with one UPDATE of plan_limits.
  * Pro flashcards "unlimited": quotas are integers, so the notes_ai meter is
    given an abuse-guard ceiling (PRO_NOTES_AI_PER_DAY) that no student
    reaches by studying; it is not a product limit.
  * Which existing endpoints are "advanced learning intelligence": the gated
    set is listed in PRO_FEATURES below.
"""
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException

from backend.ai import config

PLAN_FREE = "free"
PLAN_PRO = "pro"

# --- features ----------------------------------------------------------------
NOTE_FLASHCARDS = "note_flashcards"            # AI flashcards from a saved note
NOTE_QUIZ = "note_quiz"                        # AI mini-quiz from a saved note
LEARNING_INSIGHTS = "learning_insights"        # GET /api/profile/insights
LEARNING_SNAPSHOT = "learning_snapshot"        # GET /api/profile/snapshot
LEARNING_MISTAKES = "learning_mistakes"        # GET /api/profile/mistakes
LEARNING_NEXT_STEPS = "learning_next_steps"    # GET /api/profile/next-steps
LEARNING_MATRIX = "learning_matrix"            # GET /api/profile/topics (topic mastery matrix)

PRO_FEATURES = frozenset({
    NOTE_FLASHCARDS, NOTE_QUIZ, LEARNING_INSIGHTS, LEARNING_SNAPSHOT,
    LEARNING_MISTAKES, LEARNING_NEXT_STEPS, LEARNING_MATRIX,
})
PLAN_FEATURES = {PLAN_FREE: frozenset(), PLAN_PRO: PRO_FEATURES}

# --- quota policy as data (mirrors supabase/migrations/0014...) ---------------
PRO_NOTES_AI_PER_DAY = 200
QUOTA_POLICY = {
    # plan: {kind: (period, limit, first_day_limit)}
    PLAN_FREE: {"chat": ("day", 1, 3), "quiz": ("week", 3, None), "notes_ai": ("day", 6, None)},
    PLAN_PRO: {"chat": ("day", 20, None), "quiz": ("day", 15, None), "notes_ai": ("day", PRO_NOTES_AI_PER_DAY, None)},
}

CODE_PLAN_REQUIRED = "PLAN_REQUIRED"

_FEATURE_LABEL = {
    NOTE_FLASHCARDS: "Flashcards from notes",
    NOTE_QUIZ: "Quizzes from notes",
    LEARNING_INSIGHTS: "Learning insights",
    LEARNING_SNAPSHOT: "Learning snapshot",
    LEARNING_MISTAKES: "Mistake analysis",
    LEARNING_NEXT_STEPS: "Personalised next steps",
    LEARNING_MATRIX: "Learning matrix",
}


def normalize_plan(plan: Optional[str]) -> str:
    return PLAN_PRO if plan == PLAN_PRO else PLAN_FREE


def is_allowed(plan: Optional[str], feature: str) -> bool:
    """Unknown plan values are treated as Free (fail closed for paid features)."""
    return feature in PLAN_FEATURES[normalize_plan(plan)]


def quota_kind_for(feature: str, plan: Optional[str]) -> str:
    """Which meter a feature spends. A note-generated quiz spends the QUIZ
    allowance (same pool as ordinary quizzes); flashcards and other AI note
    tools spend the notes_ai meter."""
    if feature == NOTE_QUIZ:
        return "quiz"
    return "notes_ai"


def plan_required_exception(feature: str) -> HTTPException:
    label = _FEATURE_LABEL.get(feature, "This feature")
    return HTTPException(
        status_code=403,
        detail={
            "code": CODE_PLAN_REQUIRED,
            "feature": feature,
            "message": f"{label} is available on the Pro plan.",
            "required_plan": PLAN_PRO,
        },
    )


def require_feature(plan: Optional[str], feature: str) -> None:
    """Raises 403 PLAN_REQUIRED when the plan lacks the feature. No-op when
    PLAN_GATING=off (documented rollback) or the quota backend is unmetered."""
    if config.plan_gating_mode() == "off" or plan == "unmetered":
        return
    if not is_allowed(plan, feature):
        raise plan_required_exception(feature)
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

FINALIZED PLAN (migration 0016, CEO specification):
  Free: chat 1/day (3 on the signup day), quiz 1/week, Photo Solve 1 per account for life,
        no PDF upload, no Pocket Notes, no advanced models, no learning intelligence.
  Pro : chat 20/day, Photo Solve 5/day, PDF upload 2/day (max 5 pages), quiz 5/day, Pocket Notes and
        flashcards, learning intelligence, advanced models.
  Every number lives in public.plan_limits and is mirrored in QUOTA_POLICY below.

INTERPRETATIONS (implemented, documented in docs/PLANS_AND_QUOTAS.md, flagged for the CEO):
  * A request that carries a photo spends the Photo Solve meter INSTEAD of the chat meter. The two
    allowances are separate lines in the plan, and a photo is one AI generation either way, so cost
    is bounded by the sum of both meters. Free's one trial photo therefore never depends on whether
    today's single chat has been used.
  * Free has no PDF upload and no Pocket Notes (the plan table lists neither). Reading, listing and
    deleting notes a student already owns stays allowed: a plan change deletes nothing.
  * Pro flashcards are "Unlimited flashcards" in everything the student sees. Quotas are
    integers, so the database holds a TEMPORARY, CONFIGURABLE abuse guard of
    PRO_FLASHCARDS_PER_DAY (200) on a dedicated `flashcards` meter (migration 0015).
    It is internal: never advertised, never quoted in a student-facing message. Change it
    with: update public.plan_limits set limit_count = N where plan='pro' and kind='flashcards';
  * Pro `notes_ai` (chat titles + tag suggestions) keeps a separate generic guard
    (PRO_NOTES_AI_PER_DAY) so the two can never throttle each other.
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
POCKET_NOTES = "pocket_notes"                  # creating / editing a note, AI tag suggestions (migration 0016)
PDF_UPLOAD = "pdf_upload"                      # POST /api/pdf/upload (migration 0016)

PRO_FEATURES = frozenset({
    NOTE_FLASHCARDS, NOTE_QUIZ, LEARNING_INSIGHTS, LEARNING_SNAPSHOT,
    LEARNING_MISTAKES, LEARNING_NEXT_STEPS, LEARNING_MATRIX, POCKET_NOTES, PDF_UPLOAD,
})
PLAN_FEATURES = {PLAN_FREE: frozenset(), PLAN_PRO: PRO_FEATURES}

# --- quota policy as data (mirrors supabase/migrations/0014...) ---------------
PRO_NOTES_AI_PER_DAY = 200       # titles + tag suggestions (generic abuse guard, not a product number)
PRO_CHATS_PER_DAY = 20
PRO_PHOTO_SOLVES_PER_DAY = 5
PRO_PDF_UPLOADS_PER_DAY = 2
PRO_QUIZZES_PER_DAY = 5
PRO_FLASHCARDS_PER_DAY = 200     # TEMPORARY internal abuse guard behind "Unlimited flashcards"
QUOTA_POLICY = {
    # plan: {kind: (period, limit, first_day_limit)}. Free has NO flashcards and NO pdf_upload row on purpose.
    # Free `notes_ai` (6/day) is the guard behind chat-title generation only: Free has no Pocket Notes.
    PLAN_FREE: {"chat": ("day", 1, 3), "quiz": ("week", 1, None), "photo_solve": ("lifetime", 1, None),
                "notes_ai": ("day", 6, None)},
    PLAN_PRO: {"chat": ("day", PRO_CHATS_PER_DAY, None), "quiz": ("day", PRO_QUIZZES_PER_DAY, None),
               "photo_solve": ("day", PRO_PHOTO_SOLVES_PER_DAY, None),
               "pdf_upload": ("day", PRO_PDF_UPLOADS_PER_DAY, None),
               "notes_ai": ("day", PRO_NOTES_AI_PER_DAY, None),
               "flashcards": ("day", PRO_FLASHCARDS_PER_DAY, None)},
}

# --- upload limits (enforced on the SERVER from the real bytes, never from a client-reported size) --------
# "Less than 3 MB": a photo of 3 MiB (3,145,728 bytes) or more is refused; one byte less is accepted.
# Applied to every plan: Free's single trial photo is the same kind of request, and a Free limit larger
# than Pro's would make no sense.
PHOTO_MAX_BYTES = 3 * 1024 * 1024 - 1
PHOTO_LIMIT_LABEL = "3MB"
PDF_MAX_PAGES = 5

# What the student is TOLD Pro includes: the single source for upgrade-UI copy, asserted by tests.
# "Unlimited flashcards" is the advertised promise; the 200/day guard above is internal.
PRO_FEATURE_LIST = (
    "20x more usage limit",
    "35x more quizzes than Free plan",
    "Unlimited flashcards",
    "Quizzes and flashcards from your saved notes",
    "Learning insights and your learning matrix",
    "Personalised next steps and mistake analysis",
)

CODE_PLAN_REQUIRED = "PLAN_REQUIRED"

_FEATURE_LABEL = {
    NOTE_FLASHCARDS: "Flashcards from notes",
    NOTE_QUIZ: "Quizzes from notes",
    LEARNING_INSIGHTS: "Learning insights",
    LEARNING_SNAPSHOT: "Learning snapshot",
    LEARNING_MISTAKES: "Mistake analysis",
    LEARNING_NEXT_STEPS: "Personalised next steps",
    LEARNING_MATRIX: "Learning matrix",
    POCKET_NOTES: "Pocket Notes",
    PDF_UPLOAD: "PDF upload",
}


def normalize_plan(plan: Optional[str]) -> str:
    return PLAN_PRO if plan == PLAN_PRO else PLAN_FREE


def is_allowed(plan: Optional[str], feature: str) -> bool:
    """Unknown plan values are treated as Free (fail closed for paid features)."""
    return feature in PLAN_FEATURES[normalize_plan(plan)]


def quota_kind_for(feature: str, plan: Optional[str]) -> str:
    """Which meter a feature spends. A note-generated quiz spends the QUIZ
    allowance (same pool as ordinary quizzes); flashcards spend their own
    `flashcards` meter; other AI note tools spend notes_ai."""
    if feature == NOTE_QUIZ:
        return "quiz"
    if feature == NOTE_FLASHCARDS:
        return "flashcards"
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
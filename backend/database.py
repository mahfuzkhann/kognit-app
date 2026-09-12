"""
Supabase (Postgres via PostgREST) persistence helpers.

MVP APPROACH: writes go through Supabase's REST API (PostgREST) using
httpx - the same way backend/main.py already verifies auth tokens against
Supabase's Auth REST API (GET /auth/v1/user). This avoids adding a new
dependency (supabase-py) purely to perform two table inserts; httpx is
already a project dependency.

CRITICAL SECURITY PROPERTY: every insert here forwards the CALLING
STUDENT'S OWN Supabase access token - never a service-role key. That
means every write is subject to Postgres Row Level Security exactly as if
the student's own browser had called PostgREST directly. A student can
only ever insert/select rows where auth.uid() = user_id because that is
what the RLS policies (see supabase/migrations/0001_quiz_persistence.sql)
enforce - not because this module trusts anything the caller passes in.

This module has no service-role key and should never be given one. If a
real backend-only privileged write is ever needed later, that is a new,
explicit decision - it must not be smuggled into this module by editing
_rest_headers() to add one.
"""
import os
import re
import logging
from collections import OrderedDict
from datetime import datetime
from typing import Optional

import httpx

from backend.mastery_engine import EvidenceEvent, compute_topic_status
from backend.learning_memory import (
    CONFIDENCE_KNOWN,
    CONFIDENCE_PROBABLE,
    VALID_ATTRIBUTION_CONFIDENCES,
    VALID_SIGNAL_STRENGTHS,
    VALID_SIGNAL_TYPES,
    build_conversation_notes,
)
from backend.insight_engine import ChatEvidenceRecord, compute_insights

logger = logging.getLogger("kognit.database")

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")

DB_REQUEST_TIMEOUT_SECONDS = 10


class DatabaseError(Exception):
    """
    Raised when a Supabase REST call fails for any reason (network error,
    non-2xx response, unexpected response shape).

    The message on this exception may contain Postgres/PostgREST error
    text and is safe to log server-side, but callers MUST NOT return
    str(exception) to the student - translate it into one of the
    hand-written, generic messages already used elsewhere in this project
    (see GENERIC_CHAT_ERROR in backend/ai_engine.py for the established
    pattern).
    """
    pass


def _rest_headers(user_token: str) -> dict:
    return {
        "Authorization": f"Bearer {user_token}",
        "apikey": SUPABASE_ANON_KEY,
        "Content-Type": "application/json",
    }


_WHITESPACE_RUN_RE = re.compile(r"\s+")


def normalize_topic_key(topic: str) -> str:
    """
    Phase 5A deterministic topic normalization: trim + collapse internal
    whitespace + casefold.

    Computed HERE, server-side, at write time (see save_quiz_attempt) -
    never accepted from the client. The original human-readable `topic`
    text is always stored and returned unchanged alongside this key (see
    the `topic` column/field everywhere else in this module) - this
    function only produces the grouping key used for topic-level
    aggregation.

    Deliberately does NOT do any of the following (Phase 5A scope,
    approved): no LLM call, no embeddings, no fuzzy matching, no
    synonym inference, no Bangla/English/Banglish unification. Two
    genuinely different spellings of the same real-world topic will
    produce two different topic_key values on purpose - see
    supabase/migrations/0002_quiz_topic_identity.sql for the documented
    limitation and the long-term fix (a curriculum-aware topic
    vocabulary, not built here).

    `casefold()` is used rather than `lower()` because it is the
    Unicode-aware, more aggressive normalization recommended for
    case-insensitive comparison - relevant here since Kognit's topics are
    frequently typed in Bangla script or mixed Bangla/English.

    An empty/whitespace-only topic normalizes to "" - still a valid,
    deterministic key, just not a meaningful one; callers are not
    expected to treat "" specially.
    """
    if not isinstance(topic, str):
        return ""
    collapsed = _WHITESPACE_RUN_RE.sub(" ", topic.strip())
    return collapsed.casefold()


def _parse_timestamptz(value: str) -> Optional[datetime]:
    """
    Parses a PostgREST/Postgres `timestamptz` string (e.g.
    "2026-09-02T10:15:30.123456+00:00" or "...Z") into a timezone-aware
    datetime. Returns None (rather than raising) on anything unparseable
    so one malformed row can be skipped by the caller instead of crashing
    an entire profile request - see get_user_topic_profile.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Could not parse timestamptz value from Supabase: %r", value)
        return None


async def save_quiz_attempt(
    user_token: str,
    user_id: str,
    board: str,
    user_class: str,
    subject: str,
    stream: Optional[str],
    topic: str,
    questions: list,
    selected_answers: list,
) -> dict:
    """
    Persists one completed quiz attempt plus its per-question answers.

    `stream` is Optional: ISSUE 1 FIX (Phase 6A final correction) - a
    Class 6-8 student's profile has no stream at all (see
    NO_STREAM_CLASSES in backend/main.py), and quiz_attempts.stream has
    always been nullable (supabase/migrations/0002_quiz_topic_identity.sql
    added it without a NOT NULL). This function stores whatever it is
    given; it does not decide when a null stream is acceptable.

    PHASE 5A DATA MODEL: `subject` must be a real academic subject (e.g.
    "Physics"), and `stream` is the Science/Commerce/Arts track - these
    are now two distinct fields (previously the caller only had a single
    "subject" field that actually held the stream value; see
    supabase/migrations/0002_quiz_topic_identity.sql). This function does
    not validate that `subject` "looks like" a real subject - that
    correction lives entirely in how the caller (backend/main.py, fed by
    the new subject dropdown in the quiz UI) populates these two
    parameters now. `topic_key` is computed here, deterministically, from
    `topic` via normalize_topic_key() - never accepted from the caller.

    Grading happens HERE, not in the caller: `questions[i]["correct_index"]`
    (the server-held, validated quiz definition - see
    backend.ai_engine.validate_quiz_questions and
    backend.main.active_quiz_definitions) is compared against
    `selected_answers[i]` (the student's submitted choice) to compute both
    the per-question `is_correct` flag and the overall `score`. The caller
    must NOT pass in a pre-computed score or is_correct value - there is
    deliberately no parameter for either, so there is nothing here for a
    compromised/buggy caller to blindly trust from the client.

    Preconditions the caller (backend/main.py) is responsible for before
    calling this function:
      - len(questions) == len(selected_answers)
      - each selected_answers[i] is an int or None (never a bool - Python
        bools are an int subclass and `True == 1` would silently corrupt
        grading for option index 1)
      - `user_id` and `user_token` both come from a verified Supabase
        session for the SAME request - never mix a user_id from one
        request with a token from another

    Returns {"attempt_id": str, "score": int, "total_questions": int} on
    success.

    Raises DatabaseError on any failure. On a failure that happens AFTER
    the quiz_attempts row was already inserted (i.e. the quiz_answers
    insert fails), this function makes a best-effort attempt to delete
    that now-orphaned quiz_attempts row before raising, so a failed
    submission never leaves a "0 answers, non-zero score" ghost record.
    That delete is itself best-effort (also over PostgREST, also using the
    student's own token) - if it fails too, the orphan is logged for
    manual cleanup, but DatabaseError is still raised either way so the
    caller never reports success for a partially-failed write.
    """
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        raise DatabaseError(
            "Supabase is not configured on the server (SUPABASE_URL/SUPABASE_ANON_KEY missing)."
        )

    if len(questions) != len(selected_answers):
        # Defensive - callers should already guarantee this, but a
        # mismatch here would silently misgrade questions by position.
        raise DatabaseError(
            f"questions/selected_answers length mismatch "
            f"({len(questions)} vs {len(selected_answers)})"
        )

    total_questions = len(questions)
    graded_answers = []
    score = 0
    for i, q in enumerate(questions):
        selected = selected_answers[i]
        correct_index = q["correct_index"]
        is_correct = selected is not None and selected == correct_index
        if is_correct:
            score += 1
        graded_answers.append({
            "question_index": i,
            "question_text": q["question"],
            "selected_index": selected,
            "correct_index": correct_index,
            "is_correct": is_correct,
        })

    attempt_payload = {
        "user_id": user_id,
        "board": board,
        "user_class": user_class,
        "subject": subject,
        "stream": stream,
        "topic": topic,
        "topic_key": normalize_topic_key(topic),
        "total_questions": total_questions,
        "score": score,
    }

    try:
        async with httpx.AsyncClient(timeout=DB_REQUEST_TIMEOUT_SECONDS) as client:
            attempt_resp = await client.post(
                f"{SUPABASE_URL}/rest/v1/quiz_attempts",
                headers={**_rest_headers(user_token), "Prefer": "return=representation"},
                json=attempt_payload,
            )
    except httpx.RequestError:
        logger.exception(
            "save_quiz_attempt: network error inserting quiz_attempts (user_id=%s)", user_id
        )
        raise DatabaseError("network error inserting quiz_attempts")

    if attempt_resp.status_code not in (200, 201):
        logger.error(
            "save_quiz_attempt: quiz_attempts insert failed status=%d body=%s (user_id=%s)",
            attempt_resp.status_code, attempt_resp.text[:500], user_id,
        )
        raise DatabaseError(f"quiz_attempts insert failed with status {attempt_resp.status_code}")

    try:
        attempt_rows = attempt_resp.json()
        attempt_id = attempt_rows[0]["id"]
    except (ValueError, KeyError, IndexError, TypeError):
        logger.error(
            "save_quiz_attempt: unexpected quiz_attempts response shape (user_id=%s): %r",
            user_id, attempt_resp.text[:500],
        )
        raise DatabaseError("unexpected response shape from quiz_attempts insert")

    for row in graded_answers:
        row["attempt_id"] = attempt_id

    try:
        async with httpx.AsyncClient(timeout=DB_REQUEST_TIMEOUT_SECONDS) as client:
            answers_resp = await client.post(
                f"{SUPABASE_URL}/rest/v1/quiz_answers",
                headers=_rest_headers(user_token),
                json=graded_answers,
            )
    except httpx.RequestError:
        logger.exception(
            "save_quiz_attempt: network error inserting quiz_answers (attempt_id=%s) - "
            "attempting rollback of orphaned quiz_attempts row", attempt_id
        )
        await _rollback_attempt(user_token, attempt_id)
        raise DatabaseError("network error inserting quiz_answers")

    if answers_resp.status_code not in (200, 201, 204):
        logger.error(
            "save_quiz_attempt: quiz_answers insert failed status=%d body=%s (attempt_id=%s) - "
            "attempting rollback of orphaned quiz_attempts row",
            answers_resp.status_code, answers_resp.text[:500], attempt_id,
        )
        await _rollback_attempt(user_token, attempt_id)
        raise DatabaseError(f"quiz_answers insert failed with status {answers_resp.status_code}")

    return {"attempt_id": attempt_id, "score": score, "total_questions": total_questions}


async def _rollback_attempt(user_token: str, attempt_id: str) -> None:
    """
    Best-effort compensating delete of a quiz_attempts row whose
    quiz_answers insert failed. Uses the student's own token (permitted by
    the quiz_attempts_delete_own RLS policy - see the migration file).

    Never raises - this is a cleanup best-effort, not part of the
    request's success/failure path. If it fails, the orphaned row is
    logged for manual cleanup; the caller always raises DatabaseError
    regardless of whether this succeeds.
    """
    try:
        async with httpx.AsyncClient(timeout=DB_REQUEST_TIMEOUT_SECONDS) as client:
            resp = await client.delete(
                f"{SUPABASE_URL}/rest/v1/quiz_attempts",
                headers=_rest_headers(user_token),
                params={"id": f"eq.{attempt_id}"},
            )
        if resp.status_code not in (200, 204):
            logger.error(
                "save_quiz_attempt rollback: failed to delete orphaned quiz_attempts row "
                "id=%s status=%d body=%s - needs manual cleanup",
                attempt_id, resp.status_code, resp.text[:500],
            )
    except httpx.RequestError:
        logger.exception(
            "save_quiz_attempt rollback: network error deleting orphaned quiz_attempts "
            "row id=%s - needs manual cleanup", attempt_id
        )


# ---------------------------------------------------------------------------
# Phase 5A: GET /api/profile/topics read path.
#
# quiz_attempts remains the single, immutable, authoritative evidence
# source - nothing is persisted here beyond what save_quiz_attempt already
# writes. Status is recomputed on every call from the raw rows, so
# retuning backend.mastery_engine's thresholds later needs no migration
# and no backfill: the next request simply computes a different answer
# from the same unchanged evidence.
# ---------------------------------------------------------------------------

_PROFILE_TOPICS_SELECT = "id,subject,topic,topic_key,total_questions,score,created_at"


async def get_user_topic_profile(user_token: str, user_id: str) -> list:
    """
    Fetches the authenticated student's own quiz evidence, grouped by
    (subject, topic_key), and returns one explainable status dict per
    topic (see backend.mastery_engine.compute_topic_status for the exact
    shape).

    SECURITY: forwards the student's OWN Supabase access token, exactly
    like save_quiz_attempt above - RLS (`auth.uid() = user_id`, see
    quiz_attempts_select_own in 0001_quiz_persistence.sql) is what
    actually restricts the returned rows to this student, not any filter
    applied here. `user_id` is accepted only for logging, matching the
    established pattern in this module - it is never used to build a
    query filter, so it cannot be used to read another user's data even
    if a caller passed the wrong value by mistake.

    LEGACY ROWS: rows with `topic_key IS NULL` predate the Phase 5A
    subject/stream correction (see the migration file's "LEGACY ROWS"
    section) and are excluded via the `topic_key=not.is.null` filter
    below - they must never be silently reinterpreted as clean Phase 5A
    evidence by, e.g., normalizing their `topic` text on the fly here.

    Raises DatabaseError on any failure, same convention as
    save_quiz_attempt - callers must translate this into a generic,
    student-safe message rather than surfacing str(exception).
    """
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        raise DatabaseError(
            "Supabase is not configured on the server (SUPABASE_URL/SUPABASE_ANON_KEY missing)."
        )

    try:
        async with httpx.AsyncClient(timeout=DB_REQUEST_TIMEOUT_SECONDS) as client:
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/quiz_attempts",
                headers=_rest_headers(user_token),
                params={
                    "select": _PROFILE_TOPICS_SELECT,
                    "topic_key": "not.is.null",
                    "order": "created_at.asc",
                },
            )
    except httpx.RequestError:
        logger.exception(
            "get_user_topic_profile: network error fetching quiz_attempts (user_id=%s)", user_id
        )
        raise DatabaseError("network error fetching quiz_attempts")

    if resp.status_code != 200:
        logger.error(
            "get_user_topic_profile: quiz_attempts select failed status=%d body=%s (user_id=%s)",
            resp.status_code, resp.text[:500], user_id,
        )
        raise DatabaseError(f"quiz_attempts select failed with status {resp.status_code}")

    try:
        rows = resp.json()
    except ValueError:
        logger.error(
            "get_user_topic_profile: unexpected quiz_attempts response shape (user_id=%s): %r",
            user_id, resp.text[:500],
        )
        raise DatabaseError("unexpected response shape from quiz_attempts select")

    if not isinstance(rows, list):
        raise DatabaseError("unexpected response shape from quiz_attempts select")

    return _build_topic_profile(rows, user_id=user_id)


def _build_topic_profile(rows: list, user_id: str) -> list:
    """
    Groups raw quiz_attempts rows by (subject, topic_key), translates
    each row into a backend.mastery_engine.EvidenceEvent, and returns one
    explainable status dict per group.

    This is the ONLY place in the codebase that knows both "what a
    quiz_attempts row looks like" AND "what an EvidenceEvent looks like" -
    backend.mastery_engine never sees a row, and never learns that its
    input came from a quiz. Malformed individual rows are skipped
    (logged) rather than failing the whole profile - one bad row must not
    hide a student's entire topic history.

    Uses OrderedDict keyed by (subject, topic_key) so the LAST row seen
    for a group (rows arrive created_at ascending) provides the
    human-readable `topic` display text - i.e. the most recently typed
    spelling of a topic wins for display, even though older spellings
    that normalized to the same topic_key still contribute their
    evidence to the same group.
    """
    groups: "OrderedDict[tuple, dict]" = OrderedDict()

    for row in rows:
        if not isinstance(row, dict):
            continue

        subject = row.get("subject")
        topic = row.get("topic")
        topic_key = row.get("topic_key")
        score = row.get("score")
        total_questions = row.get("total_questions")
        created_at_raw = row.get("created_at")
        event_id = row.get("id")

        if not isinstance(subject, str) or not subject:
            continue
        if not isinstance(topic_key, str) or not topic_key:
            # Defensive - the `topic_key=not.is.null` filter should
            # already exclude these, but a pure function operating on
            # already-fetched rows should not trust that unconditionally.
            continue
        if not isinstance(score, int) or isinstance(score, bool):
            continue
        if not isinstance(total_questions, int) or isinstance(total_questions, bool) or total_questions <= 0:
            continue

        occurred_at = _parse_timestamptz(created_at_raw)
        if occurred_at is None:
            logger.warning(
                "get_user_topic_profile: skipping row with unparseable created_at "
                "(user_id=%s, attempt_id=%r)", user_id, event_id,
            )
            continue

        key = (subject, topic_key)
        group = groups.setdefault(key, {"topic": topic if isinstance(topic, str) else topic_key, "events": []})
        # Most recently seen row's topic text wins for display (rows are
        # fetched created_at ascending, so later iterations are more recent).
        if isinstance(topic, str) and topic:
            group["topic"] = topic
        group["events"].append(
            EvidenceEvent(
                score=score,
                total_questions=total_questions,
                occurred_at=occurred_at,
                event_id=str(event_id) if event_id is not None else "",
            )
        )

    results = []
    for (subject, topic_key), group in groups.items():
        status_dict = compute_topic_status(group["events"])
        results.append({
            "subject": subject,
            "topic": group["topic"],
            "topic_key": topic_key,
            **status_dict,
        })

    # Most recently practiced topics first - a reasonable default surface
    # order, not a claim about importance/weakness.
    results.sort(key=lambda r: r["last_attempt_at"] or "", reverse=True)
    return results

# ---------------------------------------------------------------------------
# Phase 5B/5C: chat-derived learning evidence + conversation index writes.
#
# Same security/write pattern as save_quiz_attempt above: the CALLING
# STUDENT'S OWN Supabase access token is forwarded, never a service-role
# key - RLS (see supabase/migrations/0003_learning_memory_foundation.sql)
# is the actual enforcement boundary.
#
# CALLER CONTRACT (enforced by backend/main.py, not re-validated here any
# more strictly than save_quiz_attempt re-validates its own callers):
#   - subject/topic/attribution_confidence must already have come from
#     backend.learning_memory.resolve_academic_context() returning Known
#     or Probable - this module has no opinion on attribution, it only
#     persists what it's given and enforces the DB-level CHECK constraints
#     via the same "known"/"probable" values.
#   - signal_type/signal_strength must be one of
#     backend.learning_memory's VALID_SIGNAL_TYPES/VALID_SIGNAL_STRENGTHS.
# ---------------------------------------------------------------------------

async def save_chat_learning_evidence(
    user_token: str,
    user_id: str,
    subject: str,
    signal_type: str,
    signal_strength: str,
    attribution_confidence: str,
    topic: Optional[str] = None,
    chat_id: str = "",
) -> None:
    """
    Persists one chat-derived learning_evidence row.

    ONLY ever called with Known/Probable attribution - see
    backend/learning_memory.py's CONTEXT RULE and backend/main.py's
    chat_endpoint integration. `attribution_confidence` MUST be exactly
    "known" or "probable" (see the migration's own CHECK constraint in
    supabase/migrations/0003_learning_memory_foundation.sql, which this
    validation mirrors); "unknown" is rejected here as a hard error, not
    silently written - an Unknown-attribution chat interaction should
    never reach this function at all (the caller in backend/main.py
    already gates on `context.confidence in ("known", "probable")` before
    scheduling this call), but this function does not trust that
    unconditionally either, matching the existing validation style for
    signal_type/signal_strength below.

    BUGFIX (attribution-confidence propagation): this parameter used to
    not exist at all - the row written was unconditionally stamped
    "known" regardless of what resolve_academic_context() actually
    determined, silently discarding a genuine "probable" (inherited-
    context) resolution. The caller in backend/main.py now forwards
    `context.confidence` through unchanged - see
    _background_persist_chat_evidence there.

    PHASE 5C ACTIVATION UPDATE: `topic` is optional (defaults to None) -
    subject-only evidence (topic=None) is a legitimate, common, honest
    outcome of resolve_academic_context's deterministic subject
    detection, not a malformed call. `subject` remains required: every
    Known/Probable ContextResolution always has a subject; only topic is
    ever absent.

    Best-effort from the caller's perspective: raises DatabaseError on
    failure like every other write in this module, but backend/main.py
    calls this from a FastAPI BackgroundTask specifically so a failure
    here can NEVER turn a successful chat response into an error - see
    the chat_endpoint docstring for the non-negotiable resilience rule.

    topic_key is computed HERE, server-side, via the same
    normalize_topic_key() Phase 5A already uses for quiz topics - never
    accepted from the caller, so chat and quiz topics aggregate under an
    identical normalization rule. topic_key is None whenever topic is
    None/blank.
    """
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        raise DatabaseError(
            "Supabase is not configured on the server (SUPABASE_URL/SUPABASE_ANON_KEY missing)."
        )
    if attribution_confidence not in (CONFIDENCE_KNOWN, CONFIDENCE_PROBABLE):
        # Deliberately a STRICTER check than VALID_ATTRIBUTION_CONFIDENCES
        # (which also permits "unknown", correctly, for conversation_index
        # below) - "unknown" is never a valid value for this table. This
        # is the enforcement point for requirement "Unknown context must
        # continue to produce no learning_evidence row", independent of
        # whatever the caller's own gating logic does.
        raise DatabaseError(f"invalid attribution_confidence for learning_evidence: {attribution_confidence!r}")
    if signal_type not in VALID_SIGNAL_TYPES:
        raise DatabaseError(f"invalid signal_type: {signal_type!r}")
    if signal_strength not in VALID_SIGNAL_STRENGTHS:
        raise DatabaseError(f"invalid signal_strength: {signal_strength!r}")
    if not subject or not subject.strip():
        # Defensive - callers should never reach here without a subject,
        # since resolve_academic_context() only returns Known/Probable
        # with a subject present, but a pure persistence function should
        # not trust that unconditionally either.
        raise DatabaseError("subject is required for known/probable chat evidence")

    has_topic = bool(topic and topic.strip())

    payload = {
        "user_id": user_id,
        "source": "chat",
        "subject": subject,
        "topic": topic.strip() if has_topic else None,
        "topic_key": normalize_topic_key(topic) if has_topic else None,
        "attribution_confidence": attribution_confidence,
        "signal_type": signal_type,
        "signal_strength": signal_strength,
        "chat_id": chat_id or None,
    }

    try:
        async with httpx.AsyncClient(timeout=DB_REQUEST_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{SUPABASE_URL}/rest/v1/learning_evidence",
                headers=_rest_headers(user_token),
                json=payload,
            )
    except httpx.RequestError:
        logger.exception(
            "save_chat_learning_evidence: network error inserting learning_evidence (user_id=%s)", user_id
        )
        raise DatabaseError("network error inserting learning_evidence")

    if resp.status_code not in (200, 201):
        logger.error(
            "save_chat_learning_evidence: insert failed status=%d body=%s (user_id=%s)",
            resp.status_code, resp.text[:500], user_id,
        )
        raise DatabaseError(f"learning_evidence insert failed with status {resp.status_code}")


async def save_conversation_index_entry(
    user_token: str,
    user_id: str,
    chat_id: str,
    short_label: str,
    subject: Optional[str] = None,
    attribution_confidence: str = "unknown",
) -> None:
    """
    Persists one lightweight conversation_index row - "a chat happened,
    roughly about this, at this time." See
    supabase/migrations/0003_learning_memory_foundation.sql for why this
    is intentionally allowed to happen more than once per chat_id over
    that chat's lifetime (each row is a truthful snapshot, not a mutable
    summary).

    short_label MUST come from an already-computed value (see
    backend/main.py:chat_title_endpoint, which reuses the existing
    generate_chat_title() Gemini call) - this function makes no AI call
    and has no knowledge of Gemini at all.

    Best-effort from the caller's perspective, same convention as
    save_chat_learning_evidence above - called from a FastAPI
    BackgroundTask so persistence failure never affects the chat title
    response already sent to the student.
    """
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        raise DatabaseError(
            "Supabase is not configured on the server (SUPABASE_URL/SUPABASE_ANON_KEY missing)."
        )
    if attribution_confidence not in VALID_ATTRIBUTION_CONFIDENCES:
        raise DatabaseError(f"invalid attribution_confidence: {attribution_confidence!r}")
    if not chat_id or not chat_id.strip():
        raise DatabaseError("chat_id is required for a conversation_index entry")
    if not short_label or not short_label.strip():
        raise DatabaseError("short_label is required for a conversation_index entry")

    payload = {
        "user_id": user_id,
        "chat_id": chat_id,
        "subject": subject if (subject and subject.strip()) else None,
        "attribution_confidence": attribution_confidence,
        "short_label": short_label.strip(),
    }

    try:
        async with httpx.AsyncClient(timeout=DB_REQUEST_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{SUPABASE_URL}/rest/v1/conversation_index",
                headers=_rest_headers(user_token),
                json=payload,
            )
    except httpx.RequestError:
        logger.exception(
            "save_conversation_index_entry: network error inserting conversation_index (user_id=%s)", user_id
        )
        raise DatabaseError("network error inserting conversation_index")

    if resp.status_code not in (200, 201):
        logger.error(
            "save_conversation_index_entry: insert failed status=%d body=%s (user_id=%s)",
            resp.status_code, resp.text[:500], user_id,
        )
        raise DatabaseError(f"conversation_index insert failed with status {resp.status_code}")


# ---------------------------------------------------------------------------
# Phase 5D: GET /api/learning/history read path.
# ---------------------------------------------------------------------------

_CONVERSATION_INDEX_SELECT = "chat_id,subject,attribution_confidence,short_label,occurred_at"
_LEARNING_EVIDENCE_SELECT = "subject,topic,topic_key,signal_type,signal_strength,occurred_at"

# Defensive ceiling independent of whatever the caller requests - mirrors
# the MAX_QUIZ_ANSWERS pattern in backend/main.py: generous for real use,
# just not literally unbounded.
MAX_LEARNING_HISTORY_LIMIT = 200


async def get_learning_history(
    user_token: str,
    user_id: str,
    since_iso: str,
    until_iso: str,
    limit: int = 100,
) -> dict:
    """
    Fetches this student's own learning-memory activity in [since_iso,
    until_iso), RLS-scoped exactly like get_user_topic_profile above -
    `user_id` is accepted for logging only, never used to build a query
    filter.

    Returns:
        {
            "conversations": [
                {"chat_id", "subject", "attribution_confidence",
                 "short_label", "occurred_at"}, ...
            ],  # from conversation_index, most recent first
            "evidence_notes": [
                {"topic", "topic_key", "notes": [str, ...]}, ...
            ],  # from learning_evidence, via
                # backend.learning_memory.build_conversation_notes -
                # explainable, non-numeric, never merged with quiz mastery
        }

    since_iso/until_iso are expected to already be validated, bounded ISO
    8601 UTC timestamps (see backend/main.py:learning_history_endpoint) -
    this function does not itself impose a maximum range width, only a
    maximum row count via `limit`.

    Raises DatabaseError on any failure, same convention as every other
    read/write in this module.
    """
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        raise DatabaseError(
            "Supabase is not configured on the server (SUPABASE_URL/SUPABASE_ANON_KEY missing)."
        )

    bounded_limit = max(1, min(limit, MAX_LEARNING_HISTORY_LIMIT))

    try:
        async with httpx.AsyncClient(timeout=DB_REQUEST_TIMEOUT_SECONDS) as client:
            conv_resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/conversation_index",
                headers=_rest_headers(user_token),
                params={
                    "select": _CONVERSATION_INDEX_SELECT,
                    "occurred_at": [f"gte.{since_iso}", f"lt.{until_iso}"],
                    "order": "occurred_at.desc",
                    "limit": str(bounded_limit),
                },
            )
            evidence_resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/learning_evidence",
                headers=_rest_headers(user_token),
                params={
                    "select": _LEARNING_EVIDENCE_SELECT,
                    "occurred_at": [f"gte.{since_iso}", f"lt.{until_iso}"],
                    "order": "occurred_at.desc",
                    "limit": str(bounded_limit),
                },
            )
    except httpx.RequestError:
        logger.exception(
            "get_learning_history: network error (user_id=%s)", user_id
        )
        raise DatabaseError("network error fetching learning history")

    if conv_resp.status_code != 200:
        logger.error(
            "get_learning_history: conversation_index select failed status=%d body=%s (user_id=%s)",
            conv_resp.status_code, conv_resp.text[:500], user_id,
        )
        raise DatabaseError(f"conversation_index select failed with status {conv_resp.status_code}")
    if evidence_resp.status_code != 200:
        logger.error(
            "get_learning_history: learning_evidence select failed status=%d body=%s (user_id=%s)",
            evidence_resp.status_code, evidence_resp.text[:500], user_id,
        )
        raise DatabaseError(f"learning_evidence select failed with status {evidence_resp.status_code}")

    try:
        conversations = conv_resp.json()
        evidence_rows = evidence_resp.json()
    except ValueError:
        raise DatabaseError("unexpected response shape from learning history select")

    if not isinstance(conversations, list) or not isinstance(evidence_rows, list):
        raise DatabaseError("unexpected response shape from learning history select")

    return {
        "conversations": [c for c in conversations if isinstance(c, dict)],
        "evidence_notes": build_conversation_notes(
            [e for e in evidence_rows if isinstance(e, dict)]
        ),
    }


# ---------------------------------------------------------------------------
# Phase 6A: student profile (name/class/stream) - read + upsert.
#
# PURELY IDENTITY/CONTEXT DATA. See
# supabase/migrations/0005_student_profile.sql for why this lives in its
# own table and is never merged with quiz_attempts/learning_evidence -
# this module must never be changed to have these two functions read from
# or write into those tables, or vice versa.
#
# Validation of name/user_class/stream happens in backend/main.py BEFORE
# either function below is called - exactly like every other write path
# in this module, this code trusts its caller for value validity and only
# guards against missing Supabase configuration and unexpected PostgREST
# responses.
# ---------------------------------------------------------------------------

_STUDENT_PROFILE_SELECT = "user_id,name,user_class,stream,created_at,updated_at"


async def get_student_profile(user_token: str, user_id: str) -> Optional[dict]:
    """
    Fetches the authenticated student's own profile.

    Returns None if the student has never created a profile yet -
    backend/main.py:get_profile_endpoint turns that into HTTP 404, which
    the frontend uses as the sole signal to show the (single, compact)
    onboarding form.

    SECURITY: forwards the student's OWN Supabase access token, exactly
    like get_user_topic_profile above - RLS (`auth.uid() = user_id`, see
    student_profiles_select_own in 0005_student_profile.sql) is what
    actually restricts the returned row to this student. `user_id` is
    accepted only for logging, matching the established pattern in this
    module - it is never used to build a query filter.

    Raises DatabaseError on any failure, same convention as every other
    read in this module.
    """
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        raise DatabaseError(
            "Supabase is not configured on the server (SUPABASE_URL/SUPABASE_ANON_KEY missing)."
        )

    try:
        async with httpx.AsyncClient(timeout=DB_REQUEST_TIMEOUT_SECONDS) as client:
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/student_profiles",
                headers=_rest_headers(user_token),
                params={"select": _STUDENT_PROFILE_SELECT, "limit": "1"},
            )
    except httpx.RequestError:
        logger.exception(
            "get_student_profile: network error fetching student_profiles (user_id=%s)", user_id
        )
        raise DatabaseError("network error fetching student_profiles")

    if resp.status_code != 200:
        logger.error(
            "get_student_profile: student_profiles select failed status=%d body=%s (user_id=%s)",
            resp.status_code, resp.text[:500], user_id,
        )
        raise DatabaseError(f"student_profiles select failed with status {resp.status_code}")

    try:
        rows = resp.json()
    except ValueError:
        logger.error(
            "get_student_profile: unexpected student_profiles response shape (user_id=%s): %r",
            user_id, resp.text[:500],
        )
        raise DatabaseError("unexpected response shape from student_profiles select")

    if not isinstance(rows, list):
        raise DatabaseError("unexpected response shape from student_profiles select")

    if not rows:
        return None

    return rows[0]


async def upsert_student_profile(
    user_token: str,
    user_id: str,
    name: str,
    user_class: str,
    stream: Optional[str],
) -> dict:
    """
    Creates the authenticated student's profile if none exists yet, or
    updates it in place if one already does - a single idempotent write,
    matching the "GET /api/profile, PUT /api/profile" shape (no separate
    create/update endpoints needed).

    `stream` is Optional: BUG 2 FIX (Phase 6A correction) - Class 6-8 has
    no Science/Commerce/Arts stream in the NCTB curriculum, so
    backend/main.py's _validate_profile_stream passes None for that case.
    This function stores whatever it is given (None -> SQL NULL via
    PostgREST) - it does not itself decide which classes may have a null
    stream, that decision was already made by the caller. See
    supabase/migrations/0006_student_profile_stream_optional.sql for the
    column-level change that made this possible.

    Uses PostgREST's upsert (`Prefer: resolution=merge-duplicates`, POST
    with `on_conflict=user_id`) against the table's primary key - this is
    what makes "one profile per authenticated user" hold even under a
    duplicate/racing submit, without this function needing to first SELECT
    to decide insert-vs-update itself.

    `updated_at` is deliberately NOT set here -
    supabase/migrations/0005_student_profile.sql's
    trg_student_profiles_updated_at trigger sets it from the database's
    own clock on every UPDATE (including the UPDATE half of this upsert),
    exactly like `created_at`'s DB-side default handles the INSERT case.
    This function never computes or trusts a client-adjacent timestamp for
    either column.

    SECURITY: forwards the student's OWN Supabase access token - RLS
    (`auth.uid() = user_id`, see student_profiles_insert_own /
    student_profiles_update_own) is what actually restricts this write to
    the caller's own row. A forged `user_id` could never satisfy those
    policies' `with check`.

    Raises DatabaseError on any failure, same convention as every other
    write in this module.
    """
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        raise DatabaseError(
            "Supabase is not configured on the server (SUPABASE_URL/SUPABASE_ANON_KEY missing)."
        )

    payload = {
        "user_id": user_id,
        "name": name,
        "user_class": user_class,
        "stream": stream,
    }

    try:
        async with httpx.AsyncClient(timeout=DB_REQUEST_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{SUPABASE_URL}/rest/v1/student_profiles",
                headers={
                    **_rest_headers(user_token),
                    "Prefer": "resolution=merge-duplicates,return=representation",
                },
                params={"on_conflict": "user_id"},
                json=payload,
            )
    except httpx.RequestError:
        logger.exception(
            "upsert_student_profile: network error upserting student_profiles (user_id=%s)", user_id
        )
        raise DatabaseError("network error upserting student_profiles")

    if resp.status_code not in (200, 201):
        logger.error(
            "upsert_student_profile: student_profiles upsert failed status=%d body=%s (user_id=%s)",
            resp.status_code, resp.text[:500], user_id,
        )
        raise DatabaseError(f"student_profiles upsert failed with status {resp.status_code}")

    try:
        rows = resp.json()
        return rows[0]
    except (ValueError, KeyError, IndexError, TypeError):
        logger.error(
            "upsert_student_profile: unexpected student_profiles response shape (user_id=%s): %r",
            user_id, resp.text[:500],
        )
        raise DatabaseError("unexpected response shape from student_profiles upsert")

# ---------------------------------------------------------------------------
# Phase 5E: GET /api/profile/insights read path.
#
# This is the ONLY place in the codebase that knows both "what a
# learning_evidence row looks like" AND "what a ChatEvidenceRecord looks
# like" - backend.insight_engine never sees a row, and never learns that
# its input came from PostgREST, exactly mirroring get_user_topic_profile/
# _build_topic_profile's relationship with backend.mastery_engine above.
# ---------------------------------------------------------------------------

_INSIGHT_EVIDENCE_SELECT = "id,subject,topic,topic_key,signal_type,attribution_confidence,occurred_at"

# Bounded by row COUNT, not a date range (unlike get_learning_history,
# which is a recall/date-picker feature) - Phase 5E insights should
# reflect a student's evidence regardless of exactly how long ago it
# happened, but must still never scan unlimited history (brief Section
# 21). 500 rows is generous for a beta-stage evidence volume while still
# being a real ceiling, not "unbounded in practice."
MAX_INSIGHT_EVIDENCE_ROWS = 500


async def get_learning_insights(user_token: str, user_id: str) -> list:
    """
    Fetches this student's own chat-derived learning_evidence (bounded by
    MAX_INSIGHT_EVIDENCE_ROWS, most recent first) and computes Phase 5E
    Student Intelligence insights via backend.insight_engine.compute_insights.

    SECURITY: identical pattern to get_user_topic_profile/get_learning_history -
    forwards the student's OWN Supabase access token; RLS
    (`auth.uid() = user_id` on learning_evidence, see
    supabase/migrations/0003_learning_memory_foundation.sql) is what
    actually restricts the returned rows to this student. `user_id` is
    accepted for logging only, never used to build a query filter.

    Raises DatabaseError on any failure, same convention as every other
    read/write in this module.
    """
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        raise DatabaseError(
            "Supabase is not configured on the server (SUPABASE_URL/SUPABASE_ANON_KEY missing)."
        )

    try:
        async with httpx.AsyncClient(timeout=DB_REQUEST_TIMEOUT_SECONDS) as client:
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/learning_evidence",
                headers=_rest_headers(user_token),
                params={
                    "select": _INSIGHT_EVIDENCE_SELECT,
                    "order": "occurred_at.desc",
                    "limit": str(MAX_INSIGHT_EVIDENCE_ROWS),
                },
            )
    except httpx.RequestError:
        logger.exception(
            "get_learning_insights: network error fetching learning_evidence (user_id=%s)", user_id
        )
        raise DatabaseError("network error fetching learning_evidence")

    if resp.status_code != 200:
        logger.error(
            "get_learning_insights: learning_evidence select failed status=%d body=%s (user_id=%s)",
            resp.status_code, resp.text[:500], user_id,
        )
        raise DatabaseError(f"learning_evidence select failed with status {resp.status_code}")

    try:
        rows = resp.json()
    except ValueError:
        logger.error(
            "get_learning_insights: unexpected learning_evidence response shape (user_id=%s): %r",
            user_id, resp.text[:500],
        )
        raise DatabaseError("unexpected response shape from learning_evidence select")

    if not isinstance(rows, list):
        raise DatabaseError("unexpected response shape from learning_evidence select")

    return _build_insights(rows, user_id=user_id)


def _build_insights(rows: list, user_id: str) -> list:
    """
    Translates raw learning_evidence rows into ChatEvidenceRecord
    instances and runs backend.insight_engine.compute_insights.

    Malformed individual rows are skipped (logged), matching
    _build_topic_profile's established resilience convention - one bad
    row must not hide a student's entire insight set.
    """
    records = []

    for row in rows:
        if not isinstance(row, dict):
            continue

        subject = row.get("subject")
        signal_type = row.get("signal_type")
        attribution_confidence = row.get("attribution_confidence")
        record_id = row.get("id")
        occurred_at_raw = row.get("occurred_at")

        if not isinstance(subject, str) or not subject.strip():
            continue
        if not isinstance(signal_type, str) or not signal_type:
            continue
        if not isinstance(attribution_confidence, str) or attribution_confidence not in (
            CONFIDENCE_KNOWN, CONFIDENCE_PROBABLE,
        ):
            # Includes the CONFIDENCE_UNKNOWN case defensively - no row in
            # learning_evidence should ever actually have this value (the
            # table's own CHECK constraint forbids it), but a translation
            # function should not trust that unconditionally either.
            continue

        occurred_at = _parse_timestamptz(occurred_at_raw)
        if occurred_at is None:
            logger.warning(
                "get_learning_insights: skipping row with unparseable occurred_at "
                "(user_id=%s, evidence_id=%r)", user_id, record_id,
            )
            continue

        records.append(
            ChatEvidenceRecord(
                record_id=str(record_id) if record_id is not None else "",
                subject=subject,
                topic=row.get("topic") if isinstance(row.get("topic"), str) else None,
                topic_key=row.get("topic_key") if isinstance(row.get("topic_key"), str) else None,
                signal_type=signal_type,
                attribution_confidence=attribution_confidence,
                occurred_at=occurred_at,
            )
        )

    insights = compute_insights(records)

    return [
        {
            "subject": insight.subject,
            "topic": insight.topic,
            "topic_key": insight.topic_key,
            "insight_type": insight.insight_type,
            "confidence": insight.confidence,
            "evidence_count": insight.evidence_count,
            "first_observed_at": insight.first_observed_at.isoformat(),
            "last_observed_at": insight.last_observed_at.isoformat(),
        }
        for insight in insights
    ]
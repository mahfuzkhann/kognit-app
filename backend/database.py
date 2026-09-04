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
import logging

import httpx

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


async def save_quiz_attempt(
    user_token: str,
    user_id: str,
    board: str,
    user_class: str,
    subject: str,
    topic: str,
    questions: list,
    selected_answers: list,
) -> dict:
    """
    Persists one completed quiz attempt plus its per-question answers.

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
        "topic": topic,
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
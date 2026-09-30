import os
import io
import json
import logging
import random
import re
import time
import uuid
from typing import Optional

import httpx
from PIL import Image
from google import genai
from google.genai import types
from google.genai import errors as genai_errors
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("kognit.ai_engine")

# ---------------------------------------------------------------------------
# SDK MIGRATION (google-generativeai -> google-genai), see investigation
# report for the full root-cause writeup. Short version:
#
# gemini-3.6-flash is a dynamic-thinking ("reasoning") model. By default it
# decides its own internal reasoning effort per request (Google's own model
# card documents this and explicitly warns of "occasional slowness or
# timeout issues"). The previously-pinned google-generativeai==0.8.3 SDK is
# the pre-Gemini-3 legacy client - its GenerationConfig protobuf has no
# thinking_config/thinking_level/thinking_budget field at all (confirmed by
# inspecting the installed protobuf schema directly, not assumed), so there
# was no way to bound this. That is why an "ordinary" image question could
# intermittently take ~29s and then fail: the model was genuinely still
# thinking when Kognit's own client-side timeout fired.
#
# google-genai (the current official SDK) exposes thinking_level, which
# Google's own documentation names as the explicit recommendation for
# "real-time chat" and other latency-critical interactive use cases - see
# CHAT_THINKING_LEVEL below.
# ---------------------------------------------------------------------------
_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# P0 STABILIZATION - model configuration.
#
# The model name used to be a hardcoded literal. It is now read from the
# GEMINI_MODEL environment variable (set it in .env - see .env.example) and
# falls back to DEFAULT_MODEL_NAME when the variable is absent, empty, or not
# a plausible model identifier. The DEFAULT is intentionally unchanged from
# the previous hardcoded value, so behavior is identical unless GEMINI_MODEL
# is set. This is configuration only: there is no model routing and no
# automatic fallback to a different model.
DEFAULT_MODEL_NAME = "gemini-3.6-flash"
MODEL_ENV_VAR = "GEMINI_MODEL"
_MODEL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")


def _resolve_model_name(env=None) -> str:
    """Return the model to use: env override if valid, else the default.

    `env` is any mapping with .get (defaults to os.environ) so this can be
    unit-tested without touching the real process environment. An invalid
    value (whitespace inside, odd characters, too long) is ignored with a
    warning rather than sent to the provider - a typo in .env must not turn
    every request into a provider 4xx.
    """
    source = os.environ if env is None else env
    configured = (source.get(MODEL_ENV_VAR) or "").strip()
    if not configured:
        return DEFAULT_MODEL_NAME
    if not _MODEL_NAME_PATTERN.match(configured):
        logger.warning(
            "%s is set but is not a valid model identifier - ignoring it and using the default model %s",
            MODEL_ENV_VAR, DEFAULT_MODEL_NAME,
        )
        return DEFAULT_MODEL_NAME
    return configured


MODEL_NAME = _resolve_model_name()
logger.info(
    "ai_engine model=%s (source=%s)",
    MODEL_NAME, MODEL_ENV_VAR if MODEL_NAME != DEFAULT_MODEL_NAME else "default",
)

# Phase 7B (evaluation subsystem) prompt-versioning support.
#
# Bumped by hand whenever CHAT_SYSTEM_INSTRUCTION_RULES below changes in a
# way that could affect answer behavior. The evaluation runner logs this
# alongside a SHA-256 hash of the constant itself (see
# evaluation/prompt_identity.py) - the hash is what actually proves the
# prompt content, since this label only helps if someone remembers to bump
# it. Neither of these has any effect on production behavior; main.py never
# reads this constant.
CHAT_PROMPT_VERSION = "2026-09-20-phase9c-takeaway"

# Extracted, byte-identical, from generate_ai_response()'s previously
# inline system_instruction f-string (Phase 7B production-metadata-capture
# work). This is the STATIC part of the chat system instruction - the part
# that never varies per request. The per-request academic_clause (board/
# class/stream), PDF-context block, and Socratic-mode suffix remain built
# dynamically inside generate_ai_response(), exactly as before.
#
# This exists so the evaluation subsystem (evaluation/prompt_identity.py)
# can import and hash the actual production prompt content directly,
# rather than duplicating this text inside evaluation/ - hashing the full
# per-request system_instruction would produce a different hash for every
# single request (since it embeds student-specific class/stream/PDF text),
# which would not be a meaningful "prompt version" signal at all.
CHAT_SYSTEM_INSTRUCTION_RULES = (
    "STRICT ACADEMIC & VISION RULES:\n"
    "1. IMAGE ANALYSIS: If an image is provided, carefully read handwritten questions, printed equations, or diagrams. Solve step-by-step.\n"
    "2. PDF CONTEXT: If a PDF document text context is provided below, prioritize answering questions based on that document content.\n"
    "3. HYPER-LOCAL CQ FORMAT: When answering Creative Questions (সৃজনশীল) or solutions, strictly format using (ক) জ্ঞানমূলক, (খ) অনুধাবনমূলক, (গ) প্রয়োগমূলক, and (ঘ) উচ্চতর দক্ষতার standard exam rules.\n"
    "4. FORMULA NOTATION: Wrap inline math in $ ... $ and main equations in $$ ... $$. "
    "CRITICAL: Only pure mathematical notation belongs inside $ ... $ or $$ ... $$ - "
    "variables, numbers, operators, and standard math symbols (e.g. FV, PV, i, n, +, =, /). "
    "NEVER put Bangla or English words, labels, or explanations inside math delimiters - "
    "this breaks Bangla text rendering. Write all Bangla/English labels, explanations, "
    "and descriptions as normal Markdown text OUTSIDE the $ ... $ / $$ ... $$ delimiters.\n"
    "5. Tone must be encouraging, clear, precise, and aligned with the student's curriculum.\n"
    "6. LANGUAGE: Students write in Bangla, English, Banglish (Bangla typed in Latin "
    "script), or a natural mix of these, sometimes with typos or informal phrasing. "
    "Understand the question as intended without asking the student to rephrase it in a "
    "'proper' language first. Respond primarily in whichever language the student's "
    "message is dominantly in - if they write mostly Banglish or Bangla, reply in natural "
    "Bangla; if they write mostly English, reply in English. Keep standard English "
    "technical/subject terms (e.g. 'gross profit ratio', 'acceleration') as-is even inside "
    "a Bangla reply where that is how the term is normally taught, rather than forcing an "
    "awkward translation. If the student explicitly asks for a specific language, use it.\n"
    "7. HANDLING UNCLEAR QUESTIONS: If a question is short, informal, or loosely phrased "
    "but its academic intent is reasonably clear from context (subject, board, class, "
    "prior chat history, or an attached PDF/image), answer it directly using the most "
    "reasonable interpretation - do not refuse or ask for clarification merely because the "
    "phrasing is casual, mixed-language, or contains minor typos. Only ask ONE short, "
    "specific clarifying question when the request is genuinely ambiguous in a way that "
    "would change the answer (e.g. it's unclear which chapter, which of two problems, or "
    "which subject is meant). Never invent facts, textbook page numbers, or details you are "
    "not given in order to avoid asking that clarifying question.\n"
    "8. KEY TAKEAWAY (optional): for a genuinely conceptual, explanatory, or "
    "multi-step answer, you MAY end your response with one additional block, "
    "in exactly this format, after your normal answer:\n"
    "<!--KOGNIT_TAKEAWAY\n"
    "One or two sentences synthesizing the core idea a student should remember.\n"
    "KOGNIT_TAKEAWAY-->\n"
    "Rules for this block: (a) OMIT it entirely for simple factual answers, "
    "short definitions, or anything where a summary would just repeat what "
    "was already said - most answers should have NO takeaway block; "
    "(b) the takeaway must state ONLY something already established in your "
    "answer above it - never introduce a new fact, number, or claim that "
    "does not appear in the answer; (c) keep it to 1-2 sentences, in the "
    "same language as the rest of your answer; (d) use the exact delimiter "
    "text shown above, with nothing else on those two delimiter lines; "
    "(e) this block is invisible to the student as raw text - it is "
    "extracted and shown separately - so do not reference it or refer to "
    "it as a \"box\" or \"note\" from within your main answer."
)

# User-facing fallback messages. Never expose str(exception) to the client -
# that can leak internal details (stack traces, provider error text, etc).
GENERIC_CHAT_ERROR = (
    "Sorry, Kognit couldn't process that request right now. "
    "Please try again in a moment."
)

# Shown specifically when the Gemini API rejects a request due to quota
# exhaustion (google.genai.errors.ClientError, HTTP 429 / RESOURCE_EXHAUSTED).
# Never interpolate str(exception) into this - the raw error contains
# provider-internal details (quota metric names, limits, status codes)
# that must not reach the client. See GENERIC_CHAT_ERROR comment above.
QUOTA_EXHAUSTED_ERROR = (
    "⚠️ Kognit-এর AI ব্যবহারের সীমা এই মুহূর্তে পূর্ণ হয়ে গেছে।\n"
    "এই মুহূর্তে আপনার প্রশ্নের উত্তর তৈরি করা যাচ্ছে না। কিছুক্ষণ পরে আবার চেষ্টা করুন।"
)

# Shown when Gemini returns a response with no usable content - almost
# always because its safety filters blocked the prompt or the generated
# candidate (finish_reason != STOP). In google-genai, response.text simply
# returns None in this case (it does NOT raise, unlike the old SDK - see
# the ValueError handling this replaces, below). This is NOT a network/
# provider failure, so retrying would not help - kept as a distinct,
# non-retried message so it's diagnosable from a student's report without
# needing server log access.
BLOCKED_RESPONSE_ERROR = (
    "Kognit couldn't generate a response for this specific question - it may "
    "have been blocked by a content safety filter. Please try rephrasing your question."
)

# Shown when the uploaded image itself can't be decoded (corrupted file,
# truncated upload, or a format PIL can't read despite passing the size
# check in main.py, which does not validate image content). Distinct from
# GENERIC_CHAT_ERROR so this failure mode is diagnosable in student reports
# without needing server log access - "the photo didn't load" vs "the AI
# didn't respond" are different problems with different fixes.
IMAGE_DECODE_ERROR = (
    "Kognit couldn't read the image you attached - it may be corrupted or in an "
    "unsupported format. Please try uploading it again or use a different photo."
)

# P0 STABILIZATION - stable machine-readable error codes.
#
# Carried on the "error" and "interrupted" stream events (field "code") and in
# every stream_telemetry line, so a provider 503 no longer looks identical to
# every other failure. These are internal labels - they never contain raw
# provider text, keys, stack traces or student content. The student-facing
# message is a separate, safe string (see the *_ERROR constants above).
ERROR_PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"        # 5xx / 409 after bounded retries
ERROR_PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"      # 429
ERROR_PROVIDER_SAFETY = "PROVIDER_SAFETY"                  # blocked by a safety-type finish reason
ERROR_PROVIDER_MAX_TOKENS = "PROVIDER_MAX_TOKENS"          # ran out of output budget
ERROR_PROVIDER_EMPTY_RESPONSE = "PROVIDER_EMPTY_RESPONSE"  # no text and no recognised reason
ERROR_PROVIDER_INVALID_REQUEST = "PROVIDER_INVALID_REQUEST"  # non-retryable 4xx (e.g. 400/404)
ERROR_PROVIDER_AUTH_FAILED = "PROVIDER_AUTH_FAILED"        # 401/403 - bad/blocked API key
ERROR_PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"                # our client deadline / connect failure
ERROR_REQUEST_INVALID = "REQUEST_INVALID"                  # request could not be built (bad image)
ERROR_STREAM_INTERRUPTED = "STREAM_INTERRUPTED"            # cut off after partial output
ERROR_INTERNAL = "INTERNAL_ERROR"                          # anything unexpected

# Shown when the provider is temporarily unavailable/overloaded (5xx) and the
# bounded retries are exhausted. Distinct from GENERIC_CHAT_ERROR so a
# student knows this is temporary and worth retrying shortly; never contains
# provider text.
PROVIDER_UNAVAILABLE_ERROR = (
    "Kognit's AI service is very busy right now. Please try again in a minute.\n"
    "Kognit-এর AI সার্ভিসে এই মুহূর্তে অনেক চাপ আছে। এক মিনিট পর আবার চেষ্টা করুন।"
)


def new_request_id() -> str:
    """One short, opaque, non-guessable id per chat request (12 hex chars)."""
    return uuid.uuid4().hex[:12]


_SAFETY_FINISH_REASON_NAMES = frozenset({
    "SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII",
    "IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT", "IMAGE_RECITATION",
})


def _finish_reason_name(finish_reason) -> Optional[str]:
    if finish_reason is None:
        return None
    name = getattr(finish_reason, "name", None)
    return str(name if name else finish_reason).split(".")[-1].upper()


def _error_code_for_finish_reason(finish_reason, default: str) -> str:
    """Map a provider finish_reason to a stable internal error code."""
    name = _finish_reason_name(finish_reason)
    if name == "MAX_TOKENS":
        return ERROR_PROVIDER_MAX_TOKENS
    if name in _SAFETY_FINISH_REASON_NAMES:
        return ERROR_PROVIDER_SAFETY
    return default


# ---------------------------------------------------------------------------
# PDF CONTEXT BUDGET (Phase 1 fix: confirmed truncation problem).
#
# Previously a bare `pdf_context[:10000]` slice with the comment "Truncate if
# too long for safety" - a leftover from the pre-migration google-generativeai
# code, with no connection to what gemini-3.6-flash actually supports. Per
# Google's published model card (ai.google.dev/gemini-api/docs/models/
# gemini-3.6-flash, confirmed July 2026), gemini-3.6-flash has a 1,048,576-
# token INPUT context window and up to 65,536 output tokens - the old 10,000-
# char (~2,500-token) ceiling was using roughly 0.25% of the model's real
# input budget, truncating mid-document on almost any real textbook chapter.
#
# MAX_PDF_CONTEXT_CHARS raises this to a size that comfortably covers a
# realistic student upload (a chapter/worksheet, not an entire textbook)
# while staying far inside the model's real limit, leaving headroom for:
#   - the system instruction itself (a few thousand tokens)
#   - conversation history (main.py caps this at MAX_HISTORY_MESSAGES *
#     MAX_HISTORY_MESSAGE_CHARS = 20 * 4000 = 80,000 chars worst case)
#   - the model's own output budget (up to 65,536 tokens)
# even under a deliberately conservative ~1-token-per-character estimate for
# dense Bangla script (Bangla tokenizes far less efficiently than English's
# ~4 chars/token, so this does not assume English-like headroom).
#
# This is still a fixed-budget truncation, NOT retrieval - it does not
# chunk, rank, or select the most relevant section; long documents past this
# limit still lose their tail. True retrieval (chunking + embeddings so the
# RIGHT section is included regardless of document length) is separate,
# larger-scope work and is intentionally NOT implemented here.
MAX_PDF_CONTEXT_CHARS = 300_000

# ---------------------------------------------------------------------------
# THINKING LEVEL CONFIGURATION
#
# gemini-3.6-flash defaults to dynamic ("medium") thinking if left
# unconfigured. Kognit's primary chat path is an interactive, turn-based
# student conversation - Google's own guidance names thinking_level="low"
# as the explicit recommendation for exactly this use case ("real-time
# chat"), trading some reasoning depth for materially lower and more
# predictable latency.
#
# This is intentionally a single named constant, not a per-request
# complexity classifier - building automatic difficulty detection now would
# be premature (we do not yet have measured latency/quality data to justify
# it). If measured data later shows "low" is hurting answer quality on
# multi-step academic problems, this is the one place to change - swap the
# value (LOW / MEDIUM / HIGH) and nothing else in the integration needs to
# change.
#
# Kept as three separate named constants (chat / title / quiz) rather than
# one shared constant because they are different product surfaces with
# different quality-vs-latency tradeoffs:
#   - CHAT_THINKING_LEVEL:  the actual student-facing answer. Latency-
#     critical (this is the bug being fixed). Set to LOW.
#   - TITLE_THINKING_LEVEL: a trivial side task (see generate_chat_title
#     docstring) that should always be fast. Set to LOW.
#   - QUIZ_THINKING_LEVEL:  left at the model default (None = do not send
#     thinking_config at all) for now. Quiz generation is not a "the
#     student is staring at a spinner" moment in the same way chat is, and
#     MCQ/answer-key correctness benefits more from reasoning than it costs
#     in perceived latency. Not tuned yet - revisit once we have real
#     quiz-generation latency numbers.
# ---------------------------------------------------------------------------
CHAT_THINKING_LEVEL = types.ThinkingLevel.LOW
TITLE_THINKING_LEVEL = types.ThinkingLevel.LOW
QUIZ_THINKING_LEVEL = None

# How long to wait on a single Gemini call before giving up. HttpOptions.timeout
# is documented in milliseconds by the SDK, so this is converted with
# _seconds_to_ms() everywhere it's used below - keep the constant itself in
# seconds for readability/consistency with the rest of this file.
AI_REQUEST_TIMEOUT_SECONDS = 30

# RETRY POLICY REDESIGN.
#
# The previous policy gave attempt 1 a full 30s budget and attempt 2 only
# 15s, and retried on DeadlineExceeded (i.e. our OWN client-side timeout
# firing) as if it were a generic transient blip. The incident that
# triggered this investigation shows exactly why that is wrong: attempt 1
# took 29.4s (Gemini was still "thinking", not stuck), got killed by our own
# deadline, and attempt 2 - with a SHORTER budget - failed the same way
# almost immediately after, for a total ~46.7s wait before the student saw
# an error. Retrying with a shorter timeout after a real-work-in-progress
# timeout is not "failing faster and more gracefully", it is "guaranteeing
# the retry fails too, but only after making the student wait more".
#
# The redesigned policy explicitly distinguishes WHY a call failed and only
# retries the failure types where a second attempt is actually likely to
# help:
#
#   A. Client/request deadline exceeded (httpx.TimeoutException /
#      httpx.ConnectError - i.e. OUR OWN configured timeout fired, or we
#      could not even connect): NOT retried by default. With
#      CHAT_THINKING_LEVEL=LOW, hitting this ceiling should now be rare -
#      when it happens, the model was doing real (if slow) work, and
#      immediately repeating the same expensive call is not proven to help.
#      RETRY_ON_CLIENT_TIMEOUT below is the single toggle to revisit this
#      once we have real elapsed= timing data post-fix.
#
#   B. Genuine transient provider failures - google.genai.errors.ServerError
#      (any 5xx: 500/502/503/504) or ClientError with an HTTP 409 ("Aborted"
#      - a genuinely transient conflict per Google's own error semantics,
#      not a client mistake): these return from Google FAST (an error
#      response, not a slow success), so retrying with a full timeout budget
#      does not meaningfully raise the worst case the way shortening did.
#      Retried up to twice (3 total attempts) - see MAX_ATTEMPTS_BUCKET_B
#      below for why this was raised from a single retry.
#
#   C. Quota exhaustion - ClientError with HTTP 429 (RESOURCE_EXHAUSTED):
#      never retried, quota will not clear in a few seconds.
#
#   D. Blocked/empty response (response.text is falsy despite a successful
#      call - almost always a safety-filter block): never retried, an
#      identical request would just get blocked again.
#
#   E. Anything else (programming errors, unexpected SDK/response shapes):
#      never retried, logged, generic error returned.
#
# Bucket A (client-side timeout) and bucket B (transient provider 5xx/409)
# are given DIFFERENT attempt ceilings below - they used to share one
# MAX_ATTEMPTS constant, which meant raising bucket B's retry budget would
# have silently also let a stalled client-side call retry more times (and
# wait much longer) than intended. They are now independent:
#
#   MAX_ATTEMPTS_BUCKET_A = 2  (1 initial + 1 retry - UNCHANGED)
#   MAX_ATTEMPTS_BUCKET_B = 3  (1 initial + 2 retries - RAISED, see below)
#
# MAX_ATTEMPTS_BUCKET_A: unchanged from the original redesign. A stalled/
# unreachable client-side call is a different failure mode than a fast 5xx
# response from Google, and letting it retry indefinitely would let worst-
# case wait time keep compounding (each retry re-spends the full 30s
# budget) - one retry stays the deliberate ceiling here.
MAX_ATTEMPTS_BUCKET_A = 2

# MAX_ATTEMPTS_BUCKET_B: RAISED from 2 to 3 (Phase 1 benchmark evidence,
# BM-04/BM-08/BM-12). All three hit a genuine Gemini 5xx on attempt 1
# (503 Service Unavailable / 503 / 503), got one retry per the previous
# policy, and the retry ALSO failed (503, 503, 504 Gateway Timeout
# respectively) - i.e. a single retry recovered 0 of these 3 real
# incidents. Across the full 15-entry run, 6/15 calls hit a bucket-B 5xx on
# attempt 1 at all; of those, 3/6 recovered on the single retry the old
# policy allowed and 3/6 did not. That is real evidence a second retry is
# worth attempting before giving up, not a change made on assumption.
# Bucket B responses return from Google fast (an error, not a slow
# success - see RETRY_REQUEST_TIMEOUT_SECONDS comment below), so a second
# retry does not meaningfully change the worst-case wait the way adding a
# bucket-A retry would; observed worst case in the benchmark for a
# recovered bucket-B call was ~58s total (1 failed attempt + 1 failed retry
# + 1 successful retry, each carrying its own ~1-2s jittered delay).
# Revisit if a future benchmark run shows 3 consecutive 5xx responses on
# the same request occurring often - that would point to a real Gemini
# outage window rather than isolated transient errors, which a bounded
# retry count cannot fix.
MAX_ATTEMPTS_BUCKET_B = 3

# Kept as the overall per-request loop bound (must be >= the largest of the
# two bucket-specific ceilings above so bucket B can actually use its full
# budget). Each bucket enforces ITS OWN ceiling independently via the
# MAX_ATTEMPTS_BUCKET_A / MAX_ATTEMPTS_BUCKET_B checks in the except
# blocks below - this is not, by itself, a per-bucket attempt cap.
MAX_ATTEMPTS = max(MAX_ATTEMPTS_BUCKET_A, MAX_ATTEMPTS_BUCKET_B)

# Bucket B gets the SAME timeout budget on the retry as attempt 1 - not a
# shorter one. This is deliberate: a 5xx/409 response comes back quickly (it
# is an error, not a slow success), so giving the retry a full budget does
# not meaningfully increase worst-case wait time versus a short one, and it
# gives the retry an honest chance instead of one that is set up to fail.
RETRY_REQUEST_TIMEOUT_SECONDS = AI_REQUEST_TIMEOUT_SECONDS

# Kept as an explicit, named, OFF-by-default switch rather than silently
# never retrying bucket A. If real post-fix latency data shows client
# timeouts are still common enough to be worth one bounded retry, flip this
# - do not change the retry loop's structure to do it.
#
# FLIPPED TO TRUE (Phase 1 benchmark, BM-01): a real run hit
# httpx.ReadTimeout / "client deadline exceeded" on attempt 1/2 at
# elapsed=30.199s for a simple SSC quadratic-equation question (2x^2-5x-3=0)
# with CHAT_THINKING_LEVEL=LOW. A trivial question should not genuinely need
# 30s of model "thinking" at the low setting, so this reads as a transient
# stall (network/connection read timeout, not sustained model work) rather
# than a case where retrying is guaranteed to fail again the same way -
# exactly the condition this toggle exists for. Without a retry, that
# request was a hard, unrecoverable failure visible to the student (see
# screenshot: generic "couldn't process that request" with no fallback).
# The retry reuses the SAME 30s budget (RETRY_REQUEST_TIMEOUT_SECONDS), so
# worst case a student now waits up to ~60s before seeing an error instead
# of ~30s - traded deliberately for a real chance of success on what looks
# like a one-off stall. Revisit if further benchmark runs show this firing
# repeatedly (that would point to a different root cause than a transient
# stall).
RETRY_ON_CLIENT_TIMEOUT = True

# HTTP status codes, other than 5xx, that are treated as bucket B (genuine
# transient failures) rather than bucket E (unexpected/programming errors).
# 409 = Aborted/conflict, which Google's own client libraries have
# historically classified as retryable.
TRANSIENT_RETRYABLE_CLIENT_HTTP_CODES = (409,)

# Jittered retry delay (bucket B only) - a fixed delay means that if Gemini
# has a real transient outage affecting several concurrent Kognit requests
# at once, every one of them retries at exactly the same moment (a small
# thundering-herd risk at even modest concurrency). Spreads actual retries
# uniformly across [base - jitter, base + jitter] = [1.0s, 2.0s].
RETRY_DELAY_BASE_SECONDS = 1.5
RETRY_DELAY_JITTER_SECONDS = 0.5

# ---------------------------------------------------------------------------
# P0 STABILIZATION - bounded retry backoff for the STREAMING path.
#
# Problem (audit, confirmed by simulation): stream_ai_response retried a
# provider 5xx immediately - 3 provider calls in ~0.02s - so its whole retry
# budget was spent inside a single capacity spike. The non-streaming path
# already slept between attempts; the streaming path did not.
#
# Delay before the Nth retry (N = 1 for the first retry):
#     base = min(RETRY_DELAY_BASE_SECONDS * RETRY_BACKOFF_MULTIPLIER**(N-1),
#                RETRY_BACKOFF_MAX_SECONDS)
#     delay = uniform(base - jitter, base + jitter), then capped again at
#             RETRY_BACKOFF_MAX_SECONDS
# With the defaults (1.5s base, x2, 0.5s jitter, 8s cap) and 3 attempts that
# is ~[1.0-2.0]s before retry 1 and ~[2.5-3.5]s before retry 2, i.e. about
# 3.5-5.5s of added wait in the worst case. This changes ONLY how long we
# wait - never how many attempts are made (MAX_ATTEMPTS_BUCKET_A/B and the
# single mid-stream recovery are unchanged).
# ---------------------------------------------------------------------------
RETRY_BACKOFF_MULTIPLIER = 2.0
RETRY_BACKOFF_MAX_SECONDS = 8.0


def _compute_retry_backoff(retry_number: int, rand=random.uniform) -> float:
    """Seconds to wait before retry number `retry_number` (1-based).

    Pure apart from `rand`, which tests inject to make the result exact.
    """
    n = max(1, int(retry_number))
    base = min(
        RETRY_DELAY_BASE_SECONDS * (RETRY_BACKOFF_MULTIPLIER ** (n - 1)),
        RETRY_BACKOFF_MAX_SECONDS,
    )
    jitter = min(RETRY_DELAY_JITTER_SECONDS, base)
    delay = rand(base - jitter, base + jitter)
    return max(0.0, min(delay, RETRY_BACKOFF_MAX_SECONDS))


def _sleep(seconds: float) -> None:
    """Single indirection over time.sleep. Looked up at call time so tests
    can replace this module attribute (or patch time.sleep) - no test ever
    has to wait for a real backoff."""
    time.sleep(seconds)


def _backoff_before_retry(retry_number: int, request_id: str, reason: str) -> float:
    """Log and wait out the backoff for one retry. Returns the delay used."""
    delay = _compute_retry_backoff(retry_number)
    logger.info(
        "request_id=%s retry backoff retry=%d delay=%.2fs reason=%s",
        request_id, retry_number, delay, reason,
    )
    _sleep(delay)
    return delay


# ---------------------------------------------------------------------------
# SINGLE RETRY AUTHORITY.
#
# google-genai's own HTTP layer will ONLY retry a request if HttpOptions.
# retry_options is set (confirmed by reading the installed SDK's
# _api_client.py: retry_args(None) returns tenacity.stop_after_attempt(1),
# i.e. exactly one HTTP attempt, whenever retry_options is left unset). This
# file never sets retry_options anywhere - neither on the module-level
# Client nor on any per-call GenerateContentConfig.http_options - so every
# call below makes exactly one HTTP attempt, and the retry loops in this
# file (bucket B above) are the ONLY retry authority. There is no
# SDK-level retry to accidentally stack underneath them.
# ---------------------------------------------------------------------------

# Kognit's internal message roles -> the Gemini SDK's expected chat-history
# roles. Gemini's chats.create(history=...) requires "user"/"model"; Kognit
# stores assistant turns as "bot". This mapping must stay in sync with
# whatever role strings the frontend sends in the "history" field.
_KOGNIT_ROLE_TO_GEMINI_ROLE = {"user": "user", "bot": "model"}


def _seconds_to_ms(seconds: float) -> int:
    """google-genai's HttpOptions.timeout is documented in milliseconds."""
    return int(seconds * 1000)


def _build_gemini_history(history: list) -> list:
    """
    Convert Kognit's validated [{"role": "user"|"bot", "text": str}, ...]
    history into the google-genai SDK's expected
    [{"role": "user"|"model", "parts": [{"text": str}]}, ...] shape (a plain
    dict list - google-genai validates this against its Content/Part models
    itself, no need to construct typed objects here).

    Any entry with an unrecognized role is skipped defensively (should not
    happen if main.py's validation ran first, but this function does not
    assume that and re-checks independently).
    """
    if not history:
        return []

    gemini_history = []
    for entry in history:
        role = entry.get("role")
        text = entry.get("text")
        gemini_role = _KOGNIT_ROLE_TO_GEMINI_ROLE.get(role)
        if gemini_role is None or not text:
            continue
        gemini_history.append({"role": gemini_role, "parts": [{"text": text}]})
    return gemini_history


def _log_usage(usage, attempt: int, elapsed: float, mode: str, image: bool, pdf: bool) -> None:
    """
    Best-effort observability log for a successful call. usage_metadata's
    thoughts_token_count in particular is the direct, measurable signal for
    the root cause this migration addresses - how many tokens the model
    spent "thinking" versus answering - so future latency investigations
    have real data instead of a single incident's log lines.
    """
    prompt_tokens = getattr(usage, "prompt_token_count", None) if usage else None
    thought_tokens = getattr(usage, "thoughts_token_count", None) if usage else None
    output_tokens = getattr(usage, "candidates_token_count", None) if usage else None
    total_tokens = getattr(usage, "total_token_count", None) if usage else None
    logger.info(
        "generate_ai_response success attempt=%d/%d model=%s thinking_level=%s elapsed=%.3fs "
        "prompt_tokens=%s thought_tokens=%s output_tokens=%s total_tokens=%s "
        "(mode=%s, has_image=%s, has_pdf=%s)",
        attempt, MAX_ATTEMPTS, MODEL_NAME, CHAT_THINKING_LEVEL.value, elapsed,
        prompt_tokens, thought_tokens, output_tokens, total_tokens,
        mode, image, pdf,
    )


def _is_successful_finish(finish_reason) -> bool:
    """
    BUG 3 FIX - the explicit completion rule.

    A streamed answer is only a genuine success if:
      - the SDK's send_message_stream() iterator completed with NO
        exception, AND
      - the last finish_reason observed across all chunks is either
        absent (None) or exactly types.FinishReason.STOP.

    Any other observed value (MAX_TOKENS, SAFETY, RECITATION, LANGUAGE,
    OTHER, BLOCKLIST, PROHIBITED_CONTENT, SPII, etc. - the full enum was
    read directly off the installed google-genai==2.20.0 package, not
    guessed) is explicitly non-success.

    On `finish_reason is None`: verified directly against the installed
    SDK's own chats.py (send_message_stream's internal history-recording
    logic keeps the same "most recent non-None finish_reason across
    chunks" pattern used here, and separately treats an all-None
    finish_reason as evidence a turn may not be fully valid). Because this
    sandbox cannot reach the live Gemini API, it was NOT possible to
    verify whether gemini-3.6-flash's typical successful stream reliably
    emits finish_reason=STOP on its final chunk, or sometimes completes
    with it still unpopulated. Treating a never-observed finish_reason as
    success (rather than as interrupted) is the deliberately conservative
    choice here: it introduces ZERO regression versus the pre-fix
    behavior (which never checked finish_reason at all), whereas the
    opposite choice risks mislabeling ordinary successful answers as
    interrupted if this SDK/model combination doesn't always populate it.
    REQUIRES LIVE GEMINI VALIDATION - see the Bug 3 report.
    """
    return finish_reason is None or finish_reason == types.FinishReason.STOP


def _log_stream_telemetry(
    outcome: str,
    mode: str,
    attempt: int,
    ttft_seconds,
    chunk_count: int,
    last_chunk_elapsed,
    finish_reason,
    exception_type,
    http_status,
    total_duration: float,
    explicit_completion_received: bool,
    recovery_used: bool = False,
    request_id: Optional[str] = None,
    error_code: Optional[str] = None,
) -> None:
    """
    BUG 3 INSTRUMENTATION - exactly one structured summary line per
    streaming request, emitted at the terminal point inside
    stream_ai_response() (success, interrupted, or error). This is the
    root-cause signal the investigation found completely missing before
    this fix: finish_reason and exception_type were never captured
    anywhere, so a MAX_TOKENS truncation and a genuine STOP were
    indistinguishable from the logs, and a 503 vs. a plain RuntimeError
    were both just "failed".

    `outcome` is always one of a small fixed set of internal labels (see
    call sites in stream_ai_response) - never raw provider error text.
    BUG 3 PHASE 2 added one new outcome, "recovery_triggered", emitted the
    moment a bounded recovery generation is granted (separate from the
    eventual final "success"/"interrupted"/"error" line for the same
    request, which also carries recovery_used=True so the two lines can be
    correlated by attempt/mode/timing).

    `recovery_used` (Phase 2): whether this request had already triggered
    its one bounded recovery generation by the time this line was logged -
    lets a grep for the FINAL outcome answer "did recovery run, and did it
    help" in one field, satisfying the Phase 2 telemetry requirement
    without a second parallel logging path.

    NEVER pass prompt text, answer text, or any other student content
    here - only the fields below.
    """
    logger.info(
        "stream_telemetry outcome=%s mode=%s attempt=%d/%d model=%s ttft=%s "
        "chunk_count=%d last_chunk_at=%s finish_reason=%s exception_type=%s "
        "http_status=%s total_duration=%.3fs explicit_completion_received=%s "
        "recovery_used=%s error_code=%s request_id=%s",
        outcome, mode, attempt, MAX_ATTEMPTS, MODEL_NAME,
        ("%.3fs" % ttft_seconds) if ttft_seconds is not None else None,
        chunk_count,
        ("%.3fs" % last_chunk_elapsed) if last_chunk_elapsed is not None else None,
        finish_reason, exception_type, http_status,
        total_duration, explicit_completion_received, recovery_used,
        error_code, request_id,
    )


from dataclasses import dataclass


@dataclass
class AIGenerationResult:
    """Returned by generate_ai_response() only when return_metadata=True
    (Phase 7B evaluation subsystem; also used by Phase 7C research
    integration in backend/main.py). Never constructed or consumed by
    any caller using the default return_metadata=False, which always
    gets a plain str, unchanged."""
    text: str
    is_error: bool
    usage_metadata: Optional[object] = None
    resolved_model_version: Optional[str] = None
    response_id: Optional[str] = None
    elapsed_seconds: Optional[float] = None
    grounding_metadata: Optional[object] = None
    # Phase 7C: the raw google.genai.types.GroundingMetadata from the
    # response, when enable_research=True. Always None when
    # enable_research=False (the default) or on any error path -
    # normalization into Kognit's provider-neutral ResearchResult
    # happens in backend/research_models.py, never here; this function
    # never interprets grounding content, only passes through what the
    # SDK returned.



# ---------------------------------------------------------------------------
# PHASE 8E: SHARED GENERATION CORE
# ---------------------------------------------------------------------------
# generate_ai_response() (non-streaming) and stream_ai_response() (streaming)
# must produce IDENTICAL prompts, system instructions, image handling, PDF
# context truncation and history. Duplicating any of that would guarantee the
# two paths drift apart - a streamed answer would silently stop matching the
# non-streamed one, which is exactly the class of bug that is invisible until
# a student reports it.
#
# So the prompt-building body was EXTRACTED verbatim from generate_ai_response
# into _build_chat_request() below. It was not rewritten: the only change is
# that the image-decode failure path raises _ChatRequestBuildError instead of
# returning a wrapped error string, because the two adapters need to translate
# that failure into their own response shapes.


class _ChatRequestBuildError(Exception):
    """Raised by _build_chat_request when the request cannot be assembled at
    all (currently only an undecodable uploaded image). Carries the
    student-facing message the caller should surface."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass
class _ChatRequest:
    """Everything needed to issue one Gemini chat call, streaming or not."""
    system_instruction: str
    contents: list
    gemini_history: list


def _build_chat_request(
    prompt: str,
    mode: str,
    board: str,
    user_class: Optional[str],
    stream: Optional[str],
    image_bytes: Optional[bytes],
    pdf_context: str,
    history: Optional[list],
) -> "_ChatRequest":
    """Build the system instruction, contents and history for a chat call.

    Pure and side-effect-free apart from logging. Shared by both adapters so
    a streamed answer and a non-streamed answer are generated from exactly
    the same inputs.
    """
    # ISSUE 1 FIX (Phase 6A final correction): user_class/stream now come
    # from the student's own Profile (backend/main.py:
    # _get_academic_context) and can genuinely be None - either because
    # the student has no profile yet, or (stream only) because their
    # class has no stream (Class 6-8). "Missing profile must NOT cause
    # the system to guess a class/stream" - so this builds the academic
    # clause conditionally rather than ever substituting a fabricated
    # default class/stream into the prompt.
    if user_class and stream:
        academic_clause = f"studying {user_class} ({stream} stream)"
    elif user_class:
        academic_clause = f"studying {user_class}"
    else:
        academic_clause = "at the secondary/higher-secondary level"

    system_instruction = (
        f"You are Kognit, an expert academic AI tutor for students in {board}, {academic_clause}.\n"
        + CHAT_SYSTEM_INSTRUCTION_RULES
    )

    if pdf_context:
        pdf_total_chars = len(pdf_context)
        pdf_context_used = pdf_context[:MAX_PDF_CONTEXT_CHARS]
        pdf_was_truncated = pdf_total_chars > MAX_PDF_CONTEXT_CHARS

        if pdf_was_truncated:
            # Was previously silent (no log, no student-facing signal at all).
            # Logged so real truncation frequency/size is now measurable
            # instead of assumed.
            logger.warning(
                "generate_ai_response: PDF context truncated total_chars=%d "
                "used_chars=%d limit=%d (mode=%s, board=%s, user_class=%s)",
                pdf_total_chars, MAX_PDF_CONTEXT_CHARS, MAX_PDF_CONTEXT_CHARS,
                mode, board, user_class
            )

        system_instruction += f"\n\n[UPLOADED PDF DOCUMENT CONTENT CONTEXT]:\n{pdf_context_used}"

        if pdf_was_truncated:
            # Previously the model silently answered as if it had seen the
            # whole document, with no way for the student to know part of it
            # was missing. This does not fix incomplete context, but stops
            # Kognit from acting like it isn't incomplete.
            system_instruction += (
                "\n\n[NOTE: The document above was too long to include in full - only "
                "the beginning portion is shown; the rest of the document is NOT visible "
                "to you. If the student's question may depend on content that could be "
                "further into the document (a later chapter, page, or section), say so "
                "plainly instead of guessing, and ask them to specify or re-upload just "
                "that part.]"
            )

    if mode == "socratic":
        system_instruction += " DO NOT give direct answers immediately. Guide the student step-by-step using helpful questions!"

    # Image decoding: unaffected by the SDK migration. PIL Images are
    # accepted directly as a message part by google-genai's chat.send_message
    # the same way they were accepted by the old SDK's generate_content.
    t_stage_start = time.perf_counter()
    contents = []
    if image_bytes:
        try:
            img = Image.open(io.BytesIO(image_bytes))
            img.load()  # force decode now, not lazily inside the Gemini call
        except Exception:
            logger.exception(
                "generate_ai_response: could not decode uploaded image (mode=%s, board=%s, user_class=%s)",
                mode, board, user_class
            )
            raise _ChatRequestBuildError(IMAGE_DECODE_ERROR)
        contents.append(img)
    logger.info("generate_ai_response timing: image_decode=%.3fs", time.perf_counter() - t_stage_start)

    contents.append(prompt if prompt else "Please analyze this request based on the context.")

    # CHAT-04: history is built once - identical on every retry attempt below.
    gemini_history = _build_gemini_history(history or [])

    return _ChatRequest(
        system_instruction=system_instruction,
        contents=contents,
        gemini_history=gemini_history,
    )




def generate_ai_response(
    prompt: str,
    mode: str = "direct",
    board: str = "NCTB",
    user_class: Optional[str] = None,
    stream: Optional[str] = None,
    image_bytes: bytes = None,
    pdf_context: str = "",
    history: list = None,
    return_metadata: bool = False,
    enable_research: bool = False
):
    # return_metadata (Phase 7B): see AIGenerationResult docstring.
    #
    # enable_research (Phase 7C): additive, backward-compatible, defaults
    # to False. When True, adds Gemini's built-in Google Search grounding
    # tool to this call - Kognit does NOT scrape or run its own search;
    # Gemini decides whether/how to search and returns grounding_metadata
    # describing what it did. This function never interprets that
    # metadata - it is only captured (when return_metadata=True) and
    # passed through raw on AIGenerationResult.grounding_metadata for
    # backend/research_models.py to normalize. Every existing call site
    # (backend/main.py's non-research path) uses the default False and
    # is completely unaffected - no tools are added to the config at all
    # in that case, byte-identical to pre-Phase-7C behavior.
    def _wrap(text: str, *, is_error: bool, usage_metadata=None,
              resolved_model_version: Optional[str] = None,
              response_id: Optional[str] = None,
              elapsed_seconds: Optional[float] = None,
              grounding_metadata=None):
        if not return_metadata:
            return text
        return AIGenerationResult(
            text=text,
            is_error=is_error,
            usage_metadata=usage_metadata,
            resolved_model_version=resolved_model_version,
            response_id=response_id,
            elapsed_seconds=elapsed_seconds,
            grounding_metadata=grounding_metadata,
        )

    try:
        _req = _build_chat_request(
            prompt=prompt,
            mode=mode,
            board=board,
            user_class=user_class,
            stream=stream,
            image_bytes=image_bytes,
            pdf_context=pdf_context,
            history=history,
        )
    except _ChatRequestBuildError as exc:
        return _wrap(exc.message, is_error=True)

    system_instruction = _req.system_instruction
    contents = _req.contents
    gemini_history = _req.gemini_history

    for attempt in range(1, MAX_ATTEMPTS + 1):
        t_attempt_start = time.perf_counter()
        attempt_timeout = AI_REQUEST_TIMEOUT_SECONDS if attempt == 1 else RETRY_REQUEST_TIMEOUT_SECONDS

        # Built fresh each attempt (cheap, local - no network call) so the
        # per-attempt timeout below is always the one actually in effect.
        # retry_options is deliberately never set - see "SINGLE RETRY
        # AUTHORITY" comment above.
        # Phase 7C: the Google Search grounding tool is added ONLY when
        # enable_research=True - the config object for a normal (non-
        # research) call is built exactly as before, with no tools=[]
        # key at all, so this change cannot affect any existing request.
        config_kwargs = dict(
            system_instruction=system_instruction,
            thinking_config=types.ThinkingConfig(thinking_level=CHAT_THINKING_LEVEL),
            http_options=types.HttpOptions(timeout=_seconds_to_ms(attempt_timeout)),
        )
        if enable_research:
            config_kwargs["tools"] = [types.Tool(google_search=types.GoogleSearch())]
        config = types.GenerateContentConfig(**config_kwargs)

        try:
            # CHAT-04: multi-turn chat session so prior turns in THIS chat
            # are actually part of the request Gemini sees. history is empty
            # on a chat's first message, equivalent to the old behavior.
            chat_session = _client.chats.create(
                model=MODEL_NAME,
                config=config,
                history=gemini_history,
            )
            response = chat_session.send_message(contents)
            elapsed = time.perf_counter() - t_attempt_start

            result_text = response.text
            if not result_text:
                # google-genai returns None here instead of raising (unlike
                # the old SDK's ValueError) - almost always a safety-filter
                # block. Not retried: an identical request would just get
                # blocked again.
                logger.warning(
                    "generate_ai_response got a blocked/empty response attempt=%d/%d elapsed=%.3fs "
                    "(mode=%s, board=%s, user_class=%s)",
                    attempt, MAX_ATTEMPTS, elapsed, mode, board, user_class
                )
                return _wrap(BLOCKED_RESPONSE_ERROR, is_error=True, elapsed_seconds=elapsed)

            _log_usage(response.usage_metadata, attempt, elapsed, mode, bool(image_bytes), bool(pdf_context))
            grounding_metadata = None
            if enable_research:
                # Best-effort, never fabricated: candidates[0] and its
                # grounding_metadata attribute are read defensively - an
                # unexpected empty candidates list or a response shape
                # without this attribute results in None, handled
                # explicitly downstream (research_models.py treats a
                # None grounding_metadata on a research-requested call
                # as 'failed', never as 'used').
                candidates = getattr(response, "candidates", None) or []
                if candidates:
                    grounding_metadata = getattr(candidates[0], "grounding_metadata", None)
            return _wrap(
                result_text,
                is_error=False,
                usage_metadata=response.usage_metadata,
                resolved_model_version=getattr(response, "model_version", None),
                response_id=getattr(response, "response_id", None),
                elapsed_seconds=elapsed,
                grounding_metadata=grounding_metadata,
            )

        except genai_errors.ClientError as e:
            elapsed = time.perf_counter() - t_attempt_start
            if e.code == 429:
                # Bucket C: quota exhaustion. Never retried.
                logger.exception(
                    "generate_ai_response quota exhausted attempt=%d elapsed=%.3fs (mode=%s, board=%s, user_class=%s)",
                    attempt, elapsed, mode, board, user_class
                )
                return _wrap(QUOTA_EXHAUSTED_ERROR, is_error=True, elapsed_seconds=elapsed)
            if e.code in TRANSIENT_RETRYABLE_CLIENT_HTTP_CODES and attempt < MAX_ATTEMPTS_BUCKET_B:
                # Bucket B (409 Aborted-equivalent).
                retry_delay = random.uniform(
                    RETRY_DELAY_BASE_SECONDS - RETRY_DELAY_JITTER_SECONDS,
                    RETRY_DELAY_BASE_SECONDS + RETRY_DELAY_JITTER_SECONDS,
                )
                logger.warning(
                    "generate_ai_response transient provider error code=%s attempt=%d/%d timeout=%ds "
                    "elapsed=%.3fs (mode=%s, board=%s, user_class=%s): %s - retrying in %.2fs",
                    e.code, attempt, MAX_ATTEMPTS_BUCKET_B, attempt_timeout, elapsed,
                    mode, board, user_class, e.status, retry_delay
                )
                time.sleep(retry_delay)
                continue
            # Bucket E: any other 4xx (bad request, permission denied, etc.)
            # is a programming/config error, not something a retry fixes.
            logger.exception(
                "generate_ai_response client error code=%s attempt=%d/%d elapsed=%.3fs (mode=%s, board=%s, user_class=%s)",
                e.code, attempt, MAX_ATTEMPTS_BUCKET_B, elapsed, mode, board, user_class
            )
            return _wrap(GENERIC_CHAT_ERROR, is_error=True, elapsed_seconds=elapsed)

        except genai_errors.ServerError as e:
            # Bucket B: genuine transient provider failure (5xx).
            elapsed = time.perf_counter() - t_attempt_start
            if attempt < MAX_ATTEMPTS_BUCKET_B:
                retry_delay = random.uniform(
                    RETRY_DELAY_BASE_SECONDS - RETRY_DELAY_JITTER_SECONDS,
                    RETRY_DELAY_BASE_SECONDS + RETRY_DELAY_JITTER_SECONDS,
                )
                logger.warning(
                    "generate_ai_response transient provider error code=%s attempt=%d/%d timeout=%ds "
                    "elapsed=%.3fs (mode=%s, board=%s, user_class=%s): %s - retrying in %.2fs",
                    e.code, attempt, MAX_ATTEMPTS_BUCKET_B, attempt_timeout, elapsed,
                    mode, board, user_class, e.status, retry_delay
                )
                time.sleep(retry_delay)
                continue
            logger.exception(
                "generate_ai_response failed after %d attempts with server error code=%s (mode=%s, board=%s, user_class=%s)",
                MAX_ATTEMPTS_BUCKET_B, e.code, mode, board, user_class
            )
            return _wrap(GENERIC_CHAT_ERROR, is_error=True, elapsed_seconds=elapsed)

        except (httpx.TimeoutException, httpx.ConnectError) as e:
            # Bucket A: OUR OWN client-side deadline fired (or we could not
            # connect). Retried at most once - see RETRY_ON_CLIENT_TIMEOUT
            # and MAX_ATTEMPTS_BUCKET_A comments above for why this ceiling
            # is intentionally kept lower than bucket B's.
            elapsed = time.perf_counter() - t_attempt_start
            if RETRY_ON_CLIENT_TIMEOUT and attempt < MAX_ATTEMPTS_BUCKET_A:
                logger.warning(
                    "generate_ai_response client deadline exceeded attempt=%d/%d timeout=%ds elapsed=%.3fs "
                    "(mode=%s, board=%s, user_class=%s): %s - retrying (RETRY_ON_CLIENT_TIMEOUT=True)",
                    attempt, MAX_ATTEMPTS_BUCKET_A, attempt_timeout, elapsed, mode, board, user_class, type(e).__name__
                )
                continue
            logger.exception(
                "generate_ai_response client deadline exceeded attempt=%d/%d timeout=%ds elapsed=%.3fs "
                "(mode=%s, board=%s, user_class=%s) - not retried further (RETRY_ON_CLIENT_TIMEOUT=%s, "
                "MAX_ATTEMPTS_BUCKET_A=%d)",
                attempt, MAX_ATTEMPTS_BUCKET_A, attempt_timeout, elapsed, mode, board, user_class,
                RETRY_ON_CLIENT_TIMEOUT, MAX_ATTEMPTS_BUCKET_A
            )
            return _wrap(GENERIC_CHAT_ERROR, is_error=True, elapsed_seconds=elapsed)

        except Exception:
            # Bucket E: unexpected/programming error.
            elapsed = time.perf_counter() - t_attempt_start
            logger.exception(
                "generate_ai_response failed unexpectedly attempt=%d elapsed=%.3fs (mode=%s, board=%s, user_class=%s)",
                attempt, elapsed, mode, board, user_class
            )
            return _wrap(GENERIC_CHAT_ERROR, is_error=True, elapsed_seconds=elapsed)

    # Not reachable (the loop always returns), kept as a defensive fallback.
    return _wrap(GENERIC_CHAT_ERROR, is_error=True)


# ---------------------------------------------------------------------------
# QUIZ OUTPUT VALIDATION (Phase 1 addition, quiz-persistence work).
#
# Previously generate_quiz_questions() returned Gemini's parsed JSON
# directly with no schema check at all - any malformed entry (missing
# correct_index, options not a list, correct_index out of range, etc.)
# would silently reach the frontend. That was tolerable while the quiz was
# purely ephemeral/client-side, but the new /api/quiz/submit endpoint
# (backend/main.py) grades a student's answers by reading
# question["correct_index"] server-side. A malformed question there is no
# longer just a rendering glitch - it would break score integrity or throw
# at grading time. This validation is therefore a direct, in-scope
# requirement of the persistence feature, not a general-purpose cleanup.
#
# Deliberately DROPS the "id" field: it was never used by the frontend
# (static/js/app.js renders questions by array position, see
# renderQuizQuestion/showQuizResults) and the new server-side quiz
# definition store (backend/main.py: active_quiz_definitions) also grades
# by array position - a model-supplied "id" (which is not guaranteed
# unique or even present) is not the source of truth for anything.
# ---------------------------------------------------------------------------
def validate_quiz_questions(raw_questions) -> list:
    """
    Filters `raw_questions` (Gemini's parsed JSON) down to only
    structurally valid MCQ entries. Never raises - unusable entries are
    dropped silently (logged at debug level would be noisy per-question;
    the caller logs if the whole result ends up empty).

    A valid entry requires:
      - "question": non-empty string
      - "options": a list of at least 2 non-empty strings
      - "correct_index": an int, 0 <= correct_index < len(options)
      - "explanation": optional, coerced to "" if missing/wrong type
    """
    if not isinstance(raw_questions, list):
        return []

    validated = []
    for q in raw_questions:
        if not isinstance(q, dict):
            continue

        question_text = q.get("question")
        options = q.get("options")
        correct_index = q.get("correct_index")
        explanation = q.get("explanation")

        if not isinstance(question_text, str) or not question_text.strip():
            continue
        if not isinstance(options, list) or len(options) < 2:
            continue
        if not all(isinstance(opt, str) and opt.strip() for opt in options):
            continue
        # bool is a subclass of int in Python - reject it explicitly so
        # `correct_index: true` (unlikely, but possible malformed output)
        # can't slip through as index 1.
        if isinstance(correct_index, bool) or not isinstance(correct_index, int):
            continue
        if not (0 <= correct_index < len(options)):
            continue

        validated.append({
            "question": question_text.strip(),
            "options": [opt.strip() for opt in options],
            "correct_index": correct_index,
            "explanation": explanation.strip() if isinstance(explanation, str) else "",
        })

    return validated


def generate_quiz_questions(
    board: str,
    user_class: Optional[str],
    subject: str,
    stream: Optional[str],
    topic: str,
    count: int = 5,
) -> list:
    """
    P0 RELIABILITY FIX (504 investigation): previously this function made a
    single Gemini call with a blanket `except Exception: return []` - a
    transient 5xx (observed: 504 DEADLINE_EXCEEDED from Google's own
    infrastructure) failed the whole quiz generation immediately, with the
    only "recovery" being the student manually clicking Generate Quiz
    again. This reuses the SAME bucket classification and constants
    already proven for generate_ai_response()'s chat path (bucket B:
    genuine transient provider 5xx/409 -> retried up to MAX_ATTEMPTS_BUCKET_B
    with the same jittered delay and same retry timeout budget); it is not
    a new retry design.

    Scope note (deliberately narrower than chat's bucket set): this only
    adds bucket B (5xx/409). Client-side deadline handling (bucket A /
    MAX_ATTEMPTS_BUCKET_A / RETRY_ON_CLIENT_TIMEOUT in generate_ai_response)
    is NOT reused here - that was a separate, independently-tuned decision
    for the chat path and was not part of the approved scope for this fix.
    A client-side timeout here (httpx.TimeoutException/ConnectError) falls
    through to the generic "unexpected error" handling below and is not
    retried, matching this task's "unexpected errors -> do not retry"
    instruction. If real-world data later shows quiz generation also needs
    bucket-A handling, that is a separate, explicitly-scoped follow-up.

    `user_class`/`stream` are Optional: ISSUE 1 FIX (Phase 6A final
    correction) - see generate_ai_response's identical handling above for
    why (missing profile, or a Class 6-8 profile with no stream). Never
    fabricates a class/stream in the prompt when either is missing.
    """
    if user_class and stream:
        academic_clause = f"{user_class}, {stream} stream"
    elif user_class:
        academic_clause = f"{user_class}"
    else:
        academic_clause = "the secondary/higher-secondary level"

    system_instruction = (
        f"You are an exam paper creator for {board}, {academic_clause}, Subject: {subject}.\n"
        f"Generate {count} high-quality Multiple Choice Questions (MCQs) on the topic: '{topic}'.\n"
        "Output MUST be strict raw JSON array only. Do not wrap in markdown or include conversational text. Format:\n"
        "[\n"
        "  {\n"
        '    "id": 1,\n'
        '    "question": "Question text here",\n'
        '    "options": ["Option A", "Option B", "Option C", "Option D"],\n'
        '    "correct_index": 0,\n'
        '    "explanation": "Why option A is correct..."\n'
        "  }\n"
        "]"
    )

    for attempt in range(1, MAX_ATTEMPTS_BUCKET_B + 1):
        attempt_timeout = AI_REQUEST_TIMEOUT_SECONDS if attempt == 1 else RETRY_REQUEST_TIMEOUT_SECONDS

        try:
            config_kwargs = dict(
                system_instruction=system_instruction,
                http_options=types.HttpOptions(timeout=_seconds_to_ms(attempt_timeout)),
            )
            if QUIZ_THINKING_LEVEL is not None:
                config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=QUIZ_THINKING_LEVEL)

            response = _client.models.generate_content(
                model=MODEL_NAME,
                contents=f"Create {count} MCQ questions on {topic}.",
                config=types.GenerateContentConfig(**config_kwargs),
            )

            raw_text = (response.text or "").strip()
            if not raw_text:
                # Blocked/empty response (almost always a safety-filter
                # block) - not retried, matching generate_ai_response's
                # bucket D: an identical request would just get blocked
                # again.
                logger.warning(
                    "generate_quiz_questions got a blocked/empty response attempt=%d/%d "
                    "(board=%s, user_class=%s, subject=%s, stream=%s, topic=%s)",
                    attempt, MAX_ATTEMPTS_BUCKET_B, board, user_class, subject, stream, topic
                )
                return []

            if "```json" in raw_text:
                raw_text = raw_text.split("```json")[1].split("```")[0].strip()
            elif "```" in raw_text:
                raw_text = raw_text.split("```")[1].split("```")[0].strip()

            parsed = json.loads(raw_text)
            validated = validate_quiz_questions(parsed)
            if not validated:
                logger.error(
                    "generate_quiz_questions: Gemini output had no valid MCQ entries after "
                    "validation (board=%s, user_class=%s, subject=%s, stream=%s, topic=%s, raw_count=%s)",
                    board, user_class, subject, stream, topic,
                    len(parsed) if isinstance(parsed, list) else "not-a-list"
                )
            return validated

        except genai_errors.ClientError as e:
            if e.code == 429:
                # Bucket C: quota exhaustion. Never retried - quota will
                # not clear in a few seconds.
                logger.exception(
                    "generate_quiz_questions quota exhausted attempt=%d "
                    "(board=%s, user_class=%s, subject=%s, stream=%s, topic=%s)",
                    attempt, board, user_class, subject, stream, topic
                )
                return []
            if e.code in TRANSIENT_RETRYABLE_CLIENT_HTTP_CODES and attempt < MAX_ATTEMPTS_BUCKET_B:
                # Bucket B (409 Aborted-equivalent) - same classification
                # and constants as generate_ai_response.
                retry_delay = random.uniform(
                    RETRY_DELAY_BASE_SECONDS - RETRY_DELAY_JITTER_SECONDS,
                    RETRY_DELAY_BASE_SECONDS + RETRY_DELAY_JITTER_SECONDS,
                )
                logger.warning(
                    "generate_quiz_questions transient provider error code=%s attempt=%d/%d "
                    "timeout=%ds (board=%s, user_class=%s, subject=%s, stream=%s, topic=%s): %s - retrying in %.2fs",
                    e.code, attempt, MAX_ATTEMPTS_BUCKET_B, attempt_timeout,
                    board, user_class, subject, stream, topic, e.status, retry_delay
                )
                time.sleep(retry_delay)
                continue
            # Bucket E: any other 4xx (bad request, permission denied, etc.)
            # is a programming/config error, not something a retry fixes.
            logger.exception(
                "generate_quiz_questions client error code=%s attempt=%d/%d "
                "(board=%s, user_class=%s, subject=%s, stream=%s, topic=%s)",
                e.code, attempt, MAX_ATTEMPTS_BUCKET_B, board, user_class, subject, stream, topic
            )
            return []

        except genai_errors.ServerError as e:
            # Bucket B: genuine transient provider failure (5xx, including
            # the 504 DEADLINE_EXCEEDED this fix was written for). Returns
            # from Google fast (an error response, not a slow success), so
            # retrying with a full timeout budget does not meaningfully
            # raise the worst-case wait - same reasoning as chat's bucket B.
            if attempt < MAX_ATTEMPTS_BUCKET_B:
                retry_delay = random.uniform(
                    RETRY_DELAY_BASE_SECONDS - RETRY_DELAY_JITTER_SECONDS,
                    RETRY_DELAY_BASE_SECONDS + RETRY_DELAY_JITTER_SECONDS,
                )
                logger.warning(
                    "generate_quiz_questions transient provider error code=%s attempt=%d/%d "
                    "timeout=%ds (board=%s, user_class=%s, subject=%s, stream=%s, topic=%s): %s - retrying in %.2fs",
                    e.code, attempt, MAX_ATTEMPTS_BUCKET_B, attempt_timeout,
                    board, user_class, subject, stream, topic, e.status, retry_delay
                )
                time.sleep(retry_delay)
                continue
            logger.exception(
                "generate_quiz_questions failed after %d attempts with server error code=%s "
                "(board=%s, user_class=%s, subject=%s, stream=%s, topic=%s)",
                MAX_ATTEMPTS_BUCKET_B, e.code, board, user_class, subject, stream, topic
            )
            return []

        except Exception:
            # Bucket E: unexpected error - malformed JSON from the model,
            # a client-side timeout/connect error, or a genuine programming
            # error. Not retried: for JSON/format issues an identical
            # request would likely fail the same way again, and client-side
            # timeout handling (bucket A in generate_ai_response) was
            # deliberately not carried over here - see function docstring.
            logger.exception(
                "generate_quiz_questions failed unexpectedly attempt=%d/%d "
                "(board=%s, user_class=%s, subject=%s, stream=%s, topic=%s)",
                attempt, MAX_ATTEMPTS_BUCKET_B, board, user_class, subject, stream, topic
            )
            return []

    # Not reachable (the loop always returns), kept as a defensive fallback
    # matching the same pattern generate_ai_response uses.
    return []


# ---------------------------------------------------------------------------
# FEATURE 2: context-aware chat titles.
#
# Called at most once per chat (see maybeGenerateAiTitle() in static/js/
# app.js, which owns that "only once" guard) once there is enough real
# conversation to summarize. Deliberately a single short, non-retried call -
# a title is a nice-to-have UX detail, not core answer quality, so it is not
# worth the same retry budget as generate_ai_response. Any failure returns
# None; the caller keeps the existing (heuristic, first-message-based) title
# in that case rather than surfacing an error to the student.
# ---------------------------------------------------------------------------
MAX_CHAT_TITLE_CHARS = 45
CHAT_TITLE_TIMEOUT_SECONDS = 10

TITLE_SYSTEM_INSTRUCTION = (
    "You generate short chat titles for an academic study assistant used by "
    "Bangladeshi students (Bangla, English, or mixed/Banglish conversations).\n"
    "Read the conversation and output ONLY a short, specific title (3-6 words) "
    "summarizing the actual topic being discussed - not the app name, not a "
    "greeting, not a generic phrase like 'Study Session'.\n"
    "Match the dominant language of the conversation (Bangla in, Bangla title; "
    "English in, English title).\n"
    "Output the title text ONLY - no quotes, no punctuation at the end, no "
    "markdown, no explanation, no prefix like 'Title:'."
)


def generate_chat_title(history: list, board: str = "NCTB") -> Optional[str]:
    if not history:
        return None

    # Keep the prompt small and cheap - this call happens on the side of a
    # real answer the student is already reading, it should be fast and low
    # cost, not a second full-context AI call.
    convo_lines = []
    for entry in history[-10:]:
        role = entry.get("role")
        text = (entry.get("text") or "").strip()
        if not text:
            continue
        speaker = "Student" if role == "user" else "Kognit"
        convo_lines.append(f"{speaker}: {text[:300]}")
    convo_text = "\n".join(convo_lines)
    if not convo_text:
        return None

    try:
        response = _client.models.generate_content(
            model=MODEL_NAME,
            contents=f"Conversation (board: {board}):\n{convo_text}\n\nTitle:",
            config=types.GenerateContentConfig(
                system_instruction=TITLE_SYSTEM_INSTRUCTION,
                thinking_config=types.ThinkingConfig(thinking_level=TITLE_THINKING_LEVEL),
                http_options=types.HttpOptions(timeout=_seconds_to_ms(CHAT_TITLE_TIMEOUT_SECONDS)),
            ),
        )
        title = (response.text or "").strip()
        # Defensive cleanup: strip accidental wrapping quotes/markdown the
        # model might still add despite the instruction above.
        title = title.strip('"\'` \n')
        title = title.split("\n")[0].strip()
        if not title:
            return None
        if len(title) > MAX_CHAT_TITLE_CHARS:
            title = title[:MAX_CHAT_TITLE_CHARS].rsplit(" ", 1)[0].rstrip(".,;:!?") + "..."
        return title
    except Exception:
        logger.exception("generate_chat_title failed (board=%s) - caller will keep the existing title", board)
        return None

# ---------------------------------------------------------------------------
# PHASE 8E: STREAMING ADAPTER
# ---------------------------------------------------------------------------


@dataclass
class StreamChunk:
    """One event from stream_ai_response().

    kind:
      "text"        - an incremental piece of the answer. `text` is the
                      delta only.
      "retry"       - BUG 3 PHASE 2: a bounded recovery generation is
                      starting after a confirmed transient provider
                      failure (ServerError/5xx) hit after partial output.
                      NOT terminal - more "text" chunks and an eventual
                      "done"/"interrupted"/"error" always follow. Carries
                      no text. The consumer MUST discard everything
                      accumulated so far (the failed attempt's partial
                      text is never part of the final answer) and treat
                      what follows as a completely fresh generation.
      "done"        - terminal SUCCESS ONLY. `text` is the FULL final
                      answer; grounding_metadata carries research metadata
                      (or None). Emitted ONLY when the SDK's stream
                      iterator completed with no exception AND the last
                      observed finish_reason is absent or exactly STOP -
                      see _is_successful_finish() below (BUG 3 fix). When
                      a "retry" preceded it, this text comes ENTIRELY from
                      the recovery generation, never combined with the
                      discarded attempt.
      "interrupted" - terminal PARTIAL/NON-SUCCESS. `text` is whatever was
                      genuinely generated before the stream stopped being
                      trustworthy (exception after first output, or a
                      clean exit with a non-STOP finish_reason such as
                      MAX_TOKENS/SAFETY/RECITATION). The caller must NOT
                      treat this as a completed answer - see main.py's
                      event contract and app.js's completion check.
      "error"       - terminal failure with NO usable answer text at all
                      (blocked before any output, quota exhausted, or all
                      retries exhausted before first output). `text` is a
                      safe, student-facing message - never raw provider
                      text.

    Exactly one terminal event ("done", "interrupted", or "error") is
    always emitted, so a consumer can never be left waiting forever.

    BUG 3 (completion-integrity fix): before this fix, "done" was used for
    every non-empty outcome, including a mid-stream exception salvage and
    (unconditionally, since finish_reason was never even read) a clean but
    truncated finish. That made a genuinely complete answer and a
    truncated one wire-identical. "interrupted" now carries every case
    that isn't a verified clean STOP.

    BUG 3 PHASE 2 (recovery): "retry" is the one new, non-terminal kind. At
    most one is ever emitted per call - see `recovery_used` in
    stream_ai_response. It exists specifically for the evidenced case of a
    confirmed transient provider failure after partial output; every other
    after-first-byte failure still goes straight to "interrupted" with no
    retry, unchanged from Phase 1.
    """
    kind: str
    text: str = ""
    grounding_metadata: Optional[object] = None
    usage_metadata: Optional[object] = None
    elapsed_seconds: Optional[float] = None
    # P0 STABILIZATION: stable machine-readable code (ERROR_* above) on
    # "error" and "interrupted" chunks. None on text/retry/done.
    error_code: Optional[str] = None


def stream_ai_response(
    prompt: str,
    mode: str = "direct",
    board: str = "NCTB",
    user_class: Optional[str] = None,
    stream: Optional[str] = None,
    image_bytes: bytes = None,
    pdf_context: str = "",
    history: list = None,
    enable_research: bool = False,
    request_id: Optional[str] = None,
):
    """Streaming counterpart of generate_ai_response().

    Yields StreamChunk events. Uses the SAME _build_chat_request() core as the
    non-streaming path, so the prompt, system instruction, PDF truncation,
    image handling and history are identical - a streamed answer is the same
    answer, delivered incrementally.

    RETRY POLICY (the key architectural constraint of this phase, refined by
    the Bug 3 Phase 2 recovery mechanism below):

        Retries before the first byte are unrestricted (same as
        generate_ai_response). After the first byte, retrying used to be
        unconditionally unsafe - restarting mid-answer would either
        duplicate content or silently replace what the student is
        mid-sentence on.

    Real production telemetry (Bug 3 Phase 1 validation) showed the actual
    dominant post-first-byte failure is a CONFIRMED TRANSIENT one: Gemini
    returns HTTP 200, streams real content (9 chunks / 34 chunks observed),
    then fails with ServerError 503 "high demand... usually temporary" -
    not a request problem, a provider capacity problem. For exactly this
    evidenced case, Phase 2 grants ONE bounded recovery generation: the
    partial output is discarded completely (never concatenated - a fresh
    chat_session is created from the SAME frozen `req`, so it is the exact
    same prompt/history/context, not a continuation), a "retry" event tells
    the client to reset its in-progress bubble, and a brand new complete
    generation is attempted. See `recovery_used` below.

    Every OTHER after-first-byte failure (ClientError of any kind including
    429, a non-STOP finish_reason with no exception, or an unexpected bare
    exception) is still finalized immediately as "interrupted" with NO
    retry - none of those are the confirmed-transient case the evidence
    supports recovering from, and 429/quota specifically must never be
    blindly retried (retrying an exhausted quota cannot succeed and only
    burns latency).

    COMPLETION INTEGRITY (Bug 3 Phase 1 fix - see _is_successful_finish()
    above): "done" is reserved exclusively for a stream that the SDK
    iterator completed with no exception AND whose last observed
    finish_reason is absent or exactly STOP - including the recovery
    generation's own completion, which is what ends up in "done" when
    recovery succeeds. See StreamChunk's docstring for the full kind
    contract, including "retry".

    Never raises to the caller - every failure path yields a terminal event
    ("done"/"interrupted"/"error") instead, so the HTTP layer always has
    something well-formed to send. Exactly one terminal event per call
    ("retry" is NOT terminal - more chunks always follow it).

    P0 STABILIZATION (this pass): (1) every retry - pre-first-token and the
    one mid-stream recovery - now waits a bounded, jittered backoff first
    (see _compute_retry_backoff); the attempt limits themselves are
    unchanged. For the recovery, the "retry" event is yielded BEFORE the
    wait so the client resets its bubble immediately. (2) A non-retryable
    ClientError (400/401/403/404...) is no longer retried - only 409 is,
    matching the non-streaming path's documented policy. (3) Recovery is
    only granted when an attempt remains to run it. (4) error/interrupted
    chunks carry a stable `error_code`, and every log line carries
    `request_id` (generated here if the caller did not supply one).
    """
    request_id = request_id or new_request_id()
    t_stream_start = time.perf_counter()
    ttft_seconds = None
    chunk_count = 0
    last_chunk_elapsed = None
    # BUG 3 PHASE 2: at most ONE recovery generation is ever granted per
    # call, regardless of how many pre-first-token retries happen on either
    # side of it. This flag is the entire enforcement mechanism - see the
    # ServerError branch below.
    recovery_used = False
    # Set for exactly one loop iteration - the one immediately following a
    # recovery grant - so that iteration gets the FULL request timeout
    # (it's a complete fresh generation, not a quick incremental retry).
    pending_recovery = False
    # P0 STABILIZATION: how many retries (pre-first-token retries plus the
    # one recovery) have been granted so far. Drives ONLY how long the next
    # backoff is - it is never a limit; the attempt ceilings are unchanged.
    retries_granted = 0

    def _telemetry(outcome, attempt, *, finish_reason=None, exc=None, http_status=None,
                   error_code=None, complete=False):
        # One place that adds request_id/error_code to the per-request
        # summary line. Reads the live loop state at call time.
        _log_stream_telemetry(
            outcome=outcome, mode=mode, attempt=attempt,
            ttft_seconds=ttft_seconds, chunk_count=chunk_count,
            last_chunk_elapsed=last_chunk_elapsed, finish_reason=finish_reason,
            exception_type=type(exc).__name__ if exc is not None else None,
            http_status=http_status,
            total_duration=time.perf_counter() - t_stream_start,
            explicit_completion_received=complete,
            recovery_used=recovery_used,
            request_id=request_id, error_code=error_code,
        )

    logger.info(
        "request_id=%s stream_ai_response start model=%s mode=%s has_image=%s has_pdf=%s "
        "history_len=%d research=%s",
        request_id, MODEL_NAME, mode, bool(image_bytes), bool(pdf_context),
        len(history or []), enable_research,
    )

    try:
        req = _build_chat_request(
            prompt=prompt,
            mode=mode,
            board=board,
            user_class=user_class,
            stream=stream,
            image_bytes=image_bytes,
            pdf_context=pdf_context,
            history=history,
        )
    except _ChatRequestBuildError as exc:
        logger.warning(
            "request_id=%s request build failed - student-facing message returned code=%s",
            request_id, ERROR_REQUEST_INVALID,
        )
        yield StreamChunk(kind="error", text=exc.message, error_code=ERROR_REQUEST_INVALID)
        return

    for attempt in range(1, MAX_ATTEMPTS + 1):
        t_attempt_start = time.perf_counter()
        is_recovery_attempt = pending_recovery
        pending_recovery = False
        attempt_timeout = (
            AI_REQUEST_TIMEOUT_SECONDS if (attempt == 1 or is_recovery_attempt)
            else RETRY_REQUEST_TIMEOUT_SECONDS
        )

        config_kwargs = dict(
            system_instruction=req.system_instruction,
            thinking_config=types.ThinkingConfig(thinking_level=CHAT_THINKING_LEVEL),
            http_options=types.HttpOptions(timeout=_seconds_to_ms(attempt_timeout)),
        )
        if enable_research:
            config_kwargs["tools"] = [types.Tool(google_search=types.GoogleSearch())]
        config = types.GenerateContentConfig(**config_kwargs)

        produced_any_text = False
        collected = []
        grounding_metadata = None
        usage_metadata = None
        last_finish_reason = None
        last_finish_message = None

        logger.info(
            "request_id=%s provider attempt=%d/%d model=%s timeout=%ds recovery_attempt=%s",
            request_id, attempt, MAX_ATTEMPTS, MODEL_NAME, attempt_timeout, is_recovery_attempt,
        )

        try:
            chat_session = _client.chats.create(
                model=MODEL_NAME,
                config=config,
                history=req.gemini_history,
            )

            for response in chat_session.send_message_stream(req.contents):
                # BUG 3 INSTRUMENTATION: every chunk the SDK iterator yields
                # counts toward chunk_count, whether or not it carries text -
                # a metadata-only chunk is still a real provider round trip.
                chunk_count += 1
                last_chunk_elapsed = time.perf_counter() - t_stream_start

                # Defensive on every field: a chunk may legitimately carry no
                # text (e.g. a tool-use or metadata-only chunk). Those are not
                # errors and must not terminate the stream.
                piece = getattr(response, "text", None)
                if piece:
                    if not produced_any_text:
                        ttft_seconds = time.perf_counter() - t_stream_start
                    produced_any_text = True
                    collected.append(piece)
                    yield StreamChunk(kind="text", text=piece)

                candidates = getattr(response, "candidates", None) or []
                if candidates:
                    cand = candidates[0]
                    if enable_research:
                        gm = getattr(cand, "grounding_metadata", None)
                        # Grounding metadata typically arrives on a later
                        # chunk; keep the most recent non-None one.
                        if gm is not None:
                            grounding_metadata = gm
                    # BUG 3 FIX: finish_reason is the only signal that can
                    # distinguish a clean STOP from MAX_TOKENS/SAFETY/
                    # RECITATION/etc when the iterator exits with NO
                    # exception at all. An intermediate None is expected and
                    # must not erase an earlier real value, so keep the most
                    # recent non-None one, same pattern as grounding_metadata.
                    fr = getattr(cand, "finish_reason", None)
                    if fr is not None:
                        last_finish_reason = fr
                        last_finish_message = getattr(cand, "finish_message", None)

                if getattr(response, "usage_metadata", None) is not None:
                    usage_metadata = response.usage_metadata

            elapsed = time.perf_counter() - t_attempt_start
            final_text = "".join(collected)

            if not final_text:
                # Same meaning as the non-streaming path's empty .text: almost
                # always a safety-filter block. Not retried - an identical
                # request would be blocked again.
                code = _error_code_for_finish_reason(last_finish_reason, ERROR_PROVIDER_EMPTY_RESPONSE)
                logger.warning(
                    "request_id=%s stream_ai_response got a blocked/empty stream attempt=%d/%d "
                    "elapsed=%.3fs finish_reason=%s code=%s (mode=%s, board=%s, user_class=%s)",
                    request_id, attempt, MAX_ATTEMPTS, elapsed, last_finish_reason, code,
                    mode, board, user_class
                )
                _telemetry("blocked_empty", attempt, finish_reason=last_finish_reason, error_code=code)
                yield StreamChunk(kind="error", text=BLOCKED_RESPONSE_ERROR, error_code=code)
                return

            if _is_successful_finish(last_finish_reason):
                _log_usage(usage_metadata, attempt, elapsed, mode, bool(image_bytes), bool(pdf_context))
                _telemetry("success", attempt, finish_reason=last_finish_reason, complete=True)
                yield StreamChunk(
                    kind="done",
                    text=final_text,
                    grounding_metadata=grounding_metadata,
                    usage_metadata=usage_metadata,
                    elapsed_seconds=elapsed,
                )
                return

            # BUG 3 FIX: the iterator exited with NO exception, yet the last
            # observed finish_reason is not STOP - the silent-truncation mode.
            code = _error_code_for_finish_reason(last_finish_reason, ERROR_STREAM_INTERRUPTED)
            logger.warning(
                "request_id=%s stream_ai_response non-success finish_reason=%s finish_message=%s "
                "attempt=%d/%d elapsed=%.3fs chunk_count=%d code=%s (mode=%s, board=%s, "
                "user_class=%s) - finalizing as interrupted, not done",
                request_id, last_finish_reason, last_finish_message, attempt, MAX_ATTEMPTS,
                elapsed, chunk_count, code, mode, board, user_class
            )
            _telemetry("interrupted_finish_reason", attempt, finish_reason=last_finish_reason, error_code=code)
            yield StreamChunk(
                kind="interrupted",
                text=final_text,
                grounding_metadata=grounding_metadata,
                usage_metadata=usage_metadata,
                elapsed_seconds=elapsed,
                error_code=code,
            )
            return

        except genai_errors.ClientError as e:
            elapsed = time.perf_counter() - t_attempt_start
            status_code = getattr(e, "code", None)
            if produced_any_text:
                # Unexpected (a 4xx after bytes already flowed) - keep the
                # full traceback, this is not a known transient condition.
                logger.exception(
                    "request_id=%s stream_ai_response client error AFTER first output - finalizing "
                    "as interrupted attempt=%d elapsed=%.3fs chunk_count=%d finish_reason=%s "
                    "http_status=%s (mode=%s)",
                    request_id, attempt, elapsed, chunk_count, last_finish_reason, status_code, mode
                )
                _telemetry("interrupted_exception", attempt, finish_reason=last_finish_reason, exc=e,
                           http_status=status_code, error_code=ERROR_STREAM_INTERRUPTED)
                yield StreamChunk(
                    kind="interrupted",
                    text="".join(collected),
                    grounding_metadata=grounding_metadata,
                    usage_metadata=usage_metadata,
                    elapsed_seconds=elapsed,
                    error_code=ERROR_STREAM_INTERRUPTED,
                )
                return
            if status_code == 429:
                # Known condition (quota) - a warning, not a traceback.
                logger.warning(
                    "request_id=%s stream_ai_response quota exhausted attempt=%d elapsed=%.3fs "
                    "status=%s (mode=%s)",
                    request_id, attempt, elapsed, getattr(e, "status", None), mode
                )
                _telemetry("quota_exhausted", attempt, exc=e, http_status=429,
                           error_code=ERROR_PROVIDER_RATE_LIMITED)
                yield StreamChunk(kind="error", text=QUOTA_EXHAUSTED_ERROR,
                                  error_code=ERROR_PROVIDER_RATE_LIMITED)
                return
            if status_code in TRANSIENT_RETRYABLE_CLIENT_HTTP_CODES:
                # Bucket B (409 Aborted-equivalent) - the only ClientError
                # class that is retried, same as the non-streaming path.
                if attempt < MAX_ATTEMPTS_BUCKET_B:
                    logger.warning(
                        "request_id=%s stream_ai_response transient client error http_status=%s "
                        "attempt=%d/%d elapsed=%.3fs (mode=%s) - will retry after backoff",
                        request_id, status_code, attempt, MAX_ATTEMPTS_BUCKET_B, elapsed, mode
                    )
                    retries_granted += 1
                    _backoff_before_retry(retries_granted, request_id, "client_%s" % status_code)
                    continue
                logger.error(
                    "request_id=%s stream_ai_response transient client error http_status=%s - "
                    "gave up after %d attempts (mode=%s)",
                    request_id, status_code, attempt, mode
                )
                _telemetry("error_client", attempt, exc=e, http_status=status_code,
                           error_code=ERROR_PROVIDER_UNAVAILABLE)
                yield StreamChunk(kind="error", text=PROVIDER_UNAVAILABLE_ERROR,
                                  error_code=ERROR_PROVIDER_UNAVAILABLE)
                return
            # Any other 4xx (bad request, permission denied, not found...) is
            # a request/configuration problem - retrying cannot fix it, and
            # with backoff it would only make the student wait. Logged ONCE,
            # with a traceback, because it needs a developer.
            code = (ERROR_PROVIDER_AUTH_FAILED if status_code in (401, 403)
                    else ERROR_PROVIDER_INVALID_REQUEST)
            logger.exception(
                "request_id=%s stream_ai_response non-retryable client error http_status=%s "
                "code=%s attempt=%d elapsed=%.3fs (mode=%s) - not retried",
                request_id, status_code, code, attempt, elapsed, mode
            )
            _telemetry("error_client", attempt, exc=e, http_status=status_code, error_code=code)
            yield StreamChunk(kind="error", text=GENERIC_CHAT_ERROR, error_code=code)
            return

        except genai_errors.ServerError as e:
            # BUG 3 PHASE 2 - the confirmed-transient recovery path.
            # ServerError means Gemini's own infrastructure (5xx), not the
            # request, is the problem - this is the ONLY exception class
            # treated as safe to recover from after partial output. The real
            # production telemetry that motivated this showed "HTTP 200,
            # streaming began, N chunks delivered, then 503 UNAVAILABLE (high
            # demand... usually temporary)" - a provider capacity problem.
            elapsed = time.perf_counter() - t_attempt_start
            status_code = getattr(e, "code", None)
            if produced_any_text:
                # P0: recovery is only granted when an attempt remains to run
                # it. (Before, granting it on the final attempt emitted a
                # "retry" event and then fell out of the loop into a generic
                # error, discarding the partial answer for nothing.)
                if not recovery_used and attempt < MAX_ATTEMPTS:
                    # Grant the ONE bounded recovery generation. The partial
                    # text collected so far is discarded completely, never
                    # concatenated - continuing this same `for attempt` loop
                    # resets `collected`/`grounding_metadata`/
                    # `usage_metadata`/`last_finish_reason` fresh at the top
                    # of the next iteration, and a brand-new chat_session is
                    # created from the SAME frozen `req` - so the recovery is
                    # the same prompt/system instruction/history/image/PDF
                    # context, a completely fresh generation.
                    recovery_used = True
                    pending_recovery = True
                    retries_granted += 1
                    logger.warning(
                        "request_id=%s stream_ai_response transient server error AFTER first output - "
                        "triggering ONE bounded recovery generation, discarding partial "
                        "attempt=%d elapsed=%.3fs chunk_count=%d http_status=%s (mode=%s)",
                        request_id, attempt, elapsed, chunk_count, status_code, mode
                    )
                    _telemetry("recovery_triggered", attempt, finish_reason=last_finish_reason, exc=e,
                               http_status=status_code)
                    # The client resets its bubble on this event, so it is
                    # sent BEFORE the backoff wait - the student sees the
                    # "retrying" state immediately, not after the delay.
                    yield StreamChunk(kind="retry")
                    _backoff_before_retry(retries_granted, request_id, "recovery_after_partial_output")
                    continue
                # Recovery already used (or no attempt left to run it):
                # bounded means bounded. Finalize as interrupted with THIS
                # attempt's own text only, never combined with a discarded
                # earlier attempt. Known transient condition -> no traceback.
                logger.error(
                    "request_id=%s stream_ai_response transient server error AFTER output and no "
                    "recovery available - finalizing as interrupted attempt=%d elapsed=%.3fs "
                    "chunk_count=%d http_status=%s (mode=%s)",
                    request_id, attempt, elapsed, chunk_count, status_code, mode
                )
                _telemetry("interrupted_exception", attempt, finish_reason=last_finish_reason, exc=e,
                           http_status=status_code, error_code=ERROR_PROVIDER_UNAVAILABLE)
                yield StreamChunk(
                    kind="interrupted",
                    text="".join(collected),
                    grounding_metadata=grounding_metadata,
                    usage_metadata=usage_metadata,
                    elapsed_seconds=elapsed,
                    error_code=ERROR_PROVIDER_UNAVAILABLE,
                )
                return
            # Transient server error BEFORE any output on this attempt: the
            # existing bucket-B "fails before first token, try again"
            # protection (same MAX_ATTEMPTS_BUCKET_B ceiling as before), now
            # with a bounded backoff between attempts. Not the recovery
            # grant; does not set recovery_used.
            if attempt < MAX_ATTEMPTS_BUCKET_B:
                logger.warning(
                    "request_id=%s stream_ai_response server error attempt=%d/%d elapsed=%.3fs "
                    "http_status=%s status=%s (mode=%s) - will retry after backoff",
                    request_id, attempt, MAX_ATTEMPTS_BUCKET_B, elapsed, status_code,
                    getattr(e, "status", None), mode
                )
                retries_granted += 1
                _backoff_before_retry(retries_granted, request_id, "server_error_%s" % status_code)
                continue
            # Exhausted. A 5xx is a well-understood provider condition, so
            # this is one clear ERROR line with the status - not a traceback.
            logger.error(
                "request_id=%s stream_ai_response provider unavailable - gave up after %d attempts "
                "http_status=%s status=%s (mode=%s)",
                request_id, attempt, status_code, getattr(e, "status", None), mode
            )
            _telemetry("error_server", attempt, exc=e, http_status=status_code,
                       error_code=ERROR_PROVIDER_UNAVAILABLE)
            yield StreamChunk(kind="error", text=PROVIDER_UNAVAILABLE_ERROR,
                              error_code=ERROR_PROVIDER_UNAVAILABLE)
            return

        except Exception as e:
            elapsed = time.perf_counter() - t_attempt_start
            is_timeout = isinstance(e, (httpx.TimeoutException, httpx.ConnectError))
            status_code = getattr(e, "code", None)
            if produced_any_text:
                # Network drop / timeout mid-answer, or any other unexpected
                # exception - the exact type/status is in the telemetry line.
                logger.exception(
                    "request_id=%s stream_ai_response failed AFTER first output - finalizing as "
                    "interrupted attempt=%d elapsed=%.3fs chunk_count=%d exception_type=%s (mode=%s)",
                    request_id, attempt, elapsed, chunk_count, type(e).__name__, mode
                )
                _telemetry("interrupted_exception", attempt, finish_reason=last_finish_reason, exc=e,
                           http_status=status_code, error_code=ERROR_STREAM_INTERRUPTED)
                yield StreamChunk(
                    kind="interrupted",
                    text="".join(collected),
                    grounding_metadata=grounding_metadata,
                    usage_metadata=usage_metadata,
                    elapsed_seconds=elapsed,
                    error_code=ERROR_STREAM_INTERRUPTED,
                )
                return
            code = ERROR_PROVIDER_TIMEOUT if is_timeout else ERROR_INTERNAL
            if attempt >= MAX_ATTEMPTS:
                # Final failure: the one place an unexpected exception gets
                # its full traceback (earlier attempts log a one-line warning).
                logger.exception(
                    "request_id=%s stream_ai_response failed attempt=%d/%d elapsed=%.3fs "
                    "exception_type=%s code=%s (mode=%s)",
                    request_id, attempt, MAX_ATTEMPTS, elapsed, type(e).__name__, code, mode
                )
                _telemetry("error_exception", attempt, exc=e, http_status=status_code, error_code=code)
                yield StreamChunk(kind="error", text=GENERIC_CHAT_ERROR, error_code=code)
                return
            logger.warning(
                "request_id=%s stream_ai_response failed before first output attempt=%d/%d "
                "elapsed=%.3fs exception_type=%s code=%s (mode=%s) - will retry",
                request_id, attempt, MAX_ATTEMPTS, elapsed, type(e).__name__, code, mode
            )
            retries_granted += 1
            if is_timeout:
                # Our own client deadline already consumed the wait; adding a
                # backoff on top would only lengthen the student's wait.
                logger.info("request_id=%s retry without extra backoff (client timeout already waited)", request_id)
            else:
                _backoff_before_retry(retries_granted, request_id, "exception_%s" % type(e).__name__)
            continue

    logger.error("request_id=%s stream_ai_response exited the retry loop without a terminal event", request_id)
    yield StreamChunk(kind="error", text=GENERIC_CHAT_ERROR, error_code=ERROR_INTERNAL)
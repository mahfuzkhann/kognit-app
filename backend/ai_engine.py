import os
import io
import json
import logging
import random
import time
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

MODEL_NAME = "gemini-3.6-flash"

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


def generate_ai_response(
    prompt: str,
    mode: str = "direct",
    board: str = "NCTB",
    user_class: str = "SSC",
    stream: str = "Science",
    image_bytes: bytes = None,
    pdf_context: str = "",
    history: list = None
) -> str:
    system_instruction = (
        f"You are Kognit, an expert academic AI tutor for students in {board}, studying {user_class} ({stream} stream).\n"
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
        "not given in order to avoid asking that clarifying question."
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
            return IMAGE_DECODE_ERROR
        contents.append(img)
    logger.info("generate_ai_response timing: image_decode=%.3fs", time.perf_counter() - t_stage_start)

    contents.append(prompt if prompt else "Please analyze this request based on the context.")

    # CHAT-04: history is built once - identical on every retry attempt below.
    gemini_history = _build_gemini_history(history or [])

    for attempt in range(1, MAX_ATTEMPTS + 1):
        t_attempt_start = time.perf_counter()
        attempt_timeout = AI_REQUEST_TIMEOUT_SECONDS if attempt == 1 else RETRY_REQUEST_TIMEOUT_SECONDS

        # Built fresh each attempt (cheap, local - no network call) so the
        # per-attempt timeout below is always the one actually in effect.
        # retry_options is deliberately never set - see "SINGLE RETRY
        # AUTHORITY" comment above.
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            thinking_config=types.ThinkingConfig(thinking_level=CHAT_THINKING_LEVEL),
            http_options=types.HttpOptions(timeout=_seconds_to_ms(attempt_timeout)),
        )

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
                return BLOCKED_RESPONSE_ERROR

            _log_usage(response.usage_metadata, attempt, elapsed, mode, bool(image_bytes), bool(pdf_context))
            return result_text

        except genai_errors.ClientError as e:
            elapsed = time.perf_counter() - t_attempt_start
            if e.code == 429:
                # Bucket C: quota exhaustion. Never retried.
                logger.exception(
                    "generate_ai_response quota exhausted attempt=%d elapsed=%.3fs (mode=%s, board=%s, user_class=%s)",
                    attempt, elapsed, mode, board, user_class
                )
                return QUOTA_EXHAUSTED_ERROR
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
            return GENERIC_CHAT_ERROR

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
            return GENERIC_CHAT_ERROR

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
            return GENERIC_CHAT_ERROR

        except Exception:
            # Bucket E: unexpected/programming error.
            elapsed = time.perf_counter() - t_attempt_start
            logger.exception(
                "generate_ai_response failed unexpectedly attempt=%d elapsed=%.3fs (mode=%s, board=%s, user_class=%s)",
                attempt, elapsed, mode, board, user_class
            )
            return GENERIC_CHAT_ERROR

    # Not reachable (the loop always returns), kept as a defensive fallback.
    return GENERIC_CHAT_ERROR


def generate_quiz_questions(board: str, user_class: str, subject: str, topic: str, count: int = 5) -> list:
    system_instruction = (
        f"You are an exam paper creator for {board}, {user_class}, Subject: {subject}.\n"
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

    try:
        config_kwargs = dict(
            system_instruction=system_instruction,
            http_options=types.HttpOptions(timeout=_seconds_to_ms(AI_REQUEST_TIMEOUT_SECONDS)),
        )
        if QUIZ_THINKING_LEVEL is not None:
            config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=QUIZ_THINKING_LEVEL)

        response = _client.models.generate_content(
            model=MODEL_NAME,
            contents=f"Create {count} MCQ questions on {topic}.",
            config=types.GenerateContentConfig(**config_kwargs),
        )

        raw_text = (response.text or "").strip()
        if "```json" in raw_text:
            raw_text = raw_text.split("```json")[1].split("```")[0].strip()
        elif "```" in raw_text:
            raw_text = raw_text.split("```")[1].split("```")[0].strip()

        return json.loads(raw_text)
    except Exception:
        logger.exception(
            "generate_quiz_questions failed (board=%s, user_class=%s, subject=%s, topic=%s)",
            board, user_class, subject, topic
        )
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
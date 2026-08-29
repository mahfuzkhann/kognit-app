import os
import io
import json
import logging
import time
from typing import Optional
from PIL import Image
import google.generativeai as genai
from google.api_core.exceptions import (
    ResourceExhausted,
    DeadlineExceeded,
    ServiceUnavailable,
    InternalServerError,
    Aborted,
)
from dotenv import load_dotenv

load_dotenv()

genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

logger = logging.getLogger("kognit.ai_engine")

# User-facing fallback messages. Never expose str(exception) to the client -
# that can leak internal details (stack traces, provider error text, etc).
GENERIC_CHAT_ERROR = (
    "Sorry, Kognit couldn't process that request right now. "
    "Please try again in a moment."
)

# Shown specifically when the Gemini API rejects a request due to quota
# exhaustion (google.api_core.exceptions.ResourceExhausted / HTTP 429).
# Never interpolate str(exception) into this - the raw error contains
# provider-internal details (quota metric names, limits, status codes)
# that must not reach the client. See GENERIC_CHAT_ERROR comment above.
QUOTA_EXHAUSTED_ERROR = (
    "⚠️ Kognit-এর AI ব্যবহারের সীমা এই মুহূর্তে পূর্ণ হয়ে গেছে।\n"
    "এই মুহূর্তে আপনার প্রশ্নের উত্তর তৈরি করা যাচ্ছে না। কিছুক্ষণ পরে আবার চেষ্টা করুন।"
)

# Shown when Gemini returns a response with no usable content - almost
# always because its safety filters blocked the prompt or the generated
# candidate (finish_reason != STOP), which raises ValueError when we read
# response.text. This is NOT a network/provider failure, so retrying would
# not help - kept as a distinct, non-retried message so it's diagnosable
# from a student's report without needing server log access.
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

# How long to wait on a single Gemini call before giving up. This is passed
# straight through to the SDK's own request_options timeout (seconds), which
# is the documented/supported way to bound this call in the currently-used
# google-generativeai SDK. MVP-tunable; not env-driven yet since it's a
# single constant used in exactly two places below.
AI_REQUEST_TIMEOUT_SECONDS = 30

# BUG FIX (intermittent "Sorry, Kognit couldn't process..." errors): a single
# Gemini call can transiently fail even when the service is fine overall - a
# timeout under load, a momentary 503/500 from Google's side, etc. Previously
# ANY exception other than ResourceExhausted fell straight through to
# GENERIC_CHAT_ERROR with zero retry, so a one-off blip looked identical to a
# real failure to the student on their very first question. These four are
# the documented transient/retryable exception types in
# google.api_core.exceptions (504/503/500/409). Retrying once after a short
# pause resolves the large majority of these without the student ever seeing
# an error. Deliberately NOT applied to ResourceExhausted (quota exhaustion
# will not clear in 1.5s) or to the blocked-response case below (retrying an
# identical request that got safety-blocked just wastes another API call).
RETRYABLE_EXCEPTIONS = (DeadlineExceeded, ServiceUnavailable, InternalServerError, Aborted)
MAX_ATTEMPTS = 2  # 1 initial attempt + 1 retry
RETRY_DELAY_SECONDS = 1.5

# Kognit's internal message roles -> the Gemini SDK's expected chat-history
# roles. Gemini's start_chat(history=...) requires "user"/"model"; Kognit
# stores assistant turns as "bot". This mapping must stay in sync with
# whatever role strings the frontend sends in the "history" field.
_KOGNIT_ROLE_TO_GEMINI_ROLE = {"user": "user", "bot": "model"}


def _build_gemini_history(history: list) -> list:
    """
    Convert Kognit's validated [{"role": "user"|"bot", "text": str}, ...]
    history into the Gemini SDK's expected
    [{"role": "user"|"model", "parts": [str]}, ...] shape.

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
        gemini_history.append({"role": gemini_role, "parts": [text]})
    return gemini_history


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
        system_instruction += f"\n\n[UPLOADED PDF DOCUMENT CONTENT CONTEXT]:\n{pdf_context[:10000]}" # Truncate if too long for safety

    if mode == "socratic":
        system_instruction += " DO NOT give direct answers immediately. Guide the student step-by-step using helpful questions!"

    # LATENCY/RELIABILITY FIX: image decoding used to happen here, outside
    # any try/except in this function. main.py's own broad except Exception
    # around the whole call still caught a corrupted/unreadable image (so
    # this was never a true "no response" bug), but it produced the same
    # generic GENERIC_CHAT_ERROR-style message as an unrelated AI/network
    # failure, made troubleshooting a "my answer failed" report ambiguous,
    # and this step was never timed. Now it's wrapped, timed, and returns a
    # distinct message so image-decode failures are diagnosable separately
    # from AI-provider failures in the logs.
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
        try:
            model = genai.GenerativeModel("gemini-3.6-flash", system_instruction=system_instruction)

            # CHAT-04 fix: use the SDK's multi-turn chat session instead of a
            # stateless single-shot generate_content call, so prior turns in
            # THIS chat are actually part of the request Gemini sees. history
            # is empty on a chat's first message, which is equivalent to the
            # old behavior. system_instruction (board/class/stream/PDF context/
            # mode) is unchanged - it's baked into the model above and applies
            # across the whole chat session automatically.
            chat_session = model.start_chat(history=gemini_history)
            response = chat_session.send_message(
                contents,
                request_options={"timeout": AI_REQUEST_TIMEOUT_SECONDS},
            )
            result_text = response.text
            logger.info(
                "generate_ai_response timing: gemini_call attempt=%d elapsed=%.3fs (mode=%s, has_image=%s, has_pdf=%s)",
                attempt, time.perf_counter() - t_attempt_start, mode, bool(image_bytes), bool(pdf_context)
            )
            return result_text
        except ResourceExhausted:
            logger.exception(
                "generate_ai_response quota exhausted (mode=%s, board=%s, user_class=%s)",
                mode, board, user_class
            )
            return QUOTA_EXHAUSTED_ERROR
        except RETRYABLE_EXCEPTIONS as e:
            if attempt < MAX_ATTEMPTS:
                logger.warning(
                    "generate_ai_response transient error on attempt %d/%d elapsed=%.3fs (mode=%s, board=%s, user_class=%s): %s",
                    attempt, MAX_ATTEMPTS, time.perf_counter() - t_attempt_start, mode, board, user_class, type(e).__name__
                )
                time.sleep(RETRY_DELAY_SECONDS)
                continue
            logger.exception(
                "generate_ai_response failed after %d attempts (mode=%s, board=%s, user_class=%s)",
                MAX_ATTEMPTS, mode, board, user_class
            )
            return GENERIC_CHAT_ERROR
        except ValueError:
            # response.text raises ValueError when Gemini returns no usable
            # candidate/part - almost always a safety-filter block, not a
            # transient failure. Not retried: an identical request would
            # just get blocked again.
            logger.exception(
                "generate_ai_response got a blocked/empty response (mode=%s, board=%s, user_class=%s)",
                mode, board, user_class
            )
            return BLOCKED_RESPONSE_ERROR
        except Exception:
            logger.exception(
                "generate_ai_response failed (mode=%s, board=%s, user_class=%s)",
                mode, board, user_class
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
        model = genai.GenerativeModel("gemini-3.6-flash", system_instruction=system_instruction)
        response = model.generate_content(
            f"Create {count} MCQ questions on {topic}.",
            request_options={"timeout": AI_REQUEST_TIMEOUT_SECONDS},
        )
        
        raw_text = response.text.strip()
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
        model = genai.GenerativeModel("gemini-3.6-flash", system_instruction=TITLE_SYSTEM_INSTRUCTION)
        response = model.generate_content(
            f"Conversation (board: {board}):\n{convo_text}\n\nTitle:",
            request_options={"timeout": 10},
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
        return []
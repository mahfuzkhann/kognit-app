import json
import logging
import os
import time
import httpx
from fastapi import FastAPI, Request, Form, File, UploadFile, Header, HTTPException, Depends
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from backend.ai_engine import generate_ai_response, generate_quiz_questions, generate_chat_title
from backend.rag_engine import extract_text_from_pdf, PDFExtractionError
from typing import Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("kognit.main")

app = FastAPI(title="Kognit Academic Assistant")

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ---------------------------------------------------------------------------
# Config constants (MVP-tunable; move to env vars if these need to change
# per-deployment rather than per-code-change).
# ---------------------------------------------------------------------------
MAX_PDF_UPLOAD_SIZE_BYTES = 15 * 1024 * 1024  # 15MB - adjust after testing real student PDFs
ALLOWED_PDF_CONTENT_TYPES = {"application/pdf"}
MAX_IMAGE_UPLOAD_SIZE_BYTES = 8 * 1024 * 1024  # 8MB - adjust after testing real student photos

# CHAT-04 fix: bounded same-chat conversation history. MVP window = 10
# user+assistant exchanges = 20 messages max. Enforced server-side too
# (never trust the client actually capped it - frontend caps here as well,
# but this is the authoritative limit). Each message's text is also capped
# defensively so one oversized entry can't blow up the request.
MAX_HISTORY_MESSAGES = 20
MAX_HISTORY_MESSAGE_CHARS = 4000
VALID_HISTORY_ROLES = {"user", "bot"}

# ---------------------------------------------------------------------------
# Supabase JWT verification (P0 security fix).
#
# MVP APPROACH: verification is delegated to Supabase's own Auth API
# (GET /auth/v1/user) instead of verifying the JWT signature locally. This
# avoids having to manage a JWT secret or a JWKS cache in this backend -
# Supabase itself confirms token validity, expiry, and revocation. The
# trade-off is one extra network call to Supabase per authenticated
# request, which is acceptable at current (20-50 user) beta scale.
#
# SUPABASE_ANON_KEY here is the same publishable/anon key already used by
# the frontend (static/js/app.js) - it is not a secret by design, but is
# still read from an environment variable for consistency and easier
# per-deployment configuration.
# ---------------------------------------------------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
SUPABASE_AUTH_TIMEOUT_SECONDS = 10

# ---------------------------------------------------------------------------
# PDF context store.
#
# BUG 2 FIX: previously keyed only by the verified authenticated Supabase
# user id, which meant every chat belonging to one account shared a single
# PDF context - uploading a PDF in Chat A made it visible to Chat B, C, etc.
# for that same user. Now keyed by BOTH the verified user id AND the
# client-supplied chat_id:
#
#   active_pdf_contexts = {
#       user_id: {
#           chat_id: pdf_text,
#           ...
#       },
#       ...
#   }
#
# The outer key (user_id) always comes from get_current_user_id (the
# verified JWT) - never from a client-supplied value. The inner key
# (chat_id) is client-supplied, but since it only ever selects a bucket
# INSIDE that user's own dict, a client can never use chat_id to read or
# clear another user's context - it can only address its own chats. Do not
# key this dict differently without updating all three call sites below
# (upload_pdf, chat_endpoint, clear_pdf_endpoint).
# ---------------------------------------------------------------------------
active_pdf_contexts = {}


async def get_current_user_id(authorization: Optional[str] = Header(None)) -> str:
    """
    FastAPI dependency: verifies the caller's Supabase access token and
    returns the authenticated Supabase user id.

    Never trust a client-supplied user_id anywhere in this file - the only
    trusted source of user identity in this backend is the return value of
    this function. Missing, malformed, invalid, or expired tokens all
    result in HTTP 401.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header.")

    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token.")

    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        # Server misconfiguration, not a client error - fail closed rather
        # than silently letting requests through unauthenticated.
        logger.error("SUPABASE_URL/SUPABASE_ANON_KEY not configured - cannot verify tokens.")
        raise HTTPException(status_code=500, detail="Authentication is not configured on the server.")

    try:
        async with httpx.AsyncClient(timeout=SUPABASE_AUTH_TIMEOUT_SECONDS) as client:
            resp = await client.get(
                f"{SUPABASE_URL}/auth/v1/user",
                headers={
                    "Authorization": f"Bearer {token}",
                    "apikey": SUPABASE_ANON_KEY,
                },
            )
    except httpx.RequestError:
        # Never log the token itself - only that verification failed.
        logger.exception("Error contacting Supabase Auth API for token verification")
        raise HTTPException(status_code=503, detail="Could not verify authentication right now. Please try again.")

    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid or expired session. Please log in again.")

    try:
        user_id = resp.json()["id"]
    except (ValueError, KeyError, TypeError):
        logger.error("Supabase /auth/v1/user returned an unexpected payload shape")
        raise HTTPException(status_code=401, detail="Invalid or expired session. Please log in again.")

    return user_id


@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")

@app.post("/api/pdf/upload")
async def upload_pdf(
    file: UploadFile = File(...),
    chat_id: str = Form(...),
    user_id: str = Depends(get_current_user_id)
):
    # BUG 2 FIX: chat_id is required here specifically (unlike the softer
    # handling in chat_endpoint below) because an upload with no chat to
    # attach it to is meaningless - there is nothing safe to fall back to.
    if not chat_id or not chat_id.strip():
        raise HTTPException(status_code=400, detail="chat_id is required to upload a PDF.")

    is_pdf_content_type = file.content_type in ALLOWED_PDF_CONTENT_TYPES
    is_pdf_extension = (file.filename or "").lower().endswith(".pdf")
    if not (is_pdf_content_type or is_pdf_extension):
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")

    file_bytes = await file.read()
    if len(file_bytes) > MAX_PDF_UPLOAD_SIZE_BYTES:
        max_mb = MAX_PDF_UPLOAD_SIZE_BYTES // (1024 * 1024)
        raise HTTPException(status_code=413, detail=f"PDF exceeds the {max_mb}MB limit.")

    try:
        pdf_text = extract_text_from_pdf(file_bytes)
    except PDFExtractionError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception("Unexpected error extracting PDF %s", file.filename)
        raise HTTPException(status_code=500, detail="Could not process this PDF right now.")

    # BUG 2 FIX: stored under this user's own chat_id bucket only - never
    # overwrites or is visible to any other chat_id, for this user or any
    # other.
    active_pdf_contexts.setdefault(user_id, {})[chat_id] = pdf_text
    return {"status": "success", "filename": file.filename, "length": len(pdf_text)}


@app.post("/api/pdf/clear")
async def clear_pdf_endpoint(
    chat_id: str = Form(...),
    user_id: str = Depends(get_current_user_id)
):
    """
    BUG 2 FIX: previously there was no server-side clear at all - the
    frontend's clearPDFContext() only hid the UI, so a "removed" PDF kept
    answering questions for that chat until the process restarted. This
    endpoint actually drops that chat's entry from the authenticated user's
    own bucket. Safe/no-op if no PDF context exists for that chat_id (e.g.
    double-clicking clear, or clearing a chat that never had a PDF) - never
    raises for that case.
    """
    if not chat_id or not chat_id.strip():
        raise HTTPException(status_code=400, detail="chat_id is required to clear a PDF.")

    active_pdf_contexts.get(user_id, {}).pop(chat_id, None)
    return {"status": "success"}

def parse_and_validate_history(raw_history: str) -> list:
    """
    Parse and validate the client-supplied conversation history for CHAT-04.

    Never trusts the client blindly: malformed JSON, wrong types, unknown
    roles, or missing fields cause that entry (or the whole payload) to be
    dropped rather than passed through. Also re-enforces the message-count
    and per-message length caps server-side, independent of whatever the
    frontend already did.

    Returns a list of {"role": "user"|"bot", "text": str} dicts - never
    raises. On any failure, returns an empty list so /api/chat still works
    with no history rather than erroring out the whole request.
    """
    if not raw_history:
        return []

    try:
        parsed = json.loads(raw_history)
    except (json.JSONDecodeError, TypeError):
        logger.warning("chat history: invalid JSON received, ignoring history for this request")
        return []

    if not isinstance(parsed, list):
        logger.warning("chat history: expected a JSON array, got %s, ignoring", type(parsed).__name__)
        return []

    validated = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        role = entry.get("role")
        text = entry.get("text")
        if role not in VALID_HISTORY_ROLES:
            continue
        if not isinstance(text, str) or not text.strip():
            continue
        validated.append({
            "role": role,
            "text": text[:MAX_HISTORY_MESSAGE_CHARS],
        })

    # Re-enforce the cap server-side regardless of what the client sent.
    return validated[-MAX_HISTORY_MESSAGES:]


@app.post("/api/chat")
async def chat_endpoint(
    prompt: str = Form(...),
    mode: str = Form("direct"),
    board: str = Form("NCTB Bangla Medium"),
    user_class: str = Form("Class 9-10 / SSC"),
    stream: str = Form("Science"),
    image: Optional[UploadFile] = File(None),
    history: str = Form("[]"),
    # BUG 2 FIX: identifies which chat this message belongs to, so PDF
    # context can be looked up per-chat instead of per-user. Deliberately
    # NOT required (default "") rather than Form(...) - a normal text/image
    # chat message must keep working even if chat_id is ever missing (e.g.
    # a stale cached frontend mid-rollout); it just won't be able to pull
    # any PDF context in that case, which fails safe rather than failing
    # the whole request. PDF upload/clear below DO require it, since those
    # actions are meaningless without a chat to attach to.
    chat_id: str = Form(""),
    user_id: str = Depends(get_current_user_id)
):
    # Read and validate the image BEFORE the try/except below. HTTPException
    # is a subclass of Exception, so raising it inside that broad handler
    # would get swallowed into a generic 200 "reply" - it needs to propagate
    # untouched so the client actually sees a real 413.
    image_bytes = None
    if image:
        image_bytes = await image.read()
        if len(image_bytes) > MAX_IMAGE_UPLOAD_SIZE_BYTES:
            max_mb = MAX_IMAGE_UPLOAD_SIZE_BYTES // (1024 * 1024)
            raise HTTPException(status_code=413, detail=f"Image exceeds the {max_mb}MB limit.")

    # LATENCY INSTRUMENTATION: request-side stages timed separately from the
    # AI call itself (which ai_engine.py times internally in more detail -
    # image decode vs the actual Gemini call). This does not change any
    # behavior; it is server-side logging only, nothing is exposed to the
    # client. Goal: distinguish "request handling is slow" from "Gemini is
    # slow" from "the frontend is slow to render" (the last of those cannot
    # be measured from the backend - it would need client-side timing, which
    # is a separate, frontend-only change not made here).
    t_request_start = time.perf_counter()
    try:
        if not chat_id:
            logger.warning("chat_endpoint: request received with no chat_id - proceeding without PDF context")
        pdf_context = active_pdf_contexts.get(user_id, {}).get(chat_id, "") if chat_id else ""
        conversation_history = parse_and_validate_history(history)
        t_after_parsing = time.perf_counter()

        # generate_ai_response is a synchronous, blocking call (it calls the
        # Gemini SDK directly). Running it in FastAPI's threadpool keeps it
        # off the main async event loop, so one slow/stuck AI call no longer
        # blocks every other concurrent request. ai_engine.py itself is
        # unchanged - this is purely how main.py invokes it.
        response = await run_in_threadpool(
            generate_ai_response,
            prompt=prompt,
            mode=mode,
            board=board,
            user_class=user_class,
            stream=stream,
            image_bytes=image_bytes,
            pdf_context=pdf_context,
            history=conversation_history
        )
        t_after_ai = time.perf_counter()
        logger.info(
            "chat_endpoint timing: parse=%.3fs ai_total=%.3fs request_total=%.3fs "
            "(mode=%s, has_image=%s, has_pdf=%s, history_len=%d, chat_id=%s)",
            t_after_parsing - t_request_start,
            t_after_ai - t_after_parsing,
            t_after_ai - t_request_start,
            mode, bool(image_bytes), bool(pdf_context), len(conversation_history), chat_id or "(none)"
        )
        return {"reply": response}
    except Exception:
        logger.exception(
            "Unexpected error in /api/chat after %.3fs",
            time.perf_counter() - t_request_start
        )
        return {"reply": "Sorry, something went wrong handling your request. Please try again."}

@app.post("/api/chat/title")
async def chat_title_endpoint(
    history: str = Form("[]"),
    board: str = Form("NCTB Bangla Medium"),
    user_id: str = Depends(get_current_user_id)
):
    """
    FEATURE 2: generates a short, context-aware title for a chat from its
    recent message history. Requires auth like every other endpoint here,
    but is intentionally NOT tied to active_pdf_contexts or any other
    per-request state - it is a small, side-channel call the frontend makes
    at most once per chat (see maybeGenerateAiTitle() in app.js), not part
    of the main answer path. Reuses the exact same history validation as
    /api/chat so this endpoint cannot be used to smuggle an oversized or
    malformed payload past the caps enforced there.
    """
    conversation_history = parse_and_validate_history(history)
    if not conversation_history:
        return {"title": None}

    title = await run_in_threadpool(generate_chat_title, history=conversation_history, board=board)
    return {"title": title}


@app.post("/api/quiz/generate")
async def quiz_generate_endpoint(
    board: str = Form("NCTB Bangla Medium"),
    user_class: str = Form("Class 9-10 / SSC"),
    subject: str = Form("Science"),
    topic: str = Form("General Practice"),
    count: int = Form(5),
    user_id: str = Depends(get_current_user_id)
):
    # Same blocking-call issue as /api/chat, same fix: offload to the
    # threadpool so this request doesn't block the event loop either.
    questions = await run_in_threadpool(
        generate_quiz_questions,
        board=board,
        user_class=user_class,
        subject=subject,
        topic=topic,
        count=count
    )
    return {"questions": questions}
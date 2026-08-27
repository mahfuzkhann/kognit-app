import json
import logging
import os
import httpx
from fastapi import FastAPI, Request, Form, File, UploadFile, Header, HTTPException, Depends
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from backend.ai_engine import generate_ai_response, generate_quiz_questions
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
# Keyed by the verified authenticated Supabase user id (see
# get_current_user_id below) - NOT by an anonymous browser/session cookie.
# The previous anonymous-cookie design has been removed now that every
# endpoint that reads/writes this dict requires authentication. Do not key
# this dict by anything other than the verified user_id without updating
# both call sites below (upload_pdf, chat_endpoint).
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
async def upload_pdf(file: UploadFile = File(...), user_id: str = Depends(get_current_user_id)):
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

    active_pdf_contexts[user_id] = pdf_text
    return {"status": "success", "filename": file.filename, "length": len(pdf_text)}

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

    try:
        pdf_context = active_pdf_contexts.get(user_id, "")
        conversation_history = parse_and_validate_history(history)

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
        return {"reply": response}
    except Exception:
        logger.exception("Unexpected error in /api/chat")
        return {"reply": "Sorry, something went wrong handling your request. Please try again."}

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
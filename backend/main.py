import logging
import os
import secrets
from fastapi import FastAPI, Request, Form, File, UploadFile, Response, Header, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from backend.ai_engine import generate_ai_response, generate_quiz_questions
from backend.rag_engine import extract_text_from_pdf, PDFExtractionError
from typing import Optional
import json

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
SESSION_COOKIE_NAME = "kognit_session"

# Cookies should not require Secure (HTTPS-only) in local HTTP development,
# but should require it in production. Default is "false" so local dev keeps
# working out of the box; set SESSION_COOKIE_SECURE=true in production
# (behind HTTPS) via environment variable, no code change needed.
SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "false").strip().lower() == "true"

# ---------------------------------------------------------------------------
# PDF context store.
#
# TEMPORARY MVP DESIGN: keyed by an anonymous per-browser session id (a random
# token set as an HTTP-only cookie), NOT by hardcoded key and NOT by
# authenticated user identity. This fixes the cross-user leak (every visitor
# used to share one global "current_pdf" slot) without requiring the backend
# auth/JWT-verification work that hasn't been built yet.
#
# NEXT STEP (not implemented here): once backend Supabase JWT verification
# exists, replace `get_session_id(request)` below with a function that
# derives the key from the verified authenticated user id instead of an
# anonymous cookie. Everything downstream (the dict itself, its usage in
# upload_pdf/chat_endpoint) stays the same shape - only the key source
# changes. Do not key this dict by anything else without updating both
# call sites below.
# ---------------------------------------------------------------------------
active_pdf_contexts = {}


def get_session_id(request: Request) -> str:
    """
    Return this request's session id. Set by ensure_session_cookie below on
    request.state so it's available even on a brand-new browser's very first
    request (before the Set-Cookie header has round-tripped).
    """
    return request.state.session_id


@app.middleware("http")
async def ensure_session_cookie(request: Request, call_next):
    """
    Ensure every browser has a stable, random session id.
    This id is ONLY used to scope in-memory PDF context per-browser; it is
    not an authentication mechanism and grants no privileges.
    """
    existing = request.cookies.get(SESSION_COOKIE_NAME)
    session_id = existing or secrets.token_hex(16)
    request.state.session_id = session_id

    response = await call_next(request)

    if not existing:
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=session_id,
            httponly=True,
            samesite="lax",
            secure=SESSION_COOKIE_SECURE,
        )
    return response

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")

@app.post("/api/pdf/upload")
async def upload_pdf(request: Request, file: UploadFile = File(...)):
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

    session_id = get_session_id(request)
    active_pdf_contexts[session_id] = pdf_text
    return {"status": "success", "filename": file.filename, "length": len(pdf_text)}

@app.post("/api/chat")
async def chat_endpoint(
    request: Request,
    prompt: str = Form(...),
    mode: str = Form("direct"),
    board: str = Form("NCTB Bangla Medium"),
    user_class: str = Form("Class 9-10 / SSC"),
    stream: str = Form("Science"),
    image: Optional[UploadFile] = File(None),
    authorization: Optional[str] = Header(None)
):
    # authorization header থেকে পাওয়া jwt token যাচাই করার কোড থাকবে
    # (backend auth/JWT verification is a separate, not-yet-approved step)

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
        session_id = get_session_id(request)
        pdf_context = active_pdf_contexts.get(session_id, "")

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
            pdf_context=pdf_context
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
    count: int = Form(5)
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

@app.post("/api/export/markdown")
async def export_markdown_endpoint(
    title: str = Form("Kognit_Study_Note"),
    chat_data: str = Form("[]")
):
    try:
        messages = json.loads(chat_data)
        md_content = f"# {title}\n\n*Generated by Kognit AI Assistant*\n\n---\n\n"

        for msg in messages:
            role_title = "👤 User" if msg.get("role") == "user" else "🤖 Kognit AI"
            text = msg.get("text", "")
            md_content += f"### {role_title}\n{text}\n\n---\n\n"

        headers = {
            "content-disposition": f'attachment; filename={title.replace(" ", "_")}.md'
        }
        return Response(content=md_content, media_type="text/markdown", headers=headers)
    except Exception:
        logger.exception("Unexpected error exporting markdown (title=%s)", title)
        return Response(
            content="Sorry, something went wrong exporting this chat. Please try again.",
            status_code=500,
        )
import os
import io
import json
import logging
from PIL import Image
import google.generativeai as genai
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

# How long to wait on a single Gemini call before giving up. This is passed
# straight through to the SDK's own request_options timeout (seconds), which
# is the documented/supported way to bound this call in the currently-used
# google-generativeai SDK. MVP-tunable; not env-driven yet since it's a
# single constant used in exactly two places below.
AI_REQUEST_TIMEOUT_SECONDS = 30

def generate_ai_response(
    prompt: str, 
    mode: str = "direct", 
    board: str = "NCTB", 
    user_class: str = "SSC", 
    stream: str = "Science",
    image_bytes: bytes = None,
    pdf_context: str = ""
) -> str:
    system_instruction = (
        f"You are Kognit, an expert academic AI tutor for students in {board}, studying {user_class} ({stream} stream).\n"
        "STRICT ACADEMIC & VISION RULES:\n"
        "1. IMAGE ANALYSIS: If an image is provided, carefully read handwritten questions, printed equations, or diagrams. Solve step-by-step.\n"
        "2. PDF CONTEXT: If a PDF document text context is provided below, prioritize answering questions based on that document content.\n"
        "3. HYPER-LOCAL CQ FORMAT: When answering Creative Questions (সৃজনশীল) or solutions, strictly format using (ক) জ্ঞানমূলক, (খ) অনুধাবনমূলক, (গ) প্রয়োগমূলক, and (ঘ) উচ্চতর দক্ষতার standard exam rules.\n"
        "4. FORMULA NOTATION: Wrap inline math in $ ... $ and main equations in $$ ... $$.\n"
        "5. Tone must be encouraging, clear, precise, and aligned with the student's curriculum."
    )
    
    if pdf_context:
        system_instruction += f"\n\n[UPLOADED PDF DOCUMENT CONTENT CONTEXT]:\n{pdf_context[:10000]}" # Truncate if too long for safety

    if mode == "socratic":
        system_instruction += " DO NOT give direct answers immediately. Guide the student step-by-step using helpful questions!"

    try:
        model = genai.GenerativeModel("gemini-3.6-flash", system_instruction=system_instruction)
        
        contents = []
        if image_bytes:
            img = Image.open(io.BytesIO(image_bytes))
            contents.append(img)
            
        contents.append(prompt if prompt else "Please analyze this request based on the context.")

        response = model.generate_content(
            contents,
            request_options={"timeout": AI_REQUEST_TIMEOUT_SECONDS},
        )
        return response.text
    except Exception:
        logger.exception(
            "generate_ai_response failed (mode=%s, board=%s, user_class=%s)",
            mode, board, user_class
        )
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
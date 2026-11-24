"""
Kloriz AI-content reports - pure domain logic.

This module has NO network access, NO database access and NO FastAPI imports. It
only decides what a valid report looks like and normalises it. backend/main.py
calls it for the HTTP layer; backend/database.py stores the result through the
SECURITY DEFINER function submit_content_report() defined in
supabase/migrations/0018_content_reports.sql.

LIMITS ARE DUPLICATED IN THAT MIGRATION (as CHECK constraints) on purpose -
defence in depth. If you change a limit or a category here, change it there too
(test_content_reports.py compares them).

WHAT THE CLIENT MAY SEND, AND WHAT IT MAY NOT
  May:   category, note, ai_response (the answer being reported), prompt (the
         question that produced it), request_id (the X-Request-ID the server
         returned for that answer).
  Never: user_id, status, created_at, platform, app_version. The reporter comes
         from the verified access token; platform and version come from the
         server-read X-Client-Version header; status is forced to "new" by the
         database. Any such field in the body is ignored by construction because
         build_report() reads only the allow-listed keys.

PRIVACY
  Excerpts are cut to a hard length, so a report can never carry a whole chat.
  Nothing from a report (note, excerpts) is ever written to a log line.
"""

import re
import unicodedata
from typing import Any, Dict, Optional

CATEGORIES = ("unsafe", "inappropriate", "inaccurate", "privacy", "other")

MAX_NOTE_CHARS = 500
MAX_RESPONSE_EXCERPT_CHARS = 2000
MAX_PROMPT_EXCERPT_CHARS = 1000
MAX_REQUEST_BODY_BYTES = 32 * 1024
# Rolling 24-hour cap per account. Enforced in the database; this constant exists
# so the HTTP layer can name the number in its message.
MAX_REPORTS_PER_DAY = 20

PLATFORMS = ("web", "android")
DEFAULT_PLATFORM = "web"

REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
APP_VERSION_RE = re.compile(r"^[0-9A-Za-z._+-]{1,32}$")

# Same hygiene as share_engine: control characters (except tab/newline/CR) and
# bidi overrides have no place in a report and NUL breaks Postgres text.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BIDI_RE = re.compile(r"[\u202a-\u202e\u2066-\u2069]")


class ReportValidationError(ValueError):
    """A report the server refuses. `code` is stable for the frontend; `message`
    is safe to show a student and never echoes their input."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _clean(value: Any) -> str:
    """Strings only; NFC; strips control/bidi characters; trims. Non-strings
    become empty rather than being coerced (a number or object is not text)."""
    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFC", value)
    text = _CONTROL_RE.sub("", text)
    text = _BIDI_RE.sub("", text)
    return text.strip()


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip()


def build_report(body: Any, client_version: Optional[str] = None) -> Dict[str, Optional[str]]:
    """
    Validates a parsed JSON body and returns exactly the parameters of
    submit_content_report(), keyed by the function's argument names.

    Raises ReportValidationError for: missing/unknown category, a note over the
    limit, a missing answer excerpt. Over-long excerpts are TRUNCATED, not
    refused (the student reported a long answer; refusing would silently drop a
    safety report). A malformed request_id is dropped, never refused, for the
    same reason.

    `client_version` is the raw X-Client-Version header ("android/1.2.3") or
    None; it is parsed here, never trusted beyond platform/version labelling.
    """
    if not isinstance(body, dict):
        raise ReportValidationError("REPORT_INVALID", "Invalid report.")

    category = _clean(body.get("category")).lower()
    if category not in CATEGORIES:
        raise ReportValidationError("REPORT_INVALID_CATEGORY", "Please choose a reason for your report.")

    note = _clean(body.get("note"))
    if len(note) > MAX_NOTE_CHARS:
        raise ReportValidationError(
            "REPORT_NOTE_TOO_LONG", f"Please keep your note under {MAX_NOTE_CHARS} characters."
        )

    ai_response = _truncate(_clean(body.get("ai_response")), MAX_RESPONSE_EXCERPT_CHARS)
    if not ai_response:
        raise ReportValidationError("REPORT_MISSING_CONTENT", "There is no answer to report.")

    prompt = _truncate(_clean(body.get("prompt")), MAX_PROMPT_EXCERPT_CHARS)

    request_id = _clean(body.get("request_id"))
    if not REQUEST_ID_RE.match(request_id):
        request_id = ""

    platform, app_version = parse_client(client_version)

    return {
        "p_category": category,
        "p_note": note or None,
        "p_ai_response": ai_response,
        "p_prompt": prompt or None,
        "p_request_id": request_id or None,
        "p_platform": platform,
        "p_app_version": app_version,
    }


def parse_client(header_value: Optional[str]):
    """'android/1.2.3' -> ('android', '1.2.3'). Anything else -> ('web', None).
    The header is a label for reviewers, not a credential."""
    if not isinstance(header_value, str):
        return DEFAULT_PLATFORM, None
    m = re.match(r"^([a-z]{2,16})/([0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4})$", header_value.strip().lower())
    if not m or m.group(1) not in PLATFORMS:
        return DEFAULT_PLATFORM, None
    return m.group(1), m.group(2)
"""
Kloriz chat sharing - pure domain logic.

This module has NO network access, NO database access, NO AI/provider calls
and NO FastAPI imports. It only decides:

  * what a shareable snapshot of a chat looks like (and normalizes it),
  * what the public page is allowed to receive back.

backend/main.py calls it for the HTTP layer and backend/database.py stores the
result. Keeping it pure is what makes every rule below directly unit-testable
(see test_share_engine.py).

WHAT SHARING IS
  An opt-in, frozen COPY of one chat. The student previews it and presses
  "Create link"; the link never points at the real chat or project. See
  supabase/migrations/0013_shared_chats.sql for the storage and RLS side.

WHAT A SNAPSHOT CONTAINS - AND WHAT IT NEVER CONTAINS
  Contains:  a title, and for each message only its role ("user" or "bot") and
             its text.
  Never:     images or attachments, PDF text, research/source metadata, user
             ids, emails, names, timestamps, the chat/project ids, quiz data,
             profile data, or any other message field the browser may hold.
  The allow-list below is the whole contract: anything not named is dropped by
  construction, so a future field added to the in-browser chat object can not
  leak into a share by accident.

LIMITS ARE DUPLICATED IN supabase/migrations/0013_shared_chats.sql (as CHECK
constraints and a trigger) on purpose - defence in depth. If you change a limit
here, change it there too.
"""

import re
import unicodedata
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

MAX_SHARE_MESSAGES = 200
MAX_MESSAGE_CHARS = 20000
MAX_TITLE_CHARS = 120
# Total text budget. The DB CHECK is 500,000 on snapshot::text, which includes
# JSON punctuation and escapes, so the text budget is deliberately lower.
MAX_SNAPSHOT_TEXT_CHARS = 300000
MAX_SHARES_PER_USER = 50

DEFAULT_TITLE = "Shared chat"
ALLOWED_ROLES = ("user", "bot")

CHAT_REF_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
TOKEN_RE = re.compile(r"^[0-9a-f]{40}$")

# Control characters (except tab / newline / carriage return) have no business
# in a message and are a classic way to smuggle odd rendering. NUL breaks Postgres.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BIDI_RE = re.compile(r"[\u202a-\u202e\u2066-\u2069]")
_WHITESPACE_RUN_RE = re.compile(r"\s+")


class ShareValidationError(ValueError):
    """
    A request that can not become a snapshot. `code` is a stable machine value
    the frontend maps to its own wording; `message` is a short English note for
    logs / API clients and never contains user content.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Text cleaning
# ---------------------------------------------------------------------------

def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFC", value)
    text = _CONTROL_RE.sub("", text)
    text = _BIDI_RE.sub("", text)
    return text.strip()


def clean_title(value: Any) -> str:
    """Single-line title, 1..MAX_TITLE_CHARS chars, never empty."""
    text = _clean_text(value)
    text = _WHITESPACE_RUN_RE.sub(" ", text).strip()
    if len(text) > MAX_TITLE_CHARS:
        text = text[: MAX_TITLE_CHARS - 1].rstrip() + "\u2026"
    return text or DEFAULT_TITLE


def is_valid_chat_ref(value: Any) -> bool:
    return isinstance(value, str) and bool(CHAT_REF_RE.match(value))


def is_valid_token(value: Any) -> bool:
    return isinstance(value, str) and bool(TOKEN_RE.match(value))


# ---------------------------------------------------------------------------
# Snapshot construction
# ---------------------------------------------------------------------------

def build_snapshot(chat_ref: Any, title: Any, messages: Any) -> Dict[str, Any]:
    """
    Validate a share request and return the record to store:

        {"chat_ref", "title", "snapshot": [{"role","text"}...], "message_count"}

    Raises ShareValidationError. Nothing is silently truncated: a chat that is
    too big to share is refused with TOO_LONG so the preview the student
    approved is exactly what gets stored.
    """
    if not is_valid_chat_ref(chat_ref):
        raise ShareValidationError("INVALID_CHAT", "invalid chat reference")
    if not isinstance(messages, list):
        raise ShareValidationError("INVALID_MESSAGES", "messages must be a list")

    snapshot: List[Dict[str, str]] = []
    total_chars = 0

    for raw in messages:
        if not isinstance(raw, dict):
            continue
        role = raw.get("role")
        if role not in ALLOWED_ROLES:
            continue
        # ONLY "role" and "text" are ever read from a message. Images and every
        # other field of the browser's chat object are ignored here by design.
        text = _clean_text(raw.get("text"))
        if not text:
            continue  # e.g. an image-only message: nothing shareable remains
        if len(text) > MAX_MESSAGE_CHARS:
            raise ShareValidationError("TOO_LONG", "a message is too long to share")
        total_chars += len(text)
        if total_chars > MAX_SNAPSHOT_TEXT_CHARS:
            raise ShareValidationError("TOO_LONG", "chat is too long to share")
        snapshot.append({"role": role, "text": text})
        if len(snapshot) > MAX_SHARE_MESSAGES:
            raise ShareValidationError("TOO_LONG", "chat has too many messages to share")

    if not snapshot:
        raise ShareValidationError("EMPTY", "nothing to share")

    return {
        "chat_ref": chat_ref,
        "title": clean_title(title),
        "snapshot": snapshot,
        "message_count": len(snapshot),
    }


# ---------------------------------------------------------------------------
# Public payload (what the unauthenticated page may receive)
# ---------------------------------------------------------------------------

def public_payload(rpc_result: Any) -> Optional[Dict[str, Any]]:
    """
    Re-shape whatever the database function returned into the exact, minimal
    payload the public page gets. Returns None for anything unexpected, so a
    malformed or hostile row can never be passed through verbatim.
    Only: title, messages[{role,text}], message_count, created_at.
    """
    if not isinstance(rpc_result, dict):
        return None
    raw_messages = rpc_result.get("snapshot")
    if not isinstance(raw_messages, list) or not raw_messages:
        return None

    messages: List[Dict[str, str]] = []
    for item in raw_messages[:MAX_SHARE_MESSAGES]:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        text = item.get("text")
        if role in ALLOWED_ROLES and isinstance(text, str) and text:
            messages.append({"role": role, "text": text})
    if not messages:
        return None

    created_at = rpc_result.get("created_at")
    return {
        "title": clean_title(rpc_result.get("title")),
        "messages": messages,
        "message_count": len(messages),
        "created_at": created_at if isinstance(created_at, str) else None,
    }


def owner_view(row: Any) -> Optional[Dict[str, Any]]:
    """The owner's own view of an active share (needs the token to rebuild the URL)."""
    if not isinstance(row, dict):
        return None
    token = row.get("share_token")
    if not is_valid_token(token):
        return None
    return {
        "token": token,
        "title": clean_title(row.get("title")),
        "message_count": row.get("message_count") if isinstance(row.get("message_count"), int) else None,
        "created_at": row.get("created_at") if isinstance(row.get("created_at"), str) else None,
    }
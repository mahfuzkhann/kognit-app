"""
Kognit Pocket Notes - pure domain logic.

This module has NO network access, NO database access, NO AI/provider calls
and NO FastAPI imports. It only decides:

  * what a valid note payload looks like (and normalizes it),
  * which tags can be derived deterministically at save time,
  * how student tag edits are merged with existing tag provenance,
  * whether AI-generated flashcards / mini-quiz / topic suggestions are
    structurally valid.

backend/main.py calls it for the HTTP layer, backend/database.py stores the
results, backend/notes_ai.py (the only module that talks to the AI provider
for notes) uses its validators. Keeping this pure is what makes every rule
below directly unit-testable (see test_notes_engine.py).

LIMITS ARE DUPLICATED IN supabase/migrations/0007_pocket_notes.sql (as CHECK
constraints) on purpose - defence in depth. If you change a limit here,
change it there too.

POCKET NOTES ARE NOT LEARNING EVIDENCE. Nothing here feeds mastery,
quiz_attempts or learning_evidence.
"""

import re
import unicodedata
import uuid
from typing import Any, Dict, Iterable, List, Optional

from backend.learning_memory import _extract_explicit_subject


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

MAX_NOTE_CONTENT_CHARS = 20000
MAX_QUESTION_CHARS = 4000
MAX_TAKEAWAY_CHARS = 600
MAX_SOURCES = 10
MAX_SOURCE_TITLE_CHARS = 200
MAX_SOURCE_URL_CHARS = 500
MAX_SOURCE_DOMAIN_CHARS = 100
MAX_TAGS = 12
MAX_TAG_LABEL_CHARS = 40
MAX_TOPIC_SUGGESTIONS = 3
MAX_NOTES_PER_USER = 500

MIN_FLASHCARDS_ACCEPTED = 3
MAX_FLASHCARDS = 10
MAX_FLASHCARD_FRONT_CHARS = 300
MAX_FLASHCARD_BACK_CHARS = 600

MIN_QUIZ_QUESTIONS_ACCEPTED = 2
MAX_QUIZ_QUESTIONS = 5
MAX_QUIZ_QUESTION_CHARS = 300
MAX_QUIZ_OPTION_CHARS = 150
MAX_QUIZ_OPTIONS = 4
MAX_QUIZ_EXPLANATION_CHARS = 400

MAX_GENERATED_JSON_CHARS = 60000

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 50
MAX_OFFSET = 10000

MAX_REQUEST_BODY_BYTES = 128 * 1024  # generous: 20k chars of Bangla is ~60 KB UTF-8

CLIENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

TAG_KINDS = ("subject", "class", "stream", "topic", "type", "other")
TAG_ORIGINS = ("known", "inferred", "student", "ai")

REVIEW_TAG_LABEL = "Needs review"


class NotesValidationError(ValueError):
    """A request payload failed validation. The message is safe to show to the
    student verbatim (it never contains the offending content)."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def is_valid_note_id(value: Any) -> bool:
    """True only for a canonical UUID string. Anything else (including
    strings carrying PostgREST filter syntax) is treated as 'not found'
    before it ever reaches a query."""
    if not isinstance(value, str) or len(value) != 36:
        return False
    try:
        return str(uuid.UUID(value)) == value.lower()
    except (ValueError, AttributeError):
        return False


def _clean_text(value: Any, *, field: str, max_chars: int, required: bool) -> Optional[str]:
    if value is None:
        if required:
            raise NotesValidationError(f"{field} is required.")
        return None
    if not isinstance(value, str):
        raise NotesValidationError(f"{field} must be text.")
    # PostgreSQL text columns cannot contain NUL.
    cleaned = value.replace("\x00", "").strip()
    if not cleaned:
        if required:
            raise NotesValidationError(f"{field} must not be empty.")
        return None
    if len(cleaned) > max_chars:
        raise NotesValidationError(f"{field} is too long (limit {max_chars} characters).")
    return cleaned


def _is_http_url(url: str) -> bool:
    lowered = url.lower()
    return lowered.startswith("https://") or lowered.startswith("http://")


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

_ALLOWED_PUNCTUATION = set(" _-+&./(),")


def normalize_tag_label(raw: Any) -> Optional[str]:
    """Returns a clean display label, or None if `raw` cannot be a tag.

    Strips a leading '#', collapses whitespace, enforces the length limit and
    a conservative character set (letters/digits/marks in ANY script - so
    Bangla works - plus a few punctuation marks). Control characters, markup
    characters and anything else make the label invalid; invalid labels are
    dropped rather than "fixed" into something the student did not write.
    """
    if not isinstance(raw, str):
        return None
    label = raw.strip().lstrip("#").strip()
    label = re.sub(r"\s+", " ", label)
    if not label or len(label) > MAX_TAG_LABEL_CHARS:
        return None
    for ch in label:
        category = unicodedata.category(ch)
        if category[0] in ("L", "M", "N"):
            continue
        if ch in _ALLOWED_PUNCTUATION:
            continue
        return None
    return label


def make_tag(label: str, kind: str, origin: str) -> Dict[str, str]:
    return {"label": label, "kind": kind, "origin": origin}


def _tag_key(tag: Dict[str, str]) -> tuple:
    return (tag["label"].casefold(), tag["kind"])


def _dedupe_tags(tags: Iterable[Dict[str, str]]) -> List[Dict[str, str]]:
    seen = set()
    out = []
    for tag in tags:
        key = _tag_key(tag)
        if key in seen:
            continue
        seen.add(key)
        out.append(tag)
    return out


_FORMULA_RE = re.compile(r"\$\$|\\\[|\\frac|\\sqrt|\$[^$\n]*=[^$\n]*\$")
_DEFINITION_RE = re.compile(r"(?i)\bis defined as\b|\bdefinition\b|সংজ্ঞা|বলা হয়")
# Line-anchored on purpose: the word "solution" alone appears constantly in
# Chemistry answers (a chemical solution) and must not read as "worked example".
_WORKED_EXAMPLE_RE = re.compile(
    r"(?im)^\s*(?:#+\s*|\*\*)?(?:step\s*1\b|solution\s*[:\-]|সমাধান\s*[:\-ঃ]|ধাপ\s*[১1])"
)


def derive_content_type_tags(content_md: str) -> List[Dict[str, str]]:
    tags = []
    if _FORMULA_RE.search(content_md):
        tags.append(make_tag("Formula", "type", "inferred"))
    if _DEFINITION_RE.search(content_md):
        tags.append(make_tag("Definition", "type", "inferred"))
    if _WORKED_EXAMPLE_RE.search(content_md):
        tags.append(make_tag("Worked example", "type", "inferred"))
    return tags


def derive_tags(
    question_md: Optional[str],
    content_md: str,
    user_class: Optional[str],
    stream: Optional[str],
) -> Dict[str, Any]:
    """Deterministic, zero-cost tags for a freshly saved note.

    Returns {"tags": [...], "needs_review": bool}.

    Only information the application can actually verify is used:
      * class / stream  - from the student's server-side profile  -> origin "known"
      * subject         - the existing conservative explicit-subject matcher
                          (None when zero OR several subjects match)  -> "inferred"
      * content type    - simple structural patterns in the answer    -> "inferred"

    TOPIC IS NEVER DERIVED HERE. Nothing in the repository establishes a
    chapter/topic deterministically, so inventing one would be a guess; the
    student can request an explicit AI suggestion instead.

    needs_review is True exactly when no subject could be established.
    """
    tags: List[Dict[str, str]] = []

    class_label = normalize_tag_label(user_class) if user_class else None
    if class_label:
        tags.append(make_tag(class_label, "class", "known"))
    stream_label = normalize_tag_label(stream) if stream else None
    if stream_label:
        tags.append(make_tag(stream_label, "stream", "known"))

    # The question is the student's own statement of what they were asking
    # about, so it is checked first; the (much longer, more likely to mention
    # several subjects) answer is only a fallback.
    subject = _extract_explicit_subject(question_md) or _extract_explicit_subject(content_md)
    subject_label = normalize_tag_label(subject) if subject else None
    if subject_label:
        tags.append(make_tag(subject_label, "subject", "inferred"))

    tags.extend(derive_content_type_tags(content_md))
    tags = _dedupe_tags(tags)[:MAX_TAGS]
    return {"tags": tags, "needs_review": not any(t["kind"] == "subject" for t in tags)}


def validate_tag_edit_payload(raw: Any) -> List[Dict[str, str]]:
    """Validates the `tags` array of a PATCH request. Invalid entries raise
    (a student-visible edit should fail loudly, not be silently altered)."""
    if not isinstance(raw, list):
        raise NotesValidationError("tags must be a list.")
    if len(raw) > MAX_TAGS:
        raise NotesValidationError(f"A note can have at most {MAX_TAGS} tags.")
    cleaned = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise NotesValidationError("Each tag must be an object with a label and a kind.")
        label = normalize_tag_label(entry.get("label"))
        if label is None:
            raise NotesValidationError(
                f"Tag labels must be 1-{MAX_TAG_LABEL_CHARS} characters of letters, numbers or simple punctuation."
            )
        kind = entry.get("kind", "other")
        if kind not in TAG_KINDS:
            raise NotesValidationError("Unknown tag kind.")
        origin = entry.get("origin", "student")
        cleaned.append({"label": label, "kind": kind, "origin": origin if origin in TAG_ORIGINS else "student"})
    return _dedupe_tags(cleaned)


def merge_tag_edits(existing: Any, submitted: List[Dict[str, str]]) -> Dict[str, Any]:
    """Merges a validated student tag edit with the note's existing tags.

    PROVENANCE RULES (server-decided, never trusted from the client):
      * a submitted tag that already exists (same label+kind) KEEPS its
        existing origin - a student re-saving the list must not turn a
        "known" class tag into a "student" one;
      * a new tag is origin "ai" only if the client says it came from an
        accepted AI suggestion AND it is a topic tag; otherwise "student".

    needs_review is recomputed: True exactly when no subject tag remains.
    """
    existing_origin = {}
    if isinstance(existing, list):
        for tag in existing:
            if isinstance(tag, dict) and isinstance(tag.get("label"), str) and tag.get("kind") in TAG_KINDS:
                existing_origin[_tag_key(tag)] = tag.get("origin") if tag.get("origin") in TAG_ORIGINS else "student"

    merged = []
    for tag in submitted:
        key = _tag_key(tag)
        if key in existing_origin:
            origin = existing_origin[key]
        elif tag["origin"] == "ai" and tag["kind"] == "topic":
            origin = "ai"
        else:
            origin = "student"
        merged.append(make_tag(tag["label"], tag["kind"], origin))

    merged = _dedupe_tags(merged)
    return {"tags": merged, "needs_review": not any(t["kind"] == "subject" for t in merged)}


# ---------------------------------------------------------------------------
# Create payload
# ---------------------------------------------------------------------------

def _validate_sources(raw: Any) -> List[Dict[str, str]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise NotesValidationError("sources must be a list.")
    out = []
    for entry in raw[:MAX_SOURCES]:
        if not isinstance(entry, dict):
            continue
        title = entry.get("title")
        url = entry.get("url")
        domain = entry.get("domain")
        item = {}
        if isinstance(title, str) and title.strip():
            item["title"] = title.strip()[:MAX_SOURCE_TITLE_CHARS]
        if isinstance(url, str) and url.strip() and len(url.strip()) <= MAX_SOURCE_URL_CHARS and _is_http_url(url.strip()):
            item["url"] = url.strip()
        if isinstance(domain, str) and domain.strip():
            item["domain"] = domain.strip()[:MAX_SOURCE_DOMAIN_CHARS]
        # A source with no title, url or domain carries no information.
        if item:
            out.append(item)
    return out


def _validate_source_ref(raw: Any) -> Optional[Dict[str, Any]]:
    """The soft reference back to the originating message. Never a foreign
    key: the chat may be deleted later and the note must survive that."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise NotesValidationError("source_ref must be an object.")
    project_id = raw.get("projectId")
    chat_id = raw.get("chatId")
    msg_index = raw.get("msgIndex")
    if not (isinstance(project_id, str) and 0 < len(project_id) <= 100):
        return None
    if not (isinstance(chat_id, str) and 0 < len(chat_id) <= 100):
        return None
    if isinstance(msg_index, bool) or not isinstance(msg_index, int) or not (0 <= msg_index <= 100000):
        return None
    return {"projectId": project_id, "chatId": chat_id, "msgIndex": msg_index}


def validate_create_payload(body: Any) -> Dict[str, Any]:
    """Validates and normalizes the body of POST /api/notes.

    Only the whitelisted fields are ever read. A `user_id` (or any other
    unexpected key) in the body is simply never looked at - ownership comes
    exclusively from the verified JWT in backend/main.py.
    """
    if not isinstance(body, dict):
        raise NotesValidationError("Request body must be a JSON object.")

    client_id = body.get("client_id")
    if not isinstance(client_id, str) or not CLIENT_ID_PATTERN.match(client_id):
        raise NotesValidationError("client_id is missing or invalid.")

    return {
        "client_id": client_id,
        "content_md": _clean_text(body.get("content_md"), field="Note content", max_chars=MAX_NOTE_CONTENT_CHARS, required=True),
        "takeaway": _clean_text(body.get("takeaway"), field="Takeaway", max_chars=MAX_TAKEAWAY_CHARS, required=False),
        "question_md": _clean_text(body.get("question_md"), field="Question", max_chars=MAX_QUESTION_CHARS, required=False),
        "sources": _validate_sources(body.get("sources")),
        "source_ref": _validate_source_ref(body.get("source_ref")),
    }


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

def parse_pagination(limit: Any, offset: Any) -> tuple:
    """Returns (limit, offset), clamped into the allowed bounds. Garbage input
    falls back to the defaults rather than erroring - a list endpoint with a
    bad query string should still return the first page."""
    try:
        limit_int = int(limit) if limit is not None else DEFAULT_PAGE_SIZE
    except (TypeError, ValueError):
        limit_int = DEFAULT_PAGE_SIZE
    try:
        offset_int = int(offset) if offset is not None else 0
    except (TypeError, ValueError):
        offset_int = 0
    limit_int = max(1, min(limit_int, MAX_PAGE_SIZE))
    offset_int = max(0, min(offset_int, MAX_OFFSET))
    return limit_int, offset_int


# ---------------------------------------------------------------------------
# AI output validators (structure only - the AI is never trusted)
# ---------------------------------------------------------------------------

def _as_clean_str(value: Any, max_chars: int) -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = value.replace("\x00", "").strip()
    if not cleaned or len(cleaned) > max_chars:
        return None
    return cleaned


def validate_flashcards(raw: Any, max_cards: int = MAX_FLASHCARDS) -> List[Dict[str, str]]:
    """Filters parsed model output down to valid {front, back} cards. Invalid
    or duplicate entries are dropped. The CALLER decides whether the surviving
    count is enough (MIN_FLASHCARDS_ACCEPTED)."""
    if not isinstance(raw, list):
        return []
    cards = []
    seen_fronts = set()
    for entry in raw:
        if len(cards) >= max_cards:
            break
        if not isinstance(entry, dict):
            continue
        front = _as_clean_str(entry.get("front"), MAX_FLASHCARD_FRONT_CHARS)
        back = _as_clean_str(entry.get("back"), MAX_FLASHCARD_BACK_CHARS)
        if front is None or back is None:
            continue
        key = front.casefold()
        if key in seen_fronts:
            continue
        seen_fronts.add(key)
        cards.append({"front": front, "back": back})
    return cards


def validate_mini_quiz(raw: Any, max_questions: int = MAX_QUIZ_QUESTIONS) -> List[Dict[str, Any]]:
    """Filters parsed model output down to valid MCQs. Stricter than the main
    quiz validator on purpose (unique options, hard option cap, bounded
    lengths), because this output is stored and self-graded client-side.

    A valid entry has a non-empty question, 2..4 unique non-empty options,
    and a `correct_index` that is a real int (not a bool) pointing at one of
    those options. `explanation` is optional and bounded.
    """
    if not isinstance(raw, list):
        return []
    questions = []
    for entry in raw:
        if len(questions) >= max_questions:
            break
        if not isinstance(entry, dict):
            continue
        question = _as_clean_str(entry.get("question"), MAX_QUIZ_QUESTION_CHARS)
        options = entry.get("options")
        correct_index = entry.get("correct_index")
        if question is None:
            continue
        if not isinstance(options, list) or not (2 <= len(options) <= MAX_QUIZ_OPTIONS):
            continue
        clean_options = [_as_clean_str(o, MAX_QUIZ_OPTION_CHARS) for o in options]
        if any(o is None for o in clean_options):
            continue
        if len({o.casefold() for o in clean_options}) != len(clean_options):
            continue
        if isinstance(correct_index, bool) or not isinstance(correct_index, int):
            continue
        if not (0 <= correct_index < len(clean_options)):
            continue
        explanation = entry.get("explanation")
        explanation_clean = ""
        if isinstance(explanation, str):
            explanation_clean = explanation.replace("\x00", "").strip()[:MAX_QUIZ_EXPLANATION_CHARS]
        questions.append({
            "question": question,
            "options": clean_options,
            "correct_index": correct_index,
            "explanation": explanation_clean,
        })
    return questions


def validate_topic_suggestions(raw: Any, existing_tags: Any = None) -> List[Dict[str, str]]:
    """Accepts {"topics": [..]} (preferred) or a bare list of strings. Returns
    at most MAX_TOPIC_SUGGESTIONS topic tags with origin "ai", excluding any
    label the note already carries. Never raises."""
    items = raw.get("topics") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    already = set()
    if isinstance(existing_tags, list):
        for tag in existing_tags:
            if isinstance(tag, dict) and isinstance(tag.get("label"), str):
                already.add(tag["label"].casefold())
    out = []
    for item in items:
        if len(out) >= MAX_TOPIC_SUGGESTIONS:
            break
        label = normalize_tag_label(item)
        if label is None or label.casefold() in already:
            continue
        already.add(label.casefold())
        out.append(make_tag(label, "topic", "ai"))
    return out
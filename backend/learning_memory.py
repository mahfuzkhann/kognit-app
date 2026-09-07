"""
Kognit Phase 5B/5C/5E - Learning Memory domain logic.

PURE DOMAIN MODULE, same architectural rule as backend/mastery_engine.py:
no I/O, no network calls, no database access, no Gemini calls. Everything
here takes plain Python values in and returns plain Python values out.

This module owns three separate, deliberately-not-merged responsibilities:

  1. Academic Context Resolution - decides whether a chat interaction's
     subject/topic is Known, Probable, or Unknown. NEVER guesses; only
     reports what is already reliably known.
  2. Chat Signal Detection - deterministic, rule-based, conservative
     detection of a small set of learning signals from a student's
     message. NOT an AI classifier - see module docstring in
     backend/main.py's chat_endpoint for why.
  3. Combined-intelligence explanation notes (Phase 5E) - turns raw chat
     evidence into short, explainable, non-numeric notes that sit ALONGSIDE
     backend.mastery_engine's quiz-based status, never folded into it.

ARCHITECTURE BOUNDARY: this module never imports backend.database or
backend.main, for the same reason backend/mastery_engine.py doesn't - so
an accidental layering violation is easy to catch in review. It also never
imports backend.ai_engine or google.genai - nothing here makes a model
call. See backend/main.py for how these functions are wired into
/api/chat and /api/chat/title.
"""

import re
from dataclasses import dataclass
from typing import Optional, Sequence


# ---------------------------------------------------------------------------
# 1. ACADEMIC CONTEXT RESOLUTION
# ---------------------------------------------------------------------------
#
# PHASE 5C ACTIVATION UPDATE: resolve_academic_context() is no longer a
# permanently-Unknown stub. It now performs deterministic, zero-friction
# SUBJECT detection (see _extract_explicit_subject below) against the
# student's own message text and the already-available bounded chat
# history - no new UI, no new persisted state, no per-message Gemini call.
# TOPIC is still never extracted from message text - see
# resolve_academic_context's own docstring for why that remains a
# deliberate, documented scope boundary rather than an oversight. This
# means chat-derived learning_evidence can now genuinely populate in
# production for messages containing both a recognizable subject mention
# AND a meaningful learning signal - see the Phase 5C activation
# investigation report for the full reasoning and the false-positive
# analysis (subject-word ambiguity is real for words like "History", but
# evidence requires a signal too, which sharply narrows real false
# positives - see chat_endpoint's integration in backend/main.py).
# ---------------------------------------------------------------------------

CONFIDENCE_KNOWN = "known"
CONFIDENCE_PROBABLE = "probable"
CONFIDENCE_UNKNOWN = "unknown"

VALID_ATTRIBUTION_CONFIDENCES = (CONFIDENCE_KNOWN, CONFIDENCE_PROBABLE, CONFIDENCE_UNKNOWN)


@dataclass(frozen=True)
class ContextResolution:
    """
    Result of resolving a chat interaction's academic context.

    subject is only meaningful when confidence is KNOWN or PROBABLE.
    topic is ALWAYS None for chat-derived context as of this version - see
    resolve_academic_context's docstring for why reliable topic extraction
    from freeform chat text is deliberately not attempted. Callers MUST
    treat subject as None/ignored when confidence is UNKNOWN - never fall
    back to a guess.
    """

    confidence: str
    subject: Optional[str] = None
    topic: Optional[str] = None


_UNKNOWN_CONTEXT = ContextResolution(confidence=CONFIDENCE_UNKNOWN)


# ---------------------------------------------------------------------------
# Zero-friction subject detection: a small, finite, PRODUCT-DEFINED
# vocabulary - the exact same subject list the quiz feature's
# QUIZ_SUBJECTS_BY_STREAM already uses in static/js/app.js - matched with
# word-boundary regex, never fuzzy/stemmed/substring matching. This is NOT
# a topic extractor (see below) and is NOT an NLP classifier: it is a
# fixed lookup table, the same category of mechanism as the phrase
# allow-lists in the signal detector above.
#
# DELIBERATE EXCLUSIONS:
#   - "ব্যবসায় শিক্ষা" (Business Studies' Bangla name) is NOT included as
#     a Bangla keyword, because it is ALSO the Commerce stream's Bangla
#     label ("Commerce (ব্যবসায় শিক্ষা)" in templates/index.html) - including
#     it would risk exactly the stream/subject conflation this whole
#     mechanism exists to avoid. English "business studies" is kept
#     (unambiguous).
#   - "General" (the quiz feature's stream-mismatch fallback subject) is
#     excluded entirely - it is not a real subject to attribute evidence to.
# ---------------------------------------------------------------------------

_SUBJECT_KEYWORDS = {
    "Physics": ["physics", "পদার্থবিজ্ঞান", "পদার্থ বিজ্ঞান"],
    "Chemistry": ["chemistry", "রসায়ন"],
    "Biology": ["biology", "জীববিজ্ঞান", "জীব বিজ্ঞান"],
    "Higher Math": ["higher math", "higher mathematics", "mathematics", "maths", "math", "অংক", "গণিত"],
    "ICT": ["ict", "আইসিটি"],
    "Accounting": ["accounting", "হিসাববিজ্ঞান"],
    "Business Studies": ["business studies"],
    "Finance & Banking": ["finance & banking", "finance and banking"],
    "Bangla": ["bangla", "বাংলা"],
    "English": ["english", "ইংরেজি"],
    "History": ["history", "ইতিহাস"],
    "Civics": ["civics", "পৌরনীতি"],
}

_SUBJECT_PATTERNS = {
    subject: re.compile(
        r"\b(?:" + "|".join(re.escape(kw) for kw in keywords) + r")\b",
        re.IGNORECASE,
    )
    for subject, keywords in _SUBJECT_KEYWORDS.items()
}


def _extract_explicit_subject(text: Optional[str]) -> Optional[str]:
    """
    Deterministically checks `text` against the fixed subject vocabulary
    above. Returns the matched canonical subject name ONLY when exactly
    one distinct subject matches. Returns None (never guesses) when zero
    subjects match OR when two or more distinct subjects match in the
    same text - an ambiguous message is not reliable evidence of either
    subject, per the brief's "do not treat weak guesses as probable/known"
    rule. Never raises on malformed input.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    matched = {subject for subject, pattern in _SUBJECT_PATTERNS.items() if pattern.search(text)}
    if len(matched) == 1:
        return next(iter(matched))
    return None


def resolve_academic_context(
    explicit_subject: Optional[str] = None,
    explicit_topic: Optional[str] = None,
    prompt: Optional[str] = None,
    history: Optional[list] = None,
) -> ContextResolution:
    """
    Resolves the academic context (subject/topic + confidence) for one
    chat interaction.

    CONTEXT RULE (non-negotiable): this function only ever reports context
    that is either explicitly supplied by an already-verified caller, or
    deterministically, unambiguously present in text the student
    themselves already wrote. It never infers, guesses, or fuzzy-matches
    beyond that.

    RESOLUTION ORDER (see the Phase 5C activation investigation report for
    why this is the smallest robust hierarchy):

      1. `explicit_subject` + `explicit_topic` BOTH supplied and non-blank
         -> KNOWN, with that subject AND topic. This is the original,
         still-unused-in-production path for a hypothetical future caller
         that genuinely has both (e.g. a chat explicitly linked to a quiz's
         subject/topic - no such link exists in the codebase today, see
         the investigation report; this path stays available for it).

      2. `prompt` (the CURRENT student message) contains exactly one
         unambiguous match against the fixed subject vocabulary
         (_extract_explicit_subject) -> KNOWN, subject = that match,
         topic = None.

         TOPIC IS DELIBERATELY NEVER EXTRACTED FROM MESSAGE TEXT. Kognit
         has no reliable, finite topic vocabulary (even quiz's own topic
         field is freeform, student-typed text) and reliably carving a
         topic phrase out of arbitrary Bangla/English/Banglish sentences
         without NLP/embeddings/an LLM call is not achievable with
         acceptable precision - see the investigation report. Known/
         Probable subject-only evidence (topic=None) is still honest,
         still useful (see build_conversation_notes below), and is a
         deliberate, documented scope decision, not an oversight.

      3. No match in `prompt`, but `history` (the same bounded, already-
         validated conversation history backend/main.py already builds)
         contains an earlier user message that itself unambiguously
         matched the subject vocabulary -> PROBABLE, subject = the most
         recent such match, topic = None.

         This is the "same-session continuity" mechanism (Case 2 in the
         Phase 5C brief) - implemented with ZERO new persisted state: it
         is recomputed fresh from the client-supplied history on every
         request, so it is automatically bounded by the same
         MAX_HISTORY_MESSAGES cap that already governs everything else
         about this history, and automatically resets for a new chat
         (which starts with empty history). A message that itself
         contains a DIFFERENT explicit subject always takes priority over
         an inherited one (step 2 is checked first) - this is what
         implements a context switch (Case 4) without any special-case
         "switch" logic.

      4. Otherwise -> UNKNOWN.

    Board/class/stream are never read by this function at all (not even
    accepted as parameters) - this is deliberate, not an omission, so a
    stream value like "Science" can never be conflated with an actual
    subject like "Physics" (Case 5).
    """
    if explicit_subject and explicit_subject.strip() and explicit_topic and explicit_topic.strip():
        return ContextResolution(
            confidence=CONFIDENCE_KNOWN,
            subject=explicit_subject.strip(),
            topic=explicit_topic.strip(),
        )

    current_subject = _extract_explicit_subject(prompt)
    if current_subject:
        return ContextResolution(confidence=CONFIDENCE_KNOWN, subject=current_subject, topic=None)

    if isinstance(history, list):
        for entry in reversed(history):
            if not isinstance(entry, dict) or entry.get("role") != "user":
                continue
            inherited_subject = _extract_explicit_subject(entry.get("text"))
            if inherited_subject:
                return ContextResolution(confidence=CONFIDENCE_PROBABLE, subject=inherited_subject, topic=None)

    return _UNKNOWN_CONTEXT


# ---------------------------------------------------------------------------
# 2. CHAT SIGNAL DETECTION
# ---------------------------------------------------------------------------
#
# Deterministic, rule-based only - NOT an AI classifier (see Section 13 of
# the implementation brief: no Gemini call merely to classify a message).
#
# TAXONOMY, DELIBERATELY SMALL: of the six signal types the brief lists as
# examples (repeated question, re-explanation request, explicit confusion,
# explicit misconception, successful explanation, clarification request),
# only two are implemented as real detectors here:
#
#   - SIGNAL_REPEATED_QUESTION: deterministic (exact match after the same
#     normalization Phase 5A already uses elsewhere), not a guess.
#   - SIGNAL_REEXPLANATION / SIGNAL_CONFUSION: a small, explicit phrase
#     allow-list (Bangla/English/Banglish), not fuzzy/stemmed matching.
#
# SIGNAL_MISCONCEPTION and SIGNAL_SUCCESSFUL_UNDERSTANDING are named here as
# valid taxonomy values (so the schema and future callers don't need a
# migration to add them) but are DELIBERATELY NOT DETECTED by this version.
# Reliably telling "the student stated something academically incorrect"
# or "the student now demonstrates correct understanding" apart from an
# ordinary message requires actually judging correctness - a rule-based
# phrase list cannot do this honestly, and doing it would risk exactly the
# false-positive problem the brief warns about repeatedly. Better to detect
# nothing than to detect wrong. See KNOWN LIMITATIONS in the final report.
# ---------------------------------------------------------------------------

SIGNAL_REPEATED_QUESTION = "repeated_question"
SIGNAL_REEXPLANATION = "re_explanation_request"
SIGNAL_CONFUSION = "confusion"
SIGNAL_MISCONCEPTION = "explicit_misconception"
SIGNAL_SUCCESSFUL_UNDERSTANDING = "successful_understanding"

VALID_SIGNAL_TYPES = (
    SIGNAL_REPEATED_QUESTION,
    SIGNAL_REEXPLANATION,
    SIGNAL_CONFUSION,
    SIGNAL_MISCONCEPTION,
    SIGNAL_SUCCESSFUL_UNDERSTANDING,
)

SIGNAL_STRENGTH_WEAK = "weak"
SIGNAL_STRENGTH_STRONG = "strong"
VALID_SIGNAL_STRENGTHS = (SIGNAL_STRENGTH_WEAK, SIGNAL_STRENGTH_STRONG)


@dataclass(frozen=True)
class DetectedSignal:
    signal_type: str
    signal_strength: str


_WHITESPACE_RUN_RE = re.compile(r"\s+")


def _normalize_for_comparison(text: str) -> str:
    """
    Same normalization level as backend.database.normalize_topic_key:
    trim, collapse whitespace, casefold. Used ONLY for the repeated-question
    exact-match check below - deliberately not fuzzy/stemmed, matching the
    brief's explicit "no fuzzy matching" instruction.
    """
    if not isinstance(text, str):
        return ""
    collapsed = _WHITESPACE_RUN_RE.sub(" ", text.strip())
    return collapsed.casefold()


# Minimum length (after normalization) for a repeated-question match to
# count. Prevents trivially short messages ("ok", "thanks", "হ্যাঁ") from
# being flagged as a "repeated question" merely because a student naturally
# says the same short acknowledgement twice - that is not evidence of
# anything academic.
_MIN_REPEATED_QUESTION_CHARS = 8

# Explicit phrase allow-list (lowercased/casefolded at match time), covering
# Bangla script, Banglish, and English. Deliberately short and literal - no
# stemming, no synonym expansion. False-negative (missing a real signal) is
# an acceptable cost; false-positive (flagging an ordinary question as
# confusion) is not, per the brief's explicit priority.
_REEXPLANATION_PHRASES = (
    "explain again", "explain it again", "explain this again",
    "again explain", "abar bujhiye dao", "abar bujhaiye dao",
    "abar bolo", "aro shohoj kore bolo", "aro sohoj kore bolo",
    "aro easy kore bolo", "aro shohoj kore bujhao", "aro sohoj kore bujhao",
    "aro easy kore bujhao", "abar bujhao", "differently explain",
    "explain differently", "another way",
    "একটু আবার বুঝিয়ে দাও", "আবার বলো", "আরেকটু সহজ করে বলো",
)

_CONFUSION_PHRASES = (
    "i don't understand", "i dont understand", "i don't get it",
    "i dont get it", "not clear to me", "this is confusing",
    "i'm confused", "im confused", "bujhi nai", "bujhi na",
    "bujhte parchi na", "bujhtesi na", "clear na", "বুঝি নাই",
    "বুঝিনি", "বুঝতে পারছি না", "বুঝছি না",
)


def _contains_any_phrase(normalized_text: str, phrases: Sequence[str]) -> bool:
    return any(phrase in normalized_text for phrase in phrases)


def detect_learning_signals(prompt: str, history: Optional[list] = None) -> list:
    """
    Deterministically detects learning signals in one student message.

    `prompt` is the CURRENT student message only - never the AI's reply
    (a signal is evidence about the STUDENT's state, not the model's
    output). `history` is the same bounded, already-validated
    [{"role": "user"|"bot", "text": str}, ...] list backend/main.py already
    builds for /api/chat - used ONLY for the repeated-question exact-match
    check, never sent anywhere new.

    Returns a list of DetectedSignal (usually 0 or 1 entries; both a
    confusion/re-explanation phrase AND a repeated-question match can fire
    together for the same message, so a caller should not assume at most
    one signal per call).

    FALSE-POSITIVE GUARDRAILS (see module docstring for the taxonomy
    rationale):
      - Ordinary questions never match unless they contain one of the
        explicit phrases above, or are byte-for-byte (after normalization)
        the same as an earlier message in this same bounded history.
      - A short acknowledgement ("ok", "thanks") is never treated as a
        repeated question regardless of history.
      - Never raises - malformed input (e.g. a non-string prompt) yields
        an empty result rather than an exception, so a caller integrating
        this into the chat hot path cannot have its request broken by this
        function specifically (defense in depth on top of the caller's own
        try/except - see backend/main.py).
    """
    if not isinstance(prompt, str) or not prompt.strip():
        return []

    normalized_prompt = _normalize_for_comparison(prompt)
    signals = []

    if _contains_any_phrase(normalized_prompt, _CONFUSION_PHRASES):
        signals.append(DetectedSignal(SIGNAL_CONFUSION, SIGNAL_STRENGTH_WEAK))

    if _contains_any_phrase(normalized_prompt, _REEXPLANATION_PHRASES):
        signals.append(DetectedSignal(SIGNAL_REEXPLANATION, SIGNAL_STRENGTH_WEAK))

    if len(normalized_prompt) >= _MIN_REPEATED_QUESTION_CHARS and isinstance(history, list):
        for entry in history:
            if not isinstance(entry, dict) or entry.get("role") != "user":
                continue
            prior_text = entry.get("text")
            if isinstance(prior_text, str) and _normalize_for_comparison(prior_text) == normalized_prompt:
                signals.append(DetectedSignal(SIGNAL_REPEATED_QUESTION, SIGNAL_STRENGTH_WEAK))
                break

    return signals


def is_meaningful_signal_set(signals: Sequence[DetectedSignal]) -> bool:
    """
    Thin, named predicate: is there anything here worth persisting at all?
    Currently just "non-empty", but named/isolated so a future confidence
    threshold (e.g. requiring STRONG strength, or requiring 2+ signals in
    one message) is a one-line change here, not a change at every call site.
    """
    return len(signals) > 0


# ---------------------------------------------------------------------------
# 3. COMBINED-INTELLIGENCE EXPLANATION NOTES (Phase 5E)
# ---------------------------------------------------------------------------
#
# CRITICAL RULE (brief Section 30/31, design review Section 30): chat
# signals are qualitative evidence and must NEVER be averaged, weighted, or
# folded into backend.mastery_engine's numeric correct_rate/status
# computation. compute_topic_status() is called ONLY on quiz EvidenceEvents,
# completely unchanged, exactly as before this module existed.
#
# This function does the opposite of scoring: it turns a set of chat
# evidence rows (already filtered to Known/Probable attribution by the
# caller - see backend/database.py:get_learning_history) into short,
# explainable, non-numeric notes, for display ALONGSIDE (never merged
# with) the quiz-based status for the same subject/topic.
#
# PHASE 5C ACTIVATION UPDATE: chat evidence now commonly has a known
# SUBJECT but no topic (see backend.learning_memory.resolve_academic_context
# - topic is deliberately never extracted from message text). Rows are
# grouped by topic_key when one exists (finer-grained note, e.g. "Force &
# Motion: ..."), and by subject alone when it doesn't (coarser note, e.g.
# "Physics: ...") - they are NEVER skipped just because topic is absent,
# since subject-only attribution is now a legitimate, common, honest
# outcome rather than a malformed/unexpected one.
# ---------------------------------------------------------------------------

_SIGNAL_NOTE_TEMPLATES = {
    SIGNAL_CONFUSION: "expressed confusion {count} time(s)",
    SIGNAL_REEXPLANATION: "asked for re-explanation {count} time(s)",
    SIGNAL_REPEATED_QUESTION: "asked essentially the same question {count} time(s)",
    SIGNAL_MISCONCEPTION: "showed a possible misconception {count} time(s)",
    SIGNAL_SUCCESSFUL_UNDERSTANDING: "demonstrated understanding {count} time(s)",
}


def build_conversation_notes(chat_evidence_rows: Sequence[dict]) -> list:
    """
    Groups chat-derived evidence rows by topic_key (when present) or by
    subject alone (when topic is absent - see module comment above), and
    produces one short, human-readable, purely qualitative note per
    (group, signal_type) pair - e.g. "Force & Motion: asked for
    re-explanation 2 time(s)" or, for subject-only evidence, "Physics:
    expressed confusion 1 time(s)".

    Each input row is expected to have at least:
        {"subject": str, "topic": str|None, "topic_key": str|None, "signal_type": str}
    Rows missing `subject` entirely are skipped (there is nothing to
    attribute a note to at all) - this should never happen in practice
    since resolve_academic_context never returns Known/Probable without a
    subject, but this stays defensive rather than assuming that.

    Returns a list of {"subject": str, "topic": str|None, "topic_key":
    str|None, "notes": [str, ...]} dicts, one per group, in the order
    first encountered. Never returns a numeric score, percentage, or
    status label - this is a structural guarantee, not a convention:
    nothing in this function's return shape has a slot for a number to
    go in.
    """
    order: list = []
    grouped: dict = {}

    for row in chat_evidence_rows:
        if not isinstance(row, dict):
            continue
        subject = row.get("subject")
        if not isinstance(subject, str) or not subject.strip():
            continue
        signal_type = row.get("signal_type")
        if signal_type not in VALID_SIGNAL_TYPES:
            continue

        topic_key = row.get("topic_key")
        has_topic = isinstance(topic_key, str) and bool(topic_key.strip())
        # Coarser, subject-only grouping key when no topic exists - kept
        # distinct from any real topic_key by construction (a normalized
        # topic_key can never contain "::").
        group_key = topic_key if has_topic else f"subject::{subject.strip().casefold()}"

        if group_key not in grouped:
            grouped[group_key] = {
                "subject": subject,
                "topic": row.get("topic") if has_topic and isinstance(row.get("topic"), str) else None,
                "topic_key": topic_key if has_topic else None,
                "counts": {},
            }
            order.append(group_key)

        grouped[group_key]["counts"][signal_type] = grouped[group_key]["counts"].get(signal_type, 0) + 1

    results = []
    for group_key in order:
        entry = grouped[group_key]
        notes = []
        for signal_type, count in entry["counts"].items():
            template = _SIGNAL_NOTE_TEMPLATES.get(signal_type)
            if template:
                notes.append(template.format(count=count))
        results.append({
            "subject": entry["subject"],
            "topic": entry["topic"],
            "topic_key": entry["topic_key"],
            "notes": notes,
        })

    return results
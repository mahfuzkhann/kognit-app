"""
Kognit Phase 7B - prompt identity for the evaluation subsystem.

Provides a human-readable prompt_version label AND a machine-verifiable
prompt_hash, computed automatically from the ACTUAL production prompt
content - never duplicated or hand-typed here.

Why both fields (see the Phase 7B Step 2 revision report, Section 11):
  * prompt_version is a human label (backend.ai_engine.CHAT_PROMPT_VERSION)
    - readable at a glance, but only as reliable as someone remembering to
      bump it when the prompt changes.
  * prompt_hash is computed by hashing the actual constant's content at
    call time, so it can never silently drift out of sync with reality
    the way a hand-typed label could.

This module imports backend.ai_engine.CHAT_SYSTEM_INSTRUCTION_RULES
directly - the exact static text used in production - rather than
re-declaring or paraphrasing it here. Duplicating the production prompt
inside evaluation/ was explicitly forbidden by the approved architecture.
"""

from __future__ import annotations

import hashlib

from backend import ai_engine


def get_chat_prompt_version() -> str:
    """The human-readable prompt version label currently in effect."""
    return ai_engine.CHAT_PROMPT_VERSION


def get_chat_prompt_hash() -> str:
    """SHA-256 hex digest of the actual, current static chat system
    instruction content (backend.ai_engine.CHAT_SYSTEM_INSTRUCTION_RULES).

    Deliberately hashes only the static rule block, not the full
    per-request system_instruction string generate_ai_response() builds -
    the full string embeds per-request class/stream/PDF content and would
    produce a different hash for every single request, which would not be
    a meaningful prompt-version signal at all (see module docstring and
    the Phase 7B Step 1 CONFLICT/RECOMMENDATION note this module resolves).
    """
    content = ai_engine.CHAT_SYSTEM_INSTRUCTION_RULES.encode("utf-8")
    return hashlib.sha256(content).hexdigest()

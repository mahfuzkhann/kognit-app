"""
Bounded, expiring in-memory state (Pre-Phase 10 remediation, finding H3).

WHY THIS MODULE EXISTS
----------------------
backend/main.py keeps two process-local stores:

  * active_pdf_contexts    {user_id: {chat_id: extracted_pdf_text}}
  * active_quiz_definitions {quiz_id: server-owned quiz definition}

Before this module neither had any eviction. chat_id is a client-supplied
string, so a single signed-in account could create an unlimited number of
buckets (each up to ~600,000 characters) and grow server memory until the
process was restarted. This module adds, WITHOUT changing the plain-dict shape
the rest of the code and the existing test-suite rely on:

  * an idle TTL (a context nobody touches for PDF_CONTEXT_TTL_SECONDS is dropped)
  * a per-user entry cap, a per-user character budget and a global character
    budget (least-recently-used entries are evicted first)
  * the same ideas for pending quizzes (TTL, per-user cap, global cap)

DESIGN CHOICES
--------------
* The stores stay plain dicts owned by backend/main.py. The managers below
  operate ON whatever dict object they are handed, and keep only small
  side-metadata (last-used timestamps) of their own. That keeps every existing
  test that clears, replaces or inspects the dicts directly working.
* Entries with no metadata (for example, ones a test inserted directly) are
  treated as "seen just now" instead of being evicted or crashing.
* Eviction never touches a quiz whose "submitted" flag is True: a submit is in
  flight and main.py pops the entry itself when it finishes.
* All mutation happens under a threading.Lock. This protects against threadpool
  access within ONE process. It does not make the state shared between worker
  processes - that is a deployment property documented in docs/DEPLOYMENT.md.
* Limits are environment-tunable. The defaults are ASSUMPTIONS (documented in
  docs/DEPLOYMENT.md), chosen from the current upload caps: one stored document
  is at most DEFAULT_MAX_EXTRACTED_CHARS (600,000 characters); the defaults
  allow a handful of documents per student and bound the whole process to about
  40 million characters (roughly 80 MB for Bangla text, which CPython stores as
  two bytes per character).

Nothing here logs document text or quiz content - only counts and ids' lengths.
"""

import logging
import os
import threading
import time
from typing import Callable, Dict, Optional, Tuple

logger = logging.getLogger("kognit.state_limits")


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    """Reads a positive integer from the environment; falls back to `default`
    (never raises) on a missing, malformed or too-small value."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        logger.warning("%s is not an integer - using default %d", name, default)
        return default
    if value < minimum:
        logger.warning("%s=%d is below the minimum %d - using default %d", name, value, minimum, default)
        return default
    return value


# --- PDF contexts -----------------------------------------------------------
PDF_CONTEXT_TTL_SECONDS = _env_int("PDF_CONTEXT_TTL_SECONDS", 2 * 60 * 60)
PDF_CONTEXT_MAX_PER_USER = _env_int("PDF_CONTEXT_MAX_PER_USER", 5)
PDF_CONTEXT_MAX_CHARS_PER_USER = _env_int("PDF_CONTEXT_MAX_CHARS_PER_USER", 1_500_000)
PDF_CONTEXT_MAX_CHARS_GLOBAL = _env_int("PDF_CONTEXT_MAX_CHARS_GLOBAL", 40_000_000)

# --- Pending quiz definitions ----------------------------------------------
QUIZ_DEFINITION_TTL_SECONDS = _env_int("QUIZ_DEFINITION_TTL_SECONDS", 2 * 60 * 60)
QUIZ_DEFINITION_MAX_PER_USER = _env_int("QUIZ_DEFINITION_MAX_PER_USER", 10)
QUIZ_DEFINITION_MAX_GLOBAL = _env_int("QUIZ_DEFINITION_MAX_GLOBAL", 5000)

# chat_id is client-supplied and becomes a dict key: bound its size.
MAX_CHAT_ID_CHARS = 128

# Full-store sweeps are O(n); do them at most this often from hot paths.
PRUNE_INTERVAL_SECONDS = 60


class ContextCapacityError(Exception):
    """A single document is larger than the configured per-user/global budget
    (only possible with a deliberately tiny configuration)."""


class BoundedPdfContexts:
    """Lifecycle rules for {user_id: {chat_id: text}}. See module docstring."""

    def __init__(
        self,
        *,
        ttl_seconds: int = PDF_CONTEXT_TTL_SECONDS,
        max_per_user: int = PDF_CONTEXT_MAX_PER_USER,
        max_chars_per_user: int = PDF_CONTEXT_MAX_CHARS_PER_USER,
        max_chars_global: int = PDF_CONTEXT_MAX_CHARS_GLOBAL,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_per_user = max_per_user
        self.max_chars_per_user = max_chars_per_user
        self.max_chars_global = max_chars_global
        self._clock = clock
        self._lock = threading.Lock()
        self._last_used: Dict[Tuple[str, str], float] = {}
        self._last_prune = 0.0
        self.evictions = 0   # observability counters (never student content)
        self.expirations = 0

    # -- internal helpers (caller must hold the lock) -----------------------
    def _sync_locked(self, contexts: dict, now: float) -> None:
        """Aligns metadata with the dict: forget keys that no longer exist
        (cleared / replaced by someone else) and adopt unknown ones as "now"."""
        live = set()
        for user_id, chats in contexts.items():
            if not isinstance(chats, dict):
                continue
            for chat_id in chats:
                key = (user_id, chat_id)
                live.add(key)
                self._last_used.setdefault(key, now)
        for key in [k for k in self._last_used if k not in live]:
            del self._last_used[key]

    def _drop_locked(self, contexts: dict, key: Tuple[str, str]) -> None:
        user_id, chat_id = key
        chats = contexts.get(user_id)
        if isinstance(chats, dict):
            chats.pop(chat_id, None)
        self._last_used.pop(key, None)

    def _expire_locked(self, contexts: dict, now: float) -> int:
        stale = [k for k, ts in self._last_used.items() if now - ts > self.ttl_seconds]
        for key in stale:
            self._drop_locked(contexts, key)
        # forget users with no contexts left (keeps the outer dict from growing)
        for user_id in [u for u, chats in contexts.items() if isinstance(chats, dict) and not chats]:
            del contexts[user_id]
        self.expirations += len(stale)
        return len(stale)

    @staticmethod
    def _chars(chats: dict) -> int:
        return sum(len(t) for t in chats.values() if isinstance(t, str))

    def _total_chars_locked(self, contexts: dict) -> int:
        return sum(self._chars(c) for c in contexts.values() if isinstance(c, dict))

    def _lru_key_locked(self, candidates) -> Optional[Tuple[str, str]]:
        best_key, best_ts = None, None
        for key in candidates:
            ts = self._last_used.get(key, 0.0)
            if best_ts is None or ts < best_ts:
                best_key, best_ts = key, ts
        return best_key

    # -- public API -----------------------------------------------------------
    def put(self, contexts: dict, user_id: str, chat_id: str, text: str) -> int:
        """Stores `text`, evicting older entries as needed. Returns how many
        entries were evicted to make room. Raises ContextCapacityError if the
        single document can never fit."""
        size = len(text)
        if size > self.max_chars_per_user or size > self.max_chars_global:
            raise ContextCapacityError("document exceeds the configured context budget")

        with self._lock:
            now = self._clock()
            self._sync_locked(contexts, now)
            self._expire_locked(contexts, now)

            user_chats = contexts.setdefault(user_id, {})
            if chat_id in user_chats:  # replacing: remove first so budgets are exact
                self._drop_locked(contexts, (user_id, chat_id))
                user_chats = contexts.setdefault(user_id, {})

            evicted = 0
            # per-user entry cap and per-user character budget
            while user_chats and (
                len(user_chats) >= self.max_per_user
                or self._chars(user_chats) + size > self.max_chars_per_user
            ):
                victim = self._lru_key_locked((user_id, c) for c in user_chats)
                self._drop_locked(contexts, victim)
                user_chats = contexts.setdefault(user_id, {})
                evicted += 1

            # global character budget (least-recently-used across all users)
            while self._total_chars_locked(contexts) + size > self.max_chars_global:
                everything = [(u, c) for u, chats in contexts.items() if isinstance(chats, dict) for c in chats]
                victim = self._lru_key_locked(everything)
                if victim is None:
                    break
                self._drop_locked(contexts, victim)
                evicted += 1

            contexts.setdefault(user_id, {})[chat_id] = text
            self._last_used[(user_id, chat_id)] = now
            self.evictions += evicted

        if evicted:
            logger.info("pdf_contexts: evicted %d entr%s to stay within budget", evicted, "y" if evicted == 1 else "ies")
        return evicted

    def get(self, contexts: dict, user_id: str, chat_id: str) -> str:
        """Returns the stored text, or "" if absent or idle-expired. A hit
        refreshes the idle timer (sliding TTL)."""
        if not chat_id:
            return ""
        with self._lock:
            now = self._clock()
            self._maybe_prune_locked(contexts, now)
            chats = contexts.get(user_id)
            text = chats.get(chat_id) if isinstance(chats, dict) else None
            if text is None:
                self._last_used.pop((user_id, chat_id), None)
                return ""
            key = (user_id, chat_id)
            last = self._last_used.get(key)
            if last is not None and now - last > self.ttl_seconds:
                self._drop_locked(contexts, key)
                self.expirations += 1
                return ""
            self._last_used[key] = now
            return text

    def discard(self, contexts: dict, user_id: str, chat_id: str) -> None:
        """Explicit clear. Safe if nothing is stored (no error)."""
        with self._lock:
            self._drop_locked(contexts, (user_id, chat_id))

    def prune(self, contexts: dict) -> int:
        """Full sweep; returns the number of expired entries removed."""
        with self._lock:
            now = self._clock()
            self._last_prune = now
            self._sync_locked(contexts, now)
            return self._expire_locked(contexts, now)

    def _maybe_prune_locked(self, contexts: dict, now: float) -> None:
        if now - self._last_prune >= PRUNE_INTERVAL_SECONDS:
            self._last_prune = now
            self._sync_locked(contexts, now)
            self._expire_locked(contexts, now)

    def stats(self, contexts: dict) -> dict:
        with self._lock:
            return {
                "users": len(contexts),
                "contexts": sum(len(c) for c in contexts.values() if isinstance(c, dict)),
                "chars": self._total_chars_locked(contexts),
                "evictions": self.evictions,
                "expirations": self.expirations,
            }


class BoundedQuizDefinitions:
    """Lifecycle rules for {quiz_id: definition}.

    Age is measured from when THIS manager stored (or first saw) the entry, not
    from any field inside the definition, so a definition inserted directly
    (as tests do, with arbitrary created_at values) is simply adopted as "seen
    now" instead of being treated as ancient."""

    def __init__(
        self,
        *,
        ttl_seconds: int = QUIZ_DEFINITION_TTL_SECONDS,
        max_per_user: int = QUIZ_DEFINITION_MAX_PER_USER,
        max_global: int = QUIZ_DEFINITION_MAX_GLOBAL,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_per_user = max_per_user
        self.max_global = max_global
        self._clock = clock
        self._lock = threading.Lock()
        self._stored_at: Dict[str, float] = {}
        self._last_prune = 0.0
        self.evictions = 0
        self.expirations = 0

    @staticmethod
    def _in_flight(definition) -> bool:
        return isinstance(definition, dict) and definition.get("submitted") is True

    def _sync_locked(self, definitions: dict, now: float) -> None:
        for quiz_id in definitions:
            self._stored_at.setdefault(quiz_id, now)
        for quiz_id in [q for q in self._stored_at if q not in definitions]:
            del self._stored_at[quiz_id]

    def _drop_locked(self, definitions: dict, quiz_id: str) -> None:
        definitions.pop(quiz_id, None)
        self._stored_at.pop(quiz_id, None)

    def _expired(self, definitions: dict, quiz_id: str, now: float) -> bool:
        stored = self._stored_at.get(quiz_id)
        return (
            stored is not None
            and now - stored > self.ttl_seconds
            and not self._in_flight(definitions.get(quiz_id))
        )

    def _expire_locked(self, definitions: dict, now: float) -> int:
        stale = [q for q in list(definitions) if self._expired(definitions, q, now)]
        for quiz_id in stale:
            self._drop_locked(definitions, quiz_id)
        self.expirations += len(stale)
        return len(stale)

    def _evict_oldest_locked(self, definitions: dict, candidates) -> bool:
        """Evicts the oldest evictable candidate (never an in-flight submit)."""
        evictable = sorted(
            (self._stored_at.get(q, 0.0), q)
            for q in candidates
            if q in definitions and not self._in_flight(definitions[q])
        )
        if not evictable:
            return False
        self._drop_locked(definitions, evictable[0][1])
        self.evictions += 1
        return True

    def put(self, definitions: dict, quiz_id: str, definition: dict) -> int:
        """Stores a new pending quiz, evicting the user's oldest pending quiz
        (then the globally oldest) if a cap would be exceeded. Returns the
        number of evictions."""
        user_id = definition.get("user_id")
        with self._lock:
            now = self._clock()
            self._last_prune = now
            self._sync_locked(definitions, now)
            self._expire_locked(definitions, now)
            evicted = 0

            while True:
                mine = [q for q, d in definitions.items() if isinstance(d, dict) and d.get("user_id") == user_id]
                if len(mine) < self.max_per_user:
                    break
                if not self._evict_oldest_locked(definitions, mine):
                    break
                evicted += 1

            while len(definitions) >= self.max_global:
                if not self._evict_oldest_locked(definitions, list(definitions)):
                    break
                evicted += 1

            definitions[quiz_id] = definition
            self._stored_at[quiz_id] = now
        if evicted:
            logger.info("quiz_definitions: evicted %d pending quiz(zes) to stay within caps", evicted)
        return evicted

    def get(self, definitions: dict, quiz_id: str):
        """Returns the definition or None if unknown/expired (an expired one is
        removed so the caller gets the same "not found" path as a restart)."""
        with self._lock:
            now = self._clock()
            if now - self._last_prune >= PRUNE_INTERVAL_SECONDS:
                self._last_prune = now
                self._sync_locked(definitions, now)
                self._expire_locked(definitions, now)
            definition = definitions.get(quiz_id)
            if definition is None:
                self._stored_at.pop(quiz_id, None)
                return None
            self._stored_at.setdefault(quiz_id, now)
            if self._expired(definitions, quiz_id, now):
                self._drop_locked(definitions, quiz_id)
                self.expirations += 1
                return None
            return definition

    def prune(self, definitions: dict) -> int:
        with self._lock:
            now = self._clock()
            self._last_prune = now
            self._sync_locked(definitions, now)
            return self._expire_locked(definitions, now)

    def stats(self, definitions: dict) -> dict:
        with self._lock:
            return {
                "definitions": len(definitions),
                "evictions": self.evictions,
                "expirations": self.expirations,
            }
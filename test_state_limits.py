"""
Pre-Phase 10 (finding H3): bounded, expiring in-memory state.

Pure unit tests with an injected clock - no sleeping, no network. They prove
the lifecycle rules in backend/state_limits.py: idle expiry, per-user and
global caps, least-recently-used eviction, user isolation, safe handling of
entries inserted behind the manager's back (as older tests and any future
code might), and that a pending quiz mid-submit is never evicted.
"""
import pytest

from backend import state_limits as sl


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make_pdf(clock, **kw):
    params = dict(ttl_seconds=100, max_per_user=3, max_chars_per_user=1000, max_chars_global=2500, clock=clock)
    params.update(kw)
    return sl.BoundedPdfContexts(**params)


class TestPdfContexts:
    def test_store_and_read_back(self):
        clock, store = Clock(), {}
        m = make_pdf(clock)
        m.put(store, "u1", "c1", "hello")
        assert m.get(store, "u1", "c1") == "hello"
        assert store == {"u1": {"c1": "hello"}}, "the store stays a plain dict of dicts"

    def test_missing_entries_return_empty_string_not_an_error(self):
        m = make_pdf(Clock())
        assert m.get({}, "u1", "c1") == ""
        assert m.get({}, "u1", "") == ""

    def test_users_are_isolated(self):
        store, m = {}, make_pdf(Clock())
        m.put(store, "u1", "c1", "secret-1")
        assert m.get(store, "u2", "c1") == ""
        m.discard(store, "u2", "c1")
        assert m.get(store, "u1", "c1") == "secret-1", "another user's clear must not touch it"

    def test_idle_ttl_expires_and_removes_the_entry(self):
        clock = Clock(); store = {}; m = make_pdf(clock)
        m.put(store, "u1", "c1", "doc")
        clock.advance(101)
        assert m.get(store, "u1", "c1") == ""
        assert "c1" not in store.get("u1", {}), "expired text must actually be freed"

    def test_a_hit_refreshes_the_idle_timer(self):
        clock = Clock(); store = {}; m = make_pdf(clock)
        m.put(store, "u1", "c1", "doc")
        for _ in range(5):
            clock.advance(60)                      # always inside the 100s idle window
            assert m.get(store, "u1", "c1") == "doc"

    def test_prune_removes_expired_entries_and_empty_users(self):
        clock = Clock(); store = {}; m = make_pdf(clock)
        m.put(store, "u1", "c1", "doc")
        clock.advance(500)
        assert m.prune(store) == 1
        assert store == {}

    def test_per_user_entry_cap_evicts_the_least_recently_used(self):
        clock = Clock(); store = {}; m = make_pdf(clock, max_per_user=3)
        for i in range(3):
            m.put(store, "u1", f"c{i}", "x")
            clock.advance(1)
        m.get(store, "u1", "c0")                   # c0 is now the most recently used
        clock.advance(1)
        evicted = m.put(store, "u1", "c3", "x")
        assert evicted == 1
        assert set(store["u1"]) == {"c0", "c2", "c3"}, "c1 was the least recently used"

    def test_unbounded_chat_ids_cannot_grow_one_users_memory(self):
        clock = Clock(); store = {}; m = make_pdf(clock, max_per_user=5, max_chars_per_user=10**9, max_chars_global=10**9)
        for i in range(500):                       # the verified attack: a fresh chat_id each time
            m.put(store, "attacker", f"chat-{i}", "A" * 100)
            clock.advance(0.01)
        assert len(store["attacker"]) == 5

    def test_per_user_character_budget(self):
        clock = Clock(); store = {}; m = make_pdf(clock, max_per_user=10, max_chars_per_user=1000)
        m.put(store, "u1", "a", "x" * 600); clock.advance(1)
        m.put(store, "u1", "b", "x" * 600)
        assert set(store["u1"]) == {"b"}, "adding b would exceed the user's budget, so the older a goes"

    def test_global_character_budget_evicts_across_users(self):
        clock = Clock(); store = {}; m = make_pdf(clock, max_per_user=5, max_chars_per_user=2000, max_chars_global=2500)
        m.put(store, "u1", "a", "x" * 1000); clock.advance(1)
        m.put(store, "u2", "b", "x" * 1000); clock.advance(1)
        m.put(store, "u3", "c", "x" * 1000)
        assert sum(len(t) for chats in store.values() for t in chats.values()) <= 2500
        assert m.get(store, "u3", "c") != "", "the newest entry always survives"
        assert m.get(store, "u1", "a") == "", "the oldest one was evicted"

    def test_replacing_a_chats_document_does_not_count_twice(self):
        store, m = {}, make_pdf(Clock(), max_per_user=1, max_chars_per_user=1000)
        m.put(store, "u1", "c1", "x" * 900)
        evicted = m.put(store, "u1", "c1", "y" * 900)
        assert evicted == 0 and store["u1"]["c1"] == "y" * 900

    def test_a_document_that_can_never_fit_is_refused_not_stored(self):
        store, m = {}, make_pdf(Clock(), max_chars_per_user=100)
        with pytest.raises(sl.ContextCapacityError):
            m.put(store, "u1", "c1", "x" * 101)
        assert store == {}

    def test_entries_inserted_directly_are_adopted_not_crashed_on_or_instantly_expired(self):
        clock = Clock(); store = {"u1": {"c1": "inserted by a test"}}; m = make_pdf(clock)
        assert m.get(store, "u1", "c1") == "inserted by a test"
        store.clear()                              # replaced behind the manager's back
        assert m.get(store, "u1", "c1") == ""
        m.put(store, "u1", "c2", "ok")             # stale metadata is dropped, no error
        assert store == {"u1": {"c2": "ok"}}

    def test_discard_is_safe_when_nothing_is_stored(self):
        m = make_pdf(Clock())
        m.discard({}, "u1", "nope")

    def test_stats_report_counts_only(self):
        store, m = {}, make_pdf(Clock())
        m.put(store, "u1", "c1", "abc")
        s = m.stats(store)
        assert s["contexts"] == 1 and s["chars"] == 3 and set(s) == {"users", "contexts", "chars", "evictions", "expirations"}


def make_quiz(clock, **kw):
    params = dict(ttl_seconds=100, max_per_user=3, max_global=6, clock=clock)
    params.update(kw)
    return sl.BoundedQuizDefinitions(**params)


def defn(user="u1", submitted=False, created_at=0.0):
    return {"user_id": user, "submitted": submitted, "created_at": created_at, "questions": []}


class TestQuizDefinitions:
    def test_store_and_get(self):
        store, m = {}, make_quiz(Clock())
        d = defn()
        m.put(store, "q1", d)
        assert m.get(store, "q1") is d

    def test_unknown_quiz_is_none(self):
        assert make_quiz(Clock()).get({}, "nope") is None

    def test_ttl_is_measured_from_when_the_server_stored_it_not_from_a_field_inside_it(self):
        clock, store = Clock(), {}
        # a definition with a fabricated ancient created_at is still fresh when just stored/adopted
        m = make_quiz(clock)
        m.put(store, "q1", defn(created_at=0.0))
        assert m.get(store, "q1") is not None
        clock.advance(101)
        assert m.get(store, "q1") is None and "q1" not in store

    def test_per_user_cap_evicts_that_users_oldest(self):
        clock, store = Clock(), {}
        m = make_quiz(clock, max_per_user=3, max_global=100)
        for i in range(4):
            m.put(store, f"q{i}", defn("u1")); clock.advance(1)
        assert set(store) == {"q1", "q2", "q3"}

    def test_one_user_cannot_evict_another_users_quiz(self):
        clock, store = Clock(), {}
        m = make_quiz(clock, max_per_user=2, max_global=100)
        m.put(store, "victim", defn("u2")); clock.advance(1)
        for i in range(10):
            m.put(store, f"a{i}", defn("u1")); clock.advance(1)
        assert "victim" in store

    def test_global_cap(self):
        clock, store = Clock(), {}
        m = make_quiz(clock, max_per_user=100, max_global=4)
        for i in range(10):
            m.put(store, f"q{i}", defn(f"user{i}")); clock.advance(1)
        assert len(store) <= 4 and "q9" in store

    def test_a_quiz_mid_submit_is_never_evicted_or_expired(self):
        clock, store = Clock(), {}
        m = make_quiz(clock, max_per_user=1, max_global=100)
        m.put(store, "busy", defn("u1", submitted=True))
        m.put(store, "new", defn("u1"))
        assert "busy" in store, "an in-flight submit owns its entry until main.py pops it"
        clock.advance(10_000)
        assert m.get(store, "busy") is not None

    def test_prune_and_stats(self):
        clock, store = Clock(), {}
        m = make_quiz(clock)
        m.put(store, "q1", defn()); clock.advance(500)
        assert m.prune(store) == 1 and store == {}
        assert set(m.stats(store)) == {"definitions", "evictions", "expirations"}


class TestConfiguration:
    def test_env_int_falls_back_safely(self, monkeypatch):
        monkeypatch.setenv("X_LIMIT", "banana")
        assert sl._env_int("X_LIMIT", 7) == 7
        monkeypatch.setenv("X_LIMIT", "0")
        assert sl._env_int("X_LIMIT", 7) == 7
        monkeypatch.setenv("X_LIMIT", "12")
        assert sl._env_int("X_LIMIT", 7) == 12
        monkeypatch.delenv("X_LIMIT")
        assert sl._env_int("X_LIMIT", 7) == 7

    def test_defaults_are_sane_relative_to_the_upload_caps(self):
        from backend import rag_engine
        assert sl.PDF_CONTEXT_MAX_CHARS_PER_USER >= rag_engine.DEFAULT_MAX_EXTRACTED_CHARS, \
            "a single maximum-size document must always fit the per-user budget"
        assert sl.PDF_CONTEXT_MAX_CHARS_GLOBAL >= sl.PDF_CONTEXT_MAX_CHARS_PER_USER
        assert sl.MAX_CHAT_ID_CHARS >= 64


class TestThreadSafety:
    """The stores are touched from the event loop AND from threadpool work."""

    def test_concurrent_puts_gets_and_discards_keep_every_invariant(self):
        import threading
        store = {}
        m = sl.BoundedPdfContexts(ttl_seconds=10_000, max_per_user=4, max_chars_per_user=10_000, max_chars_global=60_000)
        errors = []

        def worker(uid):
            try:
                for i in range(300):
                    m.put(store, f"user-{uid}", f"chat-{i % 25}", "x" * (50 + i % 40))
                    m.get(store, f"user-{uid}", f"chat-{(i * 7) % 25}")
                    if i % 11 == 0:
                        m.discard(store, f"user-{uid}", f"chat-{i % 25}")
                    if i % 53 == 0:
                        m.prune(store)
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker, args=(u,)) for u in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert errors == []
        assert all(len(chats) <= 4 for chats in store.values())
        assert sum(len(t) for chats in store.values() for t in chats.values()) <= 60_000

    def test_concurrent_quiz_puts_respect_the_caps(self):
        import threading
        store = {}
        m = sl.BoundedQuizDefinitions(ttl_seconds=10_000, max_per_user=5, max_global=30)

        def worker(uid):
            for i in range(200):
                m.put(store, f"q-{uid}-{i}", {"user_id": f"u{uid}", "submitted": False})
                m.get(store, f"q-{uid}-{i}")

        threads = [threading.Thread(target=worker, args=(u,)) for u in range(10)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert len(store) <= 30
        per_user = {}
        for d in store.values():
            per_user[d["user_id"]] = per_user.get(d["user_id"], 0) + 1
        assert max(per_user.values()) <= 5
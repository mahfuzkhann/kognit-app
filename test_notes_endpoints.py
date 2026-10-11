"""
Tests for the /api/notes routes in backend/main.py (Pocket Notes).

Run with: GEMINI_API_KEY=dummy python3 -m pytest test_notes_endpoints.py -v

Auth is stubbed with dependency_overrides on get_current_user_and_token (same
convention as test_student_profile_endpoint.py). The database layer is
replaced by an in-memory fake that ENFORCES OWNERSHIP PER USER, standing in
for RLS - so the cross-user tests assert real isolation behaviour at the
endpoint contract, not just that a mock was called. The real PostgREST
request shape (including the user_id filters) is covered in
test_notes_database.py; the policy SQL itself in test_schema_contract.py.
"""
import json
import os
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

import pytest
from fastapi.testclient import TestClient

from backend import main as main_module
from backend import notes_ai, notes_engine
from backend.database import DatabaseError

ALICE = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
BOB = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
LONG_NOTE = ("Newton's second law states that the net force on an object equals mass times acceleration, "
             "F = ma. A larger force gives a larger acceleration. The SI unit of force is the newton, "
             "defined as one kilogram metre per second squared.")


class FakeNotesDB:
    """In-memory stand-in for the Pocket Notes database section. Every
    operation is scoped to the user_id it is called with - exactly the
    guarantee RLS + the user_id filters provide in production."""

    def __init__(self):
        self.rows = {}          # (user_id, note_id) -> row
        self.fail = None        # name of an operation that should raise DatabaseError
        self.calls = []

    def _maybe_fail(self, op):
        self.calls.append(op)
        if self.fail == op:
            raise DatabaseError(f"boom {op} with secret internals")

    def _owned(self, user_id):
        return [r for (u, _), r in self.rows.items() if u == user_id]

    async def create(self, token, user_id, note):
        self._maybe_fail("create")
        for r in self._owned(user_id):
            if r["client_id"] == note["client_id"]:
                return None
        row = {
            "id": str(uuid.uuid4()), "takeaway": None, "question_md": None, "sources": [], "tags": [],
            "needs_review": False, "source_ref": None, "flashcards": None, "mini_quiz": None,
            "created_at": f"2026-10-10T10:{len(self.rows):02d}:00+00:00", "updated_at": "2026-10-10T10:00:00+00:00",
        }
        row.update({k: v for k, v in note.items() if k != "user_id"})
        self.rows[(user_id, row["id"])] = row
        return dict(row)

    async def by_client_id(self, token, user_id, client_id):
        self._maybe_fail("by_client_id")
        for r in self._owned(user_id):
            if r["client_id"] == client_id:
                return dict(r)
        return None

    async def count(self, token, user_id):
        self._maybe_fail("count")
        return len(self._owned(user_id))

    async def list(self, token, user_id, limit, offset):
        self._maybe_fail("list")
        owned = sorted(self._owned(user_id), key=lambda r: r["created_at"], reverse=True)
        page = owned[offset:offset + limit + 1]
        return [dict(r) for r in page[:limit]], len(page) > limit

    async def get(self, token, user_id, note_id):
        self._maybe_fail("get")
        row = self.rows.get((user_id, note_id))
        return dict(row) if row else None

    async def update(self, token, user_id, note_id, fields):
        self._maybe_fail("update")
        row = self.rows.get((user_id, note_id))
        if not row:
            return None
        row.update(fields)
        return dict(row)

    async def delete(self, token, user_id, note_id):
        self._maybe_fail("delete")
        return self.rows.pop((user_id, note_id), None) is not None


@pytest.fixture
def db():
    fake = FakeNotesDB()
    with patch.object(main_module, "create_pocket_note", new=fake.create), \
         patch.object(main_module, "get_pocket_note_by_client_id", new=fake.by_client_id), \
         patch.object(main_module, "count_pocket_notes", new=fake.count), \
         patch.object(main_module, "list_pocket_notes", new=fake.list), \
         patch.object(main_module, "get_pocket_note", new=fake.get), \
         patch.object(main_module, "update_pocket_note", new=fake.update), \
         patch.object(main_module, "delete_pocket_note", new=fake.delete), \
         patch.object(main_module, "_get_academic_context",
                      new=AsyncMock(return_value=("Class 9-10 (SSC)", "Science (বিজ্ঞান)"))):
        yield fake


@pytest.fixture
def client():
    return TestClient(main_module.app)


@pytest.fixture(autouse=True)
def clean_state():
    main_module._rate_limit_buckets.clear()
    yield
    main_module.app.dependency_overrides.clear()
    main_module._rate_limit_buckets.clear()


def as_user(user_id, token="test-token"):
    async def _fake():
        return (user_id, token)
    main_module.app.dependency_overrides[main_module.get_current_user_and_token] = _fake


def body(**overrides):
    base = {"client_id": "n_" + uuid.uuid4().hex[:12], "content_md": LONG_NOTE, "question_md": "Explain this in physics"}
    base.update(overrides)
    return base


def save(client, **overrides):
    resp = client.post("/api/notes", json=body(**overrides))
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["note"]


def stub_ai(**kwargs):
    return patch.object(notes_ai, "get_provider", return_value=MagicMock(**kwargs))


FLASH = [{"front": f"Q{i}", "back": f"A{i}"} for i in range(6)]
QUIZ = [{"question": f"Q{i}?", "options": ["a", "b", "c", "d"], "correct_index": 1, "explanation": "e"} for i in range(5)]


# ---------------------------------------------------------------------------
# authentication
# ---------------------------------------------------------------------------

class TestAuthenticationRequired:
    NOTE = "123e4567-e89b-12d3-a456-426614174000"

    @pytest.mark.parametrize("method,path", [
        ("post", "/api/notes"), ("get", "/api/notes"), ("get", f"/api/notes/{NOTE}"),
        ("patch", f"/api/notes/{NOTE}"), ("delete", f"/api/notes/{NOTE}"),
        ("post", f"/api/notes/{NOTE}/suggest-tags"), ("post", f"/api/notes/{NOTE}/flashcards"),
        ("post", f"/api/notes/{NOTE}/mini-quiz"),
    ])
    def test_every_route_rejects_unauthenticated(self, client, db, method, path):
        resp = getattr(client, method)(path, **({"json": {}} if method in ("post", "patch") else {}))
        assert resp.status_code == 401
        assert db.calls == []                       # nothing reached the data layer


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------

class TestCreate:
    def test_creates_note_with_deterministic_tags(self, client, db):
        as_user(ALICE)
        resp = client.post("/api/notes", json=body(question_md="Explain this in physics"))
        assert resp.status_code == 201
        data = resp.json()
        assert data["created"] is True
        tags = {(t["kind"], t["label"], t["origin"]) for t in data["note"]["tags"]}
        assert ("class", "Class 9-10 (SSC)", "known") in tags
        assert ("subject", "Physics", "inferred") in tags
        assert data["note"]["needs_review"] is False

    def test_no_subject_is_flagged_for_review_not_guessed(self, client, db):
        as_user(ALICE)
        note = save(client, question_md="What is Newton's second law?", content_md=LONG_NOTE)
        assert note["needs_review"] is True
        assert not any(t["kind"] == "subject" for t in note["tags"])

    def test_no_ai_call_is_made_on_save(self, client, db):
        as_user(ALICE)
        with stub_ai() as get_provider:
            save(client)
        get_provider.assert_not_called()

    def test_idempotent_retry_returns_existing_note(self, client, db):
        as_user(ALICE)
        payload = body()
        first = client.post("/api/notes", json=payload)
        second = client.post("/api/notes", json=payload)
        assert first.status_code == 201 and second.status_code == 200
        assert second.json()["created"] is False
        assert second.json()["note"]["id"] == first.json()["note"]["id"]
        assert len(db._owned(ALICE)) == 1

    def test_retry_does_not_overwrite_edited_tags(self, client, db):
        as_user(ALICE)
        payload = body()
        note = client.post("/api/notes", json=payload).json()["note"]
        client.patch(f"/api/notes/{note['id']}", json={"tags": [{"label": "Mine", "kind": "other"}]})
        again = client.post("/api/notes", json=payload).json()["note"]
        assert [t["label"] for t in again["tags"]] == ["Mine"]

    def test_same_client_id_for_different_users_creates_two_notes(self, client, db):
        payload = body()
        as_user(ALICE)
        assert client.post("/api/notes", json=payload).status_code == 201
        as_user(BOB)
        assert client.post("/api/notes", json=payload).status_code == 201

    def test_race_on_insert_returns_the_winning_note(self, client, db):
        as_user(ALICE)
        payload = body()
        winner = save(client, **payload)
        # Simulate: the existence pre-check missed it, but the insert hits the unique constraint.
        original = db.by_client_id
        calls = {"n": 0}

        async def flaky(token, user_id, client_id):
            calls["n"] += 1
            return None if calls["n"] == 1 else await original(token, user_id, client_id)

        with patch.object(main_module, "get_pocket_note_by_client_id", new=flaky):
            resp = client.post("/api/notes", json=payload)
        assert resp.status_code == 200 and resp.json()["note"]["id"] == winner["id"]

    def test_body_user_id_is_ignored_note_belongs_to_token_user(self, client, db):
        as_user(ALICE)
        resp = client.post("/api/notes", json=body(user_id=BOB))
        assert resp.status_code == 201
        assert len(db._owned(ALICE)) == 1 and db._owned(BOB) == []

    @pytest.mark.parametrize("payload", [
        {}, {"client_id": "x"}, {"client_id": "n_abcdef123456"},
        {"client_id": "n_abcdef123456", "content_md": ""},
        {"client_id": "n_abcdef123456", "content_md": 5},
        {"client_id": "n_abcdef123456", "content_md": "x" * 20001},
    ])
    def test_invalid_payload_rejected_400(self, client, db, payload):
        as_user(ALICE)
        resp = client.post("/api/notes", json=payload)
        assert resp.status_code == 400
        assert "detail" in resp.json()
        assert db._owned(ALICE) == []

    @pytest.mark.parametrize("raw", [b"", b"not json", b"[1,2]", b'"str"'])
    def test_malformed_body_rejected_400(self, client, db, raw):
        as_user(ALICE)
        resp = client.post("/api/notes", content=raw, headers={"content-type": "application/json"})
        assert resp.status_code == 400

    def test_oversized_request_body_rejected_413(self, client, db):
        as_user(ALICE)
        resp = client.post("/api/notes", content=b"{" + b" " * (notes_engine.MAX_REQUEST_BODY_BYTES + 10) + b"}",
                           headers={"content-type": "application/json"})
        assert resp.status_code == 413

    def test_per_user_note_cap_enforced(self, client, db):
        as_user(ALICE)
        with patch.object(notes_engine, "MAX_NOTES_PER_USER", 2):
            save(client)
            save(client)
            resp = client.post("/api/notes", json=body())
        assert resp.status_code == 409
        assert len(db._owned(ALICE)) == 2

    def test_cap_does_not_block_idempotent_retry(self, client, db):
        as_user(ALICE)
        payload = body()
        with patch.object(notes_engine, "MAX_NOTES_PER_USER", 1):
            assert client.post("/api/notes", json=payload).status_code == 201
            assert client.post("/api/notes", json=payload).status_code == 200

    def test_responses_are_not_cacheable(self, client, db):
        as_user(ALICE)
        resp = client.post("/api/notes", json=body())
        assert resp.headers["cache-control"] == "no-store"

    def test_database_failure_is_502_without_internals(self, client, db):
        as_user(ALICE)
        db.fail = "create"
        resp = client.post("/api/notes", json=body())
        assert resp.status_code == 502
        assert "secret internals" not in resp.text and "boom" not in resp.text

    def test_profile_lookup_failure_still_saves_with_unknown_class(self, client, db):
        as_user(ALICE)
        with patch.object(main_module, "_get_academic_context", new=AsyncMock(return_value=(None, None))):
            note = save(client, question_md="physics q")
        assert not any(t["kind"] in ("class", "stream") for t in note["tags"])


# ---------------------------------------------------------------------------
# list / get
# ---------------------------------------------------------------------------

class TestListAndGet:
    def test_list_returns_only_own_notes(self, client, db):
        as_user(ALICE)
        a = save(client)
        as_user(BOB)
        save(client)
        as_user(ALICE)
        notes = client.get("/api/notes").json()["notes"]
        assert [n["id"] for n in notes] == [a["id"]]

    def test_list_response_never_contains_user_id(self, client, db):
        as_user(ALICE)
        save(client)
        assert "user_id" not in client.get("/api/notes").text

    def test_pagination_bounds(self, client, db):
        as_user(ALICE)
        for _ in range(5):
            save(client)
        page = client.get("/api/notes?limit=2&offset=0").json()
        assert len(page["notes"]) == 2 and page["has_more"] is True
        last = client.get("/api/notes?limit=2&offset=4").json()
        assert len(last["notes"]) == 1 and last["has_more"] is False

    def test_limit_clamped_and_garbage_tolerated(self, client, db):
        as_user(ALICE)
        assert client.get("/api/notes?limit=9999").json()["limit"] == notes_engine.MAX_PAGE_SIZE
        assert client.get("/api/notes?limit=abc&offset=-9").json()["limit"] == notes_engine.DEFAULT_PAGE_SIZE
        assert client.get("/api/notes?offset=99999999").json()["offset"] == notes_engine.MAX_OFFSET

    def test_get_own_note(self, client, db):
        as_user(ALICE)
        note = save(client)
        resp = client.get(f"/api/notes/{note['id']}")
        assert resp.status_code == 200 and resp.json()["note"]["id"] == note["id"]

    def test_get_other_users_note_is_404_identical_to_missing(self, client, db):
        as_user(ALICE)
        note = save(client)
        as_user(BOB)
        theirs = client.get(f"/api/notes/{note['id']}")
        missing = client.get(f"/api/notes/{uuid.uuid4()}")
        assert theirs.status_code == missing.status_code == 404
        assert theirs.json() == missing.json()

    @pytest.mark.parametrize("bad_id", ["not-a-uuid", "1", "eq.1", "%27%20or%201=1", "123e4567-e89b-12d3-a456-42661417400g"])
    def test_malformed_ids_are_404_and_never_reach_the_database(self, client, db, bad_id):
        as_user(ALICE)
        assert client.get(f"/api/notes/{bad_id}").status_code == 404
        assert "get" not in db.calls

    def test_list_database_failure_is_502(self, client, db):
        as_user(ALICE)
        db.fail = "list"
        resp = client.get("/api/notes")
        assert resp.status_code == 502 and "secret" not in resp.text


# ---------------------------------------------------------------------------
# update tags / delete
# ---------------------------------------------------------------------------

class TestUpdateTags:
    def test_student_edit_keeps_known_provenance_and_marks_new_tags_student(self, client, db):
        as_user(ALICE)
        note = save(client, question_md="physics")
        keep = [t for t in note["tags"] if t["kind"] == "class"]
        resp = client.patch(f"/api/notes/{note['id']}", json={"tags": keep + [{"label": "Vector", "kind": "topic"}]})
        assert resp.status_code == 200
        origins = {t["label"]: t["origin"] for t in resp.json()["note"]["tags"]}
        assert origins["Class 9-10 (SSC)"] == "known" and origins["Vector"] == "student"

    def test_removing_the_subject_tag_flags_needs_review(self, client, db):
        as_user(ALICE)
        note = save(client, question_md="physics")
        assert note["needs_review"] is False
        resp = client.patch(f"/api/notes/{note['id']}", json={"tags": []})
        assert resp.json()["note"]["needs_review"] is True

    def test_student_can_correct_by_adding_a_subject(self, client, db):
        as_user(ALICE)
        note = save(client, question_md="no keyword here")
        assert note["needs_review"] is True
        resp = client.patch(f"/api/notes/{note['id']}", json={"tags": [{"label": "Physics", "kind": "subject"}]})
        assert resp.json()["note"]["needs_review"] is False

    def test_content_is_immutable_via_patch(self, client, db):
        as_user(ALICE)
        note = save(client)
        resp = client.patch(f"/api/notes/{note['id']}", json={"tags": [], "content_md": "TAMPERED", "user_id": BOB})
        assert resp.status_code == 200
        assert db.rows[(ALICE, note["id"])]["content_md"] == LONG_NOTE

    @pytest.mark.parametrize("payload", [{}, {"tags": "x"}, {"tags": [{"label": "<b>", "kind": "topic"}]},
                                         {"tags": [{"label": "ok", "kind": "weird"}]},
                                         {"tags": [{"label": f"t{i}", "kind": "other"} for i in range(13)]}])
    def test_invalid_tag_payload_400(self, client, db, payload):
        as_user(ALICE)
        note = save(client)
        assert client.patch(f"/api/notes/{note['id']}", json=payload).status_code == 400

    def test_other_users_note_cannot_be_edited(self, client, db):
        as_user(ALICE)
        note = save(client)
        before = list(db.rows[(ALICE, note["id"])]["tags"])
        as_user(BOB)
        resp = client.patch(f"/api/notes/{note['id']}", json={"tags": [{"label": "Hacked", "kind": "other"}]})
        assert resp.status_code == 404
        assert db.rows[(ALICE, note["id"])]["tags"] == before


class TestDelete:
    def test_delete_own_note(self, client, db):
        as_user(ALICE)
        note = save(client)
        assert client.delete(f"/api/notes/{note['id']}").json() == {"deleted": True}
        assert client.get(f"/api/notes/{note['id']}").status_code == 404

    def test_double_delete_is_404(self, client, db):
        as_user(ALICE)
        note = save(client)
        client.delete(f"/api/notes/{note['id']}")
        assert client.delete(f"/api/notes/{note['id']}").status_code == 404

    def test_cannot_delete_other_users_note(self, client, db):
        as_user(ALICE)
        note = save(client)
        as_user(BOB)
        assert client.delete(f"/api/notes/{note['id']}").status_code == 404
        assert (ALICE, note["id"]) in db.rows            # survived

    def test_delete_database_failure_is_502(self, client, db):
        as_user(ALICE)
        note = save(client)
        db.fail = "delete"
        assert client.delete(f"/api/notes/{note['id']}").status_code == 502

    def test_write_rate_limit(self, client, db):
        as_user(ALICE)
        for _ in range(30):
            assert client.post("/api/notes", json=body()).status_code == 201
        resp = client.post("/api/notes", json=body())
        assert resp.status_code == 429 and "Retry-After" in resp.headers


# ---------------------------------------------------------------------------
# AI study material
# ---------------------------------------------------------------------------

def _provider_text(text):
    return stub_ai(generate=MagicMock(return_value=MagicMock(text=text)))


class TestFlashcards:
    def test_generates_validates_and_persists(self, client, db):
        as_user(ALICE)
        note = save(client)
        with _provider_text(json.dumps(FLASH)):
            resp = client.post(f"/api/notes/{note['id']}/flashcards", json={})
        assert resp.status_code == 200
        assert resp.json()["flashcards"] == FLASH and resp.json()["regenerated"] is True
        assert db.rows[(ALICE, note["id"])]["flashcards"] == FLASH

    def test_stored_flashcards_returned_without_ai_call_or_rate_budget(self, client, db):
        as_user(ALICE)
        note = save(client)
        with _provider_text(json.dumps(FLASH)):
            client.post(f"/api/notes/{note['id']}/flashcards", json={})
        with stub_ai() as get_provider:
            for _ in range(20):                                   # far above the notes_ai limit
                resp = client.post(f"/api/notes/{note['id']}/flashcards", json={})
                assert resp.status_code == 200 and resp.json()["regenerated"] is False
        get_provider.assert_not_called()

    def test_explicit_regenerate_calls_provider_again(self, client, db):
        as_user(ALICE)
        note = save(client)
        with _provider_text(json.dumps(FLASH)):
            client.post(f"/api/notes/{note['id']}/flashcards", json={})
        new_cards = [{"front": f"N{i}", "back": "x"} for i in range(5)]
        with _provider_text(json.dumps(new_cards)):
            resp = client.post(f"/api/notes/{note['id']}/flashcards", json={"regenerate": True})
        assert resp.json()["flashcards"] == new_cards
        assert db.rows[(ALICE, note["id"])]["flashcards"] == new_cards

    def test_regenerate_must_be_literal_true(self, client, db):
        as_user(ALICE)
        note = save(client)
        with _provider_text(json.dumps(FLASH)):
            client.post(f"/api/notes/{note['id']}/flashcards", json={})
        with stub_ai() as get_provider:
            resp = client.post(f"/api/notes/{note['id']}/flashcards", json={"regenerate": "true"})
        assert resp.json()["regenerated"] is False
        get_provider.assert_not_called()

    def test_malformed_output_is_502_and_nothing_stored(self, client, db):
        as_user(ALICE)
        note = save(client)
        with _provider_text("this is not json"):
            resp = client.post(f"/api/notes/{note['id']}/flashcards", json={})
        assert resp.status_code == 502
        assert db.rows[(ALICE, note["id"])]["flashcards"] is None

    @pytest.mark.parametrize("kind,status", [
        ("RATE_LIMITED", 429), ("TIMEOUT", 504), ("UNAVAILABLE", 503), ("AUTH", 502), ("UNKNOWN", 502),
    ])
    def test_provider_failures_map_to_safe_statuses(self, client, db, kind, status):
        from backend.providers import ProviderError
        from backend.providers.base import ProviderErrorKind
        as_user(ALICE)
        note = save(client)
        err = ProviderError(getattr(ProviderErrorKind, kind), provider="mock")
        with stub_ai(generate=MagicMock(side_effect=err)):
            resp = client.post(f"/api/notes/{note['id']}/flashcards", json={})
        assert resp.status_code == status
        assert "mock" not in resp.text and "kind=" not in resp.text

    def test_too_short_note_422_without_provider_call(self, client, db):
        as_user(ALICE)
        note = save(client, content_md="Too short.")
        with stub_ai() as get_provider:
            resp = client.post(f"/api/notes/{note['id']}/flashcards", json={})
        assert resp.status_code == 422
        get_provider.assert_not_called()

    def test_other_users_note_404_and_no_provider_call(self, client, db):
        as_user(ALICE)
        note = save(client)
        as_user(BOB)
        with stub_ai() as get_provider:
            resp = client.post(f"/api/notes/{note['id']}/flashcards", json={})
        assert resp.status_code == 404
        get_provider.assert_not_called()

    def test_ai_rate_limit(self, client, db):
        as_user(ALICE)
        note = save(client)
        with _provider_text(json.dumps(FLASH)):
            for _ in range(6):
                assert client.post(f"/api/notes/{note['id']}/flashcards", json={"regenerate": True}).status_code == 200
            resp = client.post(f"/api/notes/{note['id']}/flashcards", json={"regenerate": True})
        assert resp.status_code == 429 and "Retry-After" in resp.headers

    def test_persist_failure_is_502(self, client, db):
        as_user(ALICE)
        note = save(client)
        db.fail = "update"
        with _provider_text(json.dumps(FLASH)):
            resp = client.post(f"/api/notes/{note['id']}/flashcards", json={})
        assert resp.status_code == 502

    def test_malformed_request_body_400(self, client, db):
        as_user(ALICE)
        note = save(client)
        resp = client.post(f"/api/notes/{note['id']}/flashcards", content=b"[1]", headers={"content-type": "application/json"})
        assert resp.status_code == 400


class TestMiniQuiz:
    def test_generates_and_persists(self, client, db):
        as_user(ALICE)
        note = save(client)
        with _provider_text(json.dumps(QUIZ)):
            resp = client.post(f"/api/notes/{note['id']}/mini-quiz", json={})
        assert resp.status_code == 200
        assert len(resp.json()["mini_quiz"]) == 5
        assert db.rows[(ALICE, note["id"])]["mini_quiz"] == resp.json()["mini_quiz"]

    def test_invalid_answer_keys_never_stored(self, client, db):
        as_user(ALICE)
        note = save(client)
        bad = [{"question": f"Q{i}?", "options": ["a", "b", "c", "d"], "correct_index": 7} for i in range(5)]
        with _provider_text(json.dumps(bad)):
            resp = client.post(f"/api/notes/{note['id']}/mini-quiz", json={})
        assert resp.status_code == 502
        assert db.rows[(ALICE, note["id"])]["mini_quiz"] is None

    def test_mini_quiz_never_touches_learning_evidence(self, client, db):
        """Approved decision: self-graded only, no quiz_attempts / mastery /
        learning-evidence writes. Every existing evidence writer is patched
        and must stay uncalled across the whole generate + regenerate flow."""
        as_user(ALICE)
        note = save(client)
        names = ["save_quiz_attempt", "save_chat_learning_evidence", "save_conversation_index_entry"]
        mocks = {n: AsyncMock() for n in names}
        with patch.multiple(main_module, **mocks), _provider_text(json.dumps(QUIZ)):
            client.post(f"/api/notes/{note['id']}/mini-quiz", json={})
            client.post(f"/api/notes/{note['id']}/mini-quiz", json={"regenerate": True})
        for name, mock in mocks.items():
            mock.assert_not_called()

    def test_stored_quiz_reused(self, client, db):
        as_user(ALICE)
        note = save(client)
        with _provider_text(json.dumps(QUIZ)):
            client.post(f"/api/notes/{note['id']}/mini-quiz", json={})
        with stub_ai() as get_provider:
            resp = client.post(f"/api/notes/{note['id']}/mini-quiz", json={})
        assert resp.json()["regenerated"] is False
        get_provider.assert_not_called()


class TestSuggestTags:
    def test_returns_at_most_three_and_does_not_persist(self, client, db):
        as_user(ALICE)
        note = save(client)
        before = list(db.rows[(ALICE, note["id"])]["tags"])
        with _provider_text(json.dumps({"topics": ["One", "Two", "Three", "Four", "Five"]})):
            resp = client.post(f"/api/notes/{note['id']}/suggest-tags")
        assert resp.status_code == 200
        suggestions = resp.json()["suggestions"]
        assert len(suggestions) == 3
        assert all(s["kind"] == "topic" and s["origin"] == "ai" for s in suggestions)
        assert db.rows[(ALICE, note["id"])]["tags"] == before          # nothing saved until accepted

    def test_malformed_suggestion_output_502(self, client, db):
        as_user(ALICE)
        note = save(client)
        with _provider_text("nope"):
            assert client.post(f"/api/notes/{note['id']}/suggest-tags").status_code == 502

    def test_other_users_note_404(self, client, db):
        as_user(ALICE)
        note = save(client)
        as_user(BOB)
        with stub_ai() as get_provider:
            assert client.post(f"/api/notes/{note['id']}/suggest-tags").status_code == 404
        get_provider.assert_not_called()

    def test_accepting_an_ai_suggestion_marks_origin_ai(self, client, db):
        as_user(ALICE)
        note = save(client)
        resp = client.patch(f"/api/notes/{note['id']}", json={"tags": [{"label": "Vector", "kind": "topic", "origin": "ai"}]})
        assert resp.json()["note"]["tags"][0]["origin"] == "ai"


# ---------------------------------------------------------------------------
# service worker route
# ---------------------------------------------------------------------------

class TestServiceWorkerRoute:
    def test_served_from_root_with_scope_header_and_no_cache(self, client):
        resp = client.get("/sw.js")
        assert resp.status_code == 200
        assert "javascript" in resp.headers["content-type"]
        assert resp.headers["service-worker-allowed"] == "/"
        assert resp.headers["cache-control"] == "no-cache"

    def test_does_not_require_authentication(self, client):
        assert client.get("/sw.js").status_code == 200
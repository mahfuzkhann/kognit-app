"""
Tests for chat sharing: backend/share_engine.py, the share section of
backend/database.py and the five routes in backend/main.py.

Run with: GEMINI_API_KEY=dummy python3 -m pytest test_share.py -v

HOW THESE TESTS ARE BUILT
  The REAL routes and the REAL database.py functions run. Only the network
  edge is faked: httpx.MockTransport plays Supabase/PostgREST with the
  behaviour migration 0013 defines - row-level security by the caller's token,
  a database-generated token, one share per (user, chat), a per-user cap and a
  public get_shared_chat() function. That way the tests prove the actual HTTP
  requests the app makes AND that authorization holds for the access patterns
  that matter (owner, another signed-in student, an anonymous visitor, a
  recipient after revocation). The SQL itself was exercised separately against
  a real PostgreSQL (see the report that accompanies this change).

  Auth is stubbed with dependency_overrides on get_current_user_and_token (the
  convention used by test_notes_endpoints.py).
"""
import json as jsonlib
import logging
import os
import re
import sys
import uuid

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import database, share_engine
import backend.main as main_module

ALICE, BOB = "aaaaaaaa-0000-0000-0000-000000000001", "bbbbbbbb-0000-0000-0000-000000000002"
TOKEN_RE = re.compile(r"^[0-9a-f]{40}$")
SECRET_TEXT = "TOP-SECRET-CHAT-TEXT-জমা-রাখা"


# ===========================================================================
# 1. share_engine (pure)
# ===========================================================================

class TestBuildSnapshot:
    def msgs(self, *pairs):
        return [{"role": r, "text": t} for r, t in pairs]

    def test_happy_path_shape(self):
        out = share_engine.build_snapshot("chat_1", " বল ও গতি ", self.msgs(("user", "প্রশ্ন"), ("bot", "উত্তর")))
        assert out == {"chat_ref": "chat_1", "title": "বল ও গতি",
                       "snapshot": [{"role": "user", "text": "প্রশ্ন"}, {"role": "bot", "text": "উত্তর"}], "message_count": 2}

    def test_only_role_and_text_are_ever_read(self):
        raw = [{"role": "user", "text": "hi", "image": "data:image/png;base64,AAAA", "research": {"s": 1},
                "user_id": "x", "email": "a@b.c", "feedback": "like", "pdf": "secret"}]
        snap = share_engine.build_snapshot("c", "t", raw)["snapshot"]
        assert snap == [{"role": "user", "text": "hi"}]
        assert "AAAA" not in jsonlib.dumps(snap)

    def test_image_only_and_blank_messages_are_dropped(self):
        out = share_engine.build_snapshot("c", "t", [{"role": "user", "text": "", "image": "x"}, {"role": "user", "text": "   "},
                                                      {"role": "bot", "text": "ok"}])
        assert out["snapshot"] == [{"role": "bot", "text": "ok"}]

    def test_unknown_roles_and_non_dict_items_are_dropped(self):
        out = share_engine.build_snapshot("c", "t", [{"role": "system", "text": "evil"}, "str", None, 5, {"role": "bot", "text": "ok"}])
        assert [m["text"] for m in out["snapshot"]] == ["ok"]

    def test_nothing_shareable_is_refused(self):
        for messages in ([], [{"role": "user", "text": ""}], [{"role": "system", "text": "x"}]):
            with pytest.raises(share_engine.ShareValidationError) as e:
                share_engine.build_snapshot("c", "t", messages)
            assert e.value.code == "EMPTY"

    @pytest.mark.parametrize("bad", [None, "", "a b", "a/b", "../x", "x" * 81, "চ্যাট", 5, ["c"]])
    def test_bad_chat_reference_is_refused(self, bad):
        with pytest.raises(share_engine.ShareValidationError) as e:
            share_engine.build_snapshot(bad, "t", [{"role": "user", "text": "x"}])
        assert e.value.code == "INVALID_CHAT"

    @pytest.mark.parametrize("bad", [None, "x", {}, 5])
    def test_messages_must_be_a_list(self, bad):
        with pytest.raises(share_engine.ShareValidationError) as e:
            share_engine.build_snapshot("c", "t", bad)
        assert e.value.code == "INVALID_MESSAGES"

    def test_too_many_messages_is_refused_not_truncated(self):
        ok = [{"role": "user", "text": "x"}] * share_engine.MAX_SHARE_MESSAGES
        assert share_engine.build_snapshot("c", "t", ok)["message_count"] == share_engine.MAX_SHARE_MESSAGES
        with pytest.raises(share_engine.ShareValidationError) as e:
            share_engine.build_snapshot("c", "t", ok + [{"role": "user", "text": "x"}])
        assert e.value.code == "TOO_LONG"

    def test_oversized_message_and_total_are_refused(self):
        with pytest.raises(share_engine.ShareValidationError):
            share_engine.build_snapshot("c", "t", [{"role": "bot", "text": "x" * (share_engine.MAX_MESSAGE_CHARS + 1)}])
        big = [{"role": "bot", "text": "x" * share_engine.MAX_MESSAGE_CHARS}] * 16     # 320k > 300k budget
        with pytest.raises(share_engine.ShareValidationError):
            share_engine.build_snapshot("c", "t", big)

    def test_control_characters_and_bidi_overrides_are_stripped(self):
        out = share_engine.build_snapshot("c", "t", [{"role": "user", "text": "a\x00b\x07c\u202ed\u2066e\nf\tg"}])
        assert out["snapshot"][0]["text"] == "abcde\nf\tg"

    def test_title_is_single_line_trimmed_and_never_empty(self):
        assert share_engine.clean_title("  a\n\t b  ") == "a b"
        assert share_engine.clean_title("") == share_engine.DEFAULT_TITLE
        assert share_engine.clean_title(None) == share_engine.DEFAULT_TITLE
        assert share_engine.clean_title("\x00\x01") == share_engine.DEFAULT_TITLE
        long = share_engine.clean_title("ক" * 500)
        assert len(long) == share_engine.MAX_TITLE_CHARS and long.endswith("\u2026")

    def test_unicode_is_normalised(self):
        composed = share_engine.build_snapshot("c", "t", [{"role": "user", "text": "e\u0301"}])["snapshot"][0]["text"]
        assert composed == "\u00e9"


class TestPublicPayload:
    def row(self, **over):
        base = {"title": "T", "snapshot": [{"role": "user", "text": "q"}, {"role": "bot", "text": "a"}],
                "message_count": 2, "created_at": "2026-10-28T00:00:00Z", "user_id": "LEAK", "chat_ref": "LEAK", "id": "LEAK"}
        base.update(over); return base

    def test_exact_minimal_shape_and_no_owner_fields(self):
        out = share_engine.public_payload(self.row())
        assert set(out) == {"title", "messages", "message_count", "created_at"}
        assert "LEAK" not in jsonlib.dumps(out)

    @pytest.mark.parametrize("bad", [None, [], "x", {}, {"snapshot": []}, {"snapshot": "x"}, {"snapshot": [{"role": "system", "text": "x"}]}, {"snapshot": [None]}])
    def test_anything_unexpected_yields_none(self, bad):
        assert share_engine.public_payload(bad) is None

    def test_hostile_rows_are_reshaped_not_passed_through(self):
        out = share_engine.public_payload(self.row(snapshot=[{"role": "bot", "text": "a", "html": "<script>", "image": "x"}], message_count=999))
        assert out["messages"] == [{"role": "bot", "text": "a"}] and out["message_count"] == 1

    def test_owner_view_requires_a_well_formed_token(self):
        good = {"share_token": "a" * 40, "title": "t", "message_count": 3, "created_at": "x", "chat_ref": "SECRET"}
        v = share_engine.owner_view(good)
        assert v["token"] == "a" * 40 and "chat_ref" not in v
        for bad in ({"share_token": "A" * 40}, {"share_token": "z" * 40}, {"share_token": "a" * 39}, {}, None):
            assert share_engine.owner_view(bad) is None


# ===========================================================================
# 2. A fake Supabase with the behaviour of migration 0013
# ===========================================================================

class FakeSupabase:
    """httpx.MockTransport handler: PostgREST + RLS semantics for shared_chats."""

    def __init__(self):
        self.rows = []                 # dicts incl. user_id
        self.tokens = {}               # bearer token -> user id
        self.requests = []             # (method, path, params, headers)
        self.mode = "ok"               # ok | missing | down
        self.cap = 50

    def handler(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        self.requests.append((request.method, request.url.path, params, dict(request.headers)))
        if self.mode == "down":
            raise httpx.ConnectError("boom")
        if self.mode == "missing":
            return httpx.Response(404, json={"code": "PGRST205", "message": "Could not find the table 'public.shared_chats'"})

        path = request.url.path
        if path == "/rest/v1/rpc/get_shared_chat":                       # PUBLIC (anon) function
            tok = jsonlib.loads(request.content)["p_token"]
            for r in self.rows:
                if TOKEN_RE.match(tok or "") and r["share_token"] == tok:
                    return httpx.Response(200, json={"title": r["title"], "snapshot": r["snapshot"],
                                                     "message_count": r["message_count"], "created_at": r["created_at"]})
            return httpx.Response(200, content=b"null", headers={"content-type": "application/json"})   # what PostgREST sends for a NULL result

        # table access = RLS by the caller's token
        auth = request.headers.get("authorization", "")
        uid = self.tokens.get(auth.replace("Bearer ", ""))
        if uid is None:
            return httpx.Response(401, json={"message": "JWT expired"})
        visible = [r for r in self.rows if r["user_id"] == uid]

        if request.method == "POST":
            body = jsonlib.loads(request.content)
            if body.get("user_id") != uid:
                return httpx.Response(403, json={"code": "42501", "message": "new row violates row-level security policy"})
            if any(r["chat_ref"] == body["chat_ref"] for r in visible):
                return httpx.Response(409, json={"code": "23505"})
            if len(visible) >= self.cap:
                return httpx.Response(400, json={"code": "53400", "message": "share limit reached"})
            row = {**body, "share_token": uuid.uuid4().hex + uuid.uuid4().hex[:8], "created_at": "2026-10-28T08:00:00+00:00"}  # 40 hex, server-made
            self.rows.append(row)
            return httpx.Response(201, json=[self._project(row, params)])

        def match(r):
            ok = True
            for key in ("user_id", "chat_ref"):
                if key in params:
                    ok = ok and params[key] == f"eq.{r[key]}"
            return ok
        hits = [r for r in visible if match(r)]
        if request.method == "GET":
            return httpx.Response(200, json=[self._project(r, params) for r in hits][: int(params.get("limit", 1000))])
        if request.method == "DELETE":
            for r in hits:
                self.rows.remove(r)
            return httpx.Response(200, json=[{"chat_ref": r["chat_ref"]} for r in hits])
        return httpx.Response(405)

    @staticmethod
    def _project(row, params):
        cols = [c for c in params.get("select", "").split(",") if c]
        return {c: row[c] for c in cols} if cols else dict(row)


@pytest.fixture
def sb(monkeypatch):
    fake = FakeSupabase()
    fake.tokens = {"alice-token": ALICE, "bob-token": BOB}
    real = httpx.AsyncClient

    def factory(*a, **k):
        k["transport"] = httpx.MockTransport(fake.handler)
        return real(*a, **k)

    monkeypatch.setattr(database.httpx, "AsyncClient", factory)
    monkeypatch.setattr(database, "SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setattr(database, "SUPABASE_ANON_KEY", "sb_publishable_testkey")
    return fake


@pytest.fixture
def client():
    return TestClient(main_module.app)


@pytest.fixture(autouse=True)
def clean_state():
    main_module._rate_limit_buckets.clear()
    yield
    main_module.app.dependency_overrides.clear()
    main_module._rate_limit_buckets.clear()


def as_user(user_id, token):
    async def _fake():
        return (user_id, token)
    main_module.app.dependency_overrides[main_module.get_current_user_and_token] = _fake


def as_anonymous():
    main_module.app.dependency_overrides.clear()


CHAT = [{"role": "user", "text": SECRET_TEXT}, {"role": "bot", "text": "উত্তর **bold** $x^2$"}]


def create(client, chat_id="chat_1", messages=None, title="পদার্থবিজ্ঞান", **extra):
    return client.post("/api/share", json={"chat_id": chat_id, "title": title, "messages": CHAT if messages is None else messages, **extra})


# ===========================================================================
# 3. owner endpoints
# ===========================================================================

class TestOwnerEndpoints:
    def test_all_owner_routes_reject_an_unauthenticated_caller(self, client, sb):
        as_anonymous()
        for method, kwargs in (("post", {"json": {}}), ("get", {}), ("delete", {})):
            r = getattr(client, method)("/api/share?chat_id=chat_1", **kwargs)
            assert r.status_code == 401, (method, r.status_code)
        assert sb.requests == [], "nothing reached the database"

    def test_create_returns_a_token_and_stores_only_role_and_text(self, client, sb):
        as_user(ALICE, "alice-token")
        r = create(client, messages=[{"role": "user", "text": "q", "image": "data:image/png;base64,SECRETIMG", "research": {"a": 1}},
                                     {"role": "bot", "text": "a"}])
        assert r.status_code == 201, r.text
        data = r.json()
        assert data["created"] is True
        assert TOKEN_RE.match(data["share"]["token"])
        assert data["share"]["path"] == f"/s/{data['share']['token']}"
        stored = sb.rows[0]
        assert stored["snapshot"] == [{"role": "user", "text": "q"}, {"role": "bot", "text": "a"}]
        assert "SECRETIMG" not in jsonlib.dumps(stored)
        assert r.headers["cache-control"] == "no-store"

    def test_owner_identity_comes_from_the_verified_token_never_the_request(self, client, sb):
        as_user(ALICE, "alice-token")
        r = create(client, user_id=BOB, owner=BOB, share_token="f" * 40)
        assert r.status_code == 201
        assert sb.rows[0]["user_id"] == ALICE, "stored under the authenticated user"
        assert sb.rows[0]["share_token"] != "f" * 40, "the client can not choose the token"
        post = [q for q in sb.requests if q[0] == "POST"][0]
        assert post[3]["authorization"] == "Bearer alice-token", "the student's OWN token is used (never a service key)"

    def test_create_is_idempotent_and_never_overwrites_an_existing_snapshot(self, client, sb):
        as_user(ALICE, "alice-token")
        first = create(client).json()["share"]
        second = create(client, messages=[{"role": "user", "text": "DIFFERENT"}]).json()
        assert second["created"] is False and second["share"]["token"] == first["token"]
        assert len(sb.rows) == 1 and "DIFFERENT" not in jsonlib.dumps(sb.rows)

    def test_status_shows_the_owner_their_share_and_nothing_for_unshared_chats(self, client, sb):
        as_user(ALICE, "alice-token")
        assert client.get("/api/share?chat_id=chat_1").json() == {"share": None}
        tok = create(client).json()["share"]["token"]
        got = client.get("/api/share?chat_id=chat_1").json()["share"]
        assert got["token"] == tok and "chat_ref" not in got and "user_id" not in got

    def test_ANOTHER_signed_in_student_can_not_see_or_revoke_my_share(self, client, sb):
        as_user(ALICE, "alice-token")
        tok = create(client).json()["share"]["token"]
        as_user(BOB, "bob-token")
        assert client.get("/api/share?chat_id=chat_1").json() == {"share": None}, "Bob sees nothing for the same chat id"
        assert client.delete("/api/share?chat_id=chat_1").json() == {"revoked": False}
        as_anonymous()
        assert client.get(f"/api/shared/{tok}").status_code == 200, "Alice's share is untouched"
        as_user(BOB, "bob-token")
        mine = create(client).json()
        assert mine["created"] is True and mine["share"]["token"] != tok, "same chat id, different owner, different share"

    def test_forged_row_level_identity_is_blocked_by_the_database_policy(self, client, sb):
        # Belt and braces: even if the route were tricked into the wrong user_id, RLS (fake = real) refuses.
        r = httpx.Client(transport=httpx.MockTransport(sb.handler)).post(
            "https://example.supabase.co/rest/v1/shared_chats", headers={"authorization": "Bearer bob-token"},
            json={"user_id": ALICE, "chat_ref": "x", "title": "t", "snapshot": [], "message_count": 1, "share_token": "0" * 40})
        assert r.status_code == 403

    def test_revoke_deletes_the_snapshot_and_the_link_dies(self, client, sb):
        as_user(ALICE, "alice-token")
        tok = create(client).json()["share"]["token"]
        assert client.delete("/api/share?chat_id=chat_1").json() == {"revoked": True}
        assert sb.rows == [], "content is physically gone, not soft-hidden"
        assert client.get("/api/share?chat_id=chat_1").json() == {"share": None}
        as_anonymous()
        assert client.get(f"/api/shared/{tok}").status_code == 404
        assert client.get(f"/s/{tok}").status_code == 200, "the static shell still loads; the content is gone"
        as_user(ALICE, "alice-token")
        again = create(client).json()["share"]["token"]
        assert again != tok, "a new link is a NEW token; the old one stays dead"
        assert client.delete("/api/share?chat_id=chat_1").json() == {"revoked": True}
        assert client.delete("/api/share?chat_id=chat_1").json() == {"revoked": False}, "revoking twice is harmless"

    @pytest.mark.parametrize("bad_id", ["", "a b", "x" * 81, "../etc", "a;b", "চ্যাট"])
    def test_bad_chat_ids_are_rejected_before_the_database(self, client, sb, bad_id):
        as_user(ALICE, "alice-token")
        assert client.get("/api/share", params={"chat_id": bad_id}).status_code == 400
        assert client.delete("/api/share", params={"chat_id": bad_id}).status_code == 400
        assert create(client, chat_id=bad_id).status_code == 400
        assert sb.requests == []

    def test_missing_chat_id_is_rejected(self, client, sb):
        as_user(ALICE, "alice-token")
        assert client.get("/api/share").status_code == 400

    def test_validation_errors_have_stable_codes(self, client, sb):
        as_user(ALICE, "alice-token")
        r = create(client, messages=[]); assert r.status_code == 400 and r.json()["detail"]["code"] == "SHARE_EMPTY"
        r = create(client, messages=[{"role": "user", "text": "x"}] * 201); assert r.status_code == 413 and r.json()["detail"]["code"] == "SHARE_TOO_LONG"
        r = client.post("/api/share", json={"chat_id": "c", "messages": "nope"}); assert r.status_code == 400
        assert client.post("/api/share", content=b"not json", headers={"content-type": "application/json"}).status_code == 400
        assert client.post("/api/share", json=[1, 2]).status_code == 400
        assert sb.rows == []

    def test_oversized_request_body_is_rejected_cheaply(self, client, sb):
        as_user(ALICE, "alice-token")
        big = {"chat_id": "c", "title": "t", "messages": [{"role": "user", "text": "x" * 20000}] * 60}
        r = client.post("/api/share", json=big)
        assert r.status_code == 413
        assert sb.requests == []

    def test_a_maximum_size_bangla_chat_is_accepted(self, client, sb):
        as_user(ALICE, "alice-token")
        msgs = [{"role": "bot", "text": "অ" * 15000}] * 20          # 300k Bangla chars ~ 900 KB of UTF-8
        r = client.post("/api/share", json={"chat_id": "c", "title": "t", "messages": msgs})
        assert r.status_code == 201, r.text[:200]

    def test_per_user_cap_is_reported_as_a_clear_conflict(self, client, sb):
        sb.cap = 3
        as_user(ALICE, "alice-token")
        for i in range(3):
            assert create(client, chat_id=f"c{i}").status_code == 201
        r = create(client, chat_id="c3")
        assert r.status_code == 409 and r.json()["detail"]["code"] == "SHARE_LIMIT"
        as_user(BOB, "bob-token")
        assert create(client, chat_id="c0").status_code == 201, "another student's cap is independent"

    def test_write_rate_limit_applies_per_user(self, client, sb):
        as_user(ALICE, "alice-token")
        codes = [client.delete("/api/share?chat_id=c1").status_code for _ in range(12)]
        assert codes[:10] == [200] * 10 and 429 in codes[10:]
        as_user(BOB, "bob-token")
        assert client.delete("/api/share?chat_id=c1").status_code == 200, "Bob is not throttled by Alice"


# ===========================================================================
# 4. public endpoints
# ===========================================================================

class TestPublicAccess:
    def make(self, client, sb):
        as_user(ALICE, "alice-token")
        tok = create(client).json()["share"]["token"]
        as_anonymous()
        return tok

    def test_an_anonymous_recipient_sees_only_what_was_shared(self, client, sb):
        tok = self.make(client, sb)
        r = client.get(f"/api/shared/{tok}")
        assert r.status_code == 200
        data = r.json()
        assert set(data) == {"title", "messages", "message_count", "created_at"}
        assert data["messages"][0] == {"role": "user", "text": SECRET_TEXT}
        blob = r.text
        for leak in (ALICE, "alice-token", "chat_1", "user_id", "chat_ref", "share_token", tok):
            assert leak not in blob, f"leaked {leak}"

    def test_public_responses_are_uncacheable_unindexable_and_referrer_free(self, client, sb):
        tok = self.make(client, sb)
        for url in (f"/api/shared/{tok}", f"/s/{tok}"):
            h = client.get(url).headers
            assert h["cache-control"] == "no-store"
            assert "noindex" in h["x-robots-tag"] and "noarchive" in h["x-robots-tag"]
            assert h["referrer-policy"] == "no-referrer"

    def test_the_public_database_call_uses_only_the_public_key_never_a_student_token(self, client, sb):
        tok = self.make(client, sb); sb.requests.clear()
        client.get(f"/api/shared/{tok}")
        (method, path, _p, headers), = sb.requests
        assert path == "/rest/v1/rpc/get_shared_chat" and method == "POST"
        assert headers["apikey"] == "sb_publishable_testkey"
        assert "authorization" not in headers, "a publishable key must not be sent as a Bearer token"
        assert "alice-token" not in jsonlib.dumps(headers)

    def test_a_legacy_JWT_style_anon_key_is_sent_in_both_headers(self, client, sb, monkeypatch):
        tok = self.make(client, sb); sb.requests.clear()
        monkeypatch.setattr(database, "SUPABASE_ANON_KEY", "eyJhbGciOi.legacy.anon")
        client.get(f"/api/shared/{tok}")
        h = sb.requests[0][3]
        assert h["apikey"] == "eyJhbGciOi.legacy.anon" and h["authorization"] == "Bearer eyJhbGciOi.legacy.anon"

    def test_unknown_revoked_and_malformed_tokens_are_indistinguishable(self, client, sb):
        tok = self.make(client, sb)
        as_user(ALICE, "alice-token"); client.delete("/api/share?chat_id=chat_1"); as_anonymous()
        responses = [client.get(f"/api/shared/{t}") for t in (tok, "0" * 40, "short", "A" * 40, "g" * 40, "x'; DROP TABLE shared_chats;--")]
        assert {r.status_code for r in responses} == {404}
        assert len({r.text for r in responses}) == 1, "identical bodies: a probe learns nothing"
        assert responses[0].json()["detail"]["code"] == "SHARE_NOT_FOUND"

    def test_malformed_tokens_never_reach_the_database(self, client, sb):
        sb.requests.clear()
        for t in ("short", "A" * 40, "g" * 40, "1" * 41, "..", "a" * 39):
            client.get(f"/api/shared/{t}")
        assert sb.requests == []

    def test_guessing_does_not_enumerate_anything(self, client, sb):
        self.make(client, sb)
        assert all(client.get(f"/api/shared/{uuid.uuid4().hex}{uuid.uuid4().hex[:8]}").status_code == 404 for _ in range(20))

    def test_the_table_itself_is_not_readable_without_signing_in(self, client, sb):
        self.make(client, sb)
        r = httpx.Client(transport=httpx.MockTransport(sb.handler)).get("https://example.supabase.co/rest/v1/shared_chats", params={"select": "*"})
        assert r.status_code == 401, "anon has no table access - only the function"

    def test_the_html_shell_contains_no_chat_content_and_is_404_for_malformed_tokens(self, client, sb):
        tok = self.make(client, sb)
        ok = client.get(f"/s/{tok}")
        assert ok.status_code == 200 and "text/html" in ok.headers["content-type"]
        assert SECRET_TEXT not in ok.text and ALICE not in ok.text
        assert "frame-ancestors 'none'" in ok.headers["content-security-policy"]
        assert client.get("/s/not-a-token").status_code == 404
        assert client.get("/s/" + "A" * 40).status_code == 404

    def test_public_endpoints_need_no_authentication_and_ignore_a_presented_one(self, client, sb):
        tok = self.make(client, sb)
        assert client.get(f"/api/shared/{tok}", headers={"Authorization": "Bearer garbage"}).status_code == 200


# ===========================================================================
# 5. failure behaviour
# ===========================================================================

class TestFailureModes:
    def test_migration_not_applied_is_a_clear_503_everywhere_never_a_fake_success(self, client, sb):
        sb.mode = "missing"
        as_user(ALICE, "alice-token")
        for r in (create(client), client.get("/api/share?chat_id=c1"), client.delete("/api/share?chat_id=c1")):
            assert r.status_code == 503 and r.json()["detail"]["code"] == "SHARING_UNAVAILABLE"
        as_anonymous()
        r = client.get(f"/api/shared/{'a' * 40}")
        assert r.status_code == 503 and r.json()["detail"]["code"] == "SHARING_UNAVAILABLE"

    def test_database_down_is_a_generic_502_without_internals(self, client, sb):
        sb.mode = "down"
        as_user(ALICE, "alice-token")
        r = create(client)
        assert r.status_code == 502 and r.json()["detail"]["code"] == "SHARE_FAILED"
        assert not re.search(r"boom|ConnectError|supabase|example|Traceback|httpx", r.text, re.I)
        as_anonymous()
        r = client.get(f"/api/shared/{'a' * 40}")
        assert r.status_code == 502 and not re.search(r"boom|ConnectError|Traceback", r.text, re.I)

    def test_logs_never_contain_share_tokens_chat_text_or_student_tokens(self, client, sb, caplog):
        caplog.set_level(logging.DEBUG)
        as_user(ALICE, "alice-token")
        tok = create(client).json()["share"]["token"]
        client.get("/api/share?chat_id=chat_1"); client.delete("/api/share?chat_id=chat_1")
        as_anonymous(); client.get(f"/api/shared/{tok}")
        sb.mode = "down"; as_user(ALICE, "alice-token"); create(client, chat_id="chat_2"); as_anonymous(); client.get(f"/api/shared/{'b' * 40}")
        sb.mode = "missing"; as_user(ALICE, "alice-token"); create(client, chat_id="chat_3")
        # The app's own loggers. (The test client's httpx logger prints the URL it requested - that is the
        # test harness, not the server; the server-side access log is covered by the redaction test below.)
        logged = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith(("backend", "kognit")))
        for secret in (tok, "b" * 40, SECRET_TEXT, "alice-token"):
            assert secret not in logged, f"log leaked {secret!r}"

    def test_access_log_lines_have_the_token_redacted(self):
        tok = "ab" * 20
        flt = [f for f in logging.getLogger("uvicorn.access").filters if isinstance(f, main_module._RedactShareTokensFilter)]
        assert flt, "the filter is installed on uvicorn.access"
        for path in (f"/s/{tok}", f"/api/shared/{tok}", f"/s/{tok}?x=1"):
            rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d', ("1.2.3.4:5", "GET", path, "1.1", 200), None)
            flt[0].filter(rec)
            line = rec.getMessage()
            assert tok not in line and "<redacted>" in line, line
        other = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d', ("c", "GET", "/api/notes/" + tok, "1.1", 200), None)
        flt[0].filter(other)
        assert tok in other.getMessage(), "only share URLs are touched"

    def test_unexpected_database_replies_become_a_generic_error(self, client, sb, monkeypatch):
        as_user(ALICE, "alice-token")

        async def weird(*a, **k):
            return {"share_token": "NOT-A-TOKEN", "title": "t"}
        monkeypatch.setattr(main_module, "create_shared_chat", weird)
        r = create(client)
        assert r.status_code == 502 and "NOT-A-TOKEN" not in r.text

    def test_existing_routes_are_untouched(self, client):
        assert client.get("/").status_code == 200
        assert client.get("/favicon.ico").status_code == 200
        assert client.get("/favicon.ico").headers["content-type"].startswith("image/")
        assert client.get("/static/img/favicon.svg").status_code == 200
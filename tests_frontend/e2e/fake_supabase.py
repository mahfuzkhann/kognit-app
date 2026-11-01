"""
A tiny stand-in for Supabase, used ONLY by tests_frontend/test_uiux_e2e_browser.mjs.

Standard library only. It answers exactly what the backend asks for:
  GET  /auth/v1/user                      token -> {id, email}   (JWT verification)
  *    /rest/v1/shared_chats              PostgREST with row-level security by the caller's token
  POST /rest/v1/rpc/get_shared_chat       the public (anonymous) read function
It follows supabase/migrations/0013_shared_chats.sql: the database makes the token, one share per
(user, chat), a per-user cap, delete = revoke. (The SQL itself is exercised against real PostgreSQL
by supabase/tests/0013_shared_chats_check.sql.)

  python3 fake_supabase.py PORT
Also serves GET /__state (test inspection only) and POST /__reset.
"""
import json
import re
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

USERS = {"alice-token": ("aaaaaaaa-0000-0000-0000-000000000001", "alice@kloriz.test"),
         "bob-token": ("bbbbbbbb-0000-0000-0000-000000000002", "bob@kloriz.test")}
ROWS = []
LOG = []
TOKEN_RE = re.compile(r"^[0-9a-f]{40}$")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # keep test output quiet
        pass

    def _send(self, status, payload=None, raw=None):
        body = raw if raw is not None else (b"" if payload is None else json.dumps(payload).encode())
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _user(self):
        auth = self.headers.get("Authorization", "")
        return USERS.get(auth.replace("Bearer ", "", 1))

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"null") if n else None

    def _handle(self, method):
        url = urlparse(self.path)
        params = {k: v[0] for k, v in parse_qs(url.query).items()}
        LOG.append({"method": method, "path": url.path, "has_auth": bool(self.headers.get("Authorization")),
                    "apikey": self.headers.get("apikey")})

        if url.path == "/__state":
            return self._send(200, {"rows": ROWS, "log": LOG})
        if url.path == "/__reset":
            ROWS.clear(); LOG.clear(); return self._send(200, {"ok": True})

        if url.path == "/auth/v1/user":
            u = self._user()
            return self._send(200, {"id": u[0], "email": u[1]}) if u else self._send(401, {"msg": "bad jwt"})

        if url.path == "/rest/v1/rpc/get_shared_chat":
            tok = (self._body() or {}).get("p_token")
            for r in ROWS:
                if isinstance(tok, str) and TOKEN_RE.match(tok) and r["share_token"] == tok:
                    return self._send(200, {"title": r["title"], "snapshot": r["snapshot"],
                                            "message_count": r["message_count"], "created_at": r["created_at"]})
            return self._send(200, raw=b"null")

        if url.path == "/rest/v1/shared_chats":
            u = self._user()
            if not u:
                return self._send(401, {"message": "JWT"})
            uid = u[0]
            mine = [r for r in ROWS if r["user_id"] == uid]
            cols = [c for c in params.get("select", "").split(",") if c]
            proj = (lambda r: {c: r[c] for c in cols}) if cols else (lambda r: dict(r))

            if method == "POST":
                body = self._body()
                if body.get("user_id") != uid:
                    return self._send(403, {"code": "42501"})
                if any(r["chat_ref"] == body["chat_ref"] for r in mine):
                    return self._send(409, {"code": "23505"})
                if len(mine) >= 50:
                    return self._send(400, {"code": "53400"})
                row = {**body, "share_token": (uuid.uuid4().hex + uuid.uuid4().hex)[:40], "created_at": "2026-10-28T08:00:00+00:00"}
                ROWS.append(row)
                return self._send(201, [proj(row)])

            def match(r):
                return all(params.get(k) == f"eq.{r[k]}" for k in ("user_id", "chat_ref") if k in params)
            hits = [r for r in mine if match(r)]
            if method == "GET":
                return self._send(200, [proj(r) for r in hits])
            if method == "DELETE":
                for r in hits:
                    ROWS.remove(r)
                return self._send(200, [{"chat_ref": r["chat_ref"]} for r in hits])
        return self._send(404, {"code": "PGRST205", "message": "not found"})

    def do_GET(self): self._handle("GET")
    def do_POST(self): self._handle("POST")
    def do_DELETE(self): self._handle("DELETE")


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
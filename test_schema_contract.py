"""
Schema contract tests: backend <-> supabase/migrations <-> deployed database.

WHY THIS FILE EXISTS
--------------------
Live bug: POST /rest/v1/quiz_attempts -> 400 PGRST204
"Could not find the 'stream' column of 'quiz_attempts' in the schema cache".
The code was correct - backend/database.py:save_quiz_attempt has always sent
`stream` and `topic_key`, which supabase/migrations/0002_quiz_topic_identity.sql
adds - but the DEPLOYED database had never had 0002 applied.

Every other database test in this repo fakes Supabase with a handler that
accepts any column, so none of them could notice a mismatch between what the
backend sends and what the migrations define. This module closes that gap:

  * `FakePostgREST` is a PostgREST stand-in whose schema is BUILT FROM THE
    MIGRATION FILES (not hand-written), and which rejects unknown columns
    (PGRST204, same body as production) and NOT NULL violations (23502).
  * TestCodeMatchesMigrations runs every function in backend/database.py
    against the full migration schema: if the code ever starts sending or
    reading a column no migration creates, this fails in CI, before deploy.
  * TestReportedFailure reproduces the exact production failure by deploying
    "only migration 0001", shows nothing else is written, shows the endpoint
    answers safely and keeps the quiz retryable, and shows the same request
    succeeds once 0002 is applied.
  * TestDriftMatrix documents what each missing migration breaks, so the
    "which other fields are affected" question has a tested answer.
  * TestVerifySchemaSql keeps supabase/verify_schema.sql (the read-only query
    you run in the Supabase SQL editor to check the DEPLOYED database) in
    lock-step with the migrations.

No network and no real Supabase: everything runs on httpx.MockTransport.
"""
import asyncio
import glob
import json
import logging
import os
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import database, main as main_module
from backend.database import DatabaseError

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

# Captured once, at import, so repeated deploy() calls in one test never wrap an
# already-patched client (httpx.AsyncClient is patched on the shared httpx
# module, so a second deploy would otherwise inherit the FIRST fake's transport).
_REAL_ASYNC_CLIENT = httpx.AsyncClient
MIGRATIONS_DIR = os.path.join(REPO_ROOT, "supabase", "migrations")
VERIFY_SQL_PATH = os.path.join(REPO_ROOT, "supabase", "verify_schema.sql")


# ===========================================================================
# 1. Build the intended schema from the migration files
# ===========================================================================

def _migration_files():
    return sorted(glob.glob(os.path.join(MIGRATIONS_DIR, "*.sql")))


def _number(path):
    return int(os.path.basename(path).split("_", 1)[0])


def _strip_comments(sql):
    return re.sub(r"--[^\n]*", "", sql)


def _split_top_level(text, sep=","):
    """Split on `sep` outside parentheses (CHECK (x in ('a','b')) has commas)."""
    parts, depth, current = [], 0, []
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


_CONSTRAINT_WORDS = ("primary", "constraint", "unique", "check", "foreign")


def load_schema(apply=None):
    """Return {table: {column: {"nullable", "default", "added_by", "relaxed_by"}}}.

    `apply` is a set of migration numbers to apply (default: all of them), so a
    test can model a deployed database that is behind the repository.
    Handles the DDL these migrations actually use: CREATE TABLE IF NOT EXISTS,
    ALTER TABLE ... ADD COLUMN IF NOT EXISTS, ALTER COLUMN ... DROP/SET NOT NULL.
    """
    schema = {}
    for path in _migration_files():
        num = _number(path)
        if apply is not None and num not in apply:
            continue
        sql = _strip_comments(open(path, encoding="utf-8").read())

        for m in re.finditer(r"create table if not exists public\.(\w+)\s*\((.*?)\n\)\s*;", sql, re.S | re.I):
            table, body = m.group(1), m.group(2)
            cols = schema.setdefault(table, {})
            for item in _split_top_level(body):
                first = item.split()[0].lower() if item.split() else ""
                if not first or first in _CONSTRAINT_WORDS:
                    continue
                low = item.lower()
                cols.setdefault(first, {
                    "nullable": "not null" not in low and "primary key" not in low,
                    "default": " default " in f" {low} ",
                    "added_by": num,
                    "relaxed_by": None,
                })

        for m in re.finditer(r"alter table public\.(\w+)\s+(.*?);", sql, re.S | re.I):
            table, actions = m.group(1), m.group(2)
            cols = schema.setdefault(table, {})
            for action in _split_top_level(actions):
                add = re.match(r"add column if not exists\s+(\w+)\s+(.*)", action, re.S | re.I)
                if add:
                    low = add.group(2).lower()
                    cols.setdefault(add.group(1).lower(), {
                        "nullable": "not null" not in low,
                        "default": "default" in low,
                        "added_by": num,
                        "relaxed_by": None,
                    })
                    continue
                drop = re.match(r"alter column\s+(\w+)\s+drop not null", action, re.I)
                if drop and drop.group(1).lower() in cols:
                    cols[drop.group(1).lower()]["nullable"] = True
                    cols[drop.group(1).lower()]["relaxed_by"] = num
                    continue
                set_nn = re.match(r"alter column\s+(\w+)\s+set not null", action, re.I)
                if set_nn and set_nn.group(1).lower() in cols:
                    cols[set_nn.group(1).lower()]["nullable"] = False
    return schema


def rls_tables():
    out = set()
    for path in _migration_files():
        sql = _strip_comments(open(path, encoding="utf-8").read())
        out.update(re.findall(r"alter table public\.(\w+)\s+enable row level security", sql, re.I))
    return out


# ===========================================================================
# 2. A PostgREST stand-in that enforces that schema
# ===========================================================================

_RESERVED_PARAMS = {"select", "order", "limit", "offset", "on_conflict", "columns"}


class FakePostgREST:
    """httpx.MockTransport handler that behaves like PostgREST for the parts
    that matter here: unknown columns and NOT NULL violations are REJECTED
    with PostgREST's real status codes and error bodies."""

    def __init__(self, schema):
        self.schema = schema
        self.requests = []      # (method, table, params, json body)
        self.rejections = []    # the error bodies returned
        self._seq = 0
        self.rows = {}          # optional canned GET results per table

    def transport(self):
        return httpx.MockTransport(self.handle)

    def _reject(self, status, code, message):
        body = {"code": code, "details": None, "hint": None, "message": message}
        self.rejections.append(body)
        return httpx.Response(status, json=body)

    def handle(self, request):
        match = re.search(r"/rest/v1/([^/?]+)", request.url.path)
        table = match.group(1) if match else ""
        params = list(request.url.params.multi_items())
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, table, params, body))

        if table not in self.schema:
            return self._reject(404, "PGRST205", f"Could not find the table 'public.{table}' in the schema cache")
        cols = self.schema[table]

        def unknown_column(name):
            return self._reject(400, "42703", f"column {table}.{name} does not exist")

        # --- reads / deletes: select, filters, order, on_conflict
        for key, value in params:
            if key == "select":
                for name in [c.strip() for c in _split_top_level(value)]:
                    if name != "*" and name not in cols:
                        return unknown_column(name)
            elif key == "order":
                for item in value.split(","):
                    name = item.split(".")[0].strip()
                    if name not in cols:
                        return unknown_column(name)
            elif key == "on_conflict":
                for name in value.split(","):
                    if name.strip() not in cols:
                        return unknown_column(name.strip())
            elif key not in _RESERVED_PARAMS and key not in cols:
                return unknown_column(key)

        if request.method == "GET":
            return httpx.Response(200, json=self.rows.get(table, []))
        if request.method == "DELETE":
            return httpx.Response(204)

        # --- writes
        rows = body if isinstance(body, list) else [body]
        for row in rows:
            for key in row:
                if key not in cols:
                    return self._reject(
                        400, "PGRST204", f"Could not find the '{key}' column of '{table}' in the schema cache")
        for row in rows:
            for name, meta in cols.items():
                if not meta["nullable"] and not meta["default"] and row.get(name) is None:
                    return self._reject(
                        400, "23502", f'null value in column "{name}" of relation "{table}" violates not-null constraint')

        if "return=representation" in request.headers.get("prefer", ""):
            out = []
            for row in rows:
                self._seq += 1
                out.append({"id": f"{table}-{self._seq}", "created_at": "2026-10-02T00:00:00+00:00",
                            "updated_at": "2026-10-02T00:00:00+00:00", **row})
            return httpx.Response(201, json=out)
        return httpx.Response(201)


@pytest.fixture(autouse=True)
def supabase_env(monkeypatch):
    monkeypatch.setattr(database, "SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setattr(database, "SUPABASE_ANON_KEY", "anon-key-123")


def deploy(monkeypatch, apply=None):
    """Point backend/database.py at a fake Supabase whose schema is the
    migrations in `apply` (default: all)."""
    fake = FakePostgREST(load_schema(apply))

    def factory(*args, **kwargs):
        kwargs["transport"] = fake.transport()
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(database.httpx, "AsyncClient", factory)
    return fake


QUESTIONS = [
    {"question": "2+2?", "options": ["3", "4"], "correct_index": 1, "explanation": "math"},
    {"question": "Capital of BD?", "options": ["Dhaka", "Delhi"], "correct_index": 0, "explanation": "geo"},
]


def save_quiz(**overrides):
    args = dict(user_token="student-token", user_id="user-1", board="BD NCTB (Bangla)",
                user_class="Class 9-10 (SSC)", subject="Physics", stream="Science (বিজ্ঞান)",
                topic="Force & Motion", questions=QUESTIONS, selected_answers=[1, 0])
    args.update(overrides)
    return asyncio.run(database.save_quiz_attempt(**args))


# ===========================================================================
# 3. The schema model itself
# ===========================================================================

class TestSchemaModel:

    def test_quiz_attempts_columns_come_from_0001_plus_0002(self):
        cols = load_schema()["quiz_attempts"]
        assert {"id", "user_id", "board", "user_class", "subject", "topic", "total_questions",
                "score", "created_at"} == {c for c, m in cols.items() if m["added_by"] == 1}
        assert {c for c, m in cols.items() if m["added_by"] == 2} == {"stream", "topic_key"}

    def test_stream_and_topic_key_are_nullable_so_old_rows_stay_valid(self):
        cols = load_schema()["quiz_attempts"]
        assert cols["stream"]["nullable"] and cols["topic_key"]["nullable"]

    def test_nullability_relaxations_are_detected(self):
        schema = load_schema()
        assert schema["learning_evidence"]["topic"]["relaxed_by"] == 4
        assert schema["learning_evidence"]["topic_key"]["relaxed_by"] == 4
        assert schema["student_profiles"]["stream"]["relaxed_by"] == 6

    def test_partial_deployment_models_a_database_behind_the_repo(self):
        only_1 = load_schema({1})
        assert "stream" not in only_1["quiz_attempts"] and "learning_evidence" not in only_1


# ===========================================================================
# 4. The reported failure, reproduced exactly
# ===========================================================================

class TestReportedFailure:

    def test_fake_reproduces_the_exact_production_error_body(self, monkeypatch, caplog):
        fake = deploy(monkeypatch, apply={1})  # a database that never ran migration 0002
        caplog.set_level(logging.ERROR, logger="kognit.database")
        with pytest.raises(DatabaseError):
            save_quiz()
        assert fake.rejections == [{
            "code": "PGRST204", "details": None, "hint": None,
            "message": "Could not find the 'stream' column of 'quiz_attempts' in the schema cache"}]
        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert "status=400" in logged and "PGRST204" in logged and "'stream'" in logged

    def test_a_failed_insert_writes_nothing_else_and_attempts_no_rollback(self, monkeypatch):
        fake = deploy(monkeypatch, apply={1})
        with pytest.raises(DatabaseError):
            save_quiz()
        assert [(m, t) for m, t, _, _ in fake.requests] == [("POST", "quiz_attempts")]

    def test_the_backend_really_does_send_both_columns_that_0002_adds(self, monkeypatch):
        fake = deploy(monkeypatch)
        save_quiz()
        _, table, _, body = fake.requests[0]
        assert table == "quiz_attempts"
        added_by_0002 = {c for c, m in load_schema()["quiz_attempts"].items() if m["added_by"] == 2}
        assert added_by_0002 == {"stream", "topic_key"} <= set(body)
        assert body["stream"] == "Science (বিজ্ঞান)" and body["topic_key"] == "force & motion"

    def test_with_0002_applied_the_same_call_succeeds_and_grades(self, monkeypatch):
        fake = deploy(monkeypatch, apply={1, 2})
        result = save_quiz()
        assert result["score"] == 2 and result["total_questions"] == 2 and fake.rejections == []
        assert [(m, t) for m, t, _, _ in fake.requests] == [("POST", "quiz_attempts"), ("POST", "quiz_answers")]

    def test_the_other_column_0002_adds_fails_the_same_way_on_its_own(self, monkeypatch):
        fake = deploy(monkeypatch)
        del fake.schema["quiz_attempts"]["topic_key"]  # drift in only that column
        with pytest.raises(DatabaseError):
            save_quiz()
        assert fake.rejections[0]["message"] == "Could not find the 'topic_key' column of 'quiz_attempts' in the schema cache"

    def test_a_class_6_to_8_student_with_no_stream_saves_a_null_stream(self, monkeypatch):
        fake = deploy(monkeypatch, apply={1, 2})
        save_quiz(user_class="Class 6-8", stream=None)
        assert fake.requests[0][3]["stream"] is None and fake.rejections == []

    def test_rows_that_predate_0002_stay_valid_and_are_excluded_from_topic_analytics_by_design(self, monkeypatch):
        # Applying 0002 uses ADD COLUMN IF NOT EXISTS with no backfill: existing
        # attempts keep stream/topic_key = NULL and are never rewritten or
        # deleted. database.py documents that such legacy rows are excluded from
        # the topic profile (they have no topic identity to group on).
        fake = deploy(monkeypatch, apply={1, 2})
        legacy = {"id": "old", "subject": "Science", "topic": "Cells", "topic_key": None,
                  "total_questions": 5, "score": 3, "created_at": "2026-08-01T00:00:00+00:00"}
        fake.rows["quiz_attempts"] = [legacy]
        assert asyncio.run(database.get_user_topic_profile("student-token", "user-1")) == []
        # ... and the exclusion is requested of the database itself, not just done in Python
        assert [v for _, t, p, _ in fake.requests for k, v in p if k == "topic_key"] == ["not.is.null"]

        fake.rows["quiz_attempts"] = [legacy, {**legacy, "id": "new", "topic_key": "cells"}]
        profile = asyncio.run(database.get_user_topic_profile("student-token", "user-1"))
        assert [(row["subject"], row["topic_key"]) for row in profile] == [("Science", "cells")]


class TestEndpointUnderDrift:
    """The student-facing behaviour while the deployed schema is behind."""

    @pytest.fixture
    def client(self):
        main_module.active_quiz_definitions.clear()
        async def _user():
            return ("user-1", "student-token")
        main_module.app.dependency_overrides[main_module.get_current_user_and_token] = _user
        main_module.app.dependency_overrides[main_module.get_current_user_id] = lambda: "user-1"
        yield TestClient(main_module.app)
        main_module.active_quiz_definitions.clear()
        main_module.app.dependency_overrides.clear()

    def _seed(self):
        main_module.active_quiz_definitions["quiz-1"] = {
            "user_id": "user-1", "board": "BD NCTB (Bangla)", "user_class": "Class 9-10 (SSC)",
            "subject": "Biology", "stream": "Science (বিজ্ঞান)", "topic": "Cell membrane",
            "questions": QUESTIONS, "submitted": False, "created_at": 0}

    def test_drift_gives_a_safe_502_keeps_the_quiz_and_a_retry_works_after_the_fix(self, client, monkeypatch):
        fake = deploy(monkeypatch, apply={1})
        self._seed()
        body = {"quiz_id": "quiz-1", "answers": json.dumps([1, 0])}

        first = client.post("/api/quiz/submit", data=body)
        assert first.status_code == 502
        assert first.json()["detail"] == "Could not save your quiz result right now. Please try submitting again."
        for leak in ("PGRST204", "stream", "schema cache", "quiz_attempts"):
            assert leak not in first.text, "internal schema details must not reach the student"
        assert main_module.active_quiz_definitions["quiz-1"]["submitted"] is False, "quiz stays retryable"

        # the operator now applies migration 0002 (and PostgREST reloads its schema cache)
        fake.schema.clear()
        fake.schema.update(load_schema({1, 2}))

        second = client.post("/api/quiz/submit", data=body)
        assert second.status_code == 200
        assert second.json()["score"] == 2 and second.json()["status"] == "success"
        assert "quiz-1" not in main_module.active_quiz_definitions

    def test_no_double_scoring_or_extra_rows_were_written_during_the_failed_attempt(self, client, monkeypatch):
        fake = deploy(monkeypatch, apply={1})
        self._seed()
        client.post("/api/quiz/submit", data={"quiz_id": "quiz-1", "answers": json.dumps([1, 0])})
        assert [t for _, t, _, _ in fake.requests] == ["quiz_attempts"]


# ===========================================================================
# 5. Code <-> migrations contract: every database function, full schema
# ===========================================================================

def _call_every_database_function():
    """Exercises every function in backend/database.py that talks to Supabase,
    including the optional-field variants (no stream / no topic)."""
    tok, uid = "student-token", "user-1"
    run = asyncio.run
    D = database
    run(D.get_user_topic_profile(tok, uid))
    run(D.get_user_topic_mistakes(tok, uid))
    run(D.save_chat_learning_evidence(tok, uid, subject="Physics", signal_type="confusion",
                                      signal_strength="weak", attribution_confidence="known",
                                      topic="Newton's laws", chat_id="chat-1"))
    run(D.save_chat_learning_evidence(tok, uid, subject="Physics", signal_type="confusion",
                                      signal_strength="weak", attribution_confidence="probable"))  # subject-only
    run(D.save_conversation_index_entry(tok, uid, chat_id="chat-1", short_label="Newton", subject="Physics",
                                        attribution_confidence="known"))
    run(D.save_conversation_index_entry(tok, uid, chat_id="chat-2", short_label="Hello"))  # no subject
    run(D.get_learning_history(tok, uid, "2026-09-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00"))
    run(D.get_learning_insights(tok, uid))
    run(D.get_student_profile(tok, uid))
    run(D.upsert_student_profile(tok, uid, name="Mahfuz", user_class="Class 9-10 (SSC)", stream="Science (বিজ্ঞান)"))
    run(D.upsert_student_profile(tok, uid, name="Mahfuz", user_class="Class 6-8", stream=None))  # no stream
    save_quiz()
    save_quiz(user_class="Class 6-8", stream=None)


class TestCodeMatchesMigrations:

    def test_every_database_function_is_accepted_by_the_full_migration_schema(self, monkeypatch):
        fake = deploy(monkeypatch)
        _call_every_database_function()
        assert fake.rejections == [], f"backend sends/reads something the migrations do not define: {fake.rejections}"

    def test_every_table_the_backend_touches_exists_in_the_migrations(self, monkeypatch):
        fake = deploy(monkeypatch)
        _call_every_database_function()
        touched = {t for _, t, _, _ in fake.requests}
        assert touched == {"quiz_attempts", "quiz_answers", "learning_evidence", "conversation_index", "student_profiles"}
        assert touched <= set(load_schema())

    def test_every_column_written_is_a_migration_column(self, monkeypatch):
        fake = deploy(monkeypatch)
        _call_every_database_function()
        schema = load_schema()
        for method, table, _, body in fake.requests:
            if method == "POST":
                for row in (body if isinstance(body, list) else [body]):
                    assert set(row) <= set(schema[table]), (table, set(row) - set(schema[table]))

    def test_every_migration_table_with_user_data_has_row_level_security(self):
        assert rls_tables() >= {"quiz_attempts", "quiz_answers", "learning_evidence",
                                "conversation_index", "student_profiles"}

    def test_the_backend_never_uses_a_service_role_key(self):
        source = open(os.path.join(REPO_ROOT, "backend", "database.py"), encoding="utf-8").read()
        assert "service_role" not in source and "SERVICE_ROLE" not in source

    def test_the_fake_actually_rejects_unknown_columns_and_nulls(self, monkeypatch):
        # guard against a fake that silently accepts everything (the old blind spot)
        fake = deploy(monkeypatch)
        client = httpx.Client(transport=fake.transport(), base_url="https://x.test")
        assert client.post("/rest/v1/quiz_attempts", json={"nope": 1}).status_code == 400
        assert client.get("/rest/v1/quiz_attempts", params={"select": "nope"}).status_code == 400
        assert client.get("/rest/v1/quiz_attempts", params={"nope": "eq.1"}).status_code == 400
        assert client.get("/rest/v1/quiz_attempts", params={"order": "nope.asc"}).status_code == 400
        assert client.post("/rest/v1/no_such_table", json={}).status_code == 404
        assert client.post("/rest/v1/learning_evidence", json={"user_id": "u"}).json()["code"] == "23502"


# ===========================================================================
# 6. What each missing migration breaks
# ===========================================================================

def _run(monkeypatch, apply, fn):
    fake = deploy(monkeypatch, apply=apply)
    try:
        fn()
        return fake, None
    except DatabaseError as exc:
        return fake, exc


class TestDriftMatrix:
    """Deployed database vs repository. Each row is a database that is behind."""

    def test_only_0001_quiz_save_fails_on_stream(self, monkeypatch):
        fake, err = _run(monkeypatch, {1}, save_quiz)
        assert err and "'stream'" in fake.rejections[0]["message"]

    def test_only_0001_the_learning_memory_tables_do_not_exist(self, monkeypatch):
        fake, err = _run(monkeypatch, {1}, lambda: asyncio.run(database.save_chat_learning_evidence(
            "t", "u", subject="Physics", signal_type="confusion", signal_strength="weak",
            attribution_confidence="known", topic="Force")))
        assert err and fake.rejections[0]["code"] == "PGRST205"

    def test_only_0001_the_student_profile_table_does_not_exist(self, monkeypatch):
        fake, err = _run(monkeypatch, {1}, lambda: asyncio.run(database.upsert_student_profile(
            "t", "u", name="A", user_class="Class 9-10 (SSC)", stream="Science (বিজ্ঞান)")))
        assert err and fake.rejections[0]["code"] == "PGRST205"

    def test_through_0003_without_0004_a_subject_only_evidence_row_is_rejected(self, monkeypatch):
        subject_only = lambda: asyncio.run(database.save_chat_learning_evidence(
            "t", "u", subject="Physics", signal_type="confusion", signal_strength="weak",
            attribution_confidence="probable"))
        fake, err = _run(monkeypatch, {1, 2, 3}, subject_only)
        assert err and fake.rejections[0]["code"] == "23502" and '"topic"' in fake.rejections[0]["message"]
        fake, err = _run(monkeypatch, {1, 2, 3, 4}, subject_only)
        assert err is None and fake.rejections == []

    def test_through_0005_without_0006_a_class_6_to_8_profile_is_rejected(self, monkeypatch):
        no_stream = lambda: asyncio.run(database.upsert_student_profile(
            "t", "u", name="A", user_class="Class 6-8", stream=None))
        fake, err = _run(monkeypatch, {1, 2, 3, 4, 5}, no_stream)
        assert err and fake.rejections[0]["code"] == "23502" and '"stream"' in fake.rejections[0]["message"]
        fake, err = _run(monkeypatch, {1, 2, 3, 4, 5, 6}, no_stream)
        assert err is None

    def test_a_fully_migrated_database_accepts_everything(self, monkeypatch):
        fake = deploy(monkeypatch)
        _call_every_database_function()
        assert fake.rejections == []


# ===========================================================================
# 7. supabase/verify_schema.sql stays in step with the migrations
# ===========================================================================

def _parse_verify_sql():
    sql = open(VERIFY_SQL_PATH, encoding="utf-8").read()
    expected = {}
    for m in re.finditer(r"\('(\w+)',\s*'(\w+)',\s*'([\w.]+)',\s*(null|'[\w.]+')\)", _strip_comments(sql)):
        relaxed = None if m.group(4) == "null" else m.group(4).strip("'")
        expected[(m.group(1), m.group(2))] = (m.group(3), relaxed)
    rls = set(re.findall(r"'(\w+)'", re.search(r"tablename in \((.*?)\)", _strip_comments(sql), re.S).group(1)))
    return expected, rls


def _file_for(number):
    return next(os.path.basename(p) for p in _migration_files() if _number(p) == number)


class TestVerifySchemaSql:

    def test_expected_columns_equal_the_migration_schema_exactly(self):
        expected, _ = _parse_verify_sql()
        want = {(t, c) for t, cols in load_schema().items() for c in cols}
        assert set(expected) == want

    def test_each_column_names_the_migration_that_adds_it(self):
        expected, _ = _parse_verify_sql()
        for table, cols in load_schema().items():
            for col, meta in cols.items():
                assert expected[(table, col)][0] == _file_for(meta["added_by"]), (table, col)

    def test_each_relaxed_column_names_the_migration_that_relaxes_it(self):
        expected, _ = _parse_verify_sql()
        for table, cols in load_schema().items():
            for col, meta in cols.items():
                relaxed = expected[(table, col)][1]
                assert relaxed == (_file_for(meta["relaxed_by"]) if meta["relaxed_by"] else None), (table, col)

    def test_rls_check_covers_exactly_the_tables_with_rls_in_the_migrations(self):
        _, rls = _parse_verify_sql()
        assert rls == rls_tables()

    def test_the_file_is_read_only(self):
        sql = _strip_comments(open(VERIFY_SQL_PATH, encoding="utf-8").read()).lower()
        for verb in ("insert ", "update ", "delete ", "drop ", "alter ", "create ", "truncate ", "grant "):
            assert verb not in sql, f"verify_schema.sql must stay read-only (found {verb.strip()!r})"
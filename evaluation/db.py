"""
Kognit Phase 7B — evaluation database helper.

This module owns exactly two responsibilities:
  1. Opening a SQLite connection with the correct pragmas (foreign key
     enforcement is OFF by default in SQLite and MUST be turned on
     explicitly per-connection - this is the one place that happens).
  2. Applying evaluation/schema/schema.sql to initialize a database file.

It deliberately does NOT contain runner, evaluator, or judge logic -
those are later phases (7B-4 onward). Keeping this module small and
single-purpose mirrors the same discipline already used elsewhere in
this codebase (e.g. backend/database.py's helper functions each do one
thing).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Bumped whenever evaluation/schema/schema.sql changes in a way that
# affects stored data shape. Every EvaluationRun row records the schema
# version in effect at the time it ran (see the schema's evaluation_runs
# table) - this is the Python-side counterpart to that value, and is
# also written into the schema_metadata table on initialization so the
# database FILE can self-report its version independent of whichever
# version of this constant a future caller happens to be running.
EVALUATION_SCHEMA_VERSION = "1"

# Phase 7C: version for the SEPARATE research benchmark schema
# (evaluation/schema/research_schema.sql). Independent from
# EVALUATION_SCHEMA_VERSION above - the two schemas evolve on their own
# timelines and live in separate database files.
RESEARCH_SCHEMA_VERSION = "1"

_SCHEMA_SQL_PATH = Path(__file__).parent / "schema" / "schema.sql"
_RESEARCH_SCHEMA_SQL_PATH = Path(__file__).parent / "schema" / "research_schema.sql"


def utc_now_iso() -> str:
    """Return the current time as an ISO-8601 UTC timestamp string.

    Every timestamp column in evaluation/schema/schema.sql is a TEXT
    column with a CHECK constraint requiring this exact shape
    (YYYY-MM-DDTHH:MM:SS...), so every timestamp written into the
    evaluation database must go through this function (or something
    producing an identically-shaped string) rather than being formatted
    ad hoc at each call site.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def get_connection(db_path: str | Path) -> sqlite3.Connection:
    """Open a SQLite connection with foreign key enforcement turned on.

    SQLite disables foreign key enforcement by default on every new
    connection, regardless of whether the schema declares FOREIGN KEY
    constraints - the schema file's own "PRAGMA foreign_keys = ON"
    statement only affects the connection that runs the schema script
    itself, not connections opened later by other code. This function
    is the one place that pragma is set, so every caller that goes
    through get_connection() gets real FK enforcement without having to
    remember the pragma themselves.
    """
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def initialize_schema(
    conn: sqlite3.Connection,
    schema_path: str | Path | None = None,
    schema_version: str | None = None,
) -> None:
    """Apply a schema SQL file to the given connection. Defaults to
    evaluation/schema/schema.sql (Phase 7B, Answer Quality) - both new
    parameters are optional and additive, so every existing Phase 7B
    call site (which passes neither) is completely unaffected.

    Phase 7C passes evaluation/schema/research_schema.sql and
    RESEARCH_SCHEMA_VERSION explicitly to initialize a SEPARATE research
    benchmark database - kept as a distinct file/schema per the approved
    architecture's explicit instruction not to mix the research
    benchmark into the NCTB Answer Quality benchmark.

    Idempotent: every CREATE TABLE/INDEX/TRIGGER in either schema file
    uses "IF NOT EXISTS", so calling this against an already-initialized
    database is a safe no-op for existing objects.

    Also writes/refreshes the schema_metadata row so the database file
    can self-report which schema version it was initialized with.
    """
    path = Path(schema_path) if schema_path is not None else _SCHEMA_SQL_PATH
    version = schema_version if schema_version is not None else EVALUATION_SCHEMA_VERSION
    sql = path.read_text(encoding="utf-8")
    conn.executescript(sql)
    conn.execute(
        "INSERT INTO schema_metadata (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (version,),
    )
    conn.commit()


def get_schema_version_from_db(conn: sqlite3.Connection) -> str | None:
    """Read back the schema version the database file self-reports.

    Returns None if schema_metadata has no such row (e.g. a database
    that was never initialized through initialize_schema()).
    """
    row = conn.execute(
        "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
    ).fetchone()
    return row["value"] if row is not None else None

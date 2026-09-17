-- Kognit Phase 7B — Answer Quality & Evaluation
-- SQLite schema foundation (Phase 7B-1).
--
-- Scope: this file defines ONLY the storage schema approved in the Phase 7B
-- Step 2 architecture + Step 2 revision pass. It does not contain benchmark
-- content, runner logic, evaluator logic, or judge logic — those are later
-- phases (7B-2 onward).
--
-- PORTABILITY NOTE (see evaluation/README.md and the Step 2 revision
-- report, Section 21): every table/column/constraint below is written to
-- port cleanly to PostgreSQL later:
--   * Timestamps are stored as ISO-8601 TEXT (SQLite has no native
--     timestamp type); a CHECK constraint enforces the format so a future
--     migration to TIMESTAMPTZ is a straightforward cast, not a data
--     cleanup project.
--   * JSON fields are stored as opaque TEXT, parsed in Python — no SQLite
--     JSON1-extension query syntax is used anywhere in this schema or in
--     evaluation/db.py, so nothing here locks application code to
--     SQLite-specific JSON querying.
--   * Foreign keys use standard REFERENCES syntax, portable as-is.
--   * The only genuinely SQLite-specific pieces are the immutability
--     triggers at the bottom of this file (SQLite trigger syntax differs
--     from PostgreSQL's PL/pgSQL trigger functions). The *concept* they
--     enforce (append-only / immutable-after-creation rows) maps directly
--     onto Postgres triggers — they would need to be rewritten in
--     PL/pgSQL at migration time, which is normal, expected trigger
--     porting, not a data-model problem.
--
-- Foreign key enforcement is OFF by default in SQLite per-connection and
-- MUST be turned on explicitly by the caller (see evaluation/db.py,
-- get_connection(), which issues "PRAGMA foreign_keys = ON" on every
-- connection it hands out). This file does not and cannot enable it
-- itself — a schema file has no control over what pragma a future
-- connection uses, which is exactly why it is enforced in code, in one
-- place, rather than assumed.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------
-- Schema self-description
-- ---------------------------------------------------------------------
-- A single-row-per-key table so the database FILE can self-report which
-- schema version it was created with, independent of whatever Python
-- constant a given evaluation_runs row also happens to reference. This
-- protects against the case where the DB file and the code that created
-- it drift apart (e.g. someone hand-edits the file, or an old file is
-- reused with newer code).
CREATE TABLE IF NOT EXISTS schema_metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------------
-- Benchmark item identity (Step 2 Section 3 / Revision Section 13)
-- ---------------------------------------------------------------------
-- Permanent identity + provenance. NEVER mutated after creation (see the
-- immutability trigger at the bottom of this file) — a content change
-- always produces a new benchmark_item_versions row, never a change here.
--
-- Curriculum fields are stored as lightweight, free-text columns
-- directly on the item (per the Step 2 architecture's explicit decision
-- NOT to build a real curriculum taxonomy yet — see curriculum_node_id
-- below). curriculum_topics is stored as a JSON list (not a scalar
-- string) specifically because Step 2 Section 6 requires supporting
-- cross-topic questions without a future schema change.
CREATE TABLE IF NOT EXISTS benchmark_items (
    item_id            TEXT PRIMARY KEY,

    question_type      TEXT NOT NULL CHECK (question_type IN (
                            'exact_numerical', 'numerical_tolerance', 'symbolic',
                            'mcq', 'short_factual', 'long_explanation',
                            'proof_derivation', 'language_answer'
                        )),

    curriculum_class    TEXT NOT NULL,
    curriculum_stream   TEXT,              -- nullable: Class 6-8 has no stream
                                            -- (same rule already used by
                                            -- backend/database.py student_profiles)
    curriculum_subject  TEXT NOT NULL,
    curriculum_chapter  TEXT,              -- nullable, optional per Step 2 Section 6
    curriculum_topics   TEXT NOT NULL,     -- JSON list of 1+ topic strings
    curriculum_node_id  TEXT,              -- RESERVED, nullable. No taxonomy table
                                            -- exists yet (Phase 7A precedent: do not
                                            -- build ahead of evidence). This column
                                            -- exists purely so a future real
                                            -- taxonomy can be adopted without
                                            -- re-tagging every historical item.

    source_type         TEXT NOT NULL CHECK (source_type IN (
                            'original', 'adapted_from_public_source',
                            'teacher_authored', 'textbook_derived_paraphrase'
                        )),
    source_reference     TEXT,             -- nullable free text description of
                                            -- inspiration/origin. NEVER a verbatim
                                            -- textbook excerpt — see evaluation/README.md
                                            -- copyright note.
    author               TEXT NOT NULL,

    created_at           TEXT NOT NULL CHECK (created_at LIKE '____-__-__T__:__:__%')
);

-- ---------------------------------------------------------------------
-- Benchmark item content versions (Step 2 Section 4 / Revision Section 14)
-- ---------------------------------------------------------------------
-- Immutable content once created (all columns except content_status,
-- partition, and updated_at — see the immutability trigger below).
-- A corrected reference answer, a reworded question, anything about the
-- *content* of an item is always a NEW ROW with an incremented
-- version_number, never an UPDATE to an existing row's content.
CREATE TABLE IF NOT EXISTS benchmark_item_versions (
    item_version_id            TEXT PRIMARY KEY,
    item_id                    TEXT NOT NULL REFERENCES benchmark_items(item_id),
    version_number             INTEGER NOT NULL CHECK (version_number >= 1),

    -- Content maturity axis (Revision #5 / Step-2-revision Section 14).
    -- Independent of verification_records.verification_status below —
    -- conflating the two was the exact Step-2 gap this axis split fixes.
    content_status             TEXT NOT NULL DEFAULT 'draft' CHECK (content_status IN (
                                    'draft', 'reviewed', 'approved', 'active', 'deprecated'
                                )),

    -- dev/holdout partition (Step 2 Section 32 / Revision Section 20).
    -- Mutable in place (promoting an item from dev to holdout is a
    -- workflow decision, not a content change) — application-layer code
    -- in a later phase is responsible for enforcing "holdout requires
    -- content_status = approved AND verification_status = verified_correct"
    -- before allowing this to be set to 'holdout'; this schema stores the
    -- state, it does not yet enforce that specific cross-table rule
    -- (SQLite CHECK constraints cannot reference another table).
    partition                  TEXT NOT NULL DEFAULT 'dev' CHECK (partition IN ('dev', 'holdout')),

    question_text              TEXT NOT NULL,
    input_language              TEXT NOT NULL CHECK (input_language IN ('bn', 'en', 'bn-latn', 'mixed')),
    requested_output_language   TEXT NOT NULL CHECK (requested_output_language IN ('bn', 'en', 'bn-latn', 'mixed')),

    mode                        TEXT NOT NULL CHECK (mode IN ('direct', 'socratic')),
    mode_expected_behavior_json TEXT NOT NULL,   -- JSON, e.g.
                                                  -- {"expects_complete_final_answer": true}
                                                  -- or {"expects_no_immediate_final_answer": true,
                                                  --     "expects_guiding_questions": true}

    context_json                TEXT,            -- nullable JSON:
                                                  -- {"conversation_history": [...],
                                                  --  "sources": [{"fixture_id": "...", ...}]}
                                                  -- References context_fixtures.fixture_id
                                                  -- values inside the JSON list; see
                                                  -- context_fixtures table below.

    expected_behavior_json      TEXT NOT NULL,   -- JSON: {"reference_answer": ..., "rubric_text": ...,
                                                  --  "numerical_tolerance": ..., "acceptable_variants": [...]}
                                                  -- All sub-fields nullable inside the JSON —
                                                  -- e.g. a Socratic item legitimately has no
                                                  -- reference_answer (Step 2 Section 4).

    difficulty                  TEXT NOT NULL CHECK (difficulty IN ('easy', 'medium', 'hard')),
    tags_json                   TEXT,            -- nullable JSON list of free-form tags

    evaluation_policy_id         TEXT REFERENCES evaluation_policies(policy_id),
                                                  -- nullable at schema level so an item can be
                                                  -- authored before a policy is assigned; a later
                                                  -- phase's authoring tool should enforce
                                                  -- non-null before content_status can reach
                                                  -- 'approved'.

    created_at                   TEXT NOT NULL CHECK (created_at LIKE '____-__-__T__:__:__%'),
    updated_at                   TEXT NOT NULL CHECK (updated_at LIKE '____-__-__T__:__:__%'),

    UNIQUE (item_id, version_number)
);

-- ---------------------------------------------------------------------
-- Evaluation policies (Step 2 Section 8) — created before item_versions
-- can reference them, so this table is declared above but with the FK
-- forward-reference resolved by SQLite's deferred name resolution
-- (SQLite does not require tables to be declared in dependency order
-- inside a single script executed as a whole; this is standard and
-- portable — Postgres behaves the same way within one transaction/script).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS evaluation_policies (
    policy_id              TEXT PRIMARY KEY,
    name                   TEXT NOT NULL,
    dimension_methods_json TEXT NOT NULL,      -- JSON: {"correctness": "deterministic", ...}
    judge_rubric_id        TEXT,               -- nullable
    judge_rubric_version   INTEGER,            -- nullable
    created_at             TEXT NOT NULL CHECK (created_at LIKE '____-__-__T__:__:__%')
);

-- ---------------------------------------------------------------------
-- Verification (Step 2 Section 14 / Step-2-revision Sections 14 & 23)
-- ---------------------------------------------------------------------
-- Exactly one verification record per item_version (1:1) — a content
-- problem found during verification means a NEW item_version is
-- authored (fixing the record in place would let a "corrected" version
-- masquerade as the same evaluated content, which is exactly the
-- semantic-break class of bug Phase 7A's mastery-LIMIT investigation
-- already established Kognit must not tolerate).
--
-- is_self_verified is a stored, explicit flag per Mahfuz's approved
-- decision: "the system must record author and verifier separately...
-- self-verification must be identifiable in the data... do not falsely
-- represent self-verification as independent verification." This
-- column cannot be enforced by a CHECK constraint here (SQLite CHECK
-- constraints cannot look up benchmark_items.author for comparison) —
-- computing and setting it correctly (verified_by == the item's author)
-- is an application-layer responsibility for the authoring tool built in
-- a later phase. This is flagged explicitly in the Phase 7B-1 report,
-- not silently assumed solved by this column's mere existence.
CREATE TABLE IF NOT EXISTS verification_records (
    verification_id             TEXT PRIMARY KEY,
    item_version_id              TEXT NOT NULL UNIQUE
                                 REFERENCES benchmark_item_versions(item_version_id),

    verification_status         TEXT NOT NULL DEFAULT 'unverified' CHECK (verification_status IN (
                                    'unverified', 'verified_correct',
                                    'verified_needs_revision', 'rejected'
                                 )),
    verified_by                  TEXT,          -- nullable until verified
    is_self_verified              INTEGER CHECK (is_self_verified IN (0, 1)),
                                                  -- nullable until verified_by is set
    verified_at                   TEXT,          -- nullable
    verification_notes            TEXT,

    -- The six-check quality gate (Step-2-revision Section 23). Each is a
    -- plain boolean the reviewer sets; rubric_verified and
    -- language_reviewed are NULL (not 0) when not applicable to a given
    -- item, so "not applicable" is never confused with "checked and
    -- failed".
    curriculum_reviewed            INTEGER NOT NULL DEFAULT 0 CHECK (curriculum_reviewed IN (0, 1)),
    reference_answer_verified       INTEGER NOT NULL DEFAULT 0 CHECK (reference_answer_verified IN (0, 1)),
    rubric_verified                  INTEGER CHECK (rubric_verified IN (0, 1)),        -- nullable = N/A
    language_reviewed                 INTEGER CHECK (language_reviewed IN (0, 1)),      -- nullable = N/A
    ambiguity_check_passed             INTEGER NOT NULL DEFAULT 0 CHECK (ambiguity_check_passed IN (0, 1)),
    difficulty_sanity_checked           INTEGER NOT NULL DEFAULT 0 CHECK (difficulty_sanity_checked IN (0, 1)),

    created_at                    TEXT NOT NULL CHECK (created_at LIKE '____-__-__T__:__:__%'),
    updated_at                    TEXT NOT NULL CHECK (updated_at LIKE '____-__-__T__:__:__%')
);

-- ---------------------------------------------------------------------
-- Context fixtures (Step 2 Section 7/10 / Revision Section 7)
-- ---------------------------------------------------------------------
-- A registry of reusable PDF/image/retrieved-document fixtures.
-- benchmark_item_versions.context_json embeds fixture_id references
-- inside its JSON "sources" list rather than requiring a separate join
-- table — Step 2 explicitly favors this simpler shape unless real
-- cross-item fixture-usage querying is ever needed (no evidence of that
-- yet). content_hash is UNIQUE so identical fixture content is never
-- registered twice under two different fixture_ids.
CREATE TABLE IF NOT EXISTS context_fixtures (
    fixture_id       TEXT PRIMARY KEY,
    source_type      TEXT NOT NULL CHECK (source_type IN ('pdf', 'image', 'retrieved_document')),
    content_ref      TEXT,           -- nullable: path/URI to the actual fixture bytes.
                                      -- This schema records identity/hash only — it does
                                      -- NOT implement fixture storage (Step 2: "do not
                                      -- implement storage now").
    inline_text      TEXT,           -- nullable: for small text-based context stored directly
    content_hash     TEXT NOT NULL UNIQUE,
    description      TEXT,
    created_at       TEXT NOT NULL CHECK (created_at LIKE '____-__-__T__:__:__%')
);

-- ---------------------------------------------------------------------
-- Datasets & immutable dataset versions (Step 2 Section 5)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS datasets (
    dataset_id   TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    created_at   TEXT NOT NULL CHECK (created_at LIKE '____-__-__T__:__:__%')
);

-- Fully immutable once created (see trigger below) — including its own
-- display name snapshot (dataset_name_at_cut), specifically so a
-- DatasetVersion referenced by a year-old EvaluationRun remains
-- self-describing even if the parent Dataset is later renamed.
CREATE TABLE IF NOT EXISTS dataset_versions (
    dataset_version_id     TEXT PRIMARY KEY,
    dataset_id              TEXT NOT NULL REFERENCES datasets(dataset_id),
    version_number           INTEGER NOT NULL CHECK (version_number >= 1),
    dataset_name_at_cut       TEXT NOT NULL,
    created_at                TEXT NOT NULL CHECK (created_at LIKE '____-__-__T__:__:__%'),
    UNIQUE (dataset_id, version_number)
);

-- The immutable "lockfile" itself: the exact set of item versions a
-- given dataset version consists of. Rows are never updated or deleted
-- once inserted (see triggers below) — this is what makes a historical
-- run's dataset composition permanently reconstructible.
CREATE TABLE IF NOT EXISTS dataset_version_items (
    dataset_version_id   TEXT NOT NULL REFERENCES dataset_versions(dataset_version_id),
    item_version_id       TEXT NOT NULL REFERENCES benchmark_item_versions(item_version_id),
    PRIMARY KEY (dataset_version_id, item_version_id)
);

-- ---------------------------------------------------------------------
-- Evaluator identity (Step 2 Section 9/21, master-prompt "Evaluator
-- Version" requirement)
-- ---------------------------------------------------------------------
-- code_hash is nullable: meaningful/settable for deterministic evaluator
-- modules (a hash of their own source), not meaningful in the same way
-- for an LLM-judge "evaluator" whose identity is really judge_model +
-- judge_rubric_version (captured on evaluation_runs / judge_calibrations
-- instead). The master prompt's instruction ("must not depend only on a
-- human comment... use deterministic version identification where
-- practical") is honored where it is practical (deterministic
-- evaluators) without forcing a meaningless hash onto judge-type
-- evaluator rows.
CREATE TABLE IF NOT EXISTS evaluator_versions (
    evaluator_version_id  TEXT PRIMARY KEY,
    evaluator_name         TEXT NOT NULL,     -- e.g. "deterministic_numerical", "llm_judge"
    version_label           TEXT NOT NULL,     -- human-readable, e.g. "v1"
    code_hash                TEXT,             -- nullable; see note above
    description               TEXT,
    created_at                 TEXT NOT NULL CHECK (created_at LIKE '____-__-__T__:__:__%')
);

-- ---------------------------------------------------------------------
-- Judge calibration, scoped PER DIMENSION (Step-2-revision Section 15 —
-- the single most important correction from the revision pass)
-- ---------------------------------------------------------------------
-- calibration_run_id is nullable and, when set, references
-- evaluation_runs — a calibration exercise is modeled as a normal
-- EvaluationRun (so it inherits the same full reproducibility chain)
-- rather than inventing a second, parallel "run" concept.
CREATE TABLE IF NOT EXISTS judge_calibrations (
    calibration_id                TEXT PRIMARY KEY,
    evaluator_version_id           TEXT NOT NULL REFERENCES evaluator_versions(evaluator_version_id),
    judge_model                     TEXT NOT NULL,
    judge_model_version              TEXT,             -- nullable, resolved (best-effort)
    judge_rubric_id                   TEXT NOT NULL,
    judge_rubric_version                INTEGER NOT NULL,
    dimension                            TEXT NOT NULL CHECK (dimension IN (
                                            'correctness', 'curriculum_alignment', 'reasoning',
                                            'language', 'formatting', 'latency', 'cost'
                                          )),
    calibration_run_id                    TEXT REFERENCES evaluation_runs(run_id),

    sample_item_version_ids_json            TEXT NOT NULL,   -- JSON list — the exact calibration sample
    human_reference_scores_json              TEXT NOT NULL,   -- JSON: {item_version_id: score, ...}
    judge_scores_json                          TEXT NOT NULL, -- JSON: {item_version_id: score, ...}
    disagreement_summary_json                    TEXT NOT NULL,
                                                  -- JSON: {"agree_count": n, "disagree_count": n,
                                                  --        "mean_absolute_difference": f}
                                                  -- Deliberately plain counts + a mean difference,
                                                  -- NOT a named formal statistic (Cohen's kappa etc.)
                                                  -- per explicit instruction — schema-compatible
                                                  -- with adding one later as a derived field over
                                                  -- this same raw data, no redesign needed.

    trust_status                                  TEXT NOT NULL DEFAULT 'uncalibrated' CHECK (trust_status IN (
                                                    'uncalibrated', 'trusted', 'disputed'
                                                  )),
    calibrated_at                                  TEXT NOT NULL CHECK (calibrated_at LIKE '____-__-__T__:__:__%')
);

-- ---------------------------------------------------------------------
-- Evaluation runs (Step 2 Section 9 / Revision Section 9 — full
-- reproducibility-chain identity)
-- ---------------------------------------------------------------------
-- Fully immutable once created (trigger below) — a run record describes
-- a historical fact ("this is what ran"), never a mutable in-progress
-- state; a runner that needs to model "in progress" does so in
-- application memory, not by updating this row.
CREATE TABLE IF NOT EXISTS evaluation_runs (
    run_id                      TEXT PRIMARY KEY,
    created_at                   TEXT NOT NULL CHECK (created_at LIKE '____-__-__T__:__:__%'),

    dataset_version_id            TEXT NOT NULL REFERENCES dataset_versions(dataset_version_id),

    -- Reproducibility-chain fields (Step-2-revision Section 9) —
    -- mandatory per the approved architecture.
    git_commit_sha                  TEXT NOT NULL,
    evaluation_schema_version         TEXT NOT NULL,

    model_provider                     TEXT NOT NULL,
    model_name                           TEXT NOT NULL,
    resolved_model_version                 TEXT,        -- NULLABLE at schema level, deliberately.
                                                          -- Populated best-effort from the actual
                                                          -- Gemini response's model_version field
                                                          -- when the provider returns one. NEVER
                                                          -- fabricated when absent.
    model_config_snapshot_json               TEXT NOT NULL,

    prompt_version                             TEXT NOT NULL,
    prompt_hash                                  TEXT NOT NULL,

    evaluator_version_id                           TEXT NOT NULL REFERENCES evaluator_versions(evaluator_version_id),

    judge_model                                      TEXT,        -- nullable: null when no
                                                                    -- LLM-judge dimension was used
    judge_model_version                                TEXT,      -- nullable, resolved

    runtime_versions_json                                TEXT,    -- nullable, optional per Step-2-revision
                                                                    -- Section 9 (not mandatory — an SDK
                                                                    -- patch bump has low reproducibility
                                                                    -- value relative to the mandatory fields)
    environment                                            TEXT   -- nullable free text
);

-- ---------------------------------------------------------------------
-- Raw captured answer + usage + timing, one row per (run, item)
-- ---------------------------------------------------------------------
-- Deliberately separate from evaluation_results (the scores) — Step 2's
-- pipeline diagram shows "Raw Answer + Usage + Timing" as a distinct
-- stage from the scores the orchestrator later produces from it.
CREATE TABLE IF NOT EXISTS evaluation_item_runs (
    item_run_id            TEXT PRIMARY KEY,
    run_id                   TEXT NOT NULL REFERENCES evaluation_runs(run_id),
    item_version_id            TEXT NOT NULL REFERENCES benchmark_item_versions(item_version_id),

    generation_failed             INTEGER NOT NULL DEFAULT 0 CHECK (generation_failed IN (0, 1)),
    failure_reason                  TEXT,     -- nullable, set when generation_failed = 1
    raw_answer_text                   TEXT,   -- nullable when generation_failed = 1

    -- Latency model (Step 2 Section 19 / Revision Section 10). first_chunk_ts
    -- is nullable and, per explicit instruction, is NOT fabricated: it stays
    -- NULL for every row until/unless Kognit's production path adopts
    -- streaming (confirmed NOT implemented today - Phase 7B Step 1 audit).
    request_start_ts                    TEXT NOT NULL CHECK (request_start_ts LIKE '____-__-__T__:__:__%'),
    ai_call_start_ts                      TEXT CHECK (ai_call_start_ts IS NULL OR ai_call_start_ts LIKE '____-__-__T__:__:__%'),
    first_chunk_ts                          TEXT CHECK (first_chunk_ts IS NULL OR first_chunk_ts LIKE '____-__-__T__:__:__%'),
    ai_call_end_ts                            TEXT CHECK (ai_call_end_ts IS NULL OR ai_call_end_ts LIKE '____-__-__T__:__:__%'),
    response_returned_ts                        TEXT CHECK (response_returned_ts IS NULL OR response_returned_ts LIKE '____-__-__T__:__:__%'),

    -- Usage metadata (Step 2 Section 18) — raw counts only. Cost itself
    -- is NEVER computed/stored here; it is always (usage x a versioned
    -- pricing_configs row), computed at report time, per explicit
    -- instruction not to bake a point-in-time price into stored data.
    prompt_tokens                                  INTEGER,
    output_tokens                                    INTEGER,
    thoughts_tokens                                    INTEGER,
    cached_tokens                                        INTEGER,
    total_tokens                                          INTEGER,

    resolved_model_version                                  TEXT,  -- per-call ground truth; may in
                                                                     -- principle differ from the run-level
                                                                     -- value if a provider resolves
                                                                     -- differently across calls within
                                                                     -- one run — captured per-item so that
                                                                     -- possibility is never silently lost.

    created_at                                                TEXT NOT NULL CHECK (created_at LIKE '____-__-__T__:__:__%'),

    UNIQUE (run_id, item_version_id)
);

-- ---------------------------------------------------------------------
-- Pricing configuration (Step 2 Section 18 / Revision) — versioned,
-- never a hardcoded price anywhere in application logic.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pricing_configs (
    pricing_version          TEXT PRIMARY KEY,
    provider                   TEXT NOT NULL,
    model_name                   TEXT NOT NULL,
    price_per_input_token           REAL NOT NULL,
    price_per_output_token            REAL NOT NULL,
    price_per_thought_token             REAL,        -- nullable: not all models bill thinking tokens
                                                       -- separately/at all
    effective_date                        TEXT NOT NULL CHECK (effective_date LIKE '____-__-__T__:__:__%'),
    source_note                             TEXT      -- e.g. "verified against official Gemini
                                                        -- pricing page on <date>" — never invented
);

-- ---------------------------------------------------------------------
-- Dimension scores (Step 2 Section 23 — never a single forced score)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS evaluation_results (
    result_id             TEXT PRIMARY KEY,
    run_id                  TEXT NOT NULL REFERENCES evaluation_runs(run_id),
    item_version_id           TEXT NOT NULL REFERENCES benchmark_item_versions(item_version_id),

    dimension                  TEXT NOT NULL CHECK (dimension IN (
                                'correctness', 'curriculum_alignment', 'reasoning',
                                'language', 'formatting', 'latency', 'cost'
                              )),

    score                        REAL,       -- nullable: null when the item had an operational
                                              -- failure (Phase 7B_item_runs.generation_failed = 1)
                                              -- and no dimension could be scored at all
    confidence                    REAL CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
    method                          TEXT NOT NULL CHECK (method IN ('deterministic', 'llm_judge', 'human', 'hybrid')),
    evaluator_version_id             TEXT NOT NULL REFERENCES evaluator_versions(evaluator_version_id),
    evidence                           TEXT,   -- nullable free text

    created_at                          TEXT NOT NULL CHECK (created_at LIKE '____-__-__T__:__:__%'),

    UNIQUE (run_id, item_version_id, dimension)
);

-- ---------------------------------------------------------------------
-- Failure flags — multi-valued, one answer can have several (Step 2
-- Section 24, taxonomy compacted per the master-prompt's explicit
-- instruction to keep it small and evidence-driven rather than
-- reusing Step 2's longer draft list wholesale)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS failure_flags (
    failure_flag_id        TEXT PRIMARY KEY,
    run_id                    TEXT NOT NULL REFERENCES evaluation_runs(run_id),
    item_version_id             TEXT NOT NULL REFERENCES benchmark_item_versions(item_version_id),

    dimension                    TEXT NOT NULL CHECK (dimension IN (
                                    'correctness', 'curriculum_alignment', 'reasoning',
                                    'language', 'formatting', 'latency', 'cost', 'operational'
                                  )),
    failure_type                   TEXT NOT NULL CHECK (failure_type IN (
                                    'factual_error', 'numerical_error', 'curriculum_mismatch',
                                    'reasoning_error', 'unsupported_claim', 'language_issue',
                                    'formatting_issue', 'latency_failure', 'generation_failure',
                                    'mode_violation'
                                  )),
    severity                         TEXT NOT NULL CHECK (severity IN ('low', 'medium', 'high', 'critical')),

    evaluator_version_id               TEXT NOT NULL REFERENCES evaluator_versions(evaluator_version_id),
    explanation                          TEXT,

    created_at                            TEXT NOT NULL CHECK (created_at LIKE '____-__-__T__:__:__%')
);

-- =======================================================================
-- INDEXES — only added against real, named query patterns (Phase 7A's
-- own "no speculative indexes" discipline, reapplied here). Every index
-- below corresponds to a query the runner/report layer will actually run
-- in the phases immediately following this one.
-- =======================================================================

-- Look up all versions of one item, in order (authoring/review UI).
CREATE INDEX IF NOT EXISTS idx_item_versions_item_id
    ON benchmark_item_versions (item_id, version_number);

-- Filter items eligible for a new DatasetVersion cut by status/partition.
CREATE INDEX IF NOT EXISTS idx_item_versions_status_partition
    ON benchmark_item_versions (content_status, partition);

-- Look up the (exactly one) verification record for a given item version.
-- (UNIQUE constraint above already creates this index implicitly in
-- SQLite, listed here only for documentation clarity — not duplicated.)

-- Reconstruct a dataset version's exact item list (the lockfile read).
CREATE INDEX IF NOT EXISTS idx_dataset_version_items_dv
    ON dataset_version_items (dataset_version_id);

-- Report queries: all results for a run, sliced by dimension.
CREATE INDEX IF NOT EXISTS idx_results_run_dimension
    ON evaluation_results (run_id, dimension);

-- Report queries: all results for one item across runs (regression, per item).
CREATE INDEX IF NOT EXISTS idx_results_item_version
    ON evaluation_results (item_version_id, run_id);

-- Report queries: failure counts per run, sliced by dimension/severity.
CREATE INDEX IF NOT EXISTS idx_failure_flags_run_dim_severity
    ON failure_flags (run_id, dimension, severity);

-- Runner/report: all item-run records for a given run (iterate a run's items).
CREATE INDEX IF NOT EXISTS idx_item_runs_run
    ON evaluation_item_runs (run_id);

-- Calibration lookups: per evaluator, per dimension, most recent first.
CREATE INDEX IF NOT EXISTS idx_judge_calibrations_evaluator_dimension
    ON judge_calibrations (evaluator_version_id, dimension, calibrated_at);


-- =======================================================================
-- IMMUTABILITY TRIGGERS
--
-- SQLite-specific syntax (see the portability note at the top of this
-- file). These enforce, at the database layer rather than by convention
-- alone, the append-only/immutable-after-creation guarantees the
-- architecture depends on for reproducibility. Reuses the same
-- "evidence tables are append-only" instinct already established
-- elsewhere in this codebase (learning_evidence, conversation_index in
-- supabase/migrations/0003 — those enforce it via RLS with no UPDATE
-- policy; SQLite has no RLS concept, so a trigger is the equivalent
-- mechanism here).
-- =======================================================================

-- benchmark_item_versions: content is immutable; content_status,
-- partition, and updated_at are the only columns allowed to change.
CREATE TRIGGER IF NOT EXISTS trg_item_versions_protect_content
BEFORE UPDATE ON benchmark_item_versions
WHEN
    OLD.item_id                     IS NOT NEW.item_id OR
    OLD.version_number               IS NOT NEW.version_number OR
    OLD.question_text                 IS NOT NEW.question_text OR
    OLD.input_language                  IS NOT NEW.input_language OR
    OLD.requested_output_language         IS NOT NEW.requested_output_language OR
    OLD.mode                                IS NOT NEW.mode OR
    OLD.mode_expected_behavior_json           IS NOT NEW.mode_expected_behavior_json OR
    OLD.context_json                            IS NOT NEW.context_json OR
    OLD.expected_behavior_json                    IS NOT NEW.expected_behavior_json OR
    OLD.difficulty                                  IS NOT NEW.difficulty OR
    OLD.tags_json                                     IS NOT NEW.tags_json OR
    OLD.evaluation_policy_id                            IS NOT NEW.evaluation_policy_id OR
    OLD.created_at                                        IS NOT NEW.created_at
BEGIN
    SELECT RAISE(ABORT, 'benchmark_item_versions: content columns are immutable once created - create a new version instead');
END;

-- dataset_versions: fully immutable once created.
CREATE TRIGGER IF NOT EXISTS trg_dataset_versions_immutable
BEFORE UPDATE ON dataset_versions
BEGIN
    SELECT RAISE(ABORT, 'dataset_versions: immutable once created - cut a new dataset version instead');
END;

-- dataset_version_items: the lockfile itself. No updates, no deletes,
-- ever, once a row exists.
CREATE TRIGGER IF NOT EXISTS trg_dataset_version_items_no_update
BEFORE UPDATE ON dataset_version_items
BEGIN
    SELECT RAISE(ABORT, 'dataset_version_items: immutable lockfile entries - cannot be modified');
END;

CREATE TRIGGER IF NOT EXISTS trg_dataset_version_items_no_delete
BEFORE DELETE ON dataset_version_items
BEGIN
    SELECT RAISE(ABORT, 'dataset_version_items: immutable lockfile entries - cannot be removed');
END;

-- evaluation_runs: a run record describes a historical fact, never
-- mutated after creation.
CREATE TRIGGER IF NOT EXISTS trg_evaluation_runs_immutable
BEFORE UPDATE ON evaluation_runs
BEGIN
    SELECT RAISE(ABORT, 'evaluation_runs: immutable once created');
END;

-- evaluation_results, failure_flags, evaluation_item_runs: append-only
-- audit/result records. Correcting a scoring bug means re-running
-- evaluation (a new run_id), never editing a past result in place.
CREATE TRIGGER IF NOT EXISTS trg_evaluation_results_immutable
BEFORE UPDATE ON evaluation_results
BEGIN
    SELECT RAISE(ABORT, 'evaluation_results: append-only - re-run evaluation instead of editing a stored result');
END;

CREATE TRIGGER IF NOT EXISTS trg_failure_flags_immutable
BEFORE UPDATE ON failure_flags
BEGIN
    SELECT RAISE(ABORT, 'failure_flags: append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_evaluation_item_runs_immutable
BEFORE UPDATE ON evaluation_item_runs
BEGIN
    SELECT RAISE(ABORT, 'evaluation_item_runs: append-only raw capture record');
END;

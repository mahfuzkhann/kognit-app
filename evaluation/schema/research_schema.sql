-- Kognit Phase 7C — Research benchmark schema.
--
-- SEPARATE from evaluation/schema/schema.sql (Phase 7B, Answer Quality)
-- per the approved architecture's explicit instruction: "Do not mix it
-- blindly into the NCTB Answer Quality benchmark." Lives in its own
-- database file (evaluation/data/research_v1.sqlite), initialized via
-- evaluation/db.py:initialize_schema(conn, schema_path=..., schema_version=...).
--
-- Mirrors the SAME architectural discipline as schema.sql: immutable
-- versioned items, a separate verification-gate table, immutable
-- dataset-version lockfiles, append-only results/failure-flags, and the
-- same ISO-8601-TEXT / opaque-JSON-TEXT / no-JSON1-query-syntax
-- portability conventions - see that file's own header comment for the
-- full portability rationale, not repeated here.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------------
-- Research benchmark item identity (Step 11)
-- ---------------------------------------------------------------------
-- category is the Step 11 A-J coverage taxonomy. expected_research_decision
-- is the ground truth this benchmark measures the decisioning heuristic
-- against (Step 10.1: unnecessary research / missed research / correct
-- decision).
CREATE TABLE IF NOT EXISTS research_benchmark_items (
    item_id                     TEXT PRIMARY KEY,
    category                    TEXT NOT NULL CHECK (category IN (
                                    'current_factual', 'recent_development', 'current_bangladesh_info',
                                    'current_education_info', 'current_technology', 'current_science',
                                    'current_public_info', 'stable_factual_no_search_needed',
                                    'ambiguous_freshness', 'potentially_misleading_search'
                                )),
    expected_research_decision  TEXT NOT NULL CHECK (expected_research_decision IN ('required', 'not_required')),
    freshness_requirement       TEXT NOT NULL CHECK (freshness_requirement IN (
                                    'timeless', 'stable_long_term', 'changes_yearly', 'changes_frequently', 'changes_daily'
                                )),
    source_type                 TEXT NOT NULL CHECK (source_type IN (
                                    'original', 'adapted_from_public_source', 'teacher_authored', 'textbook_derived_paraphrase'
                                )),
    source_reference             TEXT,
    author                        TEXT NOT NULL,
    created_at                     TEXT NOT NULL CHECK (created_at LIKE '____-__-__T__:__:__%')
);

-- ---------------------------------------------------------------------
-- Item versions (Step 11 content, immutable) + Step 12's verification
-- lifecycle fields
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research_item_versions (
    item_version_id           TEXT PRIMARY KEY,
    item_id                     TEXT NOT NULL REFERENCES research_benchmark_items(item_id),
    version_number               INTEGER NOT NULL CHECK (version_number >= 1),

    content_status                TEXT NOT NULL DEFAULT 'draft' CHECK (content_status IN (
                                    'draft', 'reviewed', 'approved', 'active', 'deprecated'
                                )),
    partition                       TEXT NOT NULL DEFAULT 'dev' CHECK (partition IN ('dev', 'holdout')),

    question_text                    TEXT NOT NULL,
    language                          TEXT NOT NULL CHECK (language IN ('bn', 'en', 'bn-latn', 'mixed')),
    difficulty                         TEXT NOT NULL CHECK (difficulty IN ('easy', 'medium', 'hard')),

    reference_facts_json                 TEXT,   -- nullable JSON: known-at-authoring-time facts,
                                                   -- for evaluator/judge reference only - NEVER
                                                   -- treated as eternally true (see
                                                   -- verified_as_of_date below).
    citation_expectations_json             TEXT,  -- nullable JSON: {min_sources, requires_citation: bool}
    evaluation_rubric_text                   TEXT, -- nullable, for judge-scored dimensions

    tags_json                                  TEXT,

    created_at                                  TEXT NOT NULL CHECK (created_at LIKE '____-__-__T__:__:__%'),
    updated_at                                    TEXT NOT NULL CHECK (updated_at LIKE '____-__-__T__:__:__%'),

    UNIQUE (item_id, version_number)
);

-- ---------------------------------------------------------------------
-- Verification (Step 12) - mirrors Phase 7B's verification_records,
-- PLUS the Phase-7C-specific temporal verification requirement: a
-- current-information item's reference facts must record the date they
-- were true as of, since they are NOT timeless the way most Answer
-- Quality items are.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research_verification_records (
    verification_id          TEXT PRIMARY KEY,
    item_version_id            TEXT NOT NULL UNIQUE REFERENCES research_item_versions(item_version_id),

    verification_status         TEXT NOT NULL DEFAULT 'unverified' CHECK (verification_status IN (
                                    'unverified', 'verified_correct', 'verified_needs_revision', 'rejected'
                                )),
    verified_by                   TEXT,
    is_self_verified                INTEGER CHECK (is_self_verified IN (0, 1)),
    verified_at                      TEXT,
    verification_notes                TEXT,

    -- Step 12's explicit temporal requirement: for any item whose
    -- freshness_requirement is NOT 'timeless', verified_as_of_date must
    -- be set before verification_status can become 'verified_correct' -
    -- enforced in application code (research_authoring.py), not by a
    -- CHECK constraint here (cannot cross-reference
    -- research_item_versions.freshness_requirement from this table).
    verified_as_of_date              TEXT CHECK (verified_as_of_date IS NULL OR verified_as_of_date LIKE '____-__-__'),

    curriculum_reviewed                 INTEGER NOT NULL DEFAULT 0 CHECK (curriculum_reviewed IN (0, 1)),
    reference_facts_verified              INTEGER NOT NULL DEFAULT 0 CHECK (reference_facts_verified IN (0, 1)),
    ambiguity_check_passed                  INTEGER NOT NULL DEFAULT 0 CHECK (ambiguity_check_passed IN (0, 1)),

    created_at                                TEXT NOT NULL CHECK (created_at LIKE '____-__-__T__:__:__%'),
    updated_at                                  TEXT NOT NULL CHECK (updated_at LIKE '____-__-__T__:__:__%')
);

-- ---------------------------------------------------------------------
-- Datasets / immutable dataset versions - identical pattern to Phase 7B
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research_datasets (
    dataset_id   TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    created_at   TEXT NOT NULL CHECK (created_at LIKE '____-__-__T__:__:__%')
);

CREATE TABLE IF NOT EXISTS research_dataset_versions (
    dataset_version_id     TEXT PRIMARY KEY,
    dataset_id               TEXT NOT NULL REFERENCES research_datasets(dataset_id),
    version_number             INTEGER NOT NULL CHECK (version_number >= 1),
    dataset_name_at_cut          TEXT NOT NULL,
    created_at                     TEXT NOT NULL CHECK (created_at LIKE '____-__-__T__:__:__%'),
    UNIQUE (dataset_id, version_number)
);

CREATE TABLE IF NOT EXISTS research_dataset_version_items (
    dataset_version_id   TEXT NOT NULL REFERENCES research_dataset_versions(dataset_version_id),
    item_version_id       TEXT NOT NULL REFERENCES research_item_versions(item_version_id),
    PRIMARY KEY (dataset_version_id, item_version_id)
);

-- ---------------------------------------------------------------------
-- Evaluator identity - reuses the SAME concept as Phase 7B's
-- evaluator_versions, kept as its own table (not shared across the two
-- separate database files) so this schema remains fully self-contained.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research_evaluator_versions (
    evaluator_version_id  TEXT PRIMARY KEY,
    evaluator_name         TEXT NOT NULL,
    version_label            TEXT NOT NULL,
    code_hash                  TEXT,
    description                  TEXT,
    created_at                    TEXT NOT NULL CHECK (created_at LIKE '____-__-__T__:__:__%')
);

-- ---------------------------------------------------------------------
-- Judge calibration - per-dimension, same discipline as Phase 7B
-- (Step 14: source_relevance, claim_grounding, citation_correctness,
-- citation_completeness at minimum).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research_judge_calibrations (
    calibration_id                TEXT PRIMARY KEY,
    evaluator_version_id            TEXT NOT NULL REFERENCES research_evaluator_versions(evaluator_version_id),
    judge_model                       TEXT NOT NULL,
    judge_model_version                 TEXT,
    judge_rubric_id                       TEXT NOT NULL,
    judge_rubric_version                    INTEGER NOT NULL,
    dimension                                TEXT NOT NULL CHECK (dimension IN (
                                                'source_relevance', 'claim_grounding',
                                                'citation_correctness', 'citation_completeness'
                                              )),
    calibration_run_id                          TEXT REFERENCES research_evaluation_runs(run_id),
    sample_item_version_ids_json                  TEXT NOT NULL,
    human_reference_scores_json                     TEXT NOT NULL,
    judge_scores_json                                 TEXT NOT NULL,
    disagreement_summary_json                           TEXT NOT NULL,
    trust_status                                          TEXT NOT NULL DEFAULT 'uncalibrated' CHECK (trust_status IN (
                                                            'uncalibrated', 'trusted', 'disputed'
                                                          )),
    calibrated_at                                          TEXT NOT NULL CHECK (calibrated_at LIKE '____-__-__T__:__:__%')
);

-- ---------------------------------------------------------------------
-- Evaluation runs - same reproducibility-chain discipline as Phase 7B.
-- resolved_model_version/prompt_hash/git_commit_sha are all mandatory,
-- same reasoning as evaluation/schema/schema.sql.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research_evaluation_runs (
    run_id                      TEXT PRIMARY KEY,
    created_at                   TEXT NOT NULL CHECK (created_at LIKE '____-__-__T__:__:__%'),
    dataset_version_id            TEXT NOT NULL REFERENCES research_dataset_versions(dataset_version_id),
    git_commit_sha                  TEXT NOT NULL,
    research_schema_version           TEXT NOT NULL,
    model_provider                     TEXT NOT NULL,
    model_name                           TEXT NOT NULL,
    resolved_model_version                 TEXT,
    model_config_snapshot_json               TEXT NOT NULL,
    prompt_version                             TEXT NOT NULL,
    prompt_hash                                  TEXT NOT NULL,
    evaluator_version_id                           TEXT NOT NULL REFERENCES research_evaluator_versions(evaluator_version_id),
    judge_model                                      TEXT,
    judge_model_version                                TEXT,
    runtime_versions_json                                TEXT,
    environment                                            TEXT
);

-- ---------------------------------------------------------------------
-- Raw captured research execution: the decision, the answer, the raw
-- normalized ResearchResult (as JSON), and timing/usage.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research_item_runs (
    item_run_id                TEXT PRIMARY KEY,
    run_id                        TEXT NOT NULL REFERENCES research_evaluation_runs(run_id),
    item_version_id                TEXT NOT NULL REFERENCES research_item_versions(item_version_id),

    generation_failed                 INTEGER NOT NULL DEFAULT 0 CHECK (generation_failed IN (0, 1)),
    failure_reason                      TEXT,
    raw_answer_text                       TEXT,

    -- The Step 2 decision actually made for this item, and whether the
    -- provider's own grounding_metadata proves search actually occurred -
    -- these are two DISTINCT, both-recorded facts (Step 2's own
    -- distinction between "requested" and "actually occurred").
    research_requested                      INTEGER NOT NULL CHECK (research_requested IN (0, 1)),
    decision_category                         TEXT,
    grounding_status                            TEXT NOT NULL CHECK (grounding_status IN (
                                                'not_used', 'used', 'failed', 'unavailable'
                                              )),
    search_queries_json                           TEXT,
    normalized_research_result_json                 TEXT,  -- full ResearchResult, JSON-serialized,
                                                              -- for evaluator/judge input - the one
                                                              -- place raw-ish provider-derived data is
                                                              -- persisted, already normalized (never
                                                              -- raw provider objects).

    request_start_ts                                  TEXT NOT NULL CHECK (request_start_ts LIKE '____-__-__T__:__:__%'),
    ai_call_end_ts                                       TEXT CHECK (ai_call_end_ts IS NULL OR ai_call_end_ts LIKE '____-__-__T__:__:__%'),
    research_latency_seconds                               REAL,

    prompt_tokens                                            INTEGER,
    output_tokens                                              INTEGER,
    total_tokens                                                 INTEGER,

    created_at                                                     TEXT NOT NULL CHECK (created_at LIKE '____-__-__T__:__:__%'),

    UNIQUE (run_id, item_version_id)
);

-- ---------------------------------------------------------------------
-- Dimension scores - same "never a single score" discipline as Phase 7B.
-- Dimensions cover BOTH the deterministic checks (Step 13) and the
-- LLM-judge-only semantic dimensions (Step 14).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research_evaluation_results (
    result_id             TEXT PRIMARY KEY,
    run_id                   TEXT NOT NULL REFERENCES research_evaluation_runs(run_id),
    item_version_id            TEXT NOT NULL REFERENCES research_item_versions(item_version_id),

    dimension                    TEXT NOT NULL CHECK (dimension IN (
                                    'research_decision_correctness', 'citation_url_validity',
                                    'citation_mapping_validity', 'citation_coverage', 'source_count',
                                    'search_used_consistency', 'source_relevance', 'claim_grounding',
                                    'citation_correctness', 'citation_completeness'
                                  )),
    score                          REAL,
    confidence                       REAL CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
    method                             TEXT NOT NULL CHECK (method IN ('deterministic', 'llm_judge', 'human', 'hybrid')),
    evaluator_version_id                TEXT NOT NULL REFERENCES research_evaluator_versions(evaluator_version_id),
    evidence                              TEXT,

    created_at                             TEXT NOT NULL CHECK (created_at LIKE '____-__-__T__:__:__%'),

    UNIQUE (run_id, item_version_id, dimension)
);

-- ---------------------------------------------------------------------
-- Failure flags - Step 10.7's research-specific failure taxonomy.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research_failure_flags (
    failure_flag_id        TEXT PRIMARY KEY,
    run_id                    TEXT NOT NULL REFERENCES research_evaluation_runs(run_id),
    item_version_id             TEXT NOT NULL REFERENCES research_item_versions(item_version_id),

    dimension                    TEXT NOT NULL,
    failure_type                   TEXT NOT NULL CHECK (failure_type IN (
                                    'unnecessary_research', 'missed_research', 'search_failure',
                                    'no_useful_sources', 'malformed_grounding_metadata',
                                    'citation_extraction_failure', 'malformed_citation',
                                    'missing_citation', 'invalid_citation_url', 'unsupported_claim',
                                    'irrelevant_source', 'api_timeout', 'provider_error'
                                  )),
    severity                        TEXT NOT NULL CHECK (severity IN ('low', 'medium', 'high', 'critical')),

    evaluator_version_id              TEXT NOT NULL REFERENCES research_evaluator_versions(evaluator_version_id),
    explanation                         TEXT,

    created_at                            TEXT NOT NULL CHECK (created_at LIKE '____-__-__T__:__:__%')
);

-- =======================================================================
-- INDEXES - only against real, named query patterns, same discipline as
-- evaluation/schema/schema.sql.
-- =======================================================================
CREATE INDEX IF NOT EXISTS idx_research_item_versions_item_id
    ON research_item_versions (item_id, version_number);
CREATE INDEX IF NOT EXISTS idx_research_item_versions_status_partition
    ON research_item_versions (content_status, partition);
CREATE INDEX IF NOT EXISTS idx_research_dataset_version_items_dv
    ON research_dataset_version_items (dataset_version_id);
CREATE INDEX IF NOT EXISTS idx_research_results_run_dimension
    ON research_evaluation_results (run_id, dimension);
CREATE INDEX IF NOT EXISTS idx_research_failure_flags_run_dim_severity
    ON research_failure_flags (run_id, dimension, severity);
CREATE INDEX IF NOT EXISTS idx_research_item_runs_run
    ON research_item_runs (run_id);
CREATE INDEX IF NOT EXISTS idx_research_judge_calibrations_evaluator_dimension
    ON research_judge_calibrations (evaluator_version_id, dimension, calibrated_at);

-- =======================================================================
-- IMMUTABILITY TRIGGERS - same append-only discipline as Phase 7B.
-- =======================================================================
CREATE TRIGGER IF NOT EXISTS trg_research_item_versions_protect_content
BEFORE UPDATE ON research_item_versions
WHEN
    OLD.item_id IS NOT NEW.item_id OR
    OLD.version_number IS NOT NEW.version_number OR
    OLD.question_text IS NOT NEW.question_text OR
    OLD.language IS NOT NEW.language OR
    OLD.difficulty IS NOT NEW.difficulty OR
    OLD.reference_facts_json IS NOT NEW.reference_facts_json OR
    OLD.citation_expectations_json IS NOT NEW.citation_expectations_json OR
    OLD.evaluation_rubric_text IS NOT NEW.evaluation_rubric_text OR
    OLD.tags_json IS NOT NEW.tags_json OR
    OLD.created_at IS NOT NEW.created_at
BEGIN
    SELECT RAISE(ABORT, 'research_item_versions: content columns are immutable once created - create a new version instead');
END;

CREATE TRIGGER IF NOT EXISTS trg_research_dataset_versions_immutable
BEFORE UPDATE ON research_dataset_versions
BEGIN
    SELECT RAISE(ABORT, 'research_dataset_versions: immutable once created');
END;

CREATE TRIGGER IF NOT EXISTS trg_research_dataset_version_items_no_update
BEFORE UPDATE ON research_dataset_version_items
BEGIN
    SELECT RAISE(ABORT, 'research_dataset_version_items: immutable lockfile entries');
END;

CREATE TRIGGER IF NOT EXISTS trg_research_dataset_version_items_no_delete
BEFORE DELETE ON research_dataset_version_items
BEGIN
    SELECT RAISE(ABORT, 'research_dataset_version_items: immutable lockfile entries');
END;

CREATE TRIGGER IF NOT EXISTS trg_research_evaluation_runs_immutable
BEFORE UPDATE ON research_evaluation_runs
BEGIN
    SELECT RAISE(ABORT, 'research_evaluation_runs: immutable once created');
END;

CREATE TRIGGER IF NOT EXISTS trg_research_evaluation_results_immutable
BEFORE UPDATE ON research_evaluation_results
BEGIN
    SELECT RAISE(ABORT, 'research_evaluation_results: append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_research_failure_flags_immutable
BEFORE UPDATE ON research_failure_flags
BEGIN
    SELECT RAISE(ABORT, 'research_failure_flags: append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_research_item_runs_immutable
BEFORE UPDATE ON research_item_runs
BEGIN
    SELECT RAISE(ABORT, 'research_item_runs: append-only raw capture record');
END;

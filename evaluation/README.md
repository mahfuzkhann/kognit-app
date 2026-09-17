# Kognit Evaluation Subsystem (Phase 7B)

Answer Quality & Evaluation infrastructure for Kognit. Kept structurally
separate from `backend/` — see "Dependency direction" below — because
production Kognit behavior must never depend on this subsystem existing.

## Status

Phases 7B-1 through 7B-11 have real, tested implementations. **What is
genuinely NOT complete, disclosed honestly rather than glossed over:**

- **No live Gemini API calls have ever been made by this subsystem.**
  Every test uses mocked model/judge responses. This environment had no
  usable `GEMINI_API_KEY`. The runner and judge are real, working code
  against the real production `generate_ai_response()` function - they
  have simply never been exercised against a live model.
- **No LLM judge has ever been calibrated against real human reference
  scores.** `judge.py` and `calibration.py` are complete, tested
  infrastructure; no actual calibration run exists. Every judge
  `trust_status` is `'uncalibrated'` by construction. **Do not treat any
  judge output from this system as reliable until a real calibration
  pass has been run and reviewed by a human.**
- **The Core v1 benchmark has 15 items, not the 60-90 originally
  targeted**, and every item is `content_status='draft'`,
  `verification_status='unverified'`, `partition='dev'`. None are
  holdout-eligible. See `evaluation/seed_core_v1.py`'s module docstring
  for the full disclosure - this is a deliberate quality-over-count
  decision (no independent human academic reviewer was available in
  this session), not a shortfall being hidden.
- **A rendered-output (browser/MathJax) formatting checker does not
  exist.** Only raw-text formatting-artifact detection is implemented
  (Phase 7B Step 2 architecture explicitly named this as real future
  work, not Stage 1 scope).

## Dependency direction

```
evaluation/  -->  backend/        (allowed - e.g. model_adapter.py calling
                                    backend.ai_engine.generate_ai_response)

backend/     -->  evaluation/     (NEVER - confirmed by grep, zero occurrences)
```

`evaluation/` has zero import path to `backend.database` — the module
that talks to Kognit's production Supabase project. Real student data
cannot reach this subsystem because no code path exists for it to do so.

## Module map

- `schema/schema.sql`, `db.py` — SQLite schema + connection/init helper (Phase 7B-1).
- `authoring.py` — create items/versions, record verification, the six-check
  quality gate, holdout promotion, dataset version cutting (Phase 7B-2).
- `prompt_identity.py`, `git_identity.py` — prompt version+hash and git
  commit SHA capture for run reproducibility (Phase 7B-3). The
  corresponding production change is `backend/ai_engine.py`'s
  `return_metadata` parameter and `CHAT_SYSTEM_INSTRUCTION_RULES` /
  `CHAT_PROMPT_VERSION` constants - both additive, default-off, and
  covered by `evaluation/tests/test_prompt_and_metadata_capture.py`.
- `model_adapter.py` — `GeminiAdapter`, the one production-AI-calling
  boundary evaluation code uses (Phase 7B-4). Never reimplements prompt
  construction or retry logic - calls `generate_ai_response(...,
  return_metadata=True)` directly.
- `runner.py` — loads a dataset version, calls the adapter per item,
  runs evaluators, persists everything, isolates per-item failures
  (Phase 7B-5).
- `evaluators/deterministic.py` — numerical-tolerance, MCQ,
  script-correctness, and raw-formatting-artifact checks. Pure functions,
  no I/O (Phase 7B-6).
- `judge.py`, `calibration.py` — structured-output LLM judge and
  per-dimension trust calibration recording (Phase 7B-7). **Never
  exercised against a live model in this session — see Status above.**
- `seed_core_v1.py` — the 15-item Core v1 seed script; run via
  `python -m evaluation.seed_core_v1` (Phase 7B-8). Produces
  `evaluation/data/core_v1.sqlite`.
- `report.py` — per-run report generation + Markdown rendering. Never
  produces a single overall score (Phase 7B-9).
- `regression.py` — Run A vs. Run B comparison, sliced by
  subject/class/language/mode, per dimension. Never declares a single
  winner (Phase 7B-10).
- `tests/` — see the final implementation report for the exact test
  count and breakdown (Phase 7B-11 hardening validated it).

## How to create a benchmark item

```python
from evaluation import authoring, db as evaldb

conn = evaldb.get_connection("evaluation/data/core_v1.sqlite")
item_id, item_version_id = authoring.create_item_with_first_version(
    conn, authoring.ItemDraft(
        question_type="numerical_tolerance", curriculum_class="Class 10",
        curriculum_subject="Physics", curriculum_topics=["Force & Motion"],
        source_type="original", author="mahfuz",
        question_text="...", input_language="en", requested_output_language="en",
        mode="direct", expected_behavior={"reference_answer": "6 N", "numerical_tolerance": 0.01},
        difficulty="easy",
    ),
)
```

## How to verify and approve an item

```python
authoring.record_verification(conn, item_version_id, authoring.VerificationInput(
    verified_by="mahfuz", curriculum_reviewed=True, reference_answer_verified=True,
    ambiguity_check_passed=True, difficulty_sanity_checked=True,
))
authoring.advance_content_status(conn, item_version_id, "approved")
# Only now is promote_to_holdout() possible:
authoring.promote_to_holdout(conn, item_version_id)
```

## How to run an evaluation

```python
from evaluation.runner import RunConfig, run_evaluation

run_id = run_evaluation(conn, RunConfig(
    dataset_version_id=dataset_version_id,
    evaluator_version_id=evaluator_version_id,  # must exist in evaluator_versions first
    judge_client=None,  # pass a real genai.Client + JudgeConfig to enable judge dimensions
))
```

## How to compare two runs

```python
from evaluation.regression import compare_runs
comparison = compare_runs(conn, run_a_id, run_b_id)
# comparison["dimension_deltas"], comparison["slice_deltas"] - never a single winner.
```

## Portability (SQLite now, Postgres later if ever needed)

- Timestamps: ISO-8601 TEXT with a `CHECK` constraint on shape.
- JSON fields: stored as opaque TEXT, parsed in Python. No SQLite
  JSON1-extension query syntax used anywhere.
- Foreign keys: standard `REFERENCES` syntax.
- Immutability triggers: SQLite-specific syntax; the concept maps
  directly onto PostgreSQL triggers, which would need rewriting in
  PL/pgSQL at migration time - normal trigger porting, not a data-model
  problem.

## Copyright & provenance

`benchmark_items.source_type` is constrained to `original |
adapted_from_public_source | teacher_authored | textbook_derived_paraphrase`.
The last of these means an original paraphrase inspired by a curriculum
concept, never a reproduction. No verbatim textbook text is stored
anywhere in this subsystem.

## Self-verification

The same person may author and verify an item (approved decision, given
Kognit's current team size). `is_self_verified` is computed and stored
by `authoring.record_verification()` — never trusted from caller input —
so self-verification is always identifiable in the data, never
misrepresented as independent verification.

## What Phase 7B deliberately does not include

No workflow engine, no full NCTB curriculum taxonomy (see the reserved,
unpopulated `curriculum_node_id` column), no live pricing data (see
`pricing_configs` — empty until populated from verified official
sources), no rendered-output formatting checker, no CI integration, no
web dashboard, no distributed/microservice infrastructure.

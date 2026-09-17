"""
Kognit Phase 7B-5 - Evaluation Runner.

Loads an immutable DatasetVersion, calls the real production AI path
(via the GeminiAdapter, which itself calls backend.ai_engine.generate_ai_response)
for every item, runs deterministic evaluators (and, optionally, the LLM
judge when a judge_client is supplied), and persists everything -
answer, usage, timing, dimension scores, failure flags - through
evaluation.db.

One item's failure (a crash in the adapter, a malformed judge response,
etc.) is isolated and recorded, never allowed to abort the whole run -
this is a named requirement in the approved architecture
("one generation failure must not destroy the complete run").
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Optional

from evaluation import db as evaldb
from evaluation import git_identity
from evaluation.evaluators import deterministic as det
from evaluation.model_adapter import GeminiAdapter, ModelRequest


def _uid() -> str:
    return str(uuid.uuid4())


@dataclass
class RunConfig:
    dataset_version_id: str
    evaluator_version_id: str
    judge_client: Optional[object] = None       # a genai.Client, or None to skip judge dimensions
    judge_config: Optional[object] = None       # a evaluation.judge.JudgeConfig, required if judge_client is set
    environment: Optional[str] = None


def _load_dataset_items(conn, dataset_version_id: str) -> list:
    """Loads the exact, immutable item-version set for a dataset version,
    in a deterministic order (ascending item_version_id) so repeated runs
    against the same dataset version process items in the same order."""
    rows = conn.execute(
        """
        SELECT biv.*, bi.question_type, bi.curriculum_class, bi.curriculum_stream,
               bi.curriculum_subject, bi.curriculum_chapter, bi.curriculum_topics
        FROM dataset_version_items dvi
        JOIN benchmark_item_versions biv ON biv.item_version_id = dvi.item_version_id
        JOIN benchmark_items bi ON bi.item_id = biv.item_id
        WHERE dvi.dataset_version_id = ?
        ORDER BY biv.item_version_id ASC
        """,
        (dataset_version_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _validate_holdout_eligibility(items: list) -> None:
    """Defense in depth: re-checks, at run time, that every holdout-
    partition item is still content_status='approved'. authoring.py's
    promote_to_holdout() already enforces this at promotion time - this
    is a second, independent check against direct DB tampering or a
    stale in-memory reference, per the master prompt's explicit
    instruction: 'if SQLite cannot enforce this cross-table condition,
    enforce it in application logic. Do not silently bypass the rule.'
    """
    violations = [
        item["item_version_id"] for item in items
        if item["partition"] == "holdout" and item["content_status"] != "approved"
    ]
    if violations:
        raise RuntimeError(
            f"Holdout eligibility violated for item version(s): {violations}. "
            f"A holdout item must have content_status='approved'. Refusing to run."
        )


def _run_deterministic_evaluators(item: dict, answer_text: str) -> list:
    """Returns a list of DimensionEvaluation for the applicable
    deterministic checks on this item. question_type decides which
    correctness evaluator (if any) applies; script/formatting checks
    always run when there is answer text to check."""
    evaluations = []
    expected_behavior = json.loads(item["expected_behavior_json"])

    if item["question_type"] == "numerical_tolerance" or item["question_type"] == "exact_numerical":
        evaluations.append(det.evaluate_numerical_tolerance(expected_behavior, answer_text))
    elif item["question_type"] == "mcq":
        evaluations.append(det.evaluate_mcq(expected_behavior, answer_text))
    # symbolic / short_factual / long_explanation / proof_derivation /
    # language_answer: no deterministic correctness evaluator exists for
    # these (Phase 7B Step 2 Section 12) - correctness for these items is
    # only scored when a judge_client is supplied.

    evaluations.append(det.evaluate_script_correctness(item["requested_output_language"], answer_text))
    evaluations.append(det.evaluate_formatting_artifacts(answer_text))
    return evaluations


def _run_judge_evaluators(judge_client, judge_config, item: dict, answer_text: str) -> list:
    from evaluation.judge import evaluate_with_judge  # local import: avoids
    # importing google.genai types at module load time for callers that
    # never use the judge (e.g. every deterministic-only test in this
    # session, given no live GEMINI_API_KEY was available).

    curriculum_ref = {
        "class": item["curriculum_class"],
        "subject": item["curriculum_subject"],
        "topics": json.loads(item["curriculum_topics"]),
    }
    expected_behavior = json.loads(item["expected_behavior_json"])
    dimensions_to_judge = ["reasoning", "curriculum_alignment", "language"]
    if item["question_type"] in ("symbolic", "short_factual", "long_explanation", "proof_derivation", "language_answer"):
        dimensions_to_judge.append("correctness")

    evaluations = []
    for dimension in dimensions_to_judge:
        evaluations.append(evaluate_with_judge(
            judge_client, judge_config, dimension,
            question_text=item["question_text"], expected_behavior=expected_behavior,
            curriculum_ref=curriculum_ref, mode=item["mode"], answer_text=answer_text,
        ))
    return evaluations


def run_evaluation(conn, config: RunConfig) -> str:
    """Executes a full evaluation run and returns the run_id.

    Every persisted row goes through this one function's control flow -
    there is no separate code path that writes evaluation_results or
    failure_flags, which keeps "what actually happened" traceable to one
    place.
    """
    items = _load_dataset_items(conn, config.dataset_version_id)
    _validate_holdout_eligibility(items)

    adapter = GeminiAdapter()
    now = evaldb.utc_now_iso()
    run_id = _uid()

    from evaluation import prompt_identity

    prompt_version = prompt_identity.get_chat_prompt_version()
    prompt_hash = prompt_identity.get_chat_prompt_hash()
    model_config_snapshot = _capture_model_config_snapshot()

    conn.execute(
        """
        INSERT INTO evaluation_runs
            (run_id, created_at, dataset_version_id, git_commit_sha,
             evaluation_schema_version, model_provider, model_name,
             model_config_snapshot_json, prompt_version, prompt_hash,
             evaluator_version_id, judge_model, judge_model_version, environment)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id, now, config.dataset_version_id, git_identity.get_git_commit_sha(),
            evaldb.EVALUATION_SCHEMA_VERSION, adapter.model_provider, adapter.model_name,
            json.dumps(model_config_snapshot), prompt_version, prompt_hash,
            config.evaluator_version_id,
            getattr(config.judge_config, "judge_model", None) if config.judge_client else None,
            None,
            config.environment,
        ),
    )
    conn.commit()

    for item in items:
        _process_one_item(conn, run_id, item, adapter, config)

    return run_id


def _capture_model_config_snapshot() -> dict:
    from backend import ai_engine
    return {
        "thinking_level": str(ai_engine.CHAT_THINKING_LEVEL),
        "request_timeout_seconds": ai_engine.AI_REQUEST_TIMEOUT_SECONDS,
        "model_name": ai_engine.MODEL_NAME,
    }


def _process_one_item(conn, run_id: str, item: dict, adapter: GeminiAdapter, config: RunConfig) -> None:
    """Processes exactly one benchmark item within a run. Any exception
    here is caught and recorded as a generation_failure - it must never
    propagate and abort the rest of the run."""
    item_version_id = item["item_version_id"]
    request_start_iso = evaldb.utc_now_iso()

    try:
        context = json.loads(item["context_json"]) if item["context_json"] else {}
        response = adapter.generate(ModelRequest(
            prompt=item["question_text"],
            mode=item["mode"],
            board="BD NCTB (Bangla)",  # matches backend/main.py's DEFAULT_BOARD -
                                        # see the Phase 7B-1 readiness report's
                                        # observation that board is not currently
                                        # a per-request production input.
            user_class=item["curriculum_class"],
            stream=item["curriculum_stream"],
            history=context.get("conversation_history"),
        ))
    except Exception as exc:  # noqa: BLE001
        _persist_item_run(conn, run_id, item_version_id, request_start_iso, None, True, f"{type(exc).__name__}: {exc}")
        _persist_evaluations(conn, run_id, item_version_id, config.evaluator_version_id, [det.evaluate_generation_failure()])
        return

    if response.generation_failed:
        _persist_item_run(conn, run_id, item_version_id, request_start_iso, response, True, response.failure_reason)
        _persist_evaluations(conn, run_id, item_version_id, config.evaluator_version_id, [det.evaluate_generation_failure()])
        return

    _persist_item_run(conn, run_id, item_version_id, request_start_iso, response, False, None)

    if response.is_error:
        # A controlled operational error string (e.g. quota exhausted) -
        # not a crash, but there is no real answer content to evaluate.
        _persist_evaluations(conn, run_id, item_version_id, config.evaluator_version_id, [det.evaluate_generation_failure()])
        return

    evaluations = _run_deterministic_evaluators(item, response.text)
    if config.judge_client is not None:
        try:
            evaluations.extend(_run_judge_evaluators(config.judge_client, config.judge_config, item, response.text))
        except Exception as exc:  # noqa: BLE001 - a judge crash must not
            # take down the deterministic results already computed above.
            evaluations.append(det.DimensionEvaluation(
                dimension="reasoning", score=None, confidence=None, method="llm_judge",
                evidence=f"Judge evaluation crashed: {type(exc).__name__}: {exc}",
            ))

    _persist_evaluations(conn, run_id, item_version_id, config.evaluator_version_id, evaluations)


def _persist_item_run(conn, run_id: str, item_version_id: str, request_start_iso: str,
                        response, generation_failed: bool, failure_reason: Optional[str]) -> None:
    conn.execute(
        """
        INSERT INTO evaluation_item_runs
            (item_run_id, run_id, item_version_id, generation_failed, failure_reason,
             raw_answer_text, request_start_ts, ai_call_start_ts, ai_call_end_ts,
             response_returned_ts, prompt_tokens, output_tokens, thoughts_tokens,
             cached_tokens, total_tokens, resolved_model_version, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _uid(), run_id, item_version_id, int(generation_failed), failure_reason,
            None if response is None else (None if generation_failed else response.text),
            request_start_iso,
            None, None, None,  # ai_call_start_ts / first_chunk_ts are never
                                 # populated (see model_adapter.py - only
                                 # request_start_ts and ai_call_end_ts are
                                 # actually measured today; first_chunk_ts
                                 # stays NULL, since streaming is not
                                 # implemented in production - never fabricated)
            None if response is None else response.prompt_tokens,
            None if response is None else response.output_tokens,
            None if response is None else response.thoughts_tokens,
            None if response is None else response.cached_tokens,
            None if response is None else response.total_tokens,
            None if response is None else response.resolved_model_version,
            evaldb.utc_now_iso(),
        ),
    )
    conn.commit()


def _persist_evaluations(conn, run_id: str, item_version_id: str, evaluator_version_id: str, evaluations: list) -> None:
    now = evaldb.utc_now_iso()
    for evaluation in evaluations:
        if evaluation.score is not None:
            conn.execute(
                """
                INSERT INTO evaluation_results
                    (result_id, run_id, item_version_id, dimension, score, confidence,
                     method, evaluator_version_id, evidence, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _uid(), run_id, item_version_id, evaluation.dimension, evaluation.score,
                    evaluation.confidence, evaluation.method, evaluator_version_id,
                    evaluation.evidence, now,
                ),
            )
        for flag in evaluation.failure_flags:
            conn.execute(
                """
                INSERT INTO failure_flags
                    (failure_flag_id, run_id, item_version_id, dimension, failure_type,
                     severity, evaluator_version_id, explanation, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _uid(), run_id, item_version_id, flag.dimension, flag.failure_type,
                    flag.severity, evaluator_version_id, flag.explanation, now,
                ),
            )
    conn.commit()

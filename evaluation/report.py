"""
Kognit Phase 7B-9 - Evaluation Reporting.

Produces a structured report dict from stored run data (never a single
"Kognit Quality = X%" number - every dimension stays separate, per the
approved architecture's explicit, repeated instruction). A Markdown
renderer is provided for human reading; the underlying dict is the real
report model and can be serialized to JSON directly.
"""

from __future__ import annotations

import json
import statistics
from typing import Optional


def generate_report(conn, run_id: str) -> dict:
    run = conn.execute("SELECT * FROM evaluation_runs WHERE run_id = ?", (run_id,)).fetchone()
    if run is None:
        raise ValueError(f"No evaluation run found for run_id={run_id!r}")

    item_runs = conn.execute(
        "SELECT * FROM evaluation_item_runs WHERE run_id = ?", (run_id,)
    ).fetchall()
    total_items = len(item_runs)
    failed_items = sum(1 for r in item_runs if r["generation_failed"])
    completed_items = total_items - failed_items

    results = conn.execute(
        """
        SELECT er.*, bi.curriculum_subject, bi.curriculum_class, biv.input_language,
               biv.requested_output_language, biv.mode
        FROM evaluation_results er
        JOIN benchmark_item_versions biv ON biv.item_version_id = er.item_version_id
        JOIN benchmark_items bi ON bi.item_id = biv.item_id
        WHERE er.run_id = ?
        """,
        (run_id,),
    ).fetchall()

    failure_flags = conn.execute(
        "SELECT * FROM failure_flags WHERE run_id = ?", (run_id,)
    ).fetchall()

    dimension_summary = _summarize_by_dimension(results)
    failure_summary = _summarize_failures(failure_flags)
    latency_summary = _summarize_latency(item_runs)
    usage_summary = _summarize_usage(item_runs)
    subject_breakdown = _slice_by(results, "curriculum_subject")
    class_breakdown = _slice_by(results, "curriculum_class")
    language_breakdown = _slice_by(results, "requested_output_language")
    mode_breakdown = _slice_by(results, "mode")

    return {
        "run_identity": {
            "run_id": run["run_id"],
            "created_at": run["created_at"],
            "dataset_version_id": run["dataset_version_id"],
            "git_commit_sha": run["git_commit_sha"],
            "evaluation_schema_version": run["evaluation_schema_version"],
            "model_provider": run["model_provider"],
            "model_name": run["model_name"],
            "resolved_model_version": run["resolved_model_version"],
            "prompt_version": run["prompt_version"],
            "prompt_hash": run["prompt_hash"],
            "evaluator_version_id": run["evaluator_version_id"],
            "judge_model": run["judge_model"],
            "judge_model_version": run["judge_model_version"],
        },
        "item_counts": {
            "total_items": total_items,
            "completed_items": completed_items,
            "failed_operational_runs": failed_items,
        },
        "dimension_scores": dimension_summary,
        "failure_summary": failure_summary,
        "latency_summary": latency_summary,
        "usage_summary": usage_summary,
        "subject_breakdown": subject_breakdown,
        "class_breakdown": class_breakdown,
        "language_breakdown": language_breakdown,
        "mode_breakdown": mode_breakdown,
    }


def _summarize_by_dimension(results) -> dict:
    by_dim: dict = {}
    for row in results:
        by_dim.setdefault(row["dimension"], {"scores": [], "confidences": []})
        by_dim[row["dimension"]]["scores"].append(row["score"])
        if row["confidence"] is not None:
            by_dim[row["dimension"]]["confidences"].append(row["confidence"])

    summary = {}
    for dimension, data in by_dim.items():
        summary[dimension] = {
            "n": len(data["scores"]),
            "mean_score": round(statistics.mean(data["scores"]), 4) if data["scores"] else None,
            "mean_confidence": round(statistics.mean(data["confidences"]), 4) if data["confidences"] else None,
        }
    return summary


def _summarize_failures(failure_flags) -> dict:
    by_type: dict = {}
    by_severity: dict = {"low": 0, "medium": 0, "high": 0, "critical": 0}
    for flag in failure_flags:
        by_type[flag["failure_type"]] = by_type.get(flag["failure_type"], 0) + 1
        by_severity[flag["severity"]] = by_severity.get(flag["severity"], 0) + 1
    return {"total": len(failure_flags), "by_type": by_type, "by_severity": by_severity}


def _summarize_latency(item_runs) -> dict:
    durations = []
    for row in item_runs:
        if row["request_start_ts"] and row["ai_call_end_ts"]:
            # ISO-8601 strings compare lexically in the same order as
            # chronologically for this fixed format, but for an actual
            # duration we parse them.
            from datetime import datetime
            try:
                start = datetime.fromisoformat(row["request_start_ts"])
                end = datetime.fromisoformat(row["ai_call_end_ts"])
                durations.append((end - start).total_seconds())
            except ValueError:
                continue
    return {
        "n_measured": len(durations),
        "mean_seconds": round(statistics.mean(durations), 3) if durations else None,
        "max_seconds": round(max(durations), 3) if durations else None,
        "ttft_available": False,  # streaming not implemented in production
                                   # (Phase 7B Step 1 audit) - never fabricated.
    }


def _summarize_usage(item_runs) -> dict:
    prompt_tokens = [r["prompt_tokens"] for r in item_runs if r["prompt_tokens"] is not None]
    output_tokens = [r["output_tokens"] for r in item_runs if r["output_tokens"] is not None]
    total_tokens = [r["total_tokens"] for r in item_runs if r["total_tokens"] is not None]
    return {
        "n_measured": len(total_tokens),
        "total_prompt_tokens": sum(prompt_tokens) if prompt_tokens else None,
        "total_output_tokens": sum(output_tokens) if output_tokens else None,
        "total_tokens": sum(total_tokens) if total_tokens else None,
        "calculated_cost": None,  # requires a populated pricing_configs row -
                                   # never computed from an invented price.
                                   # See evaluation/README.md.
    }


def _slice_by(results, column: str) -> dict:
    by_value: dict = {}
    for row in results:
        key = row[column] or "(unknown)"
        by_value.setdefault(key, {})
        by_value[key].setdefault(row["dimension"], [])
        by_value[key][row["dimension"]].append(row["score"])

    summary = {}
    for key, dims in by_value.items():
        summary[key] = {
            dim: round(statistics.mean(scores), 4) for dim, scores in dims.items()
        }
    return summary


def render_markdown(report: dict) -> str:
    ri = report["run_identity"]
    lines = [
        f"# Evaluation Run Report: {ri['run_id']}",
        "",
        "## Run Identity",
        f"- Created: {ri['created_at']}",
        f"- Dataset version: {ri['dataset_version_id']}",
        f"- Git commit: {ri['git_commit_sha']}",
        f"- Evaluation schema version: {ri['evaluation_schema_version']}",
        f"- Model: {ri['model_provider']} / {ri['model_name']} "
        f"(resolved: {ri['resolved_model_version'] or 'not reported by provider'})",
        f"- Prompt version: {ri['prompt_version']} (hash: {ri['prompt_hash'][:12]}...)",
        f"- Evaluator version: {ri['evaluator_version_id']}",
        f"- Judge model: {ri['judge_model'] or '(no judge used in this run)'}",
        "",
        "## Item Counts",
        f"- Total items: {report['item_counts']['total_items']}",
        f"- Completed: {report['item_counts']['completed_items']}",
        f"- Failed (operational): {report['item_counts']['failed_operational_runs']}",
        "",
        "## Dimension Scores (never combined into one overall score)",
    ]
    for dim, data in report["dimension_scores"].items():
        lines.append(f"- **{dim}**: mean_score={data['mean_score']}, n={data['n']}, mean_confidence={data['mean_confidence']}")

    lines += ["", "## Failure Summary",
              f"- Total flags: {report['failure_summary']['total']}",
              f"- By severity: {report['failure_summary']['by_severity']}",
              f"- By type: {report['failure_summary']['by_type']}",
              "", "## Latency",
              f"- Measured on {report['latency_summary']['n_measured']} items, "
              f"mean={report['latency_summary']['mean_seconds']}s, max={report['latency_summary']['max_seconds']}s",
              f"- TTFT available: {report['latency_summary']['ttft_available']} (streaming not implemented in production)",
              "", "## Usage / Cost",
              f"- Total tokens: {report['usage_summary']['total_tokens']} "
              f"(prompt={report['usage_summary']['total_prompt_tokens']}, output={report['usage_summary']['total_output_tokens']})",
              f"- Calculated cost: {report['usage_summary']['calculated_cost']} "
              "(null until a pricing_configs row is populated - never invented)",
              "", "## Subject Breakdown"]
    lines.append(json.dumps(report["subject_breakdown"], indent=2))
    lines += ["", "## Class Breakdown"]
    lines.append(json.dumps(report["class_breakdown"], indent=2))
    lines += ["", "## Language Breakdown"]
    lines.append(json.dumps(report["language_breakdown"], indent=2))
    lines += ["", "## Mode Breakdown"]
    lines.append(json.dumps(report["mode_breakdown"], indent=2))

    return "\n".join(lines)

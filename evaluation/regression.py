"""
Kognit Phase 7B-10 - Regression Comparison.

Compares two evaluation runs, dimension by dimension, sliced by subject/
class/language/mode. NEVER declares a single overall winner (per the
approved architecture's repeated, explicit instruction) - the output is
always per-dimension deltas and per-item improved/regressed/unchanged
classifications, so a caller can see "Model B improved Language but
regressed Correctness in Chemistry" rather than a hidden blended score.

Only items present in BOTH runs are compared (a dataset can differ
between two runs) - items unique to one run are reported separately,
never silently dropped.
"""

from __future__ import annotations

import statistics


def compare_runs(conn, run_a_id: str, run_b_id: str) -> dict:
    if run_a_id == run_b_id:
        raise ValueError("Cannot compare a run against itself.")

    results_a = _load_results(conn, run_a_id)
    results_b = _load_results(conn, run_b_id)

    items_a = {(r["item_version_id"], r["dimension"]) for r in results_a}
    items_b = {(r["item_version_id"], r["dimension"]) for r in results_b}

    by_key_a = {(r["item_version_id"], r["dimension"]): r for r in results_a}
    by_key_b = {(r["item_version_id"], r["dimension"]): r for r in results_b}

    common_keys = items_a & items_b
    only_in_a = items_a - items_b
    only_in_b = items_b - items_a

    per_item_classification = []
    for key in common_keys:
        item_version_id, dimension = key
        score_a = by_key_a[key]["score"]
        score_b = by_key_b[key]["score"]
        if score_a is None or score_b is None:
            continue
        if score_b > score_a:
            classification = "improved"
        elif score_b < score_a:
            classification = "regressed"
        else:
            classification = "unchanged"
        per_item_classification.append({
            "item_version_id": item_version_id, "dimension": dimension,
            "score_a": score_a, "score_b": score_b, "classification": classification,
        })

    dimension_deltas = _aggregate_dimension_deltas(per_item_classification)
    slice_deltas = {
        "subject": _aggregate_slice_deltas(by_key_a, by_key_b, common_keys, "curriculum_subject"),
        "class": _aggregate_slice_deltas(by_key_a, by_key_b, common_keys, "curriculum_class"),
        "language": _aggregate_slice_deltas(by_key_a, by_key_b, common_keys, "requested_output_language"),
        "mode": _aggregate_slice_deltas(by_key_a, by_key_b, common_keys, "mode"),
    }

    newly_failing, newly_passing = _find_newly_failing_and_passing(conn, run_a_id, run_b_id)

    return {
        "run_a_id": run_a_id,
        "run_b_id": run_b_id,
        "items_only_in_run_a": sorted({k[0] for k in only_in_a}),
        "items_only_in_run_b": sorted({k[0] for k in only_in_b}),
        "dimension_deltas": dimension_deltas,
        "slice_deltas": slice_deltas,
        "newly_failing_items": newly_failing,
        "newly_passing_items": newly_passing,
        "summary_counts": {
            "improved": sum(1 for c in per_item_classification if c["classification"] == "improved"),
            "regressed": sum(1 for c in per_item_classification if c["classification"] == "regressed"),
            "unchanged": sum(1 for c in per_item_classification if c["classification"] == "unchanged"),
        },
        "note": "No single overall winner is computed. Review dimension_deltas and "
                "slice_deltas together - an improvement in one dimension/subject can "
                "coincide with a regression in another.",
    }


def _load_results(conn, run_id: str) -> list:
    rows = conn.execute(
        """
        SELECT er.*, bi.curriculum_subject, bi.curriculum_class,
               biv.requested_output_language, biv.mode
        FROM evaluation_results er
        JOIN benchmark_item_versions biv ON biv.item_version_id = er.item_version_id
        JOIN benchmark_items bi ON bi.item_id = biv.item_id
        WHERE er.run_id = ?
        """,
        (run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _aggregate_dimension_deltas(per_item_classification: list) -> dict:
    by_dim: dict = {}
    for c in per_item_classification:
        by_dim.setdefault(c["dimension"], {"deltas": [], "improved": 0, "regressed": 0, "unchanged": 0})
        by_dim[c["dimension"]]["deltas"].append(c["score_b"] - c["score_a"])
        by_dim[c["dimension"]][c["classification"]] += 1

    summary = {}
    for dim, data in by_dim.items():
        summary[dim] = {
            "mean_delta": round(statistics.mean(data["deltas"]), 4) if data["deltas"] else None,
            "improved": data["improved"], "regressed": data["regressed"], "unchanged": data["unchanged"],
        }
    return summary


def _aggregate_slice_deltas(by_key_a: dict, by_key_b: dict, common_keys, slice_column: str) -> dict:
    by_slice_dim: dict = {}
    for key in common_keys:
        row_a, row_b = by_key_a[key], by_key_b[key]
        if row_a["score"] is None or row_b["score"] is None:
            continue
        slice_value = row_a[slice_column] or "(unknown)"
        dimension = key[1]
        by_slice_dim.setdefault(slice_value, {}).setdefault(dimension, [])
        by_slice_dim[slice_value][dimension].append(row_b["score"] - row_a["score"])

    summary = {}
    for slice_value, dims in by_slice_dim.items():
        summary[slice_value] = {
            dim: round(statistics.mean(deltas), 4) for dim, deltas in dims.items()
        }
    return summary


def _find_newly_failing_and_passing(conn, run_a_id: str, run_b_id: str) -> tuple:
    """An item is 'newly failing' if run A had no generation_failure for it
    but run B does (and vice versa for 'newly passing') - an operational
    signal, distinct from a dimension-score regression."""
    def _failed_items(run_id: str) -> set:
        rows = conn.execute(
            "SELECT item_version_id FROM evaluation_item_runs WHERE run_id = ? AND generation_failed = 1",
            (run_id,),
        ).fetchall()
        return {r["item_version_id"] for r in rows}

    failed_a = _failed_items(run_a_id)
    failed_b = _failed_items(run_b_id)
    newly_failing = sorted(failed_b - failed_a)
    newly_passing = sorted(failed_a - failed_b)
    return newly_failing, newly_passing

"""Evaluators for the Kognit evaluation subsystem (Phase 7B-6/7B-7).

Every evaluator here is a PURE function: (item content, model answer) ->
DimensionEvaluation. No database access, no network calls (deterministic
evaluators only - the LLM judge in judge.py is the one exception, and it
is kept in a separate module specifically because it is not pure).

This mirrors the exact pattern already established elsewhere in this
codebase (backend/mastery_engine.py, backend/mistake_engine.py) - pure,
independently testable domain logic, uncoupled from I/O.
"""

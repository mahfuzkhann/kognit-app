"""
Kognit Phase 7B - git commit identity for evaluation run reproducibility.

evaluation_runs.git_commit_sha is mandatory (see evaluation/schema/schema.sql
and the Phase 7B Step 2 revision report, Section 9) - this is the one field
that captures "the entire code state" as a cheap superset safety net under
the more precise prompt_hash field (a retry-policy constant change, for
example, is not captured by prompt_hash but IS captured by the commit SHA).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def get_git_commit_sha() -> str:
    """Return the current HEAD commit SHA of the Kognit repository.

    Raises RuntimeError if this is not run from within a git repository
    or git is unavailable - deliberately never falls back to a placeholder
    value, since a fabricated commit SHA would silently break the
    reproducibility chain's core guarantee.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            "Could not determine the current git commit SHA - the evaluation "
            "runner requires this for run reproducibility and will not "
            "silently substitute a placeholder value."
        ) from exc
    return result.stdout.strip()


def get_git_status_is_clean() -> bool:
    """Return True if the working tree has no uncommitted changes.

    Recorded informationally on a run (not currently a hard requirement)
    so a report can flag "this run was against a dirty working tree" -
    a real reproducibility caveat worth surfacing rather than hiding.
    """
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip() == ""

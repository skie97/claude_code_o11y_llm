"""
scorer_status.py -- the scorer's run-health primitives (pure, no SDK/Loki imports).

Two jobs, both single-sourced here so the failure trigger is one tested decision:

  * is_fatal_judge_error -- separate a CONFIG error (bad JUDGE_MODEL / key /
    permission: 401/403/404, identical for every prompt -> abort the run) from a
    TRANSIENT one (rate limit, 5xx, one malformed prompt -> skip and continue).
    The old code lumped both into "skip", so a bad model skipped every prompt and
    exited 0 with empty output -- a silent failure. This is the fix.

  * build_run_status -- the one JSON row the scorer emits to Loki at the end of
    every run (and on a fatal abort), so a Grafana alert can fire on status="failed"
    or scored==0. See deploy/grafana-scoring/alerting/.
"""

from __future__ import annotations

STATUS_OK = "ok"
STATUS_FAILED = "failed"

# 401 unauthorized, 403 permission, 404 not-found: config errors, not data errors.
_FATAL_STATUS = {401, 403, 404}
_FATAL_NAMES = {"NotFoundError", "AuthenticationError", "PermissionDeniedError"}


class JudgeModelError(Exception):
    """Raised when the judge model itself is unusable (missing / wrong / unauthorized).
    Fatal: it fails identically for every prompt, so the run aborts non-zero rather
    than silently scoring nothing."""


def is_fatal_judge_error(exc: BaseException) -> bool:
    """True if `exc` is a judge CONFIG error that will fail every prompt the same way
    (abort the run), False if it is transient/per-prompt (skip and continue).

    Classifies by HTTP status first (anthropic.APIStatusError carries .status_code),
    falling back to the exception class name so it needs no anthropic import and stays
    unit-testable with stand-in exceptions."""
    code = getattr(exc, "status_code", None)
    if code in _FATAL_STATUS:
        return True
    return type(exc).__name__ in _FATAL_NAMES


def build_run_status(*, status: str, judge_model: str, scored: int, skipped: int,
                     dry_run: bool, reason: str | None = None) -> dict:
    """One run-health row for the claude-code-scorer-health Loki stream. `total` is
    derived so the alert/dashboard never has to add. `reason` is present only on
    failure (the human-readable cause, e.g. 'judge model X not found')."""
    row = {
        "kind": "run_status",
        "status": status,
        "judge_model": judge_model,
        "scored": scored,
        "skipped": skipped,
        "total": scored + skipped,
        "dry_run": dry_run,
    }
    if reason:
        row["reason"] = reason
    return row
